"""
One-off backfill: populate `created_at`/`updated_at` on every db.shopify_products
doc that doesn't have them yet.

Why this exists: `created_at`/`updated_at` were just added to the `Product`
model (see server.py) so the admin webshop can list/sort products by when
they were created or last modified. Every product synced from Shopify before
this change (and even most manually-created ones, since admin_create_product()
only ever wrote `created_at`, never `updated_at`, before this) has neither
field. This is a ONE-TIME direct-to-DB backfill for all of those - going
forward, `created_at` is stamped once at creation (admin_create_product() /
sync_all_products() picking up a brand-new product) and `updated_at` is kept
current by _apply_product_update() (PUT /admin/products/{id} and the bulk
routes that share it), admin_upload_image() (when it updates a product's
image), and sync_all_products() itself (only when a resync actually picks up
different Shopify-side data for a product it already had - see
_product_sync_content_changed() in server.py) - so this script never needs to
run again for a product once it's been touched.

Fallback used per product, in priority order:
  1. Its existing `synced_at` (when the product was last pulled from Shopify)
     if present - the closest approximation of "when this was created" we
     have for Shopify-synced products, since none of them were ever missing
     `synced_at`.
  2. The current time (`--as-of`, defaulting to when the script runs) for
     anything with neither `created_at` nor `synced_at` (manually-created
     products, whose id starts with "local-").
`updated_at` falls back to whatever `created_at` ends up being (existing or
freshly backfilled) - there's no better signal available for "last modified"
on a product nobody has touched via the API since this field existed.

Idempotent / safe to re-run: only touches products missing `created_at` and/or
`updated_at` (`{"field": None}` matches both a missing field and one
explicitly set to null); already-backfilled or already-timestamped products
are left completely alone.

Writes go directly to the DB via `bulk_write($set=...)`, NOT through
PUT /admin/products/{id}, so this does NOT stamp `source: "manual"` on
Shopify-synced products or otherwise touch any field besides created_at/
updated_at.

Usage:
    venv/Scripts/python.exe scripts/backfill_product_timestamps.py [--dry-run]

Env vars come from .env in the repo root (MONGO_URL / DB_NAME), same DB the
rest of this repo's scripts use.
"""
import argparse
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
import os

from pymongo import MongoClient, UpdateOne

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env", override=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report what would change, but don't write to the DB",
    )
    args = parser.parse_args()

    mongo_url = os.environ["MONGO_URL"]
    db_name = os.environ["DB_NAME"]
    mongo = MongoClient(mongo_url)
    db = mongo[db_name]
    print(f"Connected to DB '{db_name}'")

    script_run_at = datetime.utcnow()

    total_products = db.shopify_products.count_documents({})
    query = {"$or": [{"created_at": None}, {"updated_at": None}]}
    candidates = list(
        db.shopify_products.find(
            query,
            {"id": 1, "created_at": 1, "updated_at": 1, "synced_at": 1, "source": 1},
        )
    )

    print(f"Total products in db.shopify_products: {total_products}")
    print(f"Products missing created_at and/or updated_at: {len(candidates)}")

    missing_created = 0
    missing_updated = 0
    used_synced_at_fallback = 0
    used_now_fallback = 0
    manual_products_affected = 0
    examples = []

    ops = []
    for doc in candidates:
        set_fields = {}

        if doc.get("created_at") is None:
            missing_created += 1
            synced_at = doc.get("synced_at")
            if synced_at is not None:
                set_fields["created_at"] = synced_at
                used_synced_at_fallback += 1
            else:
                set_fields["created_at"] = script_run_at
                used_now_fallback += 1

        if doc.get("updated_at") is None:
            missing_updated += 1
            # Whatever created_at ends up being for this doc (existing, or
            # just computed above).
            set_fields["updated_at"] = set_fields.get("created_at", doc.get("created_at"))

        if doc.get("source") == "manual" or (doc.get("id") or "").startswith("local-"):
            manual_products_affected += 1

        if len(examples) < 10:
            examples.append((doc["id"], set_fields))

        ops.append(UpdateOne({"id": doc["id"]}, {"$set": set_fields}))

    print(f"  - missing created_at: {missing_created}")
    print(f"  - missing updated_at: {missing_updated}")
    print(f"  - created_at backfilled from synced_at: {used_synced_at_fallback}")
    print(f"  - created_at backfilled from current time (no synced_at, e.g. manual products): {used_now_fallback}")
    print(f"  - of those, manually-created products (source='manual' or id starts with 'local-'): {manual_products_affected}")

    if examples:
        print("\nExample writes (product_id: fields to $set):")
        for pid, fields in examples:
            print(f"  {pid}: {fields}")

    if args.dry_run:
        print("\n--dry-run set: no writes performed.")
        return

    if not ops:
        print("\nNothing to backfill.")
        return

    result = db.shopify_products.bulk_write(ops)
    print(f"\nbulk_write: matched={result.matched_count} modified={result.modified_count}")

    remaining = db.shopify_products.count_documents(query)
    print(f"Products still missing created_at and/or updated_at after backfill: {remaining}")


if __name__ == "__main__":
    main()
