"""
Focused, standalone check for the 3 new pieces server.py gained for the
courier-tracking-link feature:
  - Order.courier_awb_number / Order.courier_service fields (and that they
    flow through GET /auth/orders).
  - PATCH /admin/orders/{order_id}/courier (admin_update_order_courier).
  - GET /auth/orders/{order_id}/courier-tracking (get_order_courier_tracking).

Not a pytest suite - mirrors scripts/test_admin_customer_account_lookup.py's
and scripts/test_stock_checkout.py's approach (this repo still has no test
framework, tests/ dir, or pytest in requirements.txt). Uses mongomock +
mongomock-motor to swap in an in-memory Mongo double for server.client/db,
and calls the endpoint functions directly (no HTTP layer/TestClient).

_require_admin's own BFF-JWT verification logic is already covered
end-to-end by test_require_admin_bff_only.py, so it's monkeypatched here to
a fixed-admin stub for the admin-endpoint scenarios below - this script
isolates admin_update_order_courier's own logic (404 handling, field
writes, audit log), not the auth layer. It's still exercised once
UNPATCHED (scenario a0) to confirm the endpoint is actually wired to
_require_admin at all (missing header -> 401), which is the one thing that
absolutely must not be skipped for an admin-mutating endpoint.

The customer-facing endpoint (get_order_courier_tracking) uses the real,
unpatched _find_user_by_token + a real seeded session token throughout,
since that IS the auth mechanism under test there (same pattern as GET
/auth/orders) - plus the ownership check (order must belong to the
requesting customer's email), which is the main point of this script.
courier_fan.track_awb itself is monkeypatched (its own HTTP behavior is
separately covered by scripts/test_courier_fan.py).

Run (from repo root): python scripts/test_courier_tracking_endpoints.py
"""
import asyncio
import os
import sys
import uuid
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


def make_request(bearer_value=None, path="/api/admin/orders/x/courier"):
    headers = []
    if bearer_value is not None:
        headers.append((b"authorization", f"Bearer {bearer_value}".encode()))
    return Request({"type": "http", "headers": headers, "method": "GET", "path": path})


def patch_require_admin(admin_id="admin-1", admin_email="staff@example.com"):
    original = server._require_admin

    async def _stub(request):
        return {"id": admin_id, "email": admin_email, "role": "admin"}

    server._require_admin = _stub
    return original


async def seed_order(db, order_id, email, **extra):
    order = {
        "id": order_id,
        "session_id": "sess-1",
        "items": [],
        "customer": {
            "name": "Ion Popescu", "email": email, "phone": "0700000000",
            "address": "Str. Muncii 1", "city": "Cluj-Napoca", "county": "Cluj",
            "postal_code": "400001", "notes": "",
        },
        "subtotal": 100.0,
        "shipping": 25.0,
        "total": 125.0,
        "status": "pending",
        "payment_method": "ramburs",
        "created_at": server.datetime.utcnow(),
    }
    order.update(extra)
    await db.orders.insert_one(order)
    return order


async def seed_user_with_token(db, email):
    token = server.generate_token()
    await db.users.insert_one({
        "id": str(uuid.uuid4()),
        "email": email,
        "role": "customer",
        "tokens": [{
            "token": token,
            "created_at": server.datetime.utcnow(),
            "expires_at": server.datetime.utcnow() + server.timedelta(days=30),
        }],
    })
    return token


# ---------- PATCH /admin/orders/{order_id}/courier ----------

async def scenario_a0_admin_endpoint_actually_requires_auth():
    """Unpatched _require_admin: no Authorization header -> 401. Confirms
    admin_update_order_courier is actually wired to the real auth
    dependency, not accidentally bypassable."""
    await fresh_db()
    payload = server.OrderCourierUpdate(courier_awb_number="AWB1")
    try:
        await server.admin_update_order_courier(make_request(bearer_value=None), "order-x", payload)
        check("a0) missing admin auth rejected", False, "no exception raised")
    except server.HTTPException as e:
        check("a0) missing admin auth -> 401", e.status_code == 401, f"got {e.status_code}")


async def scenario_a_admin_sets_fields_on_existing_order():
    db = await fresh_db()
    original = patch_require_admin()
    try:
        await seed_order(db, "order-1", "client@example.com")
        payload = server.OrderCourierUpdate(courier_awb_number="AWB-555", courier_service="Standard")
        result = await server.admin_update_order_courier(make_request(), "order-1", payload)
        check("a) response has courier_awb_number", result.get("courier_awb_number") == "AWB-555", result)
        check("a) response has courier_service", result.get("courier_service") == "Standard", result)

        stored = await db.orders.find_one({"id": "order-1"})
        check("a) stored order has courier_awb_number", stored.get("courier_awb_number") == "AWB-555", stored)
        check("a) stored order has courier_service", stored.get("courier_service") == "Standard", stored)

        audit = await db.admin_audit_log.find_one({"resource_id": "order-1", "action": "order.courier_update"})
        check("a) audit log entry written", audit is not None, audit)
        if audit:
            check("a) audit log 'after' has the new AWB", audit.get("after", {}).get("courier_awb_number") == "AWB-555", audit)
    finally:
        server._require_admin = original


async def scenario_b_admin_courier_service_optional():
    db = await fresh_db()
    original = patch_require_admin()
    try:
        await seed_order(db, "order-2", "client2@example.com")
        payload = server.OrderCourierUpdate(courier_awb_number="AWB-777")
        result = await server.admin_update_order_courier(make_request(), "order-2", payload)
        check("b) courier_service defaults to None when omitted", result.get("courier_service") is None, result)
    finally:
        server._require_admin = original


async def scenario_c_admin_order_not_found():
    await fresh_db()
    original = patch_require_admin()
    try:
        payload = server.OrderCourierUpdate(courier_awb_number="AWB-1")
        try:
            await server.admin_update_order_courier(make_request(), "does-not-exist", payload)
            check("c) missing order rejected", False, "no exception raised")
        except server.HTTPException as e:
            check("c) missing order -> 404", e.status_code == 404, f"got {e.status_code}")
    finally:
        server._require_admin = original


# ---------- GET /auth/orders/{order_id}/courier-tracking ----------

async def scenario_d_tracking_no_auth_header():
    await fresh_db()
    try:
        await server.get_order_courier_tracking("order-1", make_request(bearer_value=None))
        check("d) missing auth header rejected", False, "no exception raised")
    except server.HTTPException as e:
        check("d) missing auth header -> 401", e.status_code == 401, f"got {e.status_code}")


async def scenario_e_tracking_invalid_token():
    await fresh_db()
    try:
        await server.get_order_courier_tracking("order-1", make_request(bearer_value="not-a-real-token"))
        check("e) invalid token rejected", False, "no exception raised")
    except server.HTTPException as e:
        check("e) invalid token -> 401", e.status_code == 401, f"got {e.status_code}")


async def scenario_f_tracking_order_belongs_to_another_customer():
    """The core ownership check: a logged-in customer must not be able to
    see another customer's order's tracking, even with a valid order_id."""
    db = await fresh_db()
    await seed_order(db, "order-victim", "victim@example.com", courier_awb_number="AWB-VICTIM")
    attacker_token = await seed_user_with_token(db, "attacker@example.com")
    try:
        await server.get_order_courier_tracking("order-victim", make_request(bearer_value=attacker_token))
        check("f) cross-customer order access rejected", False, "no exception raised")
    except server.HTTPException as e:
        check("f) cross-customer order access -> 404", e.status_code == 404, f"got {e.status_code}")


async def scenario_g_tracking_order_not_found():
    db = await fresh_db()
    token = await seed_user_with_token(db, "client@example.com")
    try:
        await server.get_order_courier_tracking("nonexistent-order", make_request(bearer_value=token))
        check("g) nonexistent order rejected", False, "no exception raised")
    except server.HTTPException as e:
        check("g) nonexistent order -> 404", e.status_code == 404, f"got {e.status_code}")


async def scenario_h_tracking_order_owned_but_no_awb_yet():
    db = await fresh_db()
    await seed_order(db, "order-no-awb", "client@example.com")  # no courier_awb_number set
    token = await seed_user_with_token(db, "client@example.com")
    try:
        await server.get_order_courier_tracking("order-no-awb", make_request(bearer_value=token))
        check("h) order without AWB rejected", False, "no exception raised")
    except server.HTTPException as e:
        check("h) order without AWB -> 404", e.status_code == 404, f"got {e.status_code}")


async def scenario_i_tracking_happy_path():
    db = await fresh_db()
    await seed_order(db, "order-shipped", "client@example.com", courier_awb_number="AWB-999", courier_service="Standard")
    token = await seed_user_with_token(db, "client@example.com")

    original_track_awb = server.courier_fan.track_awb
    captured_awb = {}

    async def fake_track_awb(awb_number):
        captured_awb["awb"] = awb_number
        return {
            "awb": awb_number,
            "events": [{"name": "Livrat", "location": "Cluj-Napoca", "date": "2026-08-11 09:30:00"}],
            "confirmation": {"name": "Ion Popescu", "date": "2026-08-11 09:31:00"},
            # extra internal-looking field FAN's raw response might carry -
            # must NOT leak through to the customer-facing shape below.
            "internal_courier_note": "should not appear in response",
        }

    server.courier_fan.track_awb = fake_track_awb
    try:
        result = await server.get_order_courier_tracking("order-shipped", make_request(bearer_value=token))
        check("i) correct AWB passed to courier_fan.track_awb", captured_awb.get("awb") == "AWB-999", captured_awb)
        check("i) response has events", len(result.get("events", [])) == 1, result)
        check("i) response has confirmation", result.get("confirmation", {}).get("name") == "Ion Popescu", result)
        check("i) response shape is exactly {events, confirmation} - no extra keys leaked",
              set(result.keys()) == {"events", "confirmation"}, result)
    finally:
        server.courier_fan.track_awb = original_track_awb


async def scenario_j_tracking_courier_fan_runtime_error_becomes_502():
    db = await fresh_db()
    await seed_order(db, "order-fan-down", "client@example.com", courier_awb_number="AWB-DOWN")
    token = await seed_user_with_token(db, "client@example.com")

    original_track_awb = server.courier_fan.track_awb

    async def failing_track_awb(awb_number):
        raise RuntimeError("FAN Courier nu răspunde (timeout 30s)")

    server.courier_fan.track_awb = failing_track_awb
    try:
        try:
            await server.get_order_courier_tracking("order-fan-down", make_request(bearer_value=token))
            check("j) courier_fan RuntimeError rejected", False, "no exception raised")
        except server.HTTPException as e:
            check("j) courier_fan RuntimeError -> 502 (not unhandled 500)", e.status_code == 502, f"got {e.status_code}")
    finally:
        server.courier_fan.track_awb = original_track_awb


async def scenario_k_order_model_includes_new_fields_via_auth_orders():
    """GET /auth/orders (get_user_orders) serializes db.orders docs through
    the Order model - confirm the two new fields actually flow through
    end-to-end, not just when constructed manually."""
    db = await fresh_db()
    await seed_order(db, "order-list-1", "client@example.com", courier_awb_number="AWB-LIST", courier_service="Standard")
    token = await seed_user_with_token(db, "client@example.com")
    orders = await server.get_user_orders(make_request(bearer_value=token))
    check("k) GET /auth/orders returns exactly 1 order", len(orders) == 1, orders)
    if orders:
        check("k) courier_awb_number present on Order model instance", orders[0].courier_awb_number == "AWB-LIST", orders[0])
        check("k) courier_service present on Order model instance", orders[0].courier_service == "Standard", orders[0])


async def scenario_l_order_model_defaults_none_for_old_orders():
    """A pre-existing order doc with neither field present at all (mimics
    every order created before this change) still validates through the
    Order model, defaulting both new fields to None - backward compatible."""
    db = await fresh_db()
    await seed_order(db, "order-old", "client@example.com")  # no courier_* keys at all
    order_doc = await db.orders.find_one({"id": "order-old"})
    order = server.Order(**order_doc)
    check("l) old order defaults courier_awb_number to None", order.courier_awb_number is None, order)
    check("l) old order defaults courier_service to None", order.courier_service is None, order)


async def main():
    await scenario_a0_admin_endpoint_actually_requires_auth()
    await scenario_a_admin_sets_fields_on_existing_order()
    await scenario_b_admin_courier_service_optional()
    await scenario_c_admin_order_not_found()
    await scenario_d_tracking_no_auth_header()
    await scenario_e_tracking_invalid_token()
    await scenario_f_tracking_order_belongs_to_another_customer()
    await scenario_g_tracking_order_not_found()
    await scenario_h_tracking_order_owned_but_no_awb_yet()
    await scenario_i_tracking_happy_path()
    await scenario_j_tracking_courier_fan_runtime_error_becomes_502()
    await scenario_k_order_model_includes_new_fields_via_auth_orders()
    await scenario_l_order_model_defaults_none_for_old_orders()

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
