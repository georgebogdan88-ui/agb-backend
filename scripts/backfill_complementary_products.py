"""
One-off backfill: populate the native `complementary_product_ids` field on
db.shopify_products docs from Shopify's existing
`shopify--discovery--product_recommendation.complementary_products`
metafield, so the "Posibil să ai nevoie și de" section on the storefront has
data to show via the new native field (see get_complementary_products() in
server.py) before George starts curating it manually from the admin UI.

Why this exists: server.py's `Product`/`ProductCreate`/`ProductUpdate`
models and `db.shopify_products` never stored this field before - it was
100% derived from a live Shopify GraphQL query on every request. This script
does a ONE-TIME direct-to-DB backfill of that same metafield data into the
new field, for every product that already has a Shopify-side complementary
link set up. Going forward this field is admin-managed (via the admin UI),
not Shopify-managed - do NOT wire this script into startup_event() or the
periodic auto-sync loop (see sync_all_products() in server.py, which now
has its own separate fix to *preserve* this field across its
delete+reinsert cycle - that fix is unrelated to and independent of this
script).

Writes go directly to the DB via `update_one($set=...)`, NOT through the
`PUT /admin/products/{id}` endpoint, specifically so they do NOT stamp
`source: "manual"` on these products - they should stay Shopify-owned /
auto-syncable, just carrying this one extra preserved field.

Only products whose `id` does NOT start with "local-" are checked (those
were created directly in the admin UI, never existed on Shopify, so there's
no Shopify metafield to fetch for them).

Idempotent / safe to re-run: it always overwrites
`complementary_product_ids` with the current Shopify metafield state for any
product that still has one set. Re-running is fine, but per the plan it is
NOT meant to be scheduled - it's a manual, occasional tool only.

Usage:
    venv/Scripts/python.exe scripts/backfill_complementary_products.py

Env vars come from .env in the repo root (MONGO_URL / DB_NAME / SHOPIFY_*),
same as server.py and the rest of this repo's scripts.
"""
import asyncio
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env", override=False)

MONGO_URL = os.environ["MONGO_URL"]
DB_NAME = os.environ["DB_NAME"]
SHOPIFY_STORE = os.environ.get("SHOPIFY_STORE", "43ca3c-3.myshopify.com")
SHOPIFY_STOREFRONT_TOKEN = os.environ.get("SHOPIFY_STOREFRONT_TOKEN", "")
SHOPIFY_API_VERSION = os.environ.get("SHOPIFY_API_VERSION", "2024-01")

GRAPHQL_URL = f"https://{SHOPIFY_STORE}/api/{SHOPIFY_API_VERSION}/graphql.json"

# Trimmed version of the complementaryProducts half of the query in
# get_complementary_products() (server.py) - this backfill only needs the
# referenced product ids, not the full product details (title/price/
# images/...) that endpoint also pulls for direct display, so those extra
# fields are dropped here to keep the per-product query lighter/faster over
# the whole catalog. Same metafield namespace/key, same references() shape.
COMPLEMENTARY_IDS_QUERY = """
query getComplementaryIds($id: ID!) {
    product(id: $id) {
        id
        complementaryProducts: metafield(namespace: "shopify--discovery--product_recommendation", key: "complementary_products") {
            references(first: 10) {
                edges {
                    node {
                        ... on Product {
                            id
                        }
                    }
                }
            }
        }
    }
}
"""


def strip_product_gid(gid: str) -> str:
    """Same id-stripping logic as parse_metafield_product() in server.py."""
    return (gid or "").replace("gid://shopify/Product/", "")


async def fetch_complementary_ids(http_client: httpx.AsyncClient, shopify_product_id: str) -> list:
    """Query Shopify for a single product's complementary_products metafield
    references and return the referenced product ids (gid prefix stripped,
    same format `id` is stored in on our own product docs). Returns [] if
    there's no metafield set / no references / the product no longer exists
    on Shopify."""
    variables = {"id": f"gid://shopify/Product/{shopify_product_id}"}
    headers = {
        "Content-Type": "application/json",
        "X-Shopify-Storefront-Access-Token": SHOPIFY_STOREFRONT_TOKEN,
    }
    response = await http_client.post(
        GRAPHQL_URL,
        json={"query": COMPLEMENTARY_IDS_QUERY, "variables": variables},
        headers=headers,
        timeout=30.0,
    )
    response.raise_for_status()
    data = response.json()
    if data.get("errors"):
        raise RuntimeError(str(data["errors"]))

    product_data = (data.get("data") or {}).get("product") or {}
    metafield = product_data.get("complementaryProducts")
    if not metafield or not metafield.get("references"):
        return []

    ids = []
    for edge in metafield["references"].get("edges", []):
        node = edge.get("node") or {}
        gid = node.get("id")
        if gid:
            ids.append(strip_product_gid(gid))
    return ids


async def main():
    mongo = AsyncIOMotorClient(MONGO_URL)
    db = mongo[DB_NAME]
    print(f"Connected to DB '{DB_NAME}'")

    all_products = await db.shopify_products.find({}, {"id": 1}).to_list(None)
    products_to_check = [p for p in all_products if p.get("id") and not p["id"].startswith("local-")]
    skipped_local = len(all_products) - len(products_to_check)

    print(f"Total products in db.shopify_products: {len(all_products)}")
    print(f"Skipping {skipped_local} locally-created product(s) (id starts with 'local-', never existed on Shopify)")
    print(f"Checking {len(products_to_check)} product(s) against Shopify's complementary_products metafield...\n")

    checked = 0
    found = 0
    written = 0
    errors = []
    examples = []

    async with httpx.AsyncClient() as http_client:
        for product in products_to_check:
            product_id = product["id"]
            checked += 1
            try:
                ids = await fetch_complementary_ids(http_client, product_id)
                if ids:
                    found += 1
                    result = await db.shopify_products.update_one(
                        {"id": product_id},
                        {"$set": {"complementary_product_ids": ids}},
                    )
                    if result.matched_count > 0:
                        written += 1
                        if len(examples) < 5:
                            examples.append((product_id, ids))
                    else:
                        errors.append((product_id, "update_one matched 0 documents (product removed mid-run?)"))
            except Exception as e:
                errors.append((product_id, str(e)))

            if checked % 200 == 0 or checked == len(products_to_check):
                print(f"  ... {checked}/{len(products_to_check)} checked, {found} found, {written} written, {len(errors)} errors")

            # Small delay to avoid Shopify rate limiting - same 0.2s pattern
            # already used in sync_all_products() in server.py.
            await asyncio.sleep(0.2)

    print("\n===== Backfill summary =====")
    print(f"Total products checked: {checked}")
    print(f"Had complementary metafield data on Shopify: {found}")
    print(f"Successfully written to db.shopify_products: {written}")
    print(f"Errors: {len(errors)}")
    if errors:
        print("\nError details (product_id: error):")
        for pid, err in errors[:50]:
            print(f"  {pid}: {err}")
        if len(errors) > 50:
            print(f"  ... and {len(errors) - 50} more")

    if examples:
        print("\nExample products with non-empty complementary_product_ids written:")
        for pid, ids in examples:
            print(f"  {pid}: {ids}")


if __name__ == "__main__":
    asyncio.run(main())
