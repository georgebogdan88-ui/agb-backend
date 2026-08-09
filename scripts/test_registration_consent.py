"""
Focused, standalone check for the GDPR registration-consent fields added to
POST /auth/register (register_user in server.py):
  - UserRegister.terms_accepted (new required-in-practice request field)
  - db.users.consent_accepted_at / consent_terms_version (new stored fields)
  - _serialize_user() exposing both (so /auth/me, /auth/login, PUT /auth/me
    all surface them)

Not a pytest suite - mirrors scripts/test_admin_customer_account_lookup.py's
and scripts/test_stock_checkout.py's approach (this repo still has no test
framework, tests/ dir, or pytest in requirements.txt). Uses mongomock +
mongomock-motor to swap in an in-memory Mongo double for server.client/db,
and calls server.register_user()/server.login_user() directly (no real HTTP
layer / FastAPI TestClient - also not present in requirements.txt).

Calling register_user directly like this means FastAPI never actually runs
the queued BackgroundTasks (that only happens via the real ASGI response
cycle) - so sync_account_to_crm is never invoked here, and doesn't need to
be patched out for these scenarios.

Covers:
  (a) registration with terms_accepted=True succeeds, stores
      consent_accepted_at (~now, UTC) and consent_terms_version ==
      server.CURRENT_TERMS_VERSION on the raw db.users doc, and
      _serialize_user()/GET-style shape (via login) surfaces both.
  (b) registration with terms_accepted=False is rejected with HTTP 400 and
      the exact Romanian message, and creates NO user document at all.
  (c) registration with terms_accepted omitted entirely (old-style
      webshop/mobile request body predating this change) is rejected the
      same way as (b) - same status/message, no user created.
  (d) a pre-existing user doc with no consent_accepted_at/
      consent_terms_version keys at all (simulates an account created
      before this change) can still log in normally, and _serialize_user's
      output has both keys present but set to None - proving the change
      doesn't break existing accounts or force re-consent at login.

Run (from repo root): python scripts/test_registration_consent.py
"""
import asyncio
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


def make_request(ip="203.0.113.1"):
    """register_user only reads request via _client_ip (x-forwarded-for
    header) for rate limiting - give each scenario a distinct IP so the
    5/hour REGISTER_IP_LIMIT bucket never interferes between scenarios."""
    return Request({
        "type": "http",
        "headers": [(b"x-forwarded-for", ip.encode())],
        "method": "POST",
        "path": "/auth/register",
    })


async def scenario_a_terms_accepted_true_succeeds_and_stores_consent():
    """(a) terms_accepted=True succeeds; consent_accepted_at/
    consent_terms_version are stored correctly and exposed via
    _serialize_user (checked here through login_user's response, which
    calls _serialize_user internally)."""
    db = await fresh_db()
    before = datetime.utcnow()
    result = await server.register_user(
        server.UserRegister(
            email="consent-yes@example.com",
            password="parola123",
            name="Ion Popescu",
            phone="0700000001",
            terms_accepted=True,
        ),
        background_tasks=server.BackgroundTasks(),
        request=make_request("203.0.113.10"),
    )
    after = datetime.utcnow()

    check("a) register_user returns a token", bool(result.get("token")), result)

    raw = await db.users.find_one({"email": "consent-yes@example.com"})
    check("a) user document was created", raw is not None)
    check(
        "a) consent_terms_version stored == server.CURRENT_TERMS_VERSION",
        raw.get("consent_terms_version") == server.CURRENT_TERMS_VERSION,
        raw.get("consent_terms_version"),
    )
    check(
        "a) consent_terms_version literal value is 2026-08-09",
        raw.get("consent_terms_version") == "2026-08-09",
        raw.get("consent_terms_version"),
    )
    stored_ts = raw.get("consent_accepted_at")
    check("a) consent_accepted_at is a datetime", isinstance(stored_ts, datetime), stored_ts)
    check(
        "a) consent_accepted_at is within [before, after] of the call (UTC now)",
        before - timedelta(seconds=2) <= stored_ts <= after + timedelta(seconds=2),
        f"before={before} stored={stored_ts} after={after}",
    )
    check(
        "a) consent_accepted_at == created_at (set together)",
        stored_ts == raw.get("created_at"),
        f"consent_accepted_at={stored_ts} created_at={raw.get('created_at')}",
    )

    # Confirm _serialize_user (and therefore /auth/me, /auth/login, PUT
    # /auth/me) surfaces both new fields, via a real login_user() call.
    login_result = await server.login_user(
        server.UserLogin(email="consent-yes@example.com", password="parola123"),
        request=make_request("203.0.113.11"),
    )
    user_shape = login_result["user"]
    check("a) serialized user has consent_accepted_at key", "consent_accepted_at" in user_shape, user_shape.keys())
    check("a) serialized user has consent_terms_version key", "consent_terms_version" in user_shape, user_shape.keys())
    check(
        "a) serialized consent_terms_version matches stored value",
        user_shape["consent_terms_version"] == server.CURRENT_TERMS_VERSION,
        user_shape["consent_terms_version"],
    )
    check(
        "a) serialized consent_accepted_at matches stored value",
        user_shape["consent_accepted_at"] == stored_ts,
        user_shape["consent_accepted_at"],
    )


async def scenario_b_terms_accepted_false_rejected_no_user_created():
    """(b) terms_accepted explicitly False -> HTTP 400 with the exact
    Romanian message, and no user document is created."""
    db = await fresh_db()
    try:
        await server.register_user(
            server.UserRegister(
                email="consent-no@example.com",
                password="parola123",
                name="Maria Ionescu",
                phone="0700000002",
                terms_accepted=False,
            ),
            background_tasks=server.BackgroundTasks(),
            request=make_request("203.0.113.20"),
        )
        check("b) HTTPException raised for terms_accepted=False", False, "no exception raised")
    except server.HTTPException as e:
        check("b) status code is 400", e.status_code == 400, e.status_code)
        check(
            "b) exact Romanian consent error message",
            e.detail == "Trebuie să accepți Termenii și Politica de confidențialitate.",
            e.detail,
        )
    raw = await db.users.find_one({"email": "consent-no@example.com"})
    check("b) no user document was created", raw is None, raw)


async def scenario_c_terms_accepted_omitted_rejected_no_user_created():
    """(c) terms_accepted omitted entirely from the request (old-style
    request body) -> same rejection as (b), defaulting to False rather than
    tripping FastAPI's generic 422 for a missing required field."""
    db = await fresh_db()
    try:
        await server.register_user(
            server.UserRegister(
                email="consent-omitted@example.com",
                password="parola123",
                name="Vasile Georgescu",
                phone="0700000003",
                # terms_accepted intentionally omitted
            ),
            background_tasks=server.BackgroundTasks(),
            request=make_request("203.0.113.30"),
        )
        check("c) HTTPException raised for omitted terms_accepted", False, "no exception raised")
    except server.HTTPException as e:
        check("c) status code is 400", e.status_code == 400, e.status_code)
        check(
            "c) exact Romanian consent error message",
            e.detail == "Trebuie să accepți Termenii și Politica de confidențialitate.",
            e.detail,
        )
    raw = await db.users.find_one({"email": "consent-omitted@example.com"})
    check("c) no user document was created", raw is None, raw)


async def scenario_d_existing_user_without_consent_fields_still_logs_in():
    """(d) a pre-existing account (created before this change - no
    consent_accepted_at/consent_terms_version keys at all on the doc) can
    still log in normally; _serialize_user surfaces both keys as None
    rather than erroring (no KeyError from user["..."] access)."""
    db = await fresh_db()
    password_hash = await server.hash_password("oldpassword1")
    await db.users.insert_one({
        "id": "legacy-user-1",
        "email": "legacy@example.com",
        "password_hash": password_hash,
        "name": "Cont Vechi",
        "phone": "0700000004",
        "is_company": False,
        "is_shopify_customer": False,
        "created_at": datetime.utcnow() - timedelta(days=400),
        "tokens": [],
        # deliberately NO consent_accepted_at / consent_terms_version keys
    })

    login_result = await server.login_user(
        server.UserLogin(email="legacy@example.com", password="oldpassword1"),
        request=make_request("203.0.113.40"),
    )
    check("d) legacy user login succeeds", bool(login_result.get("token")), login_result)
    user_shape = login_result["user"]
    check("d) serialized user has consent_accepted_at key (present, None)", "consent_accepted_at" in user_shape, user_shape)
    check("d) consent_accepted_at is None for legacy user", user_shape["consent_accepted_at"] is None, user_shape["consent_accepted_at"])
    check("d) serialized user has consent_terms_version key (present, None)", "consent_terms_version" in user_shape, user_shape)
    check("d) consent_terms_version is None for legacy user", user_shape["consent_terms_version"] is None, user_shape["consent_terms_version"])

    raw = await db.users.find_one({"email": "legacy@example.com"})
    check("d) raw legacy doc still has no consent keys (not backfilled)", "consent_accepted_at" not in raw, raw.keys())
    check("d) raw legacy doc still has no consent_terms_version key (not backfilled)", "consent_terms_version" not in raw, raw.keys())


async def main():
    await scenario_a_terms_accepted_true_succeeds_and_stores_consent()
    await scenario_b_terms_accepted_false_rejected_no_user_created()
    await scenario_c_terms_accepted_omitted_rejected_no_user_created()
    await scenario_d_existing_user_without_consent_fields_still_logs_in()

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
