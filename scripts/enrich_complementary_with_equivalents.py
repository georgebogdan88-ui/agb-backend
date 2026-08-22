"""
One-off enrichment: for every db.shopify_products doc whose
`complementary_product_ids` is non-empty, pull in each of those
complementary products' own `equivalent_product_ids` ("same part, different
brand" - see get_equivalent_products() in server.py) and add them DIRECTLY,
as real stored data, into the main product's own complementary_product_ids
list (George, 2026-08-22).

Why this exists: once an equivalent id is present in complementary_product_ids,
it's just an ordinary complementary entry from get_complementary_products()'s
point of view - the stock-tier + price sort already built into
_stock_sort_key (server.py, "sortarea deja construita aseara") automatically
places it after any of its still-in-stock siblings. So enriching the stored
list once, here, needs no extra read-time logic in get_complementary_products
at all.

Deliberately does NOT check the stock_status of the complementary product
before pulling in its equivalents - applies unconditionally to every
complementary_product_ids entry, whether that complementary item is itself
in stock or not (George, exact requirement, confirmed twice, 2026-08-22).

Model/style: same conventions as scripts/backfill_complementary_products.py
(direct Mongo connection via the same .env vars, update_one($set=...)
writes, idempotent, progress + summary output) - but this script needs NO
Shopify call at all, since both complementary_product_ids and
equivalent_product_ids already live locally in db.shopify_products.

Idempotent / safe to re-run: a product whose complementary_product_ids
already contains every one of its complementary items' equivalents gets no
further writes on a second run - see _compute_enriched_complementary_ids
below, and scripts/test_enrich_complementary_with_equivalents.py's
idempotency scenario.

Usage:
    venv/Scripts/python.exe scripts/enrich_complementary_with_equivalents.py

Env vars come from .env in the repo root (MONGO_URL / DB_NAME), same as
server.py and the rest of this repo's scripts.
"""
import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env", override=False)

MONGO_URL = os.environ["MONGO_URL"]
DB_NAME = os.environ["DB_NAME"]


def _compute_enriched_complementary_ids(product: dict, complementary_docs: dict) -> tuple:
    """Pure, testable core of the enrichment (no I/O).

    `product` is the main product's own doc (needs at least `id` and
    `complementary_product_ids`). `complementary_docs` is a dict of
    {complementary_product_id: that_product's_own_doc} for every id already
    listed in `product["complementary_product_ids"]` that could be found
    (missing/deleted referenced products are simply absent from the dict -
    they contribute nothing, same as they already contribute nothing in
    get_complementary_products' find_one loop).

    Returns (updated_complementary_ids, newly_added_ids):
      - updated_complementary_ids: the existing complementary_product_ids
        list with every complementary product's equivalent_product_ids
        appended, deduplicated against both the existing list and each
        other, and excluding the main product's own id (no self-reference).
      - newly_added_ids: just the ids that got appended - empty if there's
        nothing new to add (this is what makes the caller's write
        idempotent: a second pass over already-enriched data always
        produces an empty newly_added_ids, so no update_one is issued).

    Order is preserved: existing complementary ids first (unchanged, so
    nothing about already-curated ordering shifts), then newly-added
    equivalent ids appended at the end - final display order is entirely
    decided at read time by _stock_sort_key in server.py regardless of
    storage order, so this is purely about not needlessly rewriting the
    existing prefix.
    """
    main_id = product.get("id")
    existing_ids = list(product.get("complementary_product_ids") or [])

    seen = set(existing_ids)
    seen.add(main_id)

    new_ids = []
    for complementary_id in existing_ids:
        complementary_doc = complementary_docs.get(complementary_id)
        if not complementary_doc:
            continue
        for equivalent_id in complementary_doc.get("equivalent_product_ids") or []:
            if equivalent_id in seen:
                continue
            seen.add(equivalent_id)
            new_ids.append(equivalent_id)

    return existing_ids + new_ids, new_ids


async def main():
    mongo = AsyncIOMotorClient(MONGO_URL)
    db = mongo[DB_NAME]
    print(f"Connected to DB '{DB_NAME}'")

    # Preload every product's own equivalent_product_ids, keyed by id, so
    # resolving a complementary product's equivalents below is an in-memory
    # dict lookup instead of a per-id Mongo round trip (George: "preincarca
    # ... intr-un dict ca sa eviti N interogari repetate"). The catalog is
    # small enough that loading id + equivalent_product_ids for every
    # product up front is cheap.
    all_products = await db.shopify_products.find(
        {}, {"id": 1, "equivalent_product_ids": 1}
    ).to_list(None)
    equivalents_by_id = {p["id"]: p for p in all_products if p.get("id")}

    products_to_enrich = await db.shopify_products.find(
        {"complementary_product_ids": {"$exists": True, "$ne": []}},
        {"id": 1, "title": 1, "complementary_product_ids": 1},
    ).to_list(None)

    print(f"Total products in db.shopify_products: {len(all_products)}")
    print(f"Products with non-empty complementary_product_ids: {len(products_to_enrich)}\n")

    checked = 0
    updated = 0
    total_new_ids = 0
    errors = []
    examples = []

    for product in products_to_enrich:
        checked += 1
        product_id = product.get("id")
        try:
            complementary_docs = {
                complementary_id: equivalents_by_id[complementary_id]
                for complementary_id in (product.get("complementary_product_ids") or [])
                if complementary_id in equivalents_by_id
            }
            updated_ids, new_ids = _compute_enriched_complementary_ids(product, complementary_docs)

            if new_ids:
                result = await db.shopify_products.update_one(
                    {"id": product_id},
                    {"$set": {"complementary_product_ids": updated_ids}},
                )
                if result.matched_count > 0:
                    updated += 1
                    total_new_ids += len(new_ids)
                    if len(examples) < 10:
                        examples.append((product_id, product.get("title", ""), new_ids))
                else:
                    errors.append((product_id, "update_one matched 0 documents (product removed mid-run?)"))
        except Exception as e:
            errors.append((product_id, str(e)))

        if checked % 200 == 0 or checked == len(products_to_enrich):
            print(f"  ... {checked}/{len(products_to_enrich)} checked, {updated} updated, {total_new_ids} new ids added so far, {len(errors)} errors")

    print("\n===== Enrichment summary =====")
    print(f"Products checked (non-empty complementary_product_ids): {checked}")
    print(f"Products updated with new equivalent ids: {updated}")
    print(f"Total new ids added across all products: {total_new_ids}")
    print(f"Errors: {len(errors)}")
    if errors:
        print("\nError details (product_id: error):")
        for pid, err in errors[:50]:
            print(f"  {pid}: {err}")
        if len(errors) > 50:
            print(f"  ... and {len(errors) - 50} more")

    if examples:
        print("\nExample products updated (id, title, new ids added):")
        for pid, title, new_ids in examples:
            print(f"  {pid} ({title}): +{new_ids}")


if __name__ == "__main__":
    asyncio.run(main())
