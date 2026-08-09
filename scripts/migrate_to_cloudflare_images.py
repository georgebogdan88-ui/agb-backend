"""Migrate product images from Cloudinary to Cloudflare Images.

Context: agb-backend currently stores product images on Cloudinary
(`shopify_products.image_url` / `.images`, plus a `shopify_products.image_url` fed
by the earlier one-off Shopify CDN -> Cloudinary migration logged in
`db.image_migrations` - see server.py's "IMAGE MIGRATION" section). A read-only
audit found the Cloudinary account is constantly over the Free plan's limits
(~15.5k original uploads + ~16k derived `ar_1:1,c_crop,g_center` crop variants),
so the decision was made to move to Cloudflare Images instead (much more
predictable pricing: ~$5/100k stored, ~$1/100k delivered).

This script uploads each product's CURRENT (Cloudinary) `image_url` to
Cloudflare Images and records the result in two NEW, separate fields on the
product document: `cf_image_id` / `cf_image_url`. It NEVER touches or
overwrites the existing `image_url` / `images` fields - those keep working
exactly as before until a later, separate change switches the app/webshop
over to read from the Cloudflare fields (out of scope here).

Scope note: only the single primary `image_url` is migrated in this pass, not
the `images` gallery array - see README_cloudflare_migration.md for why and
how to extend this later if needed.

Crop variant note: product image URLs on Cloudinary often carry an
`ar_1:1,c_crop,g_center` transformation segment (the crop applied during the
original Shopify -> Cloudinary migration). We strip that segment before
downloading, so we upload Cloudflare the ORIGINAL asset, not the already
-cropped derivative - Cloudflare Images can apply its own variants/transforms
on delivery later without us needing to keep a separately-cropped copy.

SAFETY (same model as scripts/seed_staging_data.py):
  - Requires --mongo-url and --db-name explicitly on every run - never reads
    this repo's own .env, which points at the real shared MongoDB cluster
    used by both local dev and production.
  - Requires Cloudflare credentials explicitly too, either via
    --cf-account-id/--cf-api-token or the CLOUDFLARE_ACCOUNT_ID/
    CLOUDFLARE_API_TOKEN environment variables - never assumes a .env file.
    This is validated BEFORE any MongoDB connection is opened, so a run with
    missing credentials fails fast with a clear message instead of connecting
    first and failing mid-migration.
  - --dry-run is the DEFAULT. Pass --live explicitly to write to MongoDB or
    upload anything to Cloudflare. In dry-run mode the script still opens a
    READ-ONLY connection to --mongo-url (it needs to know how many products
    are actually pending, which only real data can answer) but performs no
    writes and no Cloudflare API calls whatsoever.
  - Idempotent: skips any product that already has `cf_image_url` set, so the
    script can be interrupted (Ctrl+C, crash, rate limit) and re-run safely
    without re-uploading already-migrated images.
  - Every attempt (success or failure) is logged to a new
    `cloudflare_image_migrations` collection, keyed by the source Cloudinary
    `image_url`, mirroring the existing `image_migrations` collection's shape
    (`status` / `error` / `migrated_at`, plus `cf_image_id` / `cf_delivery_url`).
  - Retries downloads and uploads up to --retries times (default 3) with
    simple exponential backoff on network errors / rate limiting.
  - Never prints the Cloudflare API token or the full Mongo connection string
    (only the host portion, same as seed_staging_data.py).

Usage (dry run - default, read-only, safe to run anytime against real data):
    python scripts/migrate_to_cloudflare_images.py \
        --mongo-url "mongodb+srv://user:pass@cluster0.../agb_agroparts" \
        --db-name agb_agroparts \
        --cf-account-id YOUR_CF_ACCOUNT_ID \
        --cf-api-token YOUR_CF_API_TOKEN

Usage (live - actually uploads to Cloudflare and writes to MongoDB):
    python scripts/migrate_to_cloudflare_images.py \
        --mongo-url "mongodb+srv://..." \
        --db-name agb_agroparts \
        --cf-account-id YOUR_CF_ACCOUNT_ID \
        --cf-api-token YOUR_CF_API_TOKEN \
        --live

Test on a handful of products first with --limit:
    python scripts/migrate_to_cloudflare_images.py ... --live --limit 5

See README_cloudflare_migration.md for full flag reference and how to verify
results in MongoDB.
"""
import argparse
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# Windows consoles often default stdout/stderr to cp1252, which can't encode
# diacritics in this script's Romanian messages - force UTF-8 explicitly,
# same fix as scripts/seed_staging_data.py.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx
from dotenv import load_dotenv
from pymongo import MongoClient

# Picks up CLOUDFLARE_ACCOUNT_ID / CLOUDFLARE_API_TOKEN from agb-backend/.env
# if present, same as the rest of this repo's Cloudinary/Mongo/etc. config.
# --mongo-url / --db-name stay required CLI args regardless (see SAFETY above)
# - this only affects the Cloudflare credential fallback.
load_dotenv()

CLOUDFLARE_UPLOAD_URL = "https://api.cloudflare.com/client/v4/accounts/{account_id}/images/v1"

# The exact transformation segment applied during the original Shopify CDN ->
# Cloudinary migration for product images. We only strip a path segment that
# CONTAINS "c_crop" (rather than matching this literal string exactly), so
# this keeps working even if the crop transform's exact params ever change,
# while still never touching URLs that don't have a transform segment at all.
CLOUDINARY_CROP_MARKER = "c_crop"


def strip_cloudinary_transform(url: str) -> str:
    """Return the base Cloudinary delivery URL for the ORIGINAL uploaded
    asset, removing an `ar_1:1,c_crop,g_center`-style transformation segment
    if present (keeps any `v<version>/...` segment that follows it intact).
    Only strips a segment that actually looks like a crop transform - if the
    URL doesn't contain "/upload/" or doesn't have a transform segment right
    after it, it's returned unchanged.
    """
    marker = "/upload/"
    idx = url.find(marker)
    if idx == -1:
        return url
    prefix = url[: idx + len(marker)]
    rest = url[idx + len(marker):]
    parts = rest.split("/", 1)
    if len(parts) == 2 and CLOUDINARY_CROP_MARKER in parts[0]:
        return prefix + parts[1]
    return url


def retry_with_backoff(fn, *, retries: int, what: str, base_delay: float = 1.5):
    """Call fn() (no args), retrying on any exception up to `retries` times
    total, with simple exponential backoff between attempts. Re-raises the
    last exception if every attempt fails."""
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 - deliberately broad, this is a retry wrapper
            last_exc = e
            if attempt < retries:
                wait = base_delay ** attempt
                print(f"      retry {attempt}/{retries - 1} for {what} after error: {e} (waiting {wait:.1f}s)", flush=True)
                time.sleep(wait)
    raise last_exc


def download_original_image(client: httpx.Client, url: str) -> bytes:
    resp = client.get(url, timeout=30.0)
    resp.raise_for_status()
    return resp.content


def upload_to_cloudflare(client: httpx.Client, account_id: str, token: str, filename: str, content: bytes, custom_id: str = None) -> dict:
    url = CLOUDFLARE_UPLOAD_URL.format(account_id=account_id)
    headers = {"Authorization": f"Bearer {token}"}
    files = {"file": (filename, content)}
    data = {"id": custom_id} if custom_id else None
    resp = client.post(url, headers=headers, files=files, data=data, timeout=60.0)
    if resp.status_code >= 400:
        raise RuntimeError(f"Cloudflare upload a eșuat ({resp.status_code}): {resp.text[:300]}")
    payload = resp.json()
    if not payload.get("success"):
        raise RuntimeError(f"Cloudflare a raportat eșec: {payload.get('errors')}")
    return payload["result"]


# Cloudflare's built-in "public" variant only scales an image down to fit
# 1366x768 - it does NOT crop, so a portrait original stays portrait. That
# does not match the ar_1:1,c_crop,g_center square crop every product image
# currently has on Cloudinary. A custom variant named "square"
# (fit=cover, 1000x1000 - center-crops to fill a square, same visual result
# as the Cloudinary crop) was created in the Cloudflare account for this.
DELIVERY_VARIANT = "square"


def pick_delivery_url(cf_result: dict) -> str:
    """Cloudflare returns a `variants` list of ready-to-use delivery URLs,
    one per configured variant (e.g. .../public). We swap the trailing
    segment for DELIVERY_VARIANT so every migrated product gets the
    center-cropped square version, not Cloudflare's uncropped default."""
    variants = cf_result.get("variants") or []
    if not variants:
        return None
    base = variants[0].rsplit("/", 1)[0]
    return f"{base}/{DELIVERY_VARIANT}"


def build_custom_id(product: dict, source: str, prefix: str) -> str:
    if source == "product-id":
        return f"{prefix}{product['id']}"
    if source == "sku" and product.get("sku"):
        return f"{prefix}{product['sku']}"
    return None


def process_product(product: dict, *, args, migrations_col, products_col, http_client: httpx.Client, stats: dict, stats_lock: threading.Lock):
    def bump(key: str):
        # Worker threads share `stats` - a plain dict `+= 1` isn't atomic in
        # CPython (it's a separate load/add/store), so guard every increment
        # with the shared lock to avoid dropped counts under concurrency.
        with stats_lock:
            stats[key] += 1

    image_url = product.get("image_url")
    title = (product.get("title") or "")[:60]

    if not image_url or "res.cloudinary.com" not in image_url:
        bump("skipped_not_cloudinary")
        return

    if product.get("cf_image_url"):
        bump("skipped_already_done")
        return

    existing_migration = migrations_col.find_one({"_id": image_url})
    if existing_migration and existing_migration.get("status") == "done":
        # Already uploaded to Cloudflare previously, just never got written
        # back onto this product doc (e.g. the process died between the
        # upload succeeding and the product update running). Backfill from
        # the log instead of re-uploading.
        bump("backfilled_from_log")
        if args.live:
            products_col.update_one(
                {"id": product["id"]},
                {"$set": {
                    "cf_image_id": existing_migration.get("cf_image_id"),
                    "cf_image_url": existing_migration.get("cf_delivery_url"),
                }},
            )
        return

    source_url = strip_cloudinary_transform(image_url)

    if not args.live:
        bump("would_migrate")
        print(f"  [dry-run] {product['id']} \"{title}\" -> would download {source_url} and upload to Cloudflare", flush=True)
        return

    custom_id = build_custom_id(product, args.custom_id_source, args.custom_id_prefix)
    try:
        content = retry_with_backoff(
            lambda: download_original_image(http_client, source_url),
            retries=args.retries, what=f"download {product['id']}",
        )
        filename = source_url.rsplit("/", 1)[-1] or f"{product['id']}.jpg"
        cf_result = retry_with_backoff(
            lambda: upload_to_cloudflare(http_client, args.cf_account_id, args.cf_api_token, filename, content, custom_id),
            retries=args.retries, what=f"upload {product['id']}",
        )
        cf_image_id = cf_result.get("id")
        cf_delivery_url = pick_delivery_url(cf_result)

        products_col.update_one(
            {"id": product["id"]},
            {"$set": {"cf_image_id": cf_image_id, "cf_image_url": cf_delivery_url}},
        )
        migrations_col.update_one(
            {"_id": image_url},
            {"$set": {
                "status": "done",
                "cf_image_id": cf_image_id,
                "cf_delivery_url": cf_delivery_url,
                "error": None,
                "migrated_at": datetime.now(timezone.utc),
            }},
            upsert=True,
        )
        bump("migrated")
        print(f"  [ok] {product['id']} \"{title}\" -> cf_image_id={cf_image_id}", flush=True)
    except Exception as e:
        migrations_col.update_one(
            {"_id": image_url},
            {"$set": {
                "status": "failed",
                "error": str(e),
                "migrated_at": datetime.now(timezone.utc),
            }},
            upsert=True,
        )
        bump("failed")
        print(f"  [FAILED] {product['id']} \"{title}\": {e}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mongo-url", required=True, help="MongoDB connection string - never omitted, never defaulted from .env")
    parser.add_argument("--db-name", required=True, help="Database name")
    parser.add_argument("--cf-account-id", default=os.environ.get("CLOUDFLARE_ACCOUNT_ID"),
                         help="Cloudflare account ID (or set CLOUDFLARE_ACCOUNT_ID env var)")
    parser.add_argument("--cf-api-token", default=os.environ.get("CLOUDFLARE_API_TOKEN"),
                         help="Cloudflare Images API token (or set CLOUDFLARE_API_TOKEN env var) - never logged")
    parser.add_argument("--live", action="store_true",
                         help="Actually upload to Cloudflare and write to MongoDB. Without this flag, the script only reports what it would do.")
    parser.add_argument("--limit", type=int, default=None, help="Only process the first N pending products (useful for a test run)")
    parser.add_argument("--concurrency", type=int, default=5, help="Number of products processed in parallel (default 5)")
    parser.add_argument("--retries", type=int, default=3, help="Max attempts per download/upload before marking a product failed (default 3)")
    parser.add_argument("--custom-id-source", choices=["none", "product-id", "sku"], default="none",
                         help="Whether to ask Cloudflare to use a custom image id derived from the product instead of a random one (default: none, let Cloudflare generate one)")
    parser.add_argument("--custom-id-prefix", default="agb-", help="Prefix used when --custom-id-source is product-id or sku (default: 'agb-')")
    args = parser.parse_args()

    # Validated before ANY network/DB connection is opened, so a run with
    # missing credentials fails fast and clearly rather than connecting to
    # Mongo first and only then discovering Cloudflare creds are missing.
    if not args.cf_account_id or not args.cf_api_token:
        sys.exit(
            "REFUZAT: lipsesc credențialele Cloudflare.\n"
            "Setează --cf-account-id și --cf-api-token, sau variabilele de mediu\n"
            "CLOUDFLARE_ACCOUNT_ID / CLOUDFLARE_API_TOKEN, înainte de a rula scriptul\n"
            "(inclusiv în --dry-run, ca validarea să fie identică indiferent de mod)."
        )

    mode = "LIVE (va scrie în MongoDB și va urca imagini pe Cloudflare)" if args.live else "DRY-RUN (doar citire, niciun apel real)"
    host_display = args.mongo_url.split("@")[-1] if "@" in args.mongo_url else args.mongo_url
    print(f"Mod: {mode}", flush=True)
    print(f"Target Mongo: {args.db_name} @ {host_display}", flush=True)
    print(f"Cloudflare account: {args.cf_account_id[:6]}... (token ascuns)", flush=True)
    if args.limit:
        print(f"Limit: primele {args.limit} produse eligibile", flush=True)

    # Fails fast with a clear error on an unreachable/invalid host instead of
    # pymongo's default 30s server-selection hang.
    client = MongoClient(args.mongo_url, serverSelectionTimeoutMS=8000)
    db = client[args.db_name]
    products_col = db.shopify_products
    migrations_col = db.cloudflare_image_migrations

    cursor = products_col.find(
        {"image_url": {"$regex": "res.cloudinary.com"}},
        {"id": 1, "title": 1, "sku": 1, "image_url": 1, "cf_image_url": 1},
    )
    if args.limit:
        cursor = cursor.limit(args.limit)
    products = list(cursor)
    print(f"\nProduse cu image_url pe Cloudinary găsite: {len(products)}\n", flush=True)

    stats = {
        "would_migrate": 0,
        "migrated": 0,
        "failed": 0,
        "skipped_already_done": 0,
        "skipped_not_cloudinary": 0,
        "backfilled_from_log": 0,
    }
    stats_lock = threading.Lock()

    with httpx.Client() as http_client, ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [
            pool.submit(process_product, p, args=args, migrations_col=migrations_col,
                        products_col=products_col, http_client=http_client, stats=stats, stats_lock=stats_lock)
            for p in products
        ]
        for f in as_completed(futures):
            f.result()  # re-raise anything unexpected instead of silently swallowing it

    print("\n--- Rezumat ---")
    for key, value in stats.items():
        print(f"  {key}: {value}")

    if not args.live:
        print("\n--dry-run: nimic scris în MongoDB, niciun apel către Cloudflare. Rulează cu --live ca să migrezi efectiv.")


if __name__ == "__main__":
    main()
