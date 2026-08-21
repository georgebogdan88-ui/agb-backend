"""
Two bugs reported by George (2026-08-21), both confirmed and fixed in the
same pass since they surfaced together while testing the same product:

1. Editing a product's title silently regenerated its `handle`
   (_apply_product_update, server.py) - breaking the product's public
   /produse/{handle} URL (any bookmark/shared link/indexed page) on every
   title edit. Defeats the entire point of today's id->handle migration
   (stable URLs). Fix: handle is now only ever backfilled when genuinely
   missing, never overwritten on an existing product.

2. GET /products?search=... ranked in-stock results ABOVE supplier-stock
   ones only WITHIN whatever single page .find().skip().limit() happened to
   return - relevance was computed in Python AFTER pagination, so it could
   never promote a match across a page boundary. Fix: relevance is now
   computed via aggregation ($addFields + $sort) BEFORE $skip/$limit.

Not a pytest suite - mirrors scripts/test_product_seo_fields.py's approach
(mongomock + mongomock-motor, no auth stub needed since both functions
under test here don't require admin).

Run (from repo root): python scripts/test_product_handle_stability_and_search_relevance.py
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


def base_product(**overrides):
    doc = {
        "handle": "placeholder-handle", "description": "",
        "price": 100.0, "currency": "RON", "images": [], "tags": [],
        "compatible_models": [], "collections": [], "complementary_product_ids": [],
        "equivalent_product_ids": [], "is_featured": False, "stock": 0,
    }
    doc.update(overrides)
    return doc


async def scenario_a_handle_survives_title_edit():
    db = await fresh_db()
    await db.shopify_products.insert_one(base_product(
        id="p1", title="Cupla rapida veche", handle="cupla-rapida-veche",
        title_normalized="cupla rapida veche",
    ))
    updated = await server._apply_product_update(
        "p1", server.ProductUpdate(title="Cupla rapida noua cu mai multe coduri OEM"),
    )
    check("a) handle unchanged after a title edit", updated.get("handle") == "cupla-rapida-veche", updated.get("handle"))
    check("a) title_normalized still updates", "noua" in updated.get("title_normalized", ""), updated.get("title_normalized"))
    check("a) title itself still updates", updated.get("title") == "Cupla rapida noua cu mai multe coduri OEM", updated.get("title"))

    persisted = await db.shopify_products.find_one({"id": "p1"})
    check("a) change is actually persisted to the DB, not just returned", persisted.get("handle") == "cupla-rapida-veche")


async def scenario_a2_handle_backfilled_only_when_genuinely_missing():
    db = await fresh_db()
    await db.shopify_products.insert_one(base_product(
        id="p2", title="Produs fara handle", handle=None, title_normalized="produs fara handle",
    ))
    updated = await server._apply_product_update("p2", server.ProductUpdate(title="Produs cu handle acum"))
    check("a2) handle backfilled from title when it was genuinely missing",
          updated.get("handle") == "produs-cu-handle-acum", updated.get("handle"))


async def scenario_a3_no_title_change_leaves_handle_alone():
    db = await fresh_db()
    await db.shopify_products.insert_one(base_product(
        id="p3", title="Produs neschimbat", handle="produs-neschimbat", title_normalized="produs neschimbat",
    ))
    updated = await server._apply_product_update("p3", server.ProductUpdate(price=250.0))
    check("a3) editing an unrelated field (price) never touches handle",
          updated.get("handle") == "produs-neschimbat", updated.get("handle"))


async def scenario_b_search_relevance_promotes_in_stock_across_the_full_result_set():
    db = await fresh_db()
    # 20 supplier_stock, non-matching-title "filler" docs that would occupy
    # an entire find().skip(0).limit(5) page on their own under the OLD
    # (sort-after-paginate) behavior, deliberately inserted BEFORE the real
    # in-stock match so natural/insertion order puts them first.
    for i in range(20):
        await db.shopify_products.insert_one(base_product(
            id=f"filler{i}", title=f"Piesa oarecare {i}", title_normalized=f"piesa oarecare {i}",
            stock=0, stock_status="supplier_stock",
        ))
    await db.shopify_products.insert_one(base_product(
        id="target", title="Cupla rapida distribuitor SCV John Deere FASTER",
        title_normalized="cupla rapida distribuitor scv john deere faster",
        stock=4, stock_status="in_stock",
    ))
    await db.shopify_products.insert_one(base_product(
        id="nomatch-instock", title="Alt produs oarecare in stoc",
        title_normalized="alt produs oarecare in stoc", stock=9, stock_status="in_stock",
    ))

    from fastapi.testclient import TestClient
    with TestClient(server.app) as client:
        r = client.get("/api/products", params={"search": "cupla rapida", "limit": 5})
    check("b) request succeeds", r.status_code == 200, r.text)
    results = r.json()
    ids = [p["id"] for p in results]
    check("b) the in-stock title-matching product is on page 1 (limit=5), not buried past 20 fillers",
          "target" in ids, ids)
    check("b) it's ranked first (title match + in-stock beats the non-matching in-stock product)",
          ids[0] == "target" if ids else False, ids)


async def main():
    await scenario_a_handle_survives_title_edit()
    await scenario_a2_handle_backfilled_only_when_genuinely_missing()
    await scenario_a3_no_title_change_leaves_handle_alone()
    await scenario_b_search_relevance_promotes_in_stock_across_the_full_result_set()

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
