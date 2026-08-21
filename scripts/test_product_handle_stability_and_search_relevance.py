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


async def scenario_b_in_stock_promoted_across_the_full_result_set():
    db = await fresh_db()
    # 20 supplier_stock "filler" docs that would occupy an entire
    # find().skip(0).limit(5) page on their own under the OLD (sort-after-
    # paginate) behavior, deliberately inserted BEFORE the real in-stock
    # match so natural/insertion order puts them first.
    for i in range(20):
        await db.shopify_products.insert_one(base_product(
            id=f"filler{i}", title=f"Cupla rapida oarecare {i}", title_normalized=f"cupla rapida oarecare {i}",
            stock=0, stock_status="supplier_stock", price=50.0,
        ))
    await db.shopify_products.insert_one(base_product(
        id="target", title="Cupla rapida distribuitor SCV John Deere FASTER",
        title_normalized="cupla rapida distribuitor scv john deere faster",
        stock=4, stock_status="in_stock", price=10.0,
    ))

    from fastapi.testclient import TestClient
    with TestClient(server.app) as client:
        r = client.get("/api/products", params={"search": "cupla rapida", "limit": 5})
    check("b) request succeeds", r.status_code == 200, r.text)
    results = r.json()
    ids = [p["id"] for p in results]
    check("b) the in-stock product is on page 1 (limit=5), not buried past 20 supplier_stock fillers",
          "target" in ids, ids)
    check("b) it's ranked first (in_stock tier beats supplier_stock regardless of price)",
          ids[0] == "target" if ids else False, ids)


async def scenario_b2_stock_tier_order_and_price_descending():
    """George's explicit ordering (2026-08-22): within the search-matched
    set, group by stock tier first - in_stock, THEN out_of_stock, THEN
    supplier_stock (deliberately in that order: a supplier-stock order
    needs an extra special-order step, so a flatly unavailable listing is
    still more useful to see first than one that just requires more
    waiting) - and within a tier, higher price first. The price-descending
    choice is deliberate: OEM/John Deere parts tend to cost more than
    aftermarket equivalents (Elring, Vapormatic, etc.) for the same code,
    so this naturally surfaces the OEM listing first on a tie without
    hardcoding any vendor name (the original report: searching
    "6068hz480", an Elring seal outranked the John Deere one purely by
    raw catalog id order under the old text-relevance scheme)."""
    db = await fresh_db()
    await db.shopify_products.insert_one(base_product(
        id="supplier-cheap", title="Piesa test cod", title_normalized="piesa test cod",
        stock=0, stock_status="supplier_stock", price=500.0,
    ))
    await db.shopify_products.insert_one(base_product(
        id="oos-mid", title="Piesa test cod", title_normalized="piesa test cod",
        stock=0, stock_status="out_of_stock", price=200.0,
    ))
    await db.shopify_products.insert_one(base_product(
        id="instock-cheap", title="Piesa test cod", title_normalized="piesa test cod",
        stock=3, stock_status="in_stock", price=80.0,
    ))
    await db.shopify_products.insert_one(base_product(
        id="instock-expensive-oem", title="Piesa test cod John Deere", title_normalized="piesa test cod john deere",
        stock=1, stock_status="in_stock", price=150.0,
    ))

    from fastapi.testclient import TestClient
    with TestClient(server.app) as client:
        r = client.get("/api/products", params={"search": "test cod", "limit": 10})
    check("b2) request succeeds", r.status_code == 200, r.text)
    ids = [p["id"] for p in r.json()]
    check(
        "b2) order is: in_stock (price desc) -> out_of_stock -> supplier_stock, regardless of price outliers",
        ids == ["instock-expensive-oem", "instock-cheap", "oos-mid", "supplier-cheap"],
        ids,
    )


async def scenario_c_sort_has_id_tiebreaker_for_pagination_stability():
    """Regression guard for the "6150m" duplicate-product report (George,
    2026-08-21): many real documents can share the same stock tier + price,
    and Mongo does NOT guarantee a stable relative order for ties across two
    SEPARATE query executions - e.g. the initial SSR page fetch (page.tsx)
    and ProductGridInfinite's later "load more" fetch for the next page are
    two independent aggregate() calls. Without a unique secondary sort key,
    the same document can resolve into both pages and render twice (the
    frontend appends pages without deduping by id).

    mongomock's in-memory $sort is itself deterministic/stable across calls
    (Python's sort is stable; there's no real storage-engine nondeterminism
    to reproduce here), so it can't actually surface live cross-page
    duplication the way real MongoDB did - this instead asserts directly on
    the pipeline the code builds, so a future regression (someone removing
    the tiebreaker) still gets caught even though mongomock alone
    wouldn't show any duplicates either way."""
    db = await fresh_db()
    await db.shopify_products.insert_one(base_product(
        id="p1", title="Cupla rapida test", title_normalized="cupla rapida test",
    ))

    captured_pipelines = []
    collection_cls = type(db.shopify_products)
    original_aggregate = collection_cls.aggregate

    def capturing_aggregate(self, pipeline, *args, **kwargs):
        captured_pipelines.append(pipeline)
        return original_aggregate(self, pipeline, *args, **kwargs)

    collection_cls.aggregate = capturing_aggregate
    try:
        from fastapi.testclient import TestClient
        with TestClient(server.app) as client:
            r = client.get("/api/products", params={"search": "cupla rapida", "limit": 5})
        check("c) request succeeds", r.status_code == 200, r.text)
        check("c) exactly one aggregate() call made", len(captured_pipelines) == 1, captured_pipelines)
        if captured_pipelines:
            sort_stages = [s["$sort"] for s in captured_pipelines[0] if "$sort" in s]
            check("c) pipeline has a $sort stage", len(sort_stages) == 1, captured_pipelines[0])
            if sort_stages:
                check("c) $sort orders by _stock_tier first", sort_stages[0].get("_stock_tier") == 1, sort_stages[0])
                check("c) $sort orders by price descending second", sort_stages[0].get("price") == -1, sort_stages[0])
                check(
                    "c) $sort has a unique `id` tiebreaker (pagination stability across separate page fetches)",
                    sort_stages[0].get("id") == 1, sort_stages[0],
                )
    finally:
        collection_cls.aggregate = original_aggregate


async def main():
    await scenario_a_handle_survives_title_edit()
    await scenario_a2_handle_backfilled_only_when_genuinely_missing()
    await scenario_a3_no_title_change_leaves_handle_alone()
    await scenario_b_in_stock_promoted_across_the_full_result_set()
    await scenario_b2_stock_tier_order_and_price_descending()
    await scenario_c_sort_has_id_tiebreaker_for_pagination_stability()

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
