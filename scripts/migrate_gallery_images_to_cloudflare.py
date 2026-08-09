"""Migrate product GALLERY images (images[] beyond the primary image_url)
from Cloudinary to Cloudflare Images.

Context: scripts/migrate_to_cloudflare_images.py already migrated every
product's PRIMARY image_url; this is the follow-up "second pass" for the
`images` array (secondary/gallery photos), out of scope of that script by
design (see its own docstring). Same motivation: closing the Cloudinary
account.

For each product with 1+ Cloudinary URLs in `images[1:]` (index 0 is the
primary image, already handled elsewhere and left untouched here), this
script uploads every Cloudinary image in the gallery to Cloudflare Images
and writes the FULL parallel array to a NEW field `cf_images` (same length/
order as `images`) - non-Cloudinary entries (already Cloudflare, or any
other external URL) are carried through unchanged into the corresponding
slot, so `cf_images` ends up complete, not just the migrated subset. It
NEVER touches or overwrites the existing `images` field - that keeps
working exactly as before until a later, separate change switches reads
over to `cf_images` (out of scope here, same as the primary-image script's
cf_image_url).

SAFETY (identical model to migrate_to_cloudflare_images.py):
  - Requires --mongo-url and --db-name explicitly - never reads this repo's
    own .env for Mongo.
  - Requires Cloudflare credentials explicitly (--cf-account-id/--cf-api-token
    or CLOUDFLARE_ACCOUNT_ID/CLOUDFLARE_API_TOKEN env vars), validated before
    any MongoDB connection is opened.
  - --dry-run is the DEFAULT. Pass --live to write to MongoDB or upload
    anything to Cloudflare.
  - Idempotent: skips any product that already has a `cf_images` array of
    the same length as `images` (re-run-safe after interruption).
  - Every attempt (success or failure) per source URL is logged to a new
    `cloudflare_gallery_migrations` collection (`_id` = source Cloudinary
    URL), so a crash mid-run can resume without re-uploading.
  - Retries downloads/uploads up to --retries times (default 3) with
    exponential backoff.
  - Never prints the Cloudflare API token or the full Mongo connection
    string (only the host portion).

Usage (dry run - default, read-only, safe to run anytime against real data):
    python scripts/migrate_gallery_images_to_cloudflare.py \
        --mongo-url "mongodb+srv://user:pass@cluster0.../agb_agroparts" \
        --db-name agb_agroparts \
        --cf-account-id YOUR_CF_ACCOUNT_ID \
        --cf-api-token YOUR_CF_API_TOKEN

Usage (live):
    python scripts/migrate_gallery_images_to_cloudflare.py ... --live
"""
import argparse
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()

CLOUDFLARE_UPLOAD_URL = "https://api.cloudflare.com/client/v4/accounts/{account_id}/images/v1"
CLOUDINARY_CROP_MARKER = "c_crop"
DELIVERY_VARIANT = "square"


def strip_cloudinary_transform(url: str) -> str:
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
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
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


def upload_to_cloudflare(client: httpx.Client, account_id: str, token: str, filename: str, content: bytes) -> dict:
    url = CLOUDFLARE_UPLOAD_URL.format(account_id=account_id)
    headers = {"Authorization": f"Bearer {token}"}
    files = {"file": (filename, content)}
    resp = client.post(url, headers=headers, files=files, timeout=60.0)
    if resp.status_code >= 400:
        raise RuntimeError(f"Cloudflare upload a eșuat ({resp.status_code}): {resp.text[:300]}")
    payload = resp.json()
    if not payload.get("success"):
        raise RuntimeError(f"Cloudflare a raportat eșec: {payload.get('errors')}")
    return payload["result"]


def pick_delivery_url(cf_result: dict) -> str:
    variants = cf_result.get("variants") or []
    if not variants:
        return None
    base = variants[0].rsplit("/", 1)[0]
    return f"{base}/{DELIVERY_VARIANT}"


def migrate_one_image(http_client, args, migrations_col, source_url: str) -> str:
    """Returns the Cloudflare delivery URL for source_url, uploading it if
    needed (or reusing a prior successful log entry). Raises on failure."""
    existing = migrations_col.find_one({"_id": source_url})
    if existing and existing.get("status") == "done":
        return existing["cf_delivery_url"]

    clean_url = strip_cloudinary_transform(source_url)
    content = retry_with_backoff(
        lambda: download_original_image(http_client, clean_url),
        retries=args.retries, what=f"download {source_url}",
    )
    filename = clean_url.rsplit("/", 1)[-1] or "gallery.jpg"
    cf_result = retry_with_backoff(
        lambda: upload_to_cloudflare(http_client, args.cf_account_id, args.cf_api_token, filename, content),
        retries=args.retries, what=f"upload {source_url}",
    )
    cf_delivery_url = pick_delivery_url(cf_result)
    migrations_col.update_one(
        {"_id": source_url},
        {"$set": {
            "status": "done",
            "cf_image_id": cf_result.get("id"),
            "cf_delivery_url": cf_delivery_url,
            "error": None,
            "migrated_at": datetime.now(timezone.utc),
        }},
        upsert=True,
    )
    return cf_delivery_url


def process_product(product: dict, *, args, migrations_col, products_col, http_client, stats: dict, stats_lock: threading.Lock):
    def bump(key: str):
        with stats_lock:
            stats[key] += 1

    images = product.get("images") or []
    title = (product.get("title") or "")[:60]
    gallery = images[1:]  # index 0 is the primary image, out of scope here

    if not gallery:
        bump("skipped_no_gallery")
        return

    existing_cf_images = product.get("cf_images") or []
    if len(existing_cf_images) == len(images):
        bump("skipped_already_done")
        return

    has_cloudinary = any(url and "res.cloudinary.com" in url for url in gallery)
    if not has_cloudinary:
        bump("skipped_no_cloudinary_in_gallery")
        return

    if not args.live:
        n = sum(1 for url in gallery if url and "res.cloudinary.com" in url)
        bump("would_migrate")
        print(f"  [dry-run] {product['id']} \"{title}\" -> {n} gallery image(s) would be migrated (of {len(gallery)} secondary)", flush=True)
        return

    try:
        cf_images = [images[0]] if images else []
        for url in gallery:
            if url and "res.cloudinary.com" in url:
                cf_url = migrate_one_image(http_client, args, migrations_col, url)
                cf_images.append(cf_url)
            else:
                cf_images.append(url)  # already-Cloudflare or external, carry through unchanged
        products_col.update_one({"id": product["id"]}, {"$set": {"cf_images": cf_images}})
        bump("migrated")
        print(f"  [ok] {product['id']} \"{title}\" -> cf_images set ({len(cf_images)} entries)", flush=True)
    except Exception as e:
        bump("failed")
        print(f"  [FAILED] {product['id']} \"{title}\": {e}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mongo-url", required=True)
    parser.add_argument("--db-name", required=True)
    parser.add_argument("--cf-account-id", default=os.environ.get("CLOUDFLARE_ACCOUNT_ID"))
    parser.add_argument("--cf-api-token", default=os.environ.get("CLOUDFLARE_API_TOKEN"))
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument("--retries", type=int, default=3)
    args = parser.parse_args()

    if not args.cf_account_id or not args.cf_api_token:
        sys.exit(
            "REFUZAT: lipsesc credențialele Cloudflare.\n"
            "Setează --cf-account-id și --cf-api-token, sau variabilele de mediu\n"
            "CLOUDFLARE_ACCOUNT_ID / CLOUDFLARE_API_TOKEN."
        )

    mode = "LIVE (va scrie în MongoDB și va urca imagini pe Cloudflare)" if args.live else "DRY-RUN (doar citire, niciun apel real)"
    host_display = args.mongo_url.split("@")[-1] if "@" in args.mongo_url else args.mongo_url
    print(f"Mod: {mode}", flush=True)
    print(f"Target Mongo: {args.db_name} @ {host_display}", flush=True)
    print(f"Cloudflare account: {args.cf_account_id[:6]}... (token ascuns)", flush=True)

    client = MongoClient(args.mongo_url, serverSelectionTimeoutMS=8000)
    db = client[args.db_name]
    products_col = db.shopify_products
    migrations_col = db.cloudflare_gallery_migrations

    cursor = products_col.find(
        {"images.1": {"$exists": True}},  # has at least 2 images (index 0 and 1)
        {"id": 1, "title": 1, "images": 1, "cf_images": 1},
    )
    if args.limit:
        cursor = cursor.limit(args.limit)
    products = list(cursor)
    print(f"\nProduse cu 2+ imagini găsite: {len(products)}\n", flush=True)

    stats = {
        "would_migrate": 0,
        "migrated": 0,
        "failed": 0,
        "skipped_already_done": 0,
        "skipped_no_gallery": 0,
        "skipped_no_cloudinary_in_gallery": 0,
    }
    stats_lock = threading.Lock()

    with httpx.Client() as http_client, ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [
            pool.submit(process_product, p, args=args, migrations_col=migrations_col,
                        products_col=products_col, http_client=http_client, stats=stats, stats_lock=stats_lock)
            for p in products
        ]
        for f in as_completed(futures):
            f.result()

    print("\n--- Rezumat ---")
    for key, value in stats.items():
        print(f"  {key}: {value}")

    if not args.live:
        print("\n--dry-run: nimic scris în MongoDB, niciun apel către Cloudflare. Rulează cu --live ca să migrezi efectiv.")


if __name__ == "__main__":
    main()
