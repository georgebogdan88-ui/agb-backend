"""
Focused, standalone check for the 2026-08-22 security audit follow-up
(targeted audit, same day as the Pas B fail-safe fix):

  1. GET /order/{order_id} and GET /orders/{session_id} (both under the
     "/api" prefix - api_router = APIRouter(prefix="/api")) were completely
     unauthenticated and leaked a customer's full profile (name, email,
     phone, address, CUI for companies) to anyone who knew/guessed an
     order_id or session_id. Confirmed dead code before removal: no caller
     in agb-webshop (grepped, no matches) and no caller in
     agb-shopify/apps/mobile (packages/api's fetchOrdersBySession is
     exported but never imported/called by any mobile screen -
     OrdersScreen.tsx uses the authenticated fetchMyOrders/`/auth/orders`
     path instead, CheckoutScreen.tsx reads the order confirmation directly
     from the POST /orders response). Both routes have been deleted
     outright (not just auth-gated) - GET /admin/orders/{order_id}
     (_require_admin-gated, near the other /admin/orders/* routes) already
     covers the same "look up one order by id" use case for staff.

  2. POST /cart (add_to_cart) used to accept whatever session_id a client
     sent at face value, including on the very first cart interaction -
     both agb-webshop and agb-shopify/apps/mobile generate this
     client-side (crypto.randomUUID()/generateUuid()) and just send it, so
     anyone could hand-pick their own cart session id. Hardened via new
     server._resolve_cart_session_id(): a client-sent id is only honored
     as-is if it already matches a cart or order this server recognizes
     (find_one on db.cart / db.orders); otherwise a fresh
     secrets.token_urlsafe(32) id is minted server-side (same primitive
     already used for password-reset tokens) and used/returned instead.
     CartItem's response shape is unchanged (still has `session_id`) so
     this is contract-compatible - but see the report to George/coordinator
     for why agb-webshop's and agb-shopify's cart contexts currently
     discard the returned session_id and will need a follow-up change
     there to actually pick it up and persist it (their own repos, not
     touched here).

Not a pytest suite - mirrors scripts/test_stock_checkout.py's and
scripts/test_product_handle_stability_and_search_relevance.py's approach:
mongomock + mongomock-motor for an in-memory Mongo double swapped into
server.client/db, direct calls to the real server.py functions for the
cart-hardening scenarios (add_to_cart/get_cart/update_cart_item/
remove_from_cart/clear_cart - no HTTP layer needed there), and a real
FastAPI TestClient (already used elsewhere in this repo's scripts) ONLY for
the route-removal scenarios, since proving a route no longer answers over
HTTP is the whole point there.

Run (from repo root): python scripts/test_kill_dead_order_endpoints_and_harden_cart_session.py
"""
import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path

os.environ.setdefault("MONGO_URL", "mongodb://fake-for-import-only/")
os.environ.setdefault("DB_NAME", "fake_db_for_import_only")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mongomock_motor  # noqa: E402
from starlette.requests import Request  # noqa: E402
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


def make_request(method="POST", path="/api/cart"):
    """Bare starlette Request with no Authorization header - guest cart,
    same tolerant path add_to_cart already exercises for anonymous carts
    today."""
    return Request({"type": "http", "headers": [], "method": method, "path": path})


def patch_require_admin(admin_id="admin-1", admin_email="staff@example.com"):
    original = server._require_admin

    async def _stub(request):
        return {"id": admin_id, "email": admin_email, "role": "admin"}

    server._require_admin = _stub
    return original


async def seed_product(db, product_id, price=100.0, stock=10, title=None):
    await db.shopify_products.insert_one({
        "id": product_id,
        "title": title or product_id,
        "price": price,
        "stock": stock,
        "stock_status": "in_stock",
    })


# ==================== Part 1: dead order-lookup endpoints removed ====================

def _route_exists(path_template, method):
    method = method.upper()
    for route in server.app.routes:
        if getattr(route, "path", None) == path_template and method in getattr(route, "methods", set()):
            return True
    return False


async def scenario_1a_routes_removed_from_router_table():
    check(
        "1a) GET /api/order/{order_id} no longer registered in the route table",
        not _route_exists("/api/order/{order_id}", "GET"),
    )
    check(
        "1a) GET /api/orders/{session_id} no longer registered in the route table",
        not _route_exists("/api/orders/{session_id}", "GET"),
    )


async def scenario_1b_removed_routes_404_over_real_http():
    db = await fresh_db()
    await db.orders.insert_one({
        "id": "order-should-not-leak",
        "session_id": "session-should-not-leak",
        "items": [],
        "customer": {
            "name": "Ion Popescu", "email": "ion@example.com", "phone": "0722000000",
            "address": "Str. Exemplu 1", "city": "Cluj", "county": "Cluj", "postal_code": "400000",
        },
        "subtotal": 100.0, "shipping": 25.0, "total": 125.0, "status": "pending",
        "payment_method": "ramburs", "crm_synced": False, "crm_sync_attempts": 0,
        "crm_items_dirty": False, "crm_items_sync_attempts": 0,
    })

    from fastapi.testclient import TestClient
    with TestClient(server.app) as client:
        r_order = client.get("/api/order/order-should-not-leak")
        r_orders = client.get("/api/orders/session-should-not-leak")

    check("1b) GET /api/order/{id} -> 404 (route gone, not just gated)", r_order.status_code == 404, r_order.status_code)
    check("1b) response does not leak the customer's name", "Ion Popescu" not in r_order.text, r_order.text)
    check("1b) GET /api/orders/{session_id} -> 404 (route gone, not just gated)", r_orders.status_code == 404, r_orders.status_code)
    check("1b) response does not leak the customer's email", "ion@example.com" not in r_orders.text, r_orders.text)


async def scenario_1c_neighboring_literal_route_unaffected():
    """GET /orders/mobile is a literal path that used to have to be
    registered before the now-deleted GET /orders/{session_id} wildcard to
    avoid being swallowed by it - confirm it still works standalone now
    that the wildcard is gone entirely."""
    db = await fresh_db()
    await db.mobile_orders.insert_one({"id": "mo-1", "created_at": datetime.utcnow(), "total": 50.0})
    original_admin = patch_require_admin()
    try:
        from fastapi.testclient import TestClient
        with TestClient(server.app) as client:
            r = client.get("/api/orders/mobile")
        check("1c) GET /api/orders/mobile still works (200)", r.status_code == 200, r.status_code)
        check("1c) returns the seeded mobile order", any(o.get("id") == "mo-1" for o in r.json()), r.json())
    finally:
        server._require_admin = original_admin


async def scenario_1d_admin_lookup_still_covers_the_use_case():
    db = await fresh_db()
    await db.orders.insert_one({
        "id": "order-admin-lookup",
        "session_id": "sess-1",
        "items": [],
        "customer": {
            "name": "Maria Ionescu", "email": "maria@example.com", "phone": "0733000000",
            "address": "Str. Test 2", "city": "Iasi", "county": "Iasi", "postal_code": "700000",
        },
        "subtotal": 200.0, "shipping": 25.0, "total": 225.0, "status": "pending",
        "payment_method": "ramburs", "crm_synced": False, "crm_sync_attempts": 0,
        "crm_items_dirty": False, "crm_items_sync_attempts": 0,
    })

    original_admin = patch_require_admin()
    try:
        from fastapi.testclient import TestClient
        with TestClient(server.app) as client:
            r_ok = client.get("/api/admin/orders/order-admin-lookup")
    finally:
        server._require_admin = original_admin

    check("1d) GET /api/admin/orders/{id} (authenticated) still resolves order-by-id lookup", r_ok.status_code == 200, r_ok.status_code)
    check("1d) it returns the right order's customer name", r_ok.json().get("customer", {}).get("name") == "Maria Ionescu", r_ok.text)

    # Without any admin auth stub (the real _require_admin), the same route
    # must reject, not silently behave like the old public GET /order did.
    db2 = await fresh_db()
    await db2.orders.insert_one({
        "id": "order-admin-lookup",
        "session_id": "sess-1",
        "items": [],
        "customer": {
            "name": "Maria Ionescu", "email": "maria@example.com", "phone": "0733000000",
            "address": "Str. Test 2", "city": "Iasi", "county": "Iasi", "postal_code": "700000",
        },
        "subtotal": 200.0, "shipping": 25.0, "total": 225.0, "status": "pending",
        "payment_method": "ramburs", "crm_synced": False, "crm_sync_attempts": 0,
        "crm_items_dirty": False, "crm_items_sync_attempts": 0,
    })
    from fastapi.testclient import TestClient
    with TestClient(server.app) as client:
        r_denied = client.get("/api/admin/orders/order-admin-lookup")
    check("1d) GET /api/admin/orders/{id} without admin auth is rejected, not open", r_denied.status_code in (401, 403), r_denied.status_code)


# ==================== Part 2: cart session_id hardened server-side ====================

async def scenario_2a_unknown_client_session_id_is_replaced():
    db = await fresh_db()
    await seed_product(db, "p1")
    client_chosen_id = "attacker-picked-session-id-123"

    result = await server.add_to_cart(
        server.CartItemCreate(session_id=client_chosen_id, product_id="p1", product_name="Piesa", product_image="", price=999.0, quantity=1),
        make_request(),
    )

    check("2a) server does NOT echo back the client-chosen id", result.session_id != client_chosen_id, result.session_id)
    check("2a) server-issued id looks like a real token (length >= 32)", len(result.session_id) >= 32, result.session_id)
    check("2a) server-issued id is URL-safe (no '/' or '+')", "/" not in result.session_id and "+" not in result.session_id, result.session_id)
    # price is still authoritative (unrelated existing protection, still intact)
    check("2a) price still resolved server-side, not the fabricated 999.0", result.price == 100.0, result.price)

    cart_under_client_id = await server.get_cart(client_chosen_id)
    cart_under_real_id = await server.get_cart(result.session_id)
    check("2a) nothing stored under the client's fabricated id", cart_under_client_id == [], cart_under_client_id)
    check("2a) the item is actually stored under the server-issued id", len(cart_under_real_id) == 1 and cart_under_real_id[0].product_id == "p1", cart_under_real_id)


async def scenario_2b_empty_session_id_also_replaced():
    db = await fresh_db()
    await seed_product(db, "p1")
    result = await server.add_to_cart(
        server.CartItemCreate(session_id="", product_id="p1", product_name="Piesa", product_image="", price=1.0, quantity=1),
        make_request(),
    )
    check("2b) empty-string session_id is replaced with a real generated id", bool(result.session_id) and result.session_id != "", result.session_id)


async def scenario_2c_known_cart_session_id_is_reused_as_is():
    """Once the server has issued/recognized a session_id (there's already
    a live cart under it), a client resending that SAME id must be honored
    - otherwise every subsequent add-to-cart call would keep fragmenting
    the cart across new random ids and the cart would never accumulate."""
    db = await fresh_db()
    await seed_product(db, "p1")
    await seed_product(db, "p2", price=50.0)

    first = await server.add_to_cart(
        server.CartItemCreate(session_id="whatever-client-sent", product_id="p1", product_name="Piesa 1", product_image="", price=1.0, quantity=1),
        make_request(),
    )
    real_id = first.session_id

    second = await server.add_to_cart(
        server.CartItemCreate(session_id=real_id, product_id="p2", product_name="Piesa 2", product_image="", price=1.0, quantity=1),
        make_request(),
    )
    check("2c) resending the server-issued id keeps the SAME id (not replaced again)", second.session_id == real_id, second.session_id)

    combined = await server.get_cart(real_id)
    ids_in_cart = sorted(i.product_id for i in combined)
    check("2c) both items ended up together under the one real session id", ids_in_cart == ["p1", "p2"], ids_in_cart)


async def scenario_2d_known_order_session_id_is_also_reused():
    """Task wording: 'nu există un coș SAU o comandă cu acel ID' - a
    session_id tied to a past order (cart already cleared at checkout) must
    still be honored, not treated as unknown."""
    db = await fresh_db()
    await seed_product(db, "p1")
    await db.orders.insert_one({
        "id": "past-order-1",
        "session_id": "returning-customer-session",
        "items": [], "customer": {
            "name": "X", "email": "x@example.com", "phone": "0700000000",
            "address": "A", "city": "B", "county": "C", "postal_code": "000000",
        },
        "subtotal": 10.0, "shipping": 25.0, "total": 35.0, "status": "pending",
        "payment_method": "ramburs", "crm_synced": False, "crm_sync_attempts": 0,
        "crm_items_dirty": False, "crm_items_sync_attempts": 0,
    })

    result = await server.add_to_cart(
        server.CartItemCreate(session_id="returning-customer-session", product_id="p1", product_name="Piesa", product_image="", price=1.0, quantity=1),
        make_request(),
    )
    check("2d) a session_id matching a past order is reused as-is", result.session_id == "returning-customer-session", result.session_id)


async def scenario_2e_two_unknown_ids_get_independent_fresh_ids():
    db = await fresh_db()
    await seed_product(db, "p1")
    r1 = await server.add_to_cart(
        server.CartItemCreate(session_id="guess-a", product_id="p1", product_name="Piesa", product_image="", price=1.0, quantity=1),
        make_request(),
    )
    r2 = await server.add_to_cart(
        server.CartItemCreate(session_id="guess-b", product_id="p1", product_name="Piesa", product_image="", price=1.0, quantity=1),
        make_request(),
    )
    check("2e) two different unrecognized client ids get two different fresh server ids", r1.session_id != r2.session_id, (r1.session_id, r2.session_id))
    check("2e) neither fresh id echoes either guess", r1.session_id not in ("guess-a", "guess-b") and r2.session_id not in ("guess-a", "guess-b"))


async def scenario_2f_full_cart_flow_end_to_end_with_hardened_session():
    """add -> read -> update quantity -> remove -> clear, exactly the flow
    scripts/test_* and the coordinator's task description call out, proven
    working end-to-end through the new _resolve_cart_session_id path."""
    db = await fresh_db()
    await seed_product(db, "p1", price=120.0)
    await seed_product(db, "p2", price=80.0)

    added = await server.add_to_cart(
        server.CartItemCreate(session_id="fresh-client-guess", product_id="p1", product_name="Piesa 1", product_image="", price=1.0, quantity=2),
        make_request(),
    )
    real_id = added.session_id
    check("2f) add: item persisted with correct authoritative price", added.price == 120.0, added.price)
    check("2f) add: quantity as requested", added.quantity == 2, added.quantity)

    cart = await server.get_cart(real_id)
    check("2f) read: cart has exactly the one item", len(cart) == 1, cart)

    updated = await server.update_cart_item(added.id, server.CartItemUpdate(quantity=5))
    check("2f) update: quantity changed", updated.quantity == 5, updated.quantity)

    added2 = await server.add_to_cart(
        server.CartItemCreate(session_id=real_id, product_id="p2", product_name="Piesa 2", product_image="", price=1.0, quantity=1),
        make_request(),
    )
    cart2 = await server.get_cart(real_id)
    check("2f) second add under the real id: cart now has 2 distinct items", len(cart2) == 2, cart2)

    remove_result = await server.remove_from_cart(added2.id)
    check("2f) remove: reports success", remove_result.get("message") is not None, remove_result)
    cart3 = await server.get_cart(real_id)
    check("2f) remove: cart back down to 1 item", len(cart3) == 1, cart3)

    clear_result = await server.clear_cart(real_id)
    check("2f) clear: reports success", clear_result.get("message") is not None, clear_result)
    cart4 = await server.get_cart(real_id)
    check("2f) clear: cart is now empty", cart4 == [], cart4)


async def scenario_2g_generated_ids_are_actually_random_not_deterministic():
    db = await fresh_db()
    await seed_product(db, "p1")
    ids = set()
    for i in range(10):
        db = await fresh_db()  # isolate each call - no cart/order state carried over
        await seed_product(db, "p1")
        r = await server.add_to_cart(
            server.CartItemCreate(session_id=f"same-shape-guess-{i}", product_id="p1", product_name="Piesa", product_image="", price=1.0, quantity=1),
            make_request(),
        )
        ids.add(r.session_id)
    check("2g) 10 independent generations produced 10 unique ids (real randomness, no collisions)", len(ids) == 10, ids)


async def main():
    await scenario_1a_routes_removed_from_router_table()
    await scenario_1b_removed_routes_404_over_real_http()
    await scenario_1c_neighboring_literal_route_unaffected()
    await scenario_1d_admin_lookup_still_covers_the_use_case()

    await scenario_2a_unknown_client_session_id_is_replaced()
    await scenario_2b_empty_session_id_also_replaced()
    await scenario_2c_known_cart_session_id_is_reused_as_is()
    await scenario_2d_known_order_session_id_is_also_reused()
    await scenario_2e_two_unknown_ids_get_independent_fresh_ids()
    await scenario_2f_full_cart_flow_end_to_end_with_hardened_session()
    await scenario_2g_generated_ids_are_actually_random_not_deterministic()

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
