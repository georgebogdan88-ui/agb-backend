"""One-off backfill: add "Piese noi Consumabile" as an ADDITIONAL collection
tag on every "Nou" product whose description contains "consumabil"/
"consumabile", alongside whatever category it already has (e.g. "motor") -
does NOT replace/remove the existing category. Confirmed explicitly: staff
wants a product to be able to show up under both its existing category AND
Consumabile, not have Consumabile replace it.

`collections` is a list field - Mongo already matches array-containment on
a scalar query (`{"collections": "Piese noi Consumabile"}` matches any doc
where that string is ANYWHERE in the array), and GET /products?collection=X
already queries it that way (see get_products() in server.py) - so a
product carrying both "Piese noi motor" and "Piese noi Consumabile" simply
shows up correctly under both storefront category filters with no other
code change needed.

KNOWN LIMITATION (accepted, not fixed here): the CRM admin form's Categorie
field is still single-value - extractCurrentCategory() returns only the
FIRST "Piese noi X" tag it finds in collections. If staff later opens one
of these products in the admin form and saves without deliberately
re-adding "Consumabile" as free text, build_collections() will rebuild
collections from that single value and drop the second tag added here.
Proper multi-category editing would need a real UI change - out of scope
for this one-time backfill.

Idempotent / safe to re-run - skips any product that already has the tag.

Usage: python scripts/backfill_consumabile_category.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402

TAG = "Piese noi Consumabile"


async def main():
    db = server.db
    cursor = db.shopify_products.find(
        {
            "product_type": "Nou",
            "description": {"$regex": "consumabil", "$options": "i"},
        },
        {"id": 1, "title": 1, "collections": 1},
    )
    candidates = await cursor.to_list(length=None)
    print(f"{len(candidates)} produse 'Nou' cu 'consumabil' in descriere.")

    already_tagged = 0
    updated = 0
    for p in candidates:
        existing_collections = p.get("collections") or []
        if TAG in existing_collections:
            already_tagged += 1
            continue

        new_collections = [*existing_collections, TAG]
        await db.shopify_products.update_one(
            {"id": p["id"]},
            {
                "$set": {
                    "collections": new_collections,
                    "collections_normalized": [server.normalize_text(c) for c in new_collections],
                }
            },
        )
        updated += 1
        print("  + " + str(p.get("title", p["id"])))

    print("")
    print(str(updated) + " produse au primit tag-ul Consumabile (in plus fata de categoria existenta), " + str(already_tagged) + " il aveau deja.")


if __name__ == "__main__":
    asyncio.run(main())
