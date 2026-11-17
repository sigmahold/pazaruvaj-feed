#!/usr/bin/env python3
"""
Генерира XML фийд за Pazaruvaj.com от ИЗБРАНИ колекции в Shopify магазина.

КАК СЕ ИЗБИРА КАКВО ДА СЕ КАЧИ:
В Shopify -> Продукти -> Колекции -> отворете колекция -> сложете отметка
"Качи в Pazaruvaj". Само колекциите с отметка влизат във фийда.
Категорията за Pazaruvaj е името на колекцията (Pazaruvaj сами я разпределят).
Отметката се създава автоматично при първото пускане.

КОД НА ПРОИЗВОДИТЕЛЯ (ProductNumber) се търси автоматично в метаполетата на
продукта (mpn, part number, код на производителя...). Ако няма такова - SKU.

Формат по упътването на Pazaruvaj (от юли 2021):
Products/Product с Identifier, Manufacturer, Name, ProductUrl, Price, NetPrice,
ProductNumber, ImageUrl, ImageUrl2, Category, Description, DeliveryTime,
DeliveryCost, EanCode.

Ползва само стандартната библиотека на Python - не трябва нищо да се инсталира.

Настройки (GitHub -> Settings -> Secrets and variables -> Actions):
  Secrets:
    SHOPIFY_STORE          напр. mystore.myshopify.com
    SHOPIFY_ACCESS_TOKEN   токен shpat_...   ИЛИ
    SHOPIFY_CLIENT_ID + SHOPIFY_CLIENT_SECRET
  По желание (в .github/workflows/pazaruvaj-feed.yml):
    DELIVERY_TIME          срок за доставка в дни, напр. "2"
    DELIVERY_COST          цена на доставка, напр. "2 EUR" или "Free"
    PRODUCT_NUMBER         "auto" (по подразбиране) - търси кода сам; "sku";
                           "none", или метаполе във вид "namespace.key"
    VAT_RATE               ДДС в проценти, по подразбиране 20
    ONLY_IN_STOCK          "1" = само с наличност (по подразбиране), "0" = всички
    UTM                    по желание, напр. "utm_source=pazaruvaj&utm_medium=cpc"
"""

import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from xml.sax.saxutils import escape

API_VERSION = "2025-10"
PAGE_SIZE = 50
OUT_FILE = os.path.join("public", "pazaruvaj.xml")
BAD_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


# ---------------------------------------------------------------- настройки
def env(name, default=""):
    return (os.environ.get(name) or default).strip()


STORE = env("SHOPIFY_STORE").replace("https://", "").replace("http://", "").strip("/")
if STORE and not STORE.endswith(".myshopify.com"):
    STORE += ".myshopify.com"

DELIVERY_TIME = env("DELIVERY_TIME", "")  # празно = не се посочва
DELIVERY_COST = env("DELIVERY_COST", "Free")
VAT_RATE = float(env("VAT_RATE", "20").replace(",", "."))
ONLY_IN_STOCK = env("ONLY_IN_STOCK", "1") != "0"
UTM = env("UTM").lstrip("?&")
PRODUCT_NUMBER = env("PRODUCT_NUMBER", "auto")




# ---------------------------------------------------------------- Shopify
def get_token():
    token = env("SHOPIFY_ACCESS_TOKEN")
    if token:
        return token
    cid, secret = env("SHOPIFY_CLIENT_ID"), env("SHOPIFY_CLIENT_SECRET")
    if cid and secret:
        body = urllib.parse.urlencode(
            {"grant_type": "client_credentials", "client_id": cid, "client_secret": secret}
        ).encode()
        req = urllib.request.Request(f"https://{STORE}/admin/oauth/access_token", data=body)
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode())["access_token"]
    sys.exit("Липсва SHOPIFY_ACCESS_TOKEN или SHOPIFY_CLIENT_ID + SHOPIFY_CLIENT_SECRET")


class Shopify:
    def __init__(self, token):
        self.url = f"https://{STORE}/admin/api/{API_VERSION}/graphql.json"
        self.token = token

    def gql(self, query, variables=None):
        payload = json.dumps({"query": query, "variables": variables or {}}).encode()
        for attempt in range(8):
            req = urllib.request.Request(self.url, data=payload)
            req.add_header("Content-Type", "application/json")
            req.add_header("X-Shopify-Access-Token", self.token)
            try:
                with urllib.request.urlopen(req, timeout=90) as resp:
                    res = json.loads(resp.read().decode())
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503, 504) and attempt < 7:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise
            errors = res.get("errors")
            if errors and any((x.get("extensions") or {}).get("code") == "THROTTLED" for x in errors):
                time.sleep(2 * (attempt + 1))
                continue
            if errors:
                sys.exit("Грешка от Shopify: " + json.dumps(errors, ensure_ascii=False))
            return res["data"]
        sys.exit("Shopify не отговаря - опитайте пак по-късно.")


COLLECTIONS_Q = """
query($cursor: String) {
  shop { primaryDomain { url } }
  collections(first: 250, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id title
      pz: metafield(namespace: "custom", key: "pazaruvaj") { value }
    }
  }
}"""

DEFINITION_Q = """
mutation {
  metafieldDefinitionCreate(definition: {
    name: "Качи в Pazaruvaj"
    namespace: "custom"
    key: "pazaruvaj"
    description: "Отметнете, за да влязат продуктите от тази колекция в XML фийда за Pazaruvaj."
    type: "boolean"
    ownerType: COLLECTION
    pin: true
  }) {
    createdDefinition { id }
    userErrors { code message }
  }
}"""


def ensure_definition(api):
    """Създава отметката "Качи в Pazaruvaj" в колекциите, ако още я няма."""
    try:
        d = api.gql(DEFINITION_Q)["metafieldDefinitionCreate"]
    except SystemExit as e:
        print(f"  (полето не е създадено автоматично: {e})")
        return
    if d["createdDefinition"]:
        print("  Създадох отметката 'Качи в Pazaruvaj' в колекциите на Shopify.")
    elif d["userErrors"] and not any(x.get("code") == "TAKEN" for x in d["userErrors"]):
        print("  (полето не е създадено: " + json.dumps(d["userErrors"], ensure_ascii=False) + ")")


def product_fields():
    mf = "metafields(first: 40) { nodes { namespace key value } }"
    if "." in PRODUCT_NUMBER:
        ns, key = PRODUCT_NUMBER.split(".", 1)
        mf = f'pn: metafield(namespace: "{ns}", key: "{key}") {{ value }}'
    return f"""
      legacyResourceId title handle vendor status descriptionHtml onlineStoreUrl
      totalInventory tracksInventory
      {mf}
      media(first: 2) {{ nodes {{ ... on MediaImage {{ image {{ url }} }} }} }}
      variants(first: 1) {{ nodes {{ price sku barcode availableForSale }} }}
    """


PRODUCTS_Q = """
query($id: ID!, $cursor: String) {
  collection(id: $id) {
    products(first: %d, after: $cursor) {
      pageInfo { hasNextPage endCursor }
      nodes { %s }
    }
  }
}""" % (PAGE_SIZE, product_fields())


def load_collections(api):
    cursor, domain, cols = None, None, []
    while True:
        d = api.gql(COLLECTIONS_Q, {"cursor": cursor})
        domain = domain or d["shop"]["primaryDomain"]["url"].rstrip("/")
        cols += d["collections"]["nodes"]
        if not d["collections"]["pageInfo"]["hasNextPage"]:
            return domain, cols
        cursor = d["collections"]["pageInfo"]["endCursor"]


def load_collection_products(api, cid):
    cursor, items = None, []
    while True:
        d = api.gql(PRODUCTS_Q, {"id": cid, "cursor": cursor})
        page = d["collection"]["products"]
        items += page["nodes"]
        if not page["pageInfo"]["hasNextPage"]:
            return items
        cursor = page["pageInfo"]["endCursor"]


# ---------------------------------------------------------------- XML
def clean_text(html_text, limit=3000):
    """HTML описание -> чист текст (Pazaruvaj иска без html тагове)."""
    if not html_text:
        return ""
    t = re.sub(r"(?is)<(script|style|iframe).*?</\1>", " ", html_text)
    t = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</tr>|</h\d>|</div>", "\n", t)
    t = re.sub(r"(?i)</t[dh]>", " ", t)
    t = re.sub(r"<[^>]+>", " ", t)
    t = html.unescape(t)
    t = re.sub(r"[ \t\r\f\v ]+", " ", t)
    t = re.sub(r"\s*\n\s*", "\n", t).strip()
    t = BAD_CHARS.sub("", t)
    if len(t) > limit:
        t = t[:limit].rsplit(" ", 1)[0] + "..."
    return t


def tag(name, value):
    value = BAD_CHARS.sub("", "" if value is None else str(value))
    return f"    <{name}>{escape(value)}</{name}>\n"


def add_utm(url):
    if not UTM:
        return url
    return url + ("&" if "?" in url else "?") + UTM


def valid_barcode(code):
    code = re.sub(r"\s", "", code or "")
    return code if re.fullmatch(r"\d{8,13}", code) else ""


MPN_KEYS = re.compile(
    r"(^|[_\-.])(mpn|part_?no|part_?number|partnumber|pn|manufacturer_?(code|number|part|sku)|"
    r"mfr_?(code|number|part)|producer_?code|vendor_?code|product_?number|productnumber|model_?code|"
    r"kod_?proizvoditel\w*|kod_?na_?proizvoditel\w*|код\w*_?производител\w*)($|[_\-.])",
    re.I,
)
MPN_SOURCES = {}


def find_mpn(p):
    """Търси кода на производителя в метаполетата на продукта."""
    for m in (p.get("metafields") or {}).get("nodes", []):
        val = (m.get("value") or "").strip()
        if val and len(val) <= 60 and MPN_KEYS.search(m["key"]):
            src = f'{m["namespace"]}.{m["key"]}'
            MPN_SOURCES[src] = MPN_SOURCES.get(src, 0) + 1
            return val
    MPN_SOURCES["SKU"] = MPN_SOURCES.get("SKU", 0) + 1
    return ""


def product_xml(p, category, domain):
    """Връща (xml, причина за пропускане)."""
    if p.get("status") != "ACTIVE":
        return None, "не е активен"
    variants = p["variants"]["nodes"]
    if not variants:
        return None, "няма вариант"
    v = variants[0]
    try:
        price = float(v["price"])
    except (TypeError, ValueError):
        price = 0
    if price <= 0:
        return None, "няма цена"
    in_stock = ((p.get("totalInventory") or 0) > 0 or not p.get("tracksInventory", True)) and v.get(
        "availableForSale", True
    )
    if ONLY_IN_STOCK and not in_stock:
        return None, "без наличност"
    images = [m["image"]["url"] for m in p["media"]["nodes"] if m and (m.get("image") or {}).get("url")]
    if not images:
        return None, "без снимка"

    url = p.get("onlineStoreUrl") or f"{domain}/products/{p['handle']}"
    if PRODUCT_NUMBER == "sku":
        pn = v.get("sku")
    elif "." in PRODUCT_NUMBER:
        pn = (p.get("pn") or {}).get("value")
    elif PRODUCT_NUMBER == "auto":
        pn = find_mpn(p) or v.get("sku")
    else:
        pn = ""

    x = ["  <Product>\n"]
    x.append(tag("Identifier", p["legacyResourceId"]))
    x.append(tag("Manufacturer", p.get("vendor")))
    x.append(tag("Name", p["title"]))
    x.append(tag("ProductUrl", add_utm(url)))
    x.append(tag("Price", f"{price:.2f}"))
    x.append(tag("NetPrice", f"{price / (1 + VAT_RATE / 100):.2f}"))
    if pn:
        x.append(tag("ProductNumber", pn))
    x.append(tag("ImageUrl", images[0]))
    if len(images) > 1:
        x.append(tag("ImageUrl2", images[1]))
    x.append(tag("Category", category))
    x.append(tag("Description", clean_text(p.get("descriptionHtml"))))
    if not in_stock:
        x.append(tag("DeliveryTime", "NO"))
    elif DELIVERY_TIME:
        x.append(tag("DeliveryTime", DELIVERY_TIME))
    x.append(tag("DeliveryCost", DELIVERY_COST))
    ean = valid_barcode(v.get("barcode"))
    if ean:
        x.append(tag("EanCode", ean))
    x.append("  </Product>\n")
    return "".join(x), None


def main():
    if not STORE:
        sys.exit("Липсва SHOPIFY_STORE")

    api = Shopify(get_token())
    ensure_definition(api)
    domain, all_cols = load_collections(api)
    selection = []
    for c in all_cols:
        if ((c.get("pz") or {}).get("value") or "").strip().lower() == "true":
            selection.append((c, c["title"].strip()))
    if not selection:
        sys.exit(
            "Няма избрани колекции. В Shopify отворете колекция, сложете отметка "
            "'Качи в Pazaruvaj' и пуснете пак."
        )

    out = ['<?xml version="1.0" encoding="UTF-8"?>\n<Products>\n']
    seen, written, skipped = set(), 0, {}
    for col, category in selection:
        products = load_collection_products(api, col["id"])
        n = 0
        for p in products:
            pid = p["legacyResourceId"]
            if pid in seen:  # продукт в няколко избрани колекции - влиза само веднъж
                continue
            xml, reason = product_xml(p, category, domain)
            if reason:
                skipped[reason] = skipped.get(reason, 0) + 1
                continue
            seen.add(pid)
            out.append(xml)
            n += 1
        written += n
        print(f"  {col['title']}: {n} продукта")
    out.append("</Products>\n")

    os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        f.write("".join(out))

    print(f"\nГотово: {written} продукта във фийда.")
    if MPN_SOURCES:
        print("Код на производителя взет от: " + ", ".join(f"{k} - {v}" for k, v in MPN_SOURCES.items()))
    if skipped:
        print("Пропуснати: " + ", ".join(f"{k} - {v}" for k, v in skipped.items()))
    if written == 0:
        sys.exit("Фийдът е празен - в избраните колекции няма продукти с наличност и снимка.")


if __name__ == "__main__":
    main()
