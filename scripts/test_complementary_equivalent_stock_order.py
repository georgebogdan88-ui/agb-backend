"""
Focused, standalone check for the stock-tier + price ordering applied to
GET /products/{id}/complementary and GET /products/{id}/equivalents
(George, 2026-08-22 - "Posibil să ai nevoie și de" / "Echivalente" sections
on the product page were showing admin-curated links in raw insertion
order, no relation to availability - same complaint/fix shape as the
GET /products search ordering fixed earlier the same day, see
scripts/test_product_handle_stability_and_search_relevance.py's scenario b2
and build_products_query's own $sort stage for the full rationale).

Not a pytest suite - mirrors scripts/test_product_equivalent_matching.py's
approach (mongomock + mongomock-motor, no auth needed - both endpoints
under test are public/unauthenticated).

Run (from repo root): python scripts/test_complementary_equivalent_stock_order.py
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
        "handle": "placeholder", "description": "", "currency": "RON",
        "images": [], "tags": [], "compatible_models": [], "collections": [],
        "complementary_product_ids": [], "equivalent_product_ids": [],
        "is_featured": False,
    }
    doc.update(overrides)
    return doc


async def scenario_1_complementary_ordered_by_stock_tier_then_price():
    db = await fresh_db()
    # Insertion order deliberately worst-case: cheap in-stock item LAST,
    # so this only passes if the endpoint actually sorts, not just returns
    # in the stored complementary_product_ids order (which is what George
    # observed and reported - a "Cere ofertă" supplier item shown before an
    # actually-in-stock one).
    await db.shopify_products.insert_many([
        base_product(id="main", title="Produs principal",
                     complementary_product_ids=["supplier-a", "oos-b", "instock-cheap", "instock-expensive"]),
        base_product(id="supplier-a", title="Piesa A", price=500.0, stock_status="supplier_stock"),
        base_product(id="oos-b", title="Piesa B", price=300.0, stock_status="out_of_stock"),
        base_product(id="instock-cheap", title="Piesa C", price=50.0, stock_status="in_stock"),
        base_product(id="instock-expensive", title="Piesa D", price=200.0, stock_status="in_stock"),
    ])
    result = await server.get_complementary_products("main")
    ids = [p["id"] for p in result["complementary"]]
    check(
        "1) complementary: in_stock (price desc) -> out_of_stock -> supplier_stock",
        ids == ["instock-expensive", "instock-cheap", "oos-b", "supplier-a"],
        ids,
    )


async def scenario_2_equivalents_ordered_by_stock_tier_then_price():
    db = await fresh_db()
    await db.shopify_products.insert_many([
        base_product(id="main", title="Produs principal",
                     equivalent_product_ids=["supplier-a", "instock-cheap", "instock-expensive-oem", "oos-b"]),
        base_product(id="supplier-a", title="Piesa A Vapormatic", vendor="Vapormatic", price=150.0, stock_status="supplier_stock"),
        base_product(id="instock-cheap", title="Piesa A FP Diesel", vendor="FP Diesel", price=80.0, stock_status="in_stock"),
        base_product(id="instock-expensive-oem", title="Piesa A John Deere", vendor="John Deere", price=220.0, stock_status="in_stock"),
        base_product(id="oos-b", title="Piesa A Elring", vendor="Elring", price=100.0, stock_status="out_of_stock"),
    ])
    result = await server.get_equivalent_products("main")
    ids = [p["id"] for p in result["equivalents"]]
    check(
        "2) equivalents: in_stock (price desc, OEM surfaces first) -> out_of_stock -> supplier_stock",
        ids == ["instock-expensive-oem", "instock-cheap", "oos-b", "supplier-a"],
        ids,
    )


async def scenario_3_missing_stock_status_sorts_last_but_does_not_crash():
    db = await fresh_db()
    await db.shopify_products.insert_many([
        base_product(id="main", title="Produs principal",
                     complementary_product_ids=["no-status", "instock"]),
        base_product(id="no-status", title="Piesa fara status", price=1000.0),  # no stock_status field at all
        base_product(id="instock", title="Piesa in stoc", price=10.0, stock_status="in_stock"),
    ])
    result = await server.get_complementary_products("main")
    ids = [p["id"] for p in result["complementary"]]
    check("3) missing stock_status doesn't crash and sorts after known tiers",
          ids == ["instock", "no-status"], ids)


async def main():
    await scenario_1_complementary_ordered_by_stock_tier_then_price()
    await scenario_2_equivalents_ordered_by_stock_tier_then_price()
    await scenario_3_missing_stock_status_sorts_last_but_does_not_crash()

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
