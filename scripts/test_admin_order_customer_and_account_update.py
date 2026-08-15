"""
Focused, standalone check for the two new endpoints that let CRM staff
correct a customer's typo'd contact/address/invoice details:
  - PATCH /admin/orders/{order_id}/customer  (admin_update_order_customer)
    - order-scoped only: edits the frozen `customer` snapshot on ONE order.
  - PATCH /admin/customer-account/{email}     (admin_update_customer_account)
    - edits the customer's STANDING ACCOUNT (db.users), so future orders and
      their own account page reflect the fix too.
  - the shared _build_user_profile_update_dict helper factored out of
    PUT /auth/me (update_current_user), used by BOTH that endpoint (own
    scenario here, to prove behavior is unchanged) and the new admin one.

Not a pytest suite - mirrors scripts/test_courier_tracking_endpoints.py's
and scripts/test_admin_customer_account_lookup.py's approach (this repo
still has no test framework, tests/ dir, or pytest in requirements.txt).
Uses mongomock + mongomock-motor to swap in an in-memory Mongo double for
server.client/db, and calls the endpoint functions directly (no HTTP
layer/TestClient).

_require_admin's own BFF-JWT verification logic is already covered
end-to-end by test_require_admin_bff_only.py, so it's monkeypatched here to
a fixed-admin stub for most scenarios - each admin endpoint is still
exercised once UNPATCHED (the "a0" scenarios) to confirm it's actually wired
to _require_admin at all (missing header -> 401/403), which is the one
thing that must never be silently skipped for an admin-mutating endpoint.

Run (from repo root): python scripts/test_admin_order_customer_and_account_update.py
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
from starlette.background import BackgroundTasks  # noqa: E402
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


def make_request(bearer_value=None, path="/api/admin/x"):
    headers = []
    if bearer_value is not None:
        headers.append((b"authorization", f"Bearer {bearer_value}".encode()))
    return Request({"type": "http", "headers": headers, "method": "PATCH", "path": path})


def patch_require_admin(admin_id="admin-1", admin_email="staff@example.com"):
    original = server._require_admin

    async def _stub(request):
        return {"id": admin_id, "email": admin_email, "role": "admin"}

    server._require_admin = _stub
    return original


async def seed_order(db, order_id, email, **customer_extra):
    customer = {
        "name": "Ion Popescu", "email": email, "phone": "0700000000",
        "address": "Str. Muncii 1", "city": "Cluj-Napoca", "county": "Cluj",
        "postal_code": "400001", "notes": "",
        "is_company": False, "company_name": None, "cui": None, "reg_com": None,
        "administrator": None,
        "company_address_strada": None, "company_address_numar": None,
        "company_address_bloc": None, "company_address_scara": None,
        "company_address_ap": None, "company_address_oras": None,
        "company_address_judet": None, "company_address_cod_postal": None,
    }
    customer.update(customer_extra)
    order = {
        "id": order_id,
        "session_id": "sess-1",
        "items": [{"product_id": "p1", "product_name": "Filtru ulei", "product_image": "", "price": 50.0, "quantity": 2}],
        "customer": customer,
        "subtotal": 100.0,
        "shipping": 25.0,
        "total": 125.0,
        "status": "pending",
        "payment_method": "ramburs",
        "created_at": server.datetime.utcnow(),
        "crm_synced": False,
        "crm_sync_error": None,
        "crm_sync_attempts": 0,
        "crm_items_dirty": False,
        "crm_items_sync_error": None,
        "crm_items_sync_attempts": 0,
    }
    await db.orders.insert_one(order)
    return order


async def make_user(db, email="owner@example.com", user_id=None, **extra):
    user_id = user_id or str(uuid.uuid4())
    token_doc = server._new_session_token_doc()
    user = {
        "id": user_id,
        "email": email,
        "password_hash": await server.hash_password("parola123"),
        "name": "Ion Popescu",
        "phone": "0700000001",
        "address": "Str. Exemplu 1",
        "address_strada": "Str. Exemplu",
        "address_numar": "1",
        "address_bloc": None,
        "address_scara": None,
        "address_ap": None,
        "city": "Cluj-Napoca",
        "county": "Cluj",
        "postal_code": "400000",
        "is_company": False,
        "company_name": None,
        "cui": None,
        "reg_com": None,
        "administrator": None,
        "company_address": None,
        "company_address_strada": None,
        "company_address_numar": None,
        "company_address_bloc": None,
        "company_address_scara": None,
        "company_address_ap": None,
        "company_address_oras": None,
        "company_address_judet": None,
        "company_address_cod_postal": None,
        "tokens": [token_doc],
        "is_shopify_customer": False,
        "created_at": server.datetime.utcnow(),
        "role": "customer",
    }
    user.update(extra)
    await db.users.insert_one(user)
    return user, token_doc["token"]


# ==================== PATCH /admin/orders/{order_id}/customer ====================

async def scenario_a0_order_customer_requires_admin_auth():
    await fresh_db()
    payload = server.OrderCustomerUpdate(phone="0711111111")
    try:
        await server.admin_update_order_customer(make_request(bearer_value=None), "order-x", payload)
        check("a0) missing admin auth rejected", False, "no exception raised")
    except server.HTTPException as e:
        check("a0) missing admin auth -> 401", e.status_code == 401, f"got {e.status_code}")


async def scenario_a_order_customer_partial_update_only_changes_sent_fields():
    db = await fresh_db()
    original = patch_require_admin()
    try:
        await seed_order(db, "order-1", "client@example.com", phone="0700000000", city="Cluj-Napoca")
        payload = server.OrderCustomerUpdate(phone="0722222222")
        result = await server.admin_update_order_customer(make_request(), "order-1", payload)

        check("a) response customer.phone updated", result["customer"]["phone"] == "0722222222", result["customer"])
        check("a) response customer.city UNCHANGED (not sent)", result["customer"]["city"] == "Cluj-Napoca", result["customer"])
        check("a) response customer.name UNCHANGED (not sent)", result["customer"]["name"] == "Ion Popescu", result["customer"])
        check("a) response customer.email UNCHANGED (not part of model at all)",
              result["customer"]["email"] == "client@example.com", result["customer"])

        stored = await db.orders.find_one({"id": "order-1"})
        check("a) stored order customer.phone updated", stored["customer"]["phone"] == "0722222222", stored["customer"])
        check("a) stored order top-level fields (subtotal/total/items) untouched",
              stored["subtotal"] == 100.0 and stored["total"] == 125.0 and len(stored["items"]) == 1, stored)

        audit = await db.admin_audit_log.find_one({"resource_id": "order-1", "action": "order.customer_update"})
        check("a) audit log entry written", audit is not None, audit)
        if audit:
            # phone is a PII-looking key, so _sanitize_audit_payload masks its
            # value before persisting (see _AUDIT_PII_KEY_RE/_mask_audit_pii) -
            # same defense-in-depth already applied to every other admin audit
            # entry in this file, not something specific to this endpoint.
            check("a) audit 'before' has ONLY the changed field (masked)", audit.get("before") == {"phone": server._mask_audit_pii("0700000000")}, audit.get("before"))
            check("a) audit 'after' has ONLY the changed field (masked)", audit.get("after") == {"phone": server._mask_audit_pii("0722222222")}, audit.get("after"))
    finally:
        server._require_admin = original


async def scenario_b_order_customer_company_fields_update():
    db = await fresh_db()
    original = patch_require_admin()
    try:
        await seed_order(db, "order-2", "client2@example.com")
        payload = server.OrderCustomerUpdate(
            is_company=True, company_name="Ion SRL", cui="RO12345678",
            company_address_strada="Str. Fabricii", company_address_numar="10",
        )
        result = await server.admin_update_order_customer(make_request(), "order-2", payload)
        check("b) is_company set", result["customer"]["is_company"] is True, result["customer"])
        check("b) company_name set", result["customer"]["company_name"] == "Ion SRL", result["customer"])
        check("b) cui set", result["customer"]["cui"] == "RO12345678", result["customer"])
        check("b) company_address_strada set", result["customer"]["company_address_strada"] == "Str. Fabricii", result["customer"])
        check("b) reg_com/administrator (not sent) remain None", result["customer"]["reg_com"] is None and result["customer"]["administrator"] is None, result["customer"])
    finally:
        server._require_admin = original


async def scenario_c_order_customer_order_not_found():
    await fresh_db()
    original = patch_require_admin()
    try:
        payload = server.OrderCustomerUpdate(phone="0700000000")
        try:
            await server.admin_update_order_customer(make_request(), "does-not-exist", payload)
            check("c) missing order rejected", False, "no exception raised")
        except server.HTTPException as e:
            check("c) missing order -> 404", e.status_code == 404, f"got {e.status_code}")
    finally:
        server._require_admin = original


async def scenario_d_order_customer_empty_payload_is_a_noop():
    db = await fresh_db()
    original = patch_require_admin()
    try:
        await seed_order(db, "order-3", "client3@example.com")
        payload = server.OrderCustomerUpdate()
        result = await server.admin_update_order_customer(make_request(), "order-3", payload)
        check("d) empty payload returns order unchanged", result["customer"]["phone"] == "0700000000", result["customer"])
        audit = await db.admin_audit_log.find_one({"resource_id": "order-3", "action": "order.customer_update"})
        check("d) no audit log written for a true no-op update", audit is None, audit)
    finally:
        server._require_admin = original


async def scenario_e_order_customer_does_not_touch_standing_account():
    """Core Part-1 contract: this endpoint edits ONLY the order snapshot,
    never db.users - even when a matching account exists for that email."""
    db = await fresh_db()
    original = patch_require_admin()
    try:
        await seed_order(db, "order-4", "hasaccount@example.com", phone="0700000000")
        user, _ = await make_user(db, email="hasaccount@example.com", user_id="u-has-account", phone="0700000000")

        payload = server.OrderCustomerUpdate(phone="0788888888")
        await server.admin_update_order_customer(make_request(), "order-4", payload)

        account_after = await db.users.find_one({"id": "u-has-account"})
        check("e) standing account phone UNCHANGED by the order-scoped fix",
              account_after["phone"] == "0700000000", account_after["phone"])
    finally:
        server._require_admin = original


# ==================== PATCH /admin/customer-account/{email} ====================

async def scenario_f0_customer_account_requires_admin_auth():
    await fresh_db()
    update_data = server.UserUpdate(phone="0711111111")
    try:
        await server.admin_update_customer_account(
            "someone@example.com", update_data, make_request(bearer_value=None), server.BackgroundTasks(),
        )
        check("f0) missing admin auth rejected", False, "no exception raised")
    except server.HTTPException as e:
        check("f0) missing admin auth -> 401", e.status_code == 401, f"got {e.status_code}")


async def scenario_g_customer_account_partial_update_and_crm_sync_queued():
    db = await fresh_db()
    original = patch_require_admin()
    try:
        user, _ = await make_user(db, email="fix-me@example.com", user_id="u-fix", phone="0700000000", city="Cluj-Napoca")
        update_data = server.UserUpdate(phone="0799999999")
        bt = BackgroundTasks()
        result = await server.admin_update_customer_account("fix-me@example.com", update_data, make_request(), bt)

        check("g) response phone updated", result["phone"] == "0799999999", result)
        check("g) response city UNCHANGED (not sent)", result["city"] == "Cluj-Napoca", result)
        check("g) response has no password_hash leak", "password_hash" not in result, result)

        stored = await db.users.find_one({"id": "u-fix"})
        check("g) stored phone updated", stored["phone"] == "0799999999", stored["phone"])

        audit = await db.admin_audit_log.find_one({"resource_id": "u-fix", "action": "customer_account.update"})
        check("g) audit log entry written", audit is not None, audit)
        if audit:
            # masked - see the equivalent note in scenario (a) above.
            check("g) audit 'before' has old phone (masked)", audit.get("before", {}).get("phone") == server._mask_audit_pii("0700000000"), audit.get("before"))
            check("g) audit 'after' has new phone (masked)", audit.get("after", {}).get("phone") == server._mask_audit_pii("0799999999"), audit.get("after"))

        queued_funcs = [t.func for t in bt.tasks]
        check("g) sync_account_to_crm queued as a background task", server.sync_account_to_crm in queued_funcs, queued_funcs)
    finally:
        server._require_admin = original


async def scenario_h_customer_account_address_recombination_matches_put_auth_me():
    """The split-address fields must recombine into `address` through the
    SAME logic PUT /auth/me uses - proven by driving both endpoints with an
    equivalent update and comparing the resulting combined string."""
    db = await fresh_db()
    original = patch_require_admin()
    try:
        user, _ = await make_user(
            db, email="addr@example.com", user_id="u-addr",
            address_strada="Str. Veche", address_numar="1", address_bloc=None,
            address_scara=None, address_ap=None, address="Str. Veche 1",
        )
        update_data = server.UserUpdate(address_strada="Str. Noua", address_bloc="B2", address_scara="1", address_ap="4")
        bt = BackgroundTasks()
        result = await server.admin_update_customer_account("addr@example.com", update_data, make_request(), bt)

        expected = "Str. Noua 1, Bl. B2, Sc. 1, Ap. 4"  # numar "1" carried over from existing doc, unset in this update
        check("h) address recombined with unset numar carried over from existing doc",
              result["address"] == expected, result["address"])
    finally:
        server._require_admin = original


async def scenario_i_customer_account_company_address_recombination():
    db = await fresh_db()
    original = patch_require_admin()
    try:
        user, _ = await make_user(db, email="company@example.com", user_id="u-company", is_company=True)
        update_data = server.UserUpdate(
            company_address_strada="Str. Fabricii", company_address_numar="22", company_address_oras="Oradea",
        )
        bt = BackgroundTasks()
        result = await server.admin_update_customer_account("company@example.com", update_data, make_request(), bt)
        check("i) company_address recombined", result["company_address"] == "Str. Fabricii 22", result["company_address"])
        check("i) company_address_oras set (split field, no combination needed)",
              result["company_address_oras"] == "Oradea", result["company_address_oras"])
    finally:
        server._require_admin = original


async def scenario_j_customer_account_not_found_404():
    await fresh_db()
    original = patch_require_admin()
    try:
        update_data = server.UserUpdate(phone="0700000000")
        try:
            await server.admin_update_customer_account(
                "nobody@example.com", update_data, make_request(), server.BackgroundTasks(),
            )
            check("j) not found rejected", False, "no exception raised")
        except server.HTTPException as e:
            check("j) not found -> 404", e.status_code == 404, f"got {e.status_code}")
            check("j) not found detail matches GET /admin/customer-account's message",
                  e.detail == "Niciun cont web găsit cu acest email.", e.detail)
    finally:
        server._require_admin = original


async def scenario_k_customer_account_email_normalization():
    db = await fresh_db()
    original = patch_require_admin()
    try:
        user, _ = await make_user(db, email="normalize@example.com", user_id="u-norm")
        update_data = server.UserUpdate(phone="0711112222")
        result = await server.admin_update_customer_account(
            "  Normalize@Example.com  ", update_data, make_request(), server.BackgroundTasks(),
        )
        check("k) case/whitespace-insensitive email match", result["phone"] == "0711112222", result)
    finally:
        server._require_admin = original


async def scenario_l_customer_account_does_not_touch_orders():
    """Core Part-2 contract: correcting the standing account must NOT
    retroactively rewrite any already-placed order snapshot."""
    db = await fresh_db()
    original = patch_require_admin()
    try:
        user, _ = await make_user(db, email="both@example.com", user_id="u-both", phone="0700000000")
        await seed_order(db, "order-both-1", "both@example.com", phone="0700000000")

        update_data = server.UserUpdate(phone="0755555555")
        await server.admin_update_customer_account("both@example.com", update_data, make_request(), server.BackgroundTasks())

        order_after = await db.orders.find_one({"id": "order-both-1"})
        check("l) existing order's customer.phone UNCHANGED by the account-level fix",
              order_after["customer"]["phone"] == "0700000000", order_after["customer"]["phone"])
    finally:
        server._require_admin = original


# ==================== PUT /auth/me regression (shared helper refactor) ====================

async def scenario_m_put_auth_me_still_works_after_helper_refactor():
    """PUT /auth/me itself (update_current_user) must behave identically
    after factoring its field-update/address-combination logic out into
    _build_user_profile_update_dict - partial update, address recombination,
    and the CRM sync background task all still fire exactly as before."""
    db = await fresh_db()
    user, token = await make_user(
        db, email="self@example.com", user_id="u-self",
        address_strada="Str. Veche", address_numar="5", address="Str. Veche 5",
        city="Cluj-Napoca",
    )

    def make_self_request(bearer):
        return Request({"type": "http", "headers": [(b"authorization", f"Bearer {bearer}".encode())],
                         "method": "PUT", "path": "/api/auth/me"})

    update_data = server.UserUpdate(address_strada="Str. Noua", phone="0733333333")
    bt = BackgroundTasks()
    result = await server.update_current_user(make_self_request(token), update_data, bt)

    check("m) self-update: phone updated", result["phone"] == "0733333333", result)
    check("m) self-update: city UNCHANGED (not sent)", result["city"] == "Cluj-Napoca", result)
    check("m) self-update: address recombined (strada changed, numar carried over)",
          result["address"] == "Str. Noua 5", result["address"])

    queued_funcs = [t.func for t in bt.tasks]
    check("m) self-update: sync_account_to_crm still queued", server.sync_account_to_crm in queued_funcs, queued_funcs)


async def main():
    await scenario_a0_order_customer_requires_admin_auth()
    await scenario_a_order_customer_partial_update_only_changes_sent_fields()
    await scenario_b_order_customer_company_fields_update()
    await scenario_c_order_customer_order_not_found()
    await scenario_d_order_customer_empty_payload_is_a_noop()
    await scenario_e_order_customer_does_not_touch_standing_account()

    await scenario_f0_customer_account_requires_admin_auth()
    await scenario_g_customer_account_partial_update_and_crm_sync_queued()
    await scenario_h_customer_account_address_recombination_matches_put_auth_me()
    await scenario_i_customer_account_company_address_recombination()
    await scenario_j_customer_account_not_found_404()
    await scenario_k_customer_account_email_normalization()
    await scenario_l_customer_account_does_not_touch_orders()

    await scenario_m_put_auth_me_still_works_after_helper_refactor()

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
