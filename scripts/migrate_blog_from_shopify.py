"""One-off migration: import Shopify blog articles into `db.blog_posts`.

Context: the webshop's new blog section (GET /api/blog/posts, GET
/api/blog/posts/{handle} in server.py) reads from a local Mongo collection
instead of hitting Shopify's Storefront GraphQL API on every request. This
script does the one-time (re-runnable) import that populates that
collection.

It reuses the EXACT SAME proven Storefront GraphQL query/auth pattern
already used by GET /news in server.py (`blogs -> articles`, same field
selection: id/title/handle/publishedAt/excerpt/excerptHtml/content/
contentHtml/tags/image.url/blog.title, same
X-Shopify-Storefront-Access-Token header) - the only difference is widening
`articles(first: 10, ...)` to `articles(first: 250, ...)` so every article
is fetched, not just the latest 10 GET /news shows for the notification
feed. Confirmed separately that this store currently has 30 articles across
1 blog ("news") - well under the 250 cap.

For each article, this upserts into `db.blog_posts` KEYED BY HANDLE - the
document's `_id` AND its `id`/`handle` fields are all set to the article's
Shopify handle (e.g. "cum-alegi-ambreiaj-pentru-tractor"), which is the
shared contract with the two new read endpoints. Using the handle as `_id`
(rather than an ObjectId) is what makes the upsert naturally idempotent:
re-running this script always overwrites each post with fresh data instead
of erroring or duplicating.

Field mapping (Shopify Storefront node -> db.blog_posts document):
    handle                  -> id AND handle (same value, see above)
    title                   -> title
    excerpt                 -> excerpt
    excerptHtml             -> excerpt_html
    contentHtml             -> content_html
    image.url               -> image_url
    publishedAt             -> published_at
    tags                    -> tags
    blog.title              -> blog_title

This does NOT touch GET /news or check_for_new_blog_posts (the
push-notification blog checker) in server.py - both keep reading LIVE from
Shopify, unchanged. Repointing them at db.blog_posts is a separate,
deliberate future decision, out of scope here.

SAFETY (same model as scripts/migrate_to_cloudflare_images.py):
  - Requires --mongo-url and --db-name explicitly on every run - never reads
    this repo's own .env, which points at the real shared MongoDB cluster
    used by both local dev and production.
  - Shopify Storefront credentials default from this repo's own
    SHOPIFY_STORE / SHOPIFY_STOREFRONT_TOKEN / SHOPIFY_API_VERSION env vars
    (the exact same ones GET /news already reads in server.py) via
    python-dotenv's load_dotenv(), but can be overridden explicitly with
    --shopify-store / --shopify-storefront-token / --shopify-api-version.
    Validated (non-empty) BEFORE any MongoDB connection is opened, so a run
    with a missing token fails fast with a clear message instead of
    connecting to Mongo first and failing mid-migration.
  - --dry-run is the DEFAULT. Pass --live explicitly to write to MongoDB.
    In dry-run mode the script still calls Shopify (read-only - it needs
    real article data to report accurately what it WOULD write) but opens
    NO MongoDB connection at all and performs no writes.
  - Idempotent: upsert by handle (see above) - safe to re-run any time,
    including after interruption; always ends up with fresh data, never
    duplicates.
  - Never logs the Storefront access token or the full Mongo connection
    string (only the host portion, same as the other migration scripts).

Usage (dry run - default, read-only, safe to run anytime):
    python scripts/migrate_blog_from_shopify.py \
        --mongo-url "mongodb+srv://user:pass@cluster0.../agb_agroparts" \
        --db-name agb_agroparts

Usage (live - actually writes to MongoDB):
    python scripts/migrate_blog_from_shopify.py \
        --mongo-url "mongodb+srv://..." \
        --db-name agb_agroparts \
        --live

Test on a handful of articles first with --limit:
    python scripts/migrate_blog_from_shopify.py ... --live --limit 3
"""
import argparse
import os
import sys
import time
from pathlib import Path

# Windows consoles often default stdout/stderr to cp1252, which can't encode
# diacritics in this script's Romanian messages - force UTF-8 explicitly,
# same fix as the other migration scripts in this directory.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx
from dotenv import load_dotenv
from pymongo import MongoClient

# Picks up SHOPIFY_STORE / SHOPIFY_STOREFRONT_TOKEN / SHOPIFY_API_VERSION
# from agb-backend/.env if present (same vars server.py's GET /news reads).
# --mongo-url / --db-name stay required CLI args regardless - see SAFETY
# above - this only affects the Shopify credential fallback.
load_dotenv()

# Same query GET /news uses in server.py, widened from
# `articles(first: 10, ...)` to `articles(first: 250, ...)` so every
# article is fetched, not just the latest batch.
BLOG_ARTICLES_QUERY = """
{
    blogs(first: 5) {
        edges {
            node {
                title
                handle
                articles(first: 250, sortKey: PUBLISHED_AT, reverse: true) {
                    edges {
                        node {
                            id
                            title
                            handle
                            publishedAt
                            excerpt
                            excerptHtml
                            content
                            contentHtml
                            tags
                            image {
                                url
                            }
                            blog {
                                title
                            }
                        }
                    }
                }
            }
        }
    }
}
"""


def map_article_to_blog_post(node: dict) -> dict:
    """Map a single Shopify Storefront `articles.edges[].node` dict (see
    BLOG_ARTICLES_QUERY above / GET /news in server.py) to the db.blog_posts
    document shape from the shared contract. Pure function, no I/O - kept
    separate so it can be unit tested directly (see
    scripts/test_blog_posts.py) without hitting Shopify or Mongo.
    """
    handle = node.get("handle", "")
    image = node.get("image") or {}
    blog = node.get("blog") or {}
    tags = node.get("tags") or []
    return {
        "id": handle,
        "handle": handle,
        "title": node.get("title", ""),
        "excerpt": node.get("excerpt", ""),
        "excerpt_html": node.get("excerptHtml", ""),
        "content_html": node.get("contentHtml", ""),
        "image_url": image.get("url"),
        "published_at": node.get("publishedAt", ""),
        "tags": tags,
        "blog_title": blog.get("title", ""),
    }


def extract_article_nodes(graphql_response: dict) -> list:
    """Flatten the `blogs -> articles` edges/node nesting of a Storefront
    GraphQL response into a plain list of article node dicts, across every
    blog returned (same traversal GET /news does in server.py)."""
    nodes = []
    blogs = graphql_response.get("data", {}).get("blogs", {}).get("edges", [])
    for blog_edge in blogs:
        article_edges = blog_edge.get("node", {}).get("articles", {}).get("edges", [])
        for article_edge in article_edges:
            node = article_edge.get("node")
            if node:
                nodes.append(node)
    return nodes


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
                print(f"  retry {attempt}/{retries - 1} for {what} after error: {e} (waiting {wait:.1f}s)", flush=True)
                time.sleep(wait)
    raise last_exc


def fetch_all_articles(client: httpx.Client, *, store: str, token: str, api_version: str) -> list:
    """Fetch every blog article from Shopify's Storefront GraphQL API using
    BLOG_ARTICLES_QUERY, returning the flat list of raw article node dicts
    (not yet mapped to the db.blog_posts shape)."""
    headers = {
        "Content-Type": "application/json",
        "X-Shopify-Storefront-Access-Token": token,
    }
    response = client.post(
        f"https://{store}/api/{api_version}/graphql.json",
        json={"query": BLOG_ARTICLES_QUERY},
        headers=headers,
        timeout=30.0,
    )
    response.raise_for_status()
    data = response.json()
    if data.get("errors"):
        raise RuntimeError(f"Shopify Storefront API a returnat erori: {data['errors']}")
    return extract_article_nodes(data)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mongo-url", required=True, help="MongoDB connection string - never omitted, never defaulted from .env")
    parser.add_argument("--db-name", required=True, help="Database name")
    parser.add_argument("--shopify-store", default=os.environ.get("SHOPIFY_STORE"),
                         help="Shopify store domain (or set SHOPIFY_STORE env var - same one server.py uses)")
    parser.add_argument("--shopify-storefront-token", default=os.environ.get("SHOPIFY_STOREFRONT_TOKEN"),
                         help="Shopify Storefront API access token (or set SHOPIFY_STOREFRONT_TOKEN env var) - never logged")
    parser.add_argument("--shopify-api-version", default=os.environ.get("SHOPIFY_API_VERSION", "2024-01"),
                         help="Shopify Storefront API version (or set SHOPIFY_API_VERSION env var, default 2024-01)")
    parser.add_argument("--live", action="store_true",
                         help="Actually write to MongoDB. Without this flag, the script only reports what it would do.")
    parser.add_argument("--limit", type=int, default=None, help="Only process the first N articles fetched (useful for a test run)")
    parser.add_argument("--retries", type=int, default=3, help="Max attempts for the Shopify request before giving up (default 3)")
    args = parser.parse_args()

    # Validated before ANY network/DB connection is opened, so a run with
    # missing credentials fails fast and clearly.
    if not args.shopify_store or not args.shopify_storefront_token:
        sys.exit(
            "REFUZAT: lipsesc credențialele Shopify Storefront.\n"
            "Setează --shopify-store și --shopify-storefront-token, sau variabilele de\n"
            "mediu SHOPIFY_STORE / SHOPIFY_STOREFRONT_TOKEN (același .env pe care GET\n"
            "/news din server.py îl folosește deja), înainte de a rula scriptul."
        )

    mode = "LIVE (va scrie în MongoDB)" if args.live else "DRY-RUN (doar citire de la Shopify, nimic scris)"
    host_display = args.mongo_url.split("@")[-1] if "@" in args.mongo_url else args.mongo_url
    print(f"Mod: {mode}", flush=True)
    print(f"Target Mongo: {args.db_name} @ {host_display}", flush=True)
    print(f"Shopify store: {args.shopify_store} (api version {args.shopify_api_version}, token ascuns)", flush=True)
    if args.limit:
        print(f"Limit: primele {args.limit} articole", flush=True)

    with httpx.Client() as http_client:
        nodes = retry_with_backoff(
            lambda: fetch_all_articles(
                http_client,
                store=args.shopify_store,
                token=args.shopify_storefront_token,
                api_version=args.shopify_api_version,
            ),
            retries=args.retries, what="fetch blog articles",
        )

    print(f"\nArticole găsite pe Shopify: {len(nodes)}\n", flush=True)

    if args.limit:
        nodes = nodes[: args.limit]

    docs = [map_article_to_blog_post(node) for node in nodes]

    stats = {"would_upsert": 0, "upserted": 0, "failed": 0}

    posts_col = None
    if args.live:
        # Fails fast with a clear error on an unreachable/invalid host
        # instead of pymongo's default 30s server-selection hang.
        client = MongoClient(args.mongo_url, serverSelectionTimeoutMS=8000)
        db = client[args.db_name]
        posts_col = db.blog_posts

    for doc in docs:
        handle = doc["handle"]
        title = (doc.get("title") or "")[:60]
        if not handle:
            stats["failed"] += 1
            print(f"  [FAILED] articol fără handle, sărit: \"{title}\"", flush=True)
            continue

        if not args.live:
            stats["would_upsert"] += 1
            print(f"  [dry-run] {handle} -> would upsert \"{title}\"", flush=True)
            continue

        try:
            # Keyed by handle: `_id` (via the filter) AND the `id`/`handle`
            # fields inside the document are all the same value, per the
            # shared contract - re-running always overwrites with fresh
            # data instead of erroring or duplicating.
            posts_col.update_one({"_id": handle}, {"$set": doc}, upsert=True)
            stats["upserted"] += 1
            print(f"  [ok] {handle} -> upserted \"{title}\"", flush=True)
        except Exception as e:
            stats["failed"] += 1
            print(f"  [FAILED] {handle} \"{title}\": {e}", flush=True)

    print("\n--- Rezumat ---")
    for key, value in stats.items():
        print(f"  {key}: {value}")

    if not args.live:
        print("\n--dry-run: nimic scris în MongoDB. Rulează cu --live ca să migrezi efectiv.")


if __name__ == "__main__":
    main()
