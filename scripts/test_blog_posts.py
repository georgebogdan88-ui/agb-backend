"""
Focused, standalone check for the new webshop blog-section feature:

  - GET /blog/posts        (get_blog_posts)        in server.py
  - GET /blog/posts/{handle} (get_blog_post_by_handle) in server.py
  - map_article_to_blog_post (the field-mapping logic used by
    scripts/migrate_blog_from_shopify.py), tested in isolation as a pure
    function - no Shopify/Mongo I/O involved.

Not a pytest suite - mirrors scripts/test_analytics.py's and
scripts/test_gdpr_export_and_delete.py's approach (this repo still has no
test framework, tests/ dir, or pytest in requirements.txt). Uses
mongomock + mongomock-motor to swap in an in-memory Mongo double for
server.client/db, and calls the real endpoint functions directly (no HTTP
layer/TestClient - public endpoints, so no auth stub needed).

Covers:
  (a) GET /blog/posts: pagination (limit/offset) and total count against a
      seeded set of posts with distinct published_at values.
  (b) GET /blog/posts: sort order is published_at descending (newest
      first), and the max-50 clamp/min-1 clamp on `limit`.
  (c) GET /blog/posts: list items omit content_html/excerpt_html/blog_title
      (kept small) but include id/handle/title/excerpt/image_url/
      published_at/tags, and never leak Mongo's `_id`.
  (d) GET /blog/posts/{handle}: success returns the FULL doc shape
      (including content_html/excerpt_html/blog_title), with no `_id`.
  (e) GET /blog/posts/{handle}: unknown handle -> 404 with a Romanian
      message.
  (f) map_article_to_blog_post: maps every Shopify Storefront field to the
      correct db.blog_posts field name (contentHtml -> content_html,
      excerptHtml -> excerpt_html, publishedAt -> published_at,
      image.url -> image_url, blog.title -> blog_title, handle used for
      BOTH id and handle), including the missing-image / missing-blog /
      missing-tags edge cases.

Run (from repo root): python scripts/test_blog_posts.py
"""
import asyncio
import os
import sys
from pathlib import Path

os.environ.setdefault("MONGO_URL", "mongodb://fake-for-import-only/")
os.environ.setdefault("DB_NAME", "fake_db_for_import_only")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mongomock_motor  # noqa: E402
import server  # noqa: E402
from scripts.migrate_blog_from_shopify import map_article_to_blog_post  # noqa: E402

PASS = []
FAIL = []


def check(name, condition, detail=""):
    if condition:
        PASS.append(name)
        print(f"PASS: {name}")
    else:
        FAIL.append(name)
        print(f"FAIL: {name}  {detail}")


async def fresh_db():
    mock_client = mongomock_motor.AsyncMongoMockClient()
    mock_db = mock_client["test_db"]
    server.client = mock_client
    server.db = mock_db
    return mock_db


def sample_post(handle, published_at, title=None):
    return {
        "_id": handle,
        "id": handle,
        "handle": handle,
        "title": title or f"Articol {handle}",
        "excerpt": f"Rezumat pentru {handle}",
        "excerpt_html": f"<p>Rezumat pentru {handle}</p>",
        "content_html": f"<p>Continut complet pentru {handle}</p>",
        "image_url": f"https://cdn.shopify.com/{handle}.jpg",
        "published_at": published_at,
        "tags": ["tractor", "intretinere"],
        "blog_title": "News",
    }


async def scenario_a_pagination_and_total():
    """(a) limit/offset paginate correctly, total reflects the FULL count
    regardless of pagination window."""
    db = await fresh_db()
    for i in range(5):
        await db.blog_posts.insert_one(sample_post(f"post-{i}", f"2026-01-0{i + 1}T10:00:00Z"))

    page1 = await server.get_blog_posts(limit=2, offset=0)
    check("a) page1 total is full count (5)", page1["total"] == 5, page1["total"])
    check("a) page1 returns exactly 2 posts", len(page1["posts"]) == 2, page1["posts"])

    page2 = await server.get_blog_posts(limit=2, offset=2)
    check("a) page2 total still 5", page2["total"] == 5, page2["total"])
    check("a) page2 returns exactly 2 posts", len(page2["posts"]) == 2, page2["posts"])
    check("a) page1 and page2 don't overlap",
          {p["handle"] for p in page1["posts"]}.isdisjoint({p["handle"] for p in page2["posts"]}),
          (page1["posts"], page2["posts"]))

    page3 = await server.get_blog_posts(limit=2, offset=4)
    check("a) last page returns the 1 remaining post", len(page3["posts"]) == 1, page3["posts"])


async def scenario_b_sort_order_and_limit_clamp():
    """(b) sorted newest-first by published_at, and limit is clamped to
    [1, 50] regardless of what's passed in."""
    db = await fresh_db()
    await db.blog_posts.insert_one(sample_post("oldest", "2025-01-01T00:00:00Z"))
    await db.blog_posts.insert_one(sample_post("newest", "2026-06-01T00:00:00Z"))
    await db.blog_posts.insert_one(sample_post("middle", "2025-12-01T00:00:00Z"))

    result = await server.get_blog_posts(limit=20, offset=0)
    handles_in_order = [p["handle"] for p in result["posts"]]
    check("b) sorted newest-first by published_at",
          handles_in_order == ["newest", "middle", "oldest"], handles_in_order)

    over_limit = await server.get_blog_posts(limit=999, offset=0)
    check("b) limit clamped to max 50 (only 3 posts exist, all returned)", len(over_limit["posts"]) == 3, over_limit)

    zero_limit = await server.get_blog_posts(limit=0, offset=0)
    check("b) limit=0 clamped up to at least 1 post returned", len(zero_limit["posts"]) == 1, zero_limit)


async def scenario_c_list_item_shape():
    """(c) list items are the slim shape only (no content_html/
    excerpt_html/blog_title, no _id), with the expected fields present."""
    db = await fresh_db()
    await db.blog_posts.insert_one(sample_post("shape-check", "2026-01-01T00:00:00Z"))

    result = await server.get_blog_posts(limit=20, offset=0)
    post = result["posts"][0]
    check("c) list item has exactly the slim field set",
          set(post.keys()) == {"id", "handle", "title", "excerpt", "image_url", "published_at", "tags"},
          post.keys())
    check("c) list item id == handle", post["id"] == post["handle"] == "shape-check", post)
    check("c) list item has no _id leak", "_id" not in post, post)
    check("c) list item has no content_html", "content_html" not in post, post)
    check("c) list item has no excerpt_html", "excerpt_html" not in post, post)
    check("c) list item has no blog_title", "blog_title" not in post, post)


async def scenario_d_get_by_handle_full_shape():
    """(d) GET /blog/posts/{handle} returns the FULL doc, no _id."""
    db = await fresh_db()
    await db.blog_posts.insert_one(sample_post("full-article", "2026-01-01T00:00:00Z", title="Cum alegi ambreiajul"))

    post = await server.get_blog_post_by_handle("full-article")
    check("d) full doc has no _id leak", "_id" not in post, post)
    check("d) full doc has content_html", post.get("content_html") == "<p>Continut complet pentru full-article</p>", post)
    check("d) full doc has excerpt_html", post.get("excerpt_html") == "<p>Rezumat pentru full-article</p>", post)
    check("d) full doc has blog_title", post.get("blog_title") == "News", post)
    check("d) full doc title correct", post.get("title") == "Cum alegi ambreiajul", post)
    check("d) full doc id == handle", post.get("id") == post.get("handle") == "full-article", post)


async def scenario_e_get_by_handle_404():
    """(e) unknown handle -> 404 with a Romanian message."""
    await fresh_db()
    try:
        await server.get_blog_post_by_handle("does-not-exist")
        check("e) unknown handle rejected", False, "no exception raised")
    except server.HTTPException as e:
        check("e) unknown handle -> 404", e.status_code == 404, e.status_code)
        check("e) 404 detail is a Romanian not-found message",
              "nu a fost g" in e.detail.lower(), e.detail)


def scenario_f_field_mapping():
    """(f) map_article_to_blog_post maps every Shopify Storefront field
    correctly, including edge cases (missing image/blog/tags)."""
    node = {
        "id": "gid://shopify/Article/123",
        "title": "Cum alegi ambreiajul pentru tractor",
        "handle": "cum-alegi-ambreiaj-pentru-tractor",
        "publishedAt": "2026-05-01T12:00:00Z",
        "excerpt": "Un scurt rezumat",
        "excerptHtml": "<p>Un scurt rezumat</p>",
        "content": "Continut brut",
        "contentHtml": "<p>Continut complet HTML</p>",
        "tags": ["tractor", "AMBREIAJ "],
        "image": {"url": "https://cdn.shopify.com/example.jpg"},
        "blog": {"title": "News"},
    }
    doc = map_article_to_blog_post(node)
    check("f) handle used for both id and handle",
          doc["id"] == doc["handle"] == "cum-alegi-ambreiaj-pentru-tractor", doc)
    check("f) title mapped as-is", doc["title"] == "Cum alegi ambreiajul pentru tractor", doc)
    check("f) excerpt mapped as-is", doc["excerpt"] == "Un scurt rezumat", doc)
    check("f) excerptHtml -> excerpt_html", doc["excerpt_html"] == "<p>Un scurt rezumat</p>", doc)
    check("f) contentHtml -> content_html", doc["content_html"] == "<p>Continut complet HTML</p>", doc)
    check("f) image.url -> image_url", doc["image_url"] == "https://cdn.shopify.com/example.jpg", doc)
    check("f) publishedAt -> published_at", doc["published_at"] == "2026-05-01T12:00:00Z", doc)
    check("f) tags carried through unchanged (not normalized/lowercased)",
          doc["tags"] == ["tractor", "AMBREIAJ "], doc)
    check("f) blog.title -> blog_title", doc["blog_title"] == "News", doc)

    # Edge cases: missing image / missing blog / missing tags must not raise.
    node_missing = {
        "handle": "no-extras",
        "title": "Fara imagine",
        "publishedAt": "2026-05-02T00:00:00Z",
        "excerpt": "",
        "excerptHtml": "",
        "contentHtml": "",
    }
    doc_missing = map_article_to_blog_post(node_missing)
    check("f) missing image -> image_url is None", doc_missing["image_url"] is None, doc_missing)
    check("f) missing blog -> blog_title defaults to empty string", doc_missing["blog_title"] == "", doc_missing)
    check("f) missing tags -> tags defaults to empty list", doc_missing["tags"] == [], doc_missing)
    check("f) missing-extras handle still maps to id/handle", doc_missing["id"] == doc_missing["handle"] == "no-extras", doc_missing)


async def main():
    await scenario_a_pagination_and_total()
    await scenario_b_sort_order_and_limit_clamp()
    await scenario_c_list_item_shape()
    await scenario_d_get_by_handle_full_shape()
    await scenario_e_get_by_handle_404()
    scenario_f_field_mapping()

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
