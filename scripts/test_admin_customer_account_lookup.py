"""
Focused, standalone check for GET /admin/customer-account
(admin_get_customer_account in server.py) - the new lookup CRM's "Conectează
cont web" feature uses to preview a webshop account (db.users) by exact
email before linking it to a CRM client.

Not a pytest suite - mirrors scripts/test_require_admin_bff_only.py's and
scripts/test_stock_checkout.py's approach (this repo still has no test
framework, tests/ dir, or pytest in requirements.txt). Uses mongomock +
mongomock-motor to swap in an in-memory Mongo double for server.client/db.

_require_admin itself is already covered end-to-end by
test_require_admin_bff_only.py, so it's monkeypatched here to a fixed-admin
stub - this script isolates the NEW logic in admin_get_customer_account
(email normalization, exact-match-only lookup, 404 vs found shape, rate
limiting), not the auth layer.

Run (from repo root): python scripts/test_admin_customer_account_lookup.py
"""
import asyncio
import os
import sys
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


def make_request():
    """admin_get_customer_account only ever passes `request` through to
    _require_admin (monkeypatched below to ignore it entirely) and never
    reads it directly itself, so an empty scope is enough."""
    return Request({"type": "http", "headers": [], "method": "GET", "path": "/admin/customer-account"})


def patch_require_admin(admin_id="admin-1", admin_email="staff@example.com"):
    """Replace server._require_admin with a stub that always succeeds,
    isolating the lookup logic under test from BFF-JWT auth (covered
    separately by test_require_admin_bff_only.py). Returns the original so
    callers can restore it."""
    original = server._require_admin

    async def _stub(request):
        return {"id": admin_id, "email": admin_email, "role": "admin"}

    server._require_admin = _stub
    return original


async def scenario_a_found_exact_match_returns_serialize_user_shape():
    """(a) an existing db.users doc, looked up by its exact stored email,
    returns exactly _serialize_user's field set - including that secrets
    (password_hash, tokens) are NOT present."""
    db = await fresh_db()
    original = patch_require_admin()
    try:
        await db.users.insert_one({
            "id": "u1",
            "email": "client@example.com",
            "password_hash": "should-never-leak",
            "tokens": [{"token": "should-never-leak-either"}],
            "name": "Ion Popescu",
            "phone": "0700000000",
            "is_company": True,
            "company_name": "Ion SRL",
            "created_at": server.datetime.utcnow(),
            "role": "customer",
        })
        result = await server.admin_get_customer_account(email="client@example.com", request=make_request())
        check("a) found: id matches", result.get("id") == "u1", result)
        check("a) found: email matches", result.get("email") == "client@example.com", result)
        check("a) found: name matches", result.get("name") == "Ion Popescu", result)
        check("a) found: company_name matches", result.get("company_name") == "Ion SRL", result)
        check("a) found: password_hash NOT present", "password_hash" not in result, result)
        check("a) found: tokens NOT present", "tokens" not in result, result)
        # Full expected key set per _serialize_user's current definition.
        # Includes consent_accepted_at/consent_terms_version (added for the
        # registration-consent/GDPR change) - present as None here since
        # this fixture doc predates that change and has neither key set.
        expected_keys = {
            "id", "email", "name", "phone", "address", "address_strada", "address_numar",
            "address_bloc", "address_scara", "address_ap", "city", "county", "postal_code",
            "is_company", "company_name", "cui", "reg_com", "administrator", "company_address",
            "company_address_strada", "company_address_numar", "company_address_bloc",
            "company_address_scara", "company_address_ap", "company_address_oras",
            "company_address_judet", "company_address_cod_postal", "is_shopify_customer",
            "created_at", "role", "consent_accepted_at", "consent_terms_version",
            "notify_news_email",
        }
        check("a) found: response keys exactly match _serialize_user's shape",
              set(result.keys()) == expected_keys,
              f"diff: {set(result.keys()) ^ expected_keys}")
    finally:
        server._require_admin = original


async def scenario_b_not_found_404():
    """(b) no matching account -> 404 with the specified detail message."""
    await fresh_db()
    original = patch_require_admin()
    try:
        try:
            await server.admin_get_customer_account(email="nobody@example.com", request=make_request())
            check("b) not found rejected", False, "no exception raised")
        except server.HTTPException as e:
            check("b) not found -> 404", e.status_code == 404, f"got {e.status_code}")
            check("b) not found detail message", e.detail == "Niciun cont web găsit cu acest email.", e.detail)
    finally:
        server._require_admin = original


async def scenario_c_email_normalization_case_and_whitespace():
    """(c) lookup normalizes the query the same way login/register do
    (.lower().strip()) - a differently-cased, padded input still matches a
    lowercase-stored account."""
    db = await fresh_db()
    original = patch_require_admin()
    try:
        await db.users.insert_one({
            "id": "u2",
            "email": "someone@example.com",
            "created_at": server.datetime.utcnow(),
        })
        result = await server.admin_get_customer_account(email="  SomeOne@Example.com  ", request=make_request())
        check("c) case/whitespace-insensitive match", result.get("id") == "u2", result)
    finally:
        server._require_admin = original


async def scenario_d_exact_only_no_fuzzy_match():
    """(d) a near-miss email (different local part) must NOT match - proves
    this is exact lookup only, no fuzzy/partial matching."""
    db = await fresh_db()
    original = patch_require_admin()
    try:
        await db.users.insert_one({
            "id": "u3",
            "email": "exact@example.com",
            "created_at": server.datetime.utcnow(),
        })
        try:
            await server.admin_get_customer_account(email="exact2@example.com", request=make_request())
            check("d) near-miss email rejected (no fuzzy match)", False, "no exception raised - unexpected match")
        except server.HTTPException as e:
            check("d) near-miss email -> 404 (no fuzzy match)", e.status_code == 404, f"got {e.status_code}")
    finally:
        server._require_admin = original


async def scenario_e_rate_limit_enforced():
    """(e) after ADMIN_ACTION_LIMIT (10) lookups within the window for the
    same admin, the next one is rejected with 429 - same
    ADMIN_ACTION_LIMIT/ADMIN_ACTION_WINDOW_SECONDS bucket style as this
    file's other admin-trigger endpoints. Uses a dedicated admin id so this
    scenario's hits can't be polluted by earlier scenarios' calls (they all
    used the same default admin id above) and clears any pre-existing
    bucket for that key defensively.
    """
    db = await fresh_db()
    admin_id = "rate-limit-test-admin"
    original = patch_require_admin(admin_id=admin_id)
    try:
        await db.users.insert_one({
            "id": "u4",
            "email": "ratelimit@example.com",
            "created_at": server.datetime.utcnow(),
        })
        server._rate_limiter._hits.pop(f"admin:customer-account-lookup:{admin_id}", None)
        for i in range(server.ADMIN_ACTION_LIMIT):
            await server.admin_get_customer_account(email="ratelimit@example.com", request=make_request())
        try:
            await server.admin_get_customer_account(email="ratelimit@example.com", request=make_request())
            check("e) over-limit lookup rejected", False, "no exception raised")
        except server.HTTPException as e:
            check("e) over-limit lookup -> 429", e.status_code == 429, f"got {e.status_code}")
            check("e) 429 carries Retry-After header", "Retry-After" in (e.headers or {}), e.headers)
    finally:
        server._require_admin = original
        server._rate_limiter._hits.pop(f"admin:customer-account-lookup:{admin_id}", None)


async def main():
    await scenario_a_found_exact_match_returns_serialize_user_shape()
    await scenario_b_not_found_404()
    await scenario_c_email_normalization_case_and_whitespace()
    await scenario_d_exact_only_no_fuzzy_match()
    await scenario_e_rate_limit_enforced()

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
