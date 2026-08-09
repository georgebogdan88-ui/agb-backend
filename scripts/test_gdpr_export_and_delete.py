"""
Focused, standalone check for the GDPR data-export and account-deletion
endpoints added to server.py:
  - GET  /auth/me/export        (export_current_user_data)
  - POST /auth/me/delete        (delete_current_user_account)
  - the is_deleted guard added to _authenticate_user (login rejection for
    deleted accounts)

Not a pytest suite - mirrors scripts/test_admin_customer_account_lookup.py's
and scripts/test_registration_consent.py's approach (this repo still has no
test framework, tests/ dir, or pytest in requirements.txt). Uses mongomock +
mongomock-motor to swap in an in-memory Mongo double for server.client/db,
and calls the endpoint functions directly (no real HTTP layer / FastAPI
TestClient - also not present in requirements.txt).

Covers:
  (a) export requires auth: no Authorization header, and an invalid/expired
      token, both -> 401.
  (b) export returns the right shape (profil/comenzi/utilaje/favorite) with
      real order/equipment/interest data included, a Content-Disposition
      download header with the expected filename, and does NOT leak another
      user's order into the response.
  (c) delete requires the CURRENT password: a wrong password -> 403,
      rejected, the account document is completely untouched (not even
      partially anonymized).
  (d) delete with the correct password anonymizes every field listed in the
      task (name/email/phone/address*/company*/password_hash), sets
      is_deleted/deleted_at, clears tokens[] - and does NOT touch db.orders
      for that user at all (order doc byte-for-byte unchanged).
  (e) a deleted account can no longer log in: the specific "cont șters"
      message (not the generic invalid-credentials one) fires before the
      password is even checked, AND the account is no longer reachable by
      its original email at all (freed up for reuse) - proven directly
      against db.users rather than by driving the Shopify-fallback network
      path.
  (f) a deleted account's historical order is still fully readable via the
      exact same query every staff-facing/CRM-facing path already uses
      (match by order.customer.email - see GET /auth/orders, GET
      /admin/clients/{id}) - proven directly since that field lives on the
      order, independent of and untouched by the db.users anonymization.

Run (from repo root): python scripts/test_gdpr_export_and_delete.py
"""
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta
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


def make_request(bearer_value=None, ip="203.0.113.1", path="/auth/me"):
    """Bare starlette Request built from a raw ASGI scope carrying only an
    Authorization + x-forwarded-for header - every endpoint under test only
    ever reads request.headers, so no app/route/HTTP server is needed."""
    headers = [(b"x-forwarded-for", ip.encode())]
    if bearer_value is not None:
        headers.append((b"authorization", f"Bearer {bearer_value}".encode()))
    return Request({"type": "http", "headers": headers, "method": "GET", "path": path})


def sample_order(order_id, email, name="Ion Popescu"):
    return {
        "id": order_id,
        "session_id": f"session-{order_id}",
        "items": [{"product_id": "p1", "product_name": "Filtru ulei", "price": 50.0, "quantity": 2}],
        "customer": {
            "name": name,
            "email": email,
            "phone": "0700000000",
            "address": "Str. Exemplu 1",
            "city": "Cluj-Napoca",
            "county": "Cluj",
            "postal_code": "400000",
            "notes": "",
            "is_company": False,
            "company_name": None,
            "cui": None,
            "reg_com": None,
            "administrator": None,
            "company_address_strada": None,
            "company_address_numar": None,
            "company_address_bloc": None,
            "company_address_scara": None,
            "company_address_ap": None,
            "company_address_oras": None,
            "company_address_judet": None,
            "company_address_cod_postal": None,
        },
        "subtotal": 100.0,
        "shipping": 25.0,
        "total": 125.0,
        "status": "pending",
        "payment_method": "ramburs",
        "created_at": datetime.utcnow(),
        "crm_synced": True,
        "crm_sync_error": None,
        "crm_sync_attempts": 1,
        "crm_items_dirty": False,
        "crm_items_sync_error": None,
        "crm_items_sync_attempts": 0,
    }


async def make_user(db, email="owner@example.com", password="parola123", user_id="u1"):
    token_doc = server._new_session_token_doc()
    user = {
        "id": user_id,
        "email": email,
        "password_hash": await server.hash_password(password),
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
        "created_at": datetime.utcnow() - timedelta(days=100),
        "consent_accepted_at": datetime.utcnow() - timedelta(days=100),
        "consent_terms_version": server.CURRENT_TERMS_VERSION,
        "equipment": [{
            "id": "eq1",
            "brand": "John Deere",
            "model": "6150R",
            "chassis_serial": "SN123",
            "engine_serial": None,
            "engine_type": None,
            "transmission_type": None,
            "front_axle_model": None,
            "features": ["4x4"],
            "created_at": datetime.utcnow(),
        }],
    }
    await db.users.insert_one(user)
    return user, token_doc["token"]


async def scenario_a_export_requires_auth():
    """(a) export requires auth: missing header and invalid token -> 401."""
    await fresh_db()
    try:
        await server.export_current_user_data(request=make_request(bearer_value=None))
        check("a) missing Authorization header rejected", False, "no exception raised")
    except server.HTTPException as e:
        check("a) missing Authorization header -> 401", e.status_code == 401, e.status_code)

    try:
        await server.export_current_user_data(request=make_request(bearer_value="not-a-real-token"))
        check("a) invalid token rejected", False, "no exception raised")
    except server.HTTPException as e:
        check("a) invalid token -> 401", e.status_code == 401, e.status_code)


async def scenario_b_export_shape_and_content():
    """(b) export returns profil/comenzi/utilaje/favorite with real data,
    a Content-Disposition download header, and never another user's order."""
    db = await fresh_db()
    user, token = await make_user(db, email="export-user@example.com", user_id="u-export")
    await db.orders.insert_one(sample_order("order-1", "export-user@example.com"))
    # A second user's order - must NOT show up in this export.
    await db.orders.insert_one(sample_order("order-other", "someone-else@example.com"))

    product = {
        "id": "p1", "title": "Filtru ulei", "price": 50.0, "currency": "RON",
        "image_url": "https://example.com/p1.jpg", "stock_status": "in_stock",
    }
    await db.shopify_products.insert_one(product)
    await db.customer_interests.insert_one({
        "id": "int-1", "user_id": "u-export", "product_id": "p1", "type": "favorite",
        "created_at": datetime.utcnow(), "crm_synced": False, "crm_sync_error": None, "crm_sync_attempts": 0,
    })

    response = await server.export_current_user_data(request=make_request(bearer_value=token))

    check("b) response is a Response with Content-Disposition attachment header",
          "attachment" in response.headers.get("content-disposition", ""),
          response.headers)
    check("b) filename matches date-personale-agb-{date}.json convention",
          f"date-personale-agb-{datetime.utcnow().strftime('%Y-%m-%d')}.json" in response.headers.get("content-disposition", ""),
          response.headers.get("content-disposition"))
    check("b) media type is application/json", response.media_type == "application/json", response.media_type)

    payload = json.loads(response.body)
    check("b) top-level keys present", set(payload.keys()) == {"profil", "comenzi", "utilaje", "favorite", "exportat_la"}, payload.keys())

    check("b) profil.email matches", payload["profil"]["email"] == "export-user@example.com", payload["profil"])
    check("b) profil has consent_terms_version key", "consent_terms_version" in payload["profil"], payload["profil"])
    check("b) profil has no password_hash leak", "password_hash" not in payload["profil"], payload["profil"])

    check("b) comenzi has exactly 1 order (not the other user's)", len(payload["comenzi"]) == 1, payload["comenzi"])
    order = payload["comenzi"][0]
    check("b) order id matches", order["id"] == "order-1", order)
    check("b) order has full items", order["items"] == sample_order("order-1", "x")["items"], order["items"])
    check("b) order has total/status/payment_method", order["total"] == 125.0 and order["status"] == "pending" and order["payment_method"] == "ramburs", order)

    check("b) utilaje has 1 equipment entry", len(payload["utilaje"]) == 1, payload["utilaje"])
    check("b) utilaje entry model matches", payload["utilaje"][0]["model"] == "6150R", payload["utilaje"][0])

    check("b) favorite has 1 entry", len(payload["favorite"]) == 1, payload["favorite"])
    check("b) favorite entry enriched with product_title", payload["favorite"][0]["product_title"] == "Filtru ulei", payload["favorite"][0])


async def scenario_c_delete_wrong_password_rejected():
    """(c) wrong password -> 403, account completely untouched."""
    db = await fresh_db()
    user, token = await make_user(db, email="wrongpass@example.com", user_id="u-wrongpass")

    try:
        await server.delete_current_user_account(
            request=make_request(bearer_value=token),
            delete_data=server.AccountDeleteRequest(password="not-the-real-password"),
        )
        check("c) wrong password rejected", False, "no exception raised")
    except server.HTTPException as e:
        check("c) wrong password -> 403", e.status_code == 403, e.status_code)

    raw = await db.users.find_one({"id": "u-wrongpass"})
    check("c) email untouched after failed attempt", raw["email"] == "wrongpass@example.com", raw["email"])
    check("c) name untouched after failed attempt", raw["name"] == "Ion Popescu", raw["name"])
    check("c) is_deleted NOT set after failed attempt", not raw.get("is_deleted"), raw.get("is_deleted"))
    check("c) tokens NOT cleared after failed attempt", len(raw["tokens"]) == 1, raw["tokens"])


async def scenario_d_delete_success_anonymizes_and_leaves_orders_alone():
    """(d) correct password -> every listed field anonymized/cleared,
    is_deleted/deleted_at set, tokens cleared - and db.orders is completely
    untouched (byte-for-byte, including customer.email)."""
    db = await fresh_db()
    user, token = await make_user(db, email="deleteme@example.com", user_id="u-delete", password="correcthorse1")
    await db.orders.insert_one(sample_order("order-del-1", "deleteme@example.com"))
    # Re-fetch both docs post-insert (rather than comparing against the
    # original in-memory dicts) so the "before" snapshot has already gone
    # through the same BSON millisecond-precision datetime rounding that
    # mongomock applies on write - otherwise a byte-for-byte compare against
    # the pre-insert in-memory dict spuriously fails on microsecond noise
    # that has nothing to do with the delete endpoint under test.
    user_before = await db.users.find_one({"id": "u-delete"})
    order_before = await db.orders.find_one({"id": "order-del-1"})

    result = await server.delete_current_user_account(
        request=make_request(bearer_value=token),
        delete_data=server.AccountDeleteRequest(password="correcthorse1"),
    )
    check("d) delete returns success message", "message" in result, result)

    raw = await db.users.find_one({"id": "u-delete"})
    check("d) name -> anonymized placeholder", raw["name"] == "Cont șters", raw["name"])
    check("d) email anonymized to deleted-*@deleted.local", raw["email"].startswith("deleted-") and raw["email"].endswith("@deleted.local"), raw["email"])
    check("d) email no longer the original", raw["email"] != "deleteme@example.com", raw["email"])
    for field in ("phone", "address", "address_strada", "address_numar", "address_bloc",
                  "address_scara", "address_ap", "city", "county", "postal_code",
                  "company_name", "cui", "reg_com", "administrator", "company_address",
                  "company_address_strada", "company_address_numar", "company_address_bloc",
                  "company_address_scara", "company_address_ap", "company_address_oras",
                  "company_address_judet", "company_address_cod_postal"):
        check(f"d) {field} cleared to None", raw.get(field) is None, raw.get(field))
    check("d) password_hash changed from the original", raw["password_hash"] != user_before["password_hash"], raw["password_hash"])
    check("d) old password no longer verifies against the new hash", not await server.verify_password("correcthorse1", raw["password_hash"]), "old password still verifies!")
    check("d) is_deleted is True", raw.get("is_deleted") is True, raw.get("is_deleted"))
    check("d) deleted_at is a recent datetime", isinstance(raw.get("deleted_at"), datetime), raw.get("deleted_at"))
    check("d) tokens cleared", raw.get("tokens") == [], raw.get("tokens"))
    check("d) consent_accepted_at left untouched", raw.get("consent_accepted_at") == user_before["consent_accepted_at"], raw.get("consent_accepted_at"))
    check("d) consent_terms_version left untouched", raw.get("consent_terms_version") == user_before["consent_terms_version"], raw.get("consent_terms_version"))

    order_after = await db.orders.find_one({"id": "order-del-1"})
    check("d) order document completely unchanged", order_after == order_before,
          {"before": order_before, "after": order_after})
    check("d) order.customer.email still the ORIGINAL email (untouched)",
          order_after["customer"]["email"] == "deleteme@example.com", order_after["customer"]["email"])


async def scenario_e_deleted_account_cannot_login():
    """(e) deleted account cannot log in: is_deleted guard fires with the
    specific message (not the generic wrong-credentials one) before the
    password is even checked, and the ORIGINAL email no longer resolves to
    any account at all (freed up for a brand new registration) - checked
    directly against db.users rather than by driving login_user's
    Shopify-fallback network path for the now-unmatched original email."""
    db = await fresh_db()
    user, token = await make_user(db, email="loginafterdelete@example.com", user_id="u-login-del", password="correcthorse1")
    await server.delete_current_user_account(
        request=make_request(bearer_value=token),
        delete_data=server.AccountDeleteRequest(password="correcthorse1"),
    )
    raw = await db.users.find_one({"id": "u-login-del"})
    anonymized_email = raw["email"]

    check("e) original email no longer resolves to any account",
          await db.users.find_one({"email": "loginafterdelete@example.com"}) is None, "still found by old email")

    # Even a request that DOES resolve the doc (by its new anonymized email)
    # is rejected with the specific "cont șters" message, not a generic one -
    # proving the is_deleted guard fires before the (also-now-unusable)
    # password check.
    try:
        await server._authenticate_user(anonymized_email, "correcthorse1", make_request(ip="203.0.113.50"))
        check("e) login on deleted account rejected", False, "no exception raised - login unexpectedly succeeded")
    except server.HTTPException as e:
        check("e) login on deleted account -> 403", e.status_code == 403, e.status_code)
        check("e) login rejection message mentions the account-deleted wording (specific, not generic)",
              "șters" in e.detail, e.detail)
        check("e) login rejection message is NOT the generic wrong-credentials one",
              e.detail != "Email sau parolă incorectă", e.detail)

    # Old session token is dead too (tokens[] was cleared by delete).
    try:
        await server.get_current_user(request=make_request(bearer_value=token))
        check("e) old session token rejected after delete", False, "no exception raised - old token still works")
    except server.HTTPException as e:
        check("e) old session token -> 401 after delete", e.status_code == 401, e.status_code)


async def scenario_f_order_still_readable_via_staff_facing_query():
    """(f) the deleted account's historical order is still fully readable
    via the exact query every staff-facing/CRM-facing path already uses
    (customer.email match on the order itself - see GET /auth/orders' and
    GET /admin/clients/{id}'s identical `{"customer.email": ...}` filter),
    proving deletion never touched db.orders."""
    db = await fresh_db()
    user, token = await make_user(db, email="staffcheck@example.com", user_id="u-staffcheck", password="correcthorse1")
    await db.orders.insert_one(sample_order("order-staff-1", "staffcheck@example.com"))

    await server.delete_current_user_account(
        request=make_request(bearer_value=token),
        delete_data=server.AccountDeleteRequest(password="correcthorse1"),
    )

    # Same filter GET /auth/orders and GET /admin/clients/{id} use.
    staff_visible_orders = await db.orders.find({"customer.email": "staffcheck@example.com"}).sort("created_at", -1).to_list(100)
    check("f) order still found by its own (unchanged) customer.email after account deletion",
          len(staff_visible_orders) == 1 and staff_visible_orders[0]["id"] == "order-staff-1",
          staff_visible_orders)
    check("f) order status/total/items intact", staff_visible_orders[0]["total"] == 125.0 and staff_visible_orders[0]["status"] == "pending", staff_visible_orders[0])


async def scenario_g_deleted_account_shopify_token_and_equipment_cleared():
    """(g) regression test for a review-caught bypass: a deleted account's
    stored Shopify customer access token used to survive deletion untouched,
    and _find_user_by_token(allow_shopify_access_token=True) - used by
    equipment/interests/favorites/order-history endpoints - never checked
    is_deleted at all. Together, a "deleted" account was still fully
    readable/writable via that old token. Both are fixed now: the token
    field is cleared on delete, AND _find_user_by_token itself refuses to
    resolve ANY deleted account regardless of which credential matched."""
    db = await fresh_db()
    user, token = await make_user(db, email="shopifylink@example.com", user_id="u-shopify", password="correcthorse1")
    await db.users.update_one({"id": "u-shopify"}, {"$set": {
        "shopify_access_token": "shpat_fake_customer_token",
        "shopify_customer_id": "gid://shopify/Customer/123",
        "is_shopify_customer": True,
    }})

    await server.delete_current_user_account(
        request=make_request(bearer_value=token),
        delete_data=server.AccountDeleteRequest(password="correcthorse1"),
    )

    raw = await db.users.find_one({"id": "u-shopify"})
    check("g) shopify_access_token cleared on delete", raw.get("shopify_access_token") is None, raw.get("shopify_access_token"))
    check("g) shopify_customer_id cleared on delete", raw.get("shopify_customer_id") is None, raw.get("shopify_customer_id"))
    check("g) is_shopify_customer cleared to False on delete", raw.get("is_shopify_customer") is False, raw.get("is_shopify_customer"))
    check("g) equipment cleared on delete", raw.get("equipment") == [], raw.get("equipment"))

    # Even if an old Shopify token were somehow still valid (defense in
    # depth - it isn't, per the check above), _find_user_by_token itself
    # must now refuse to resolve a deleted account on ANY credential.
    resolved = await server._find_user_by_token("shpat_fake_customer_token", allow_shopify_access_token=True)
    check("g) _find_user_by_token refuses a deleted account even via shopify_access_token", resolved is None, resolved)


async def scenario_h_delete_rate_limited():
    """(h) repeated wrong-password attempts against the delete endpoint are
    rate-limited (previously unlimited, unlike every other password-
    verification endpoint in this codebase)."""
    db = await fresh_db()
    user, token = await make_user(db, email="ratelimit@example.com", user_id="u-ratelimit", password="correcthorse1")

    rejected_with_429 = False
    for _ in range(server.ACCOUNT_DELETE_LIMIT + 3):
        try:
            await server.delete_current_user_account(
                request=make_request(bearer_value=token),
                delete_data=server.AccountDeleteRequest(password="wrong-password"),
            )
        except server.HTTPException as e:
            if e.status_code == 429:
                rejected_with_429 = True
                break
            check("h) attempt rejected as wrong password (403), not something else", e.status_code == 403, e.status_code)

    check("h) repeated wrong-password attempts eventually rate-limited (429)", rejected_with_429, "never hit 429")

    raw = await db.users.find_one({"id": "u-ratelimit"})
    check("h) account still not deleted after all failed attempts", not raw.get("is_deleted"), raw.get("is_deleted"))


async def main():
    await scenario_a_export_requires_auth()
    await scenario_b_export_shape_and_content()
    await scenario_c_delete_wrong_password_rejected()
    await scenario_d_delete_success_anonymizes_and_leaves_orders_alone()
    await scenario_e_deleted_account_cannot_login()
    await scenario_f_order_still_readable_via_staff_facing_query()
    await scenario_g_deleted_account_shopify_token_and_equipment_cleared()
    await scenario_h_delete_rate_limited()

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
