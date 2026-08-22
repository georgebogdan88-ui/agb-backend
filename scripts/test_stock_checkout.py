"""
Focused, standalone check for the checkout stock check-and-decrement fix
(create_order / _reserve_stock_for_order in server.py).

Not a pytest suite - this repo has no existing test framework, test files,
or DB-fixture pattern for this endpoint (confirmed: no tests/ dir, no
pytest in requirements.txt, no prior order/checkout tests to follow). It
lives in scripts/ alongside the repo's other one-off standalone scripts
(backfill_product_timestamps.py, import_orders_csv.py, ...) since that's
the closest thing this repo has to an existing convention for this kind of
script. Per instructions, this does NOT invent a large new test
infrastructure - it's a single script using mongomock + mongomock-motor to
swap in an in-memory Mongo double for server.client/db, then calls the
real _reserve_stock_for_order()/_decrement_stock_once() functions from
server.py directly (no HTTP layer, no FastAPI TestClient - also not
present in requirements.txt).

mongomock / mongomock-motor are NOT added to requirements.txt (they're
dev/test-only and this repo has no dev-requirements split) - they happened
to already be installed in the environment this was written in. To run
this elsewhere: pip install mongomock mongomock-motor

IMPORTANT CAVEAT (see final report to George): mongomock-motor does not
implement Mongo sessions/transactions at all (start_session()/with_
transaction() raise NotImplementedError). That means every scenario below
necessarily exercises the FALLBACK (no-transaction, sequential decrement +
manual compensating rollback) code path, not the PRIMARY multi-document-
transaction path. The transaction path was verified by code review only -
see the report for why that's believed safe (Atlas M0 is always a replica
set) and what's NOT verified end-to-end.

Run (from repo root): python scripts/test_stock_checkout.py
"""
import asyncio
import os
import sys
from pathlib import Path

# Required module-level env vars for server.py to import at all (see
# server.py:47-49) - fake values, no real Mongo/Atlas connection is ever
# made since AsyncIOMotorClient is created lazily/lazily-connecting and we
# swap it out for a mongomock client before any query runs.
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
    """A brand-new in-memory Mongo double per scenario, swapped into the
    server module's globals exactly like the real client/db are set up at
    import time (server.py:47-49)."""
    mock_client = mongomock_motor.AsyncMongoMockClient()
    mock_db = mock_client["test_db"]
    server.client = mock_client
    server.db = mock_db
    return mock_db


async def seed_product(db, product_id, stock, stock_status="in_stock", title=None):
    await db.shopify_products.insert_one({
        "id": product_id,
        "title": title or product_id,
        "price": 100.0,
        "stock": stock,
        "stock_status": stock_status,
    })


async def scenario_a_sufficient_stock():
    """(a) normal order with sufficient stock succeeds and decrements correctly."""
    db = await fresh_db()
    await seed_product(db, "p1", stock=10)
    items = [{"product_id": "p1", "quantity": 3, "price": 100.0}]
    await server._reserve_stock_for_order(items)
    doc = await db.shopify_products.find_one({"id": "p1"})
    check("a) stock decremented by requested quantity", doc["stock"] == 7, f"got {doc['stock']}")
    check("a) stock_status untouched while still >0", doc["stock_status"] == "in_stock")


async def scenario_b_exact_last_unit():
    """(b) order for exactly the last unit succeeds and stock hits 0 with
    stock_status auto-flipped to supplier_stock (George, 2026-08-22: a
    product landing on 0 stock must never be silently left "in_stock" or
    flipped to "out_of_stock" automatically - only "supplier_stock" is a
    valid automatic outcome now, see _auto_derived_stock_status)."""
    db = await fresh_db()
    await seed_product(db, "p1", stock=1)
    items = [{"product_id": "p1", "quantity": 1, "price": 100.0}]
    await server._reserve_stock_for_order(items)
    doc = await db.shopify_products.find_one({"id": "p1"})
    check("b) stock reaches exactly 0", doc["stock"] == 0, f"got {doc['stock']}")
    check("b) stock_status auto-flips to supplier_stock", doc["stock_status"] == "supplier_stock", f"got {doc['stock_status']}")


async def scenario_b2_supplier_stock_not_overwritten():
    """(b2, extra) a product already at 0 stock with stock_status=
    supplier_stock (backorder-from-supplier) must stay orderable - its
    stock_status must NOT be forced to out_of_stock by a decrement that
    keeps it at/near 0. This exercises the guard in _decrement_stock_once
    that only sets out_of_stock when stock_status != supplier_stock."""
    db = await fresh_db()
    # Give it 1 unit so the order can still succeed (quantity requested <=
    # stock), landing exactly on 0 afterwards.
    await seed_product(db, "p1", stock=1, stock_status="supplier_stock")
    items = [{"product_id": "p1", "quantity": 1, "price": 100.0}]
    await server._reserve_stock_for_order(items)
    doc = await db.shopify_products.find_one({"id": "p1"})
    check("b2) stock reaches 0", doc["stock"] == 0)
    check("b2) supplier_stock is preserved, not overwritten", doc["stock_status"] == "supplier_stock", f"got {doc['stock_status']}")


async def scenario_c_insufficient_rejected():
    """(c) order requesting more than available is rejected with no stock
    decremented."""
    db = await fresh_db()
    await seed_product(db, "p1", stock=2)
    items = [{"product_id": "p1", "quantity": 5, "price": 100.0}]
    try:
        await server._reserve_stock_for_order(items)
        check("c) HTTPException raised for insufficient stock", False, "no exception raised")
    except server.HTTPException as e:
        check("c) HTTPException status 409", e.status_code == 409, f"got {e.status_code}")
        check("c) message mentions product", "p1" in e.detail, e.detail)
        check("c) message uses George's wording", "Stoc epuizat de un alt client" in e.detail, e.detail)
    doc = await db.shopify_products.find_one({"id": "p1"})
    check("c) stock NOT decremented", doc["stock"] == 2, f"got {doc['stock']}")


async def scenario_d_multi_item_atomicity():
    """(d) multi-item order where item 1 has stock but item 2 doesn't is
    rejected AND item 1's stock is NOT decremented (proving atomicity)."""
    db = await fresh_db()
    await seed_product(db, "p1", stock=10)
    await seed_product(db, "p2", stock=1)
    items = [
        {"product_id": "p1", "quantity": 2, "price": 100.0},
        {"product_id": "p2", "quantity": 5, "price": 50.0},
    ]
    try:
        await server._reserve_stock_for_order(items)
        check("d) HTTPException raised", False, "no exception raised")
    except server.HTTPException as e:
        check("d) status 409", e.status_code == 409, f"got {e.status_code}")
    doc1 = await db.shopify_products.find_one({"id": "p1"})
    doc2 = await db.shopify_products.find_one({"id": "p2"})
    check("d) item1 (had enough stock) rolled back to original", doc1["stock"] == 10, f"got {doc1['stock']}")
    check("d) item2 (insufficient) untouched", doc2["stock"] == 1, f"got {doc2['stock']}")


async def scenario_d2_reverse_order_atomicity():
    """(d2, extra) same as (d) but with the failing item FIRST and the
    succeeding item SECOND, to make sure rollback isn't order-dependent."""
    db = await fresh_db()
    await seed_product(db, "p1", stock=1)
    await seed_product(db, "p2", stock=10)
    items = [
        {"product_id": "p1", "quantity": 5, "price": 100.0},
        {"product_id": "p2", "quantity": 2, "price": 50.0},
    ]
    try:
        await server._reserve_stock_for_order(items)
        check("d2) HTTPException raised", False, "no exception raised")
    except server.HTTPException:
        pass
    doc1 = await db.shopify_products.find_one({"id": "p1"})
    doc2 = await db.shopify_products.find_one({"id": "p2"})
    check("d2) failing item untouched", doc1["stock"] == 1, f"got {doc1['stock']}")
    check("d2) succeeding item rolled back", doc2["stock"] == 10, f"got {doc2['stock']}")


async def scenario_e_concurrent_last_unit():
    """(e) two near-simultaneous requests for the same last unit: one
    succeeds, one is cleanly rejected, without ending up with negative
    stock. Simulated via asyncio.gather - both coroutines' underlying
    find_one_and_update calls hit the same in-memory collection
    concurrently on mongomock-motor's single-threaded event loop, which
    still exercises the same "conditional filter must match or the update
    doesn't apply" logic (whether the actual OS-level serialization is
    provided by the event loop here or by MongoDB's document-level locking
    in production is an implementation detail - the correctness property
    under test is that only one of two contenders for the same last unit
    can win, which does not depend on which layer serializes access)."""
    db = await fresh_db()
    await seed_product(db, "p1", stock=1)
    items = [{"product_id": "p1", "quantity": 1, "price": 100.0}]

    async def attempt():
        try:
            await server._reserve_stock_for_order(items)
            return "success"
        except server.HTTPException as e:
            return ("rejected", e.status_code)

    results = await asyncio.gather(attempt(), attempt())
    successes = [r for r in results if r == "success"]
    rejections = [r for r in results if isinstance(r, tuple) and r[0] == "rejected"]
    check("e) exactly one success", len(successes) == 1, f"results={results}")
    check("e) exactly one rejection", len(rejections) == 1, f"results={results}")
    doc = await db.shopify_products.find_one({"id": "p1"})
    check("e) stock is 0, not negative", doc["stock"] == 0, f"got {doc['stock']}")


async def scenario_f_nonexistent_product():
    """(extra) product_id with no matching product at all is treated the
    same as insufficient stock (find_one_and_update matches nothing), not
    a crash."""
    db = await fresh_db()
    items = [{"product_id": "does-not-exist", "quantity": 1, "price": 100.0}]
    try:
        await server._reserve_stock_for_order(items)
        check("f) HTTPException raised for nonexistent product", False)
    except server.HTTPException as e:
        check("f) status 409 for nonexistent product", e.status_code == 409, f"got {e.status_code}")


async def scenario_g_no_transaction_support_fallback_confirmed():
    """(extra, meta-check) confirm the fallback path is actually the one
    being exercised in this test environment (mongomock has no session
    support), so the PASS results above are honestly scoped to the
    fallback strategy, not silently short-circuiting to a no-op."""
    db = await fresh_db()
    triggered = {"fallback": False}
    real_looks_like = server._looks_like_no_transaction_support

    def spy(exc):
        result = real_looks_like(exc)
        if result:
            triggered["fallback"] = True
        return result

    server._looks_like_no_transaction_support = spy
    try:
        await seed_product(db, "p1", stock=5)
        await server._reserve_stock_for_order([{"product_id": "p1", "quantity": 1, "price": 100.0}])
    finally:
        server._looks_like_no_transaction_support = real_looks_like
    check("g) fallback path was actually triggered in this test env", triggered["fallback"])


async def scenario_h_negative_quantity_rejected():
    """(extra, security) a crafted negative quantity must not be able to
    inflate stock via the $inc-based decrement - must be rejected with 400
    before touching the DB at all."""
    db = await fresh_db()
    await seed_product(db, "p1", stock=5)
    items = [{"product_id": "p1", "quantity": -3, "price": 100.0}]
    try:
        await server._reserve_stock_for_order(items)
        check("h) negative quantity rejected", False, "no exception raised")
    except server.HTTPException as e:
        check("h) negative quantity -> 400", e.status_code == 400, f"got {e.status_code}")
    doc = await db.shopify_products.find_one({"id": "p1"})
    check("h) stock NOT inflated", doc["stock"] == 5, f"got {doc['stock']}")


async def scenario_i_rollback_flips_supplier_stock_back_to_in_stock():
    """(extra) multi-item order where the first item's decrement lands
    exactly on 0 (auto-flipping it to supplier_stock), then a later item
    fails and the whole order rolls back via _compensate_stock - the first
    item's stock must be restored AND its stock_status must flip back to
    in_stock (not get stuck looking like a permanent backorder), mirroring
    the same "out_of_stock" rollback this used to test before the
    2026-08-22 supplier_stock policy change."""
    db = await fresh_db()
    await seed_product(db, "p1", stock=1)  # will hit exactly 0
    await seed_product(db, "p2", stock=1)  # insufficient for the requested qty
    items = [
        {"product_id": "p1", "quantity": 1, "price": 100.0},
        {"product_id": "p2", "quantity": 5, "price": 50.0},
    ]
    try:
        await server._reserve_stock_for_order(items)
        check("i) HTTPException raised", False, "no exception raised")
    except server.HTTPException:
        pass
    doc1 = await db.shopify_products.find_one({"id": "p1"})
    check("i) rolled-back item's stock restored to 1", doc1["stock"] == 1, f"got {doc1['stock']}")
    check("i) rolled-back item's stock_status restored to in_stock", doc1["stock_status"] == "in_stock", f"got {doc1['stock_status']}")


async def main():
    await scenario_a_sufficient_stock()
    await scenario_b_exact_last_unit()
    await scenario_b2_supplier_stock_not_overwritten()
    await scenario_c_insufficient_rejected()
    await scenario_d_multi_item_atomicity()
    await scenario_d2_reverse_order_atomicity()
    await scenario_e_concurrent_last_unit()
    await scenario_f_nonexistent_product()
    await scenario_g_no_transaction_support_fallback_confirmed()
    await scenario_h_negative_quantity_rejected()
    await scenario_i_rollback_flips_supplier_stock_back_to_in_stock()

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
