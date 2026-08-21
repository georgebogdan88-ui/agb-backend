"""
Focused, standalone check for the two new Shopify -> local-account migration
admin endpoints in server.py:

  - GET  /admin/customers/shopify-migration-status
        (admin_shopify_migration_status / _get_shopify_customers_total_count)
  - POST /admin/customers/migrate-shopify-bulk
        (admin_migrate_shopify_bulk / _run_shopify_migration_bulk_send /
        send_shopify_migration_email)

Built at George's explicit request (2026-08-22) to track/drive migration off
the legacy Shopify-login fallback (_legacy_shopify_login_and_migrate) ahead
of Shopify's traffic cutover - migrate-shopify-bulk itself is NEVER
triggered automatically, only from this explicit staff-facing endpoint.

Not a pytest suite - same mongomock/mongomock-motor + direct-function-call
approach, and the same real starlette.background.BackgroundTasks()-then-
await-it technique for exercising the queued fire-and-forget send, as
scripts/test_restock_auto_notify.py and scripts/test_order_confirmation_
email.py. Brevo's send_transac_email and Shopify's customers/count.json call
are both monkeypatched - this script NEVER sends a real email or makes a
real network call.

Covers:
  (a) shopify-migration-status computes all three numbers correctly: live
      Shopify total (mocked), migrated_count (db.clients matched by email to
      a db.users doc with password_hash actually set), pending_count as
      their difference.
  (b) shopify-migration-status requires admin auth (no Authorization header
      -> 401, endpoint never reached).
  (c) migrate-shopify-bulk requires admin auth (same check).
  (d) migrate-shopify-bulk: creates a new (unconfigured, no password_hash)
      db.users account only for clients missing one entirely, reuses the
      existing doc for clients who already have an account with no
      password_hash yet, and completely skips (not queued, no send
      attempted) a client whose account already has password_hash set -
      "queued" in the response matches exactly the two eligible clients.
  (e) migrate-shopify-bulk: a client whose existing account already has
      migration_email_sent_at set is excluded up front (queued does not
      count them, no resend, no spam on repeated clicks).
  (f) migrate-shopify-bulk: one recipient's send failing (simulated Brevo
      outage) does not stop the rest of the batch - the other candidates
      still get emailed and marked sent; the failed one's account IS still
      created (so it's picked up and retried on the next click) but is NOT
      marked migration_email_sent_at (since nothing was actually sent).

Run (from repo root): python scripts/test_shopify_migration_status_and_bulk.py
"""
import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("MONGO_URL", "mongodb://fake-for-import-only/")
os.environ.setdefault("DB_NAME", "fake_db_for_import_only")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mongomock_motor  # noqa: E402
from starlette.background import BackgroundTasks  # noqa: E402
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


def make_request(method="GET", path="/admin/customers/shopify-migration-status", with_auth=False):
    headers = [(b"authorization", b"Bearer fake.token.here")] if with_auth else []
    return Request({"type": "http", "headers": headers, "method": method, "path": path})


def patch_require_admin(admin_id="admin-1", admin_email="staff@example.com"):
    original = server._require_admin

    async def _stub(request):
        return {"id": admin_id, "email": admin_email, "role": "admin"}

    server._require_admin = _stub
    return original


def restore_require_admin(original):
    server._require_admin = original


class _FakeShopifyCountResponse:
    def __init__(self, count, status_code=200):
        self._count = count
        self.status_code = status_code
        self.text = "error" if status_code != 200 else ""

    def json(self):
        return {"count": self._count}


def patch_shopify_customers_count(count):
    """Patches server.httpx.AsyncClient.get so
    _get_shopify_customers_total_count sees a canned Shopify response - no
    real network call ever made. Returns the patcher context manager (use as
    `with patch_shopify_customers_count(N):`)."""
    return patch.object(
        server.httpx.AsyncClient, "get",
        new=AsyncMock(return_value=_FakeShopifyCountResponse(count)),
    )


def patch_brevo_send(behavior):
    import sib_api_v3_sdk

    original = sib_api_v3_sdk.TransactionalEmailsApi.send_transac_email

    def _stub(self, send_smtp_email, **kwargs):
        return behavior(send_smtp_email)

    sib_api_v3_sdk.TransactionalEmailsApi.send_transac_email = _stub
    return original


def restore_brevo_send(original):
    import sib_api_v3_sdk
    sib_api_v3_sdk.TransactionalEmailsApi.send_transac_email = original


def make_client(*, id, email, name="", phone="", address="", city="", county="", postal_code=""):
    return {
        "id": id,
        "shopify_customer_id": id,
        "name": name,
        "email": email,
        "email_normalized": email.strip().lower(),
        "phone": phone,
        "address": {"address": address, "city": city, "county": county, "postal_code": postal_code},
        "source": "shopify",
    }


async def scenario_a_status_computes_three_numbers():
    """(a) total_shopify_customers from the (mocked) live Shopify call,
    migrated_count only counting db.clients rows whose matching db.users doc
    has password_hash actually set, pending_count as the difference."""
    db = await fresh_db()
    original_admin = patch_require_admin()
    try:
        await db.clients.insert_many([
            make_client(id="c1", email="migrated1@example.com"),
            make_client(id="c2", email="migrated2@example.com"),
            make_client(id="c3", email="pending@example.com"),
            make_client(id="c4", email="unconfigured-account@example.com"),
        ])
        # c1, c2: fully migrated (password_hash set)
        await db.users.insert_one({"id": "u1", "email": "migrated1@example.com", "password_hash": "hash1"})
        await db.users.insert_one({"id": "u2", "email": "migrated2@example.com", "password_hash": "hash2"})
        # c4: account exists but no usable password yet -> must NOT count as migrated
        await db.users.insert_one({"id": "u4", "email": "unconfigured-account@example.com"})
        # c3: no db.users doc at all

        with patch_shopify_customers_count(300):
            result = await server.admin_shopify_migration_status(request=make_request())

        check("a) total_shopify_customers from live Shopify mock", result["total_shopify_customers"] == 300, result)
        check("a) migrated_count only counts password_hash-set accounts", result["migrated_count"] == 2, result)
        check("a) pending_count = total - migrated", result["pending_count"] == 298, result)
        check("a) response has exactly the 3 documented fields",
              set(result.keys()) == {"total_shopify_customers", "migrated_count", "pending_count"}, result)
    finally:
        restore_require_admin(original_admin)


async def scenario_b_status_requires_admin():
    """(b) no Authorization header -> 401, _require_admin's own gate, never
    reaches the migration-status logic."""
    await fresh_db()
    try:
        await server.admin_shopify_migration_status(request=make_request(with_auth=False))
        check("b) status endpoint without auth rejected", False, "no exception raised")
    except server.HTTPException as e:
        check("b) status endpoint without auth -> 401", e.status_code == 401, f"got {e.status_code}")


async def scenario_c_migrate_bulk_requires_admin():
    """(c) same admin gate on the POST bulk-migrate endpoint."""
    await fresh_db()
    try:
        await server.admin_migrate_shopify_bulk(
            request=make_request(method="POST", path="/admin/customers/migrate-shopify-bulk", with_auth=False),
            background_tasks=BackgroundTasks(),
        )
        check("c) migrate-bulk endpoint without auth rejected", False, "no exception raised")
    except server.HTTPException as e:
        check("c) migrate-bulk endpoint without auth -> 401", e.status_code == 401, f"got {e.status_code}")


async def scenario_d_creates_missing_accounts_only_for_eligible_clients():
    """(d) c1 has no db.users doc at all -> gets created (no password_hash)
    and emailed. c2 already has a db.users doc with no password_hash -> that
    existing doc is reused (not duplicated) and emailed. c3 already has
    password_hash set -> completely excluded, no account touched, no email
    attempted. queued == 2."""
    db = await fresh_db()
    original_admin = patch_require_admin()
    server.BREVO_API_KEY = "fake-test-key"
    sent_to = []
    original_send = patch_brevo_send(lambda email: sent_to.append(email.to[0]["email"]))
    try:
        await db.clients.insert_many([
            make_client(id="c1", email="brandnew@example.com", name="Ion Nou", phone="0711111111",
                        address="Str. A 1", city="Iași", county="Iași", postal_code="700000"),
            make_client(id="c2", email="partial@example.com", name="Maria Parțial"),
            make_client(id="c3", email="already-migrated@example.com", name="Deja Migrat"),
        ])
        await db.users.insert_one({"id": "u2", "email": "partial@example.com", "name": "Maria Parțial"})
        await db.users.insert_one({
            "id": "u3", "email": "already-migrated@example.com", "password_hash": "real-hash",
        })

        bt = BackgroundTasks()
        result = await server.admin_migrate_shopify_bulk(
            request=make_request(method="POST", path="/admin/customers/migrate-shopify-bulk", with_auth=True),
            background_tasks=bt,
        )
        check("d) queued counts only the 2 eligible clients", result == {"queued": 2}, result)
        await bt()  # run _run_shopify_migration_bulk_send

        check("d) both eligible recipients emailed",
              sorted(sent_to) == ["brandnew@example.com", "partial@example.com"], sent_to)

        new_user = await db.users.find_one({"email": "brandnew@example.com"})
        check("d) new account created for c1", new_user is not None, new_user)
        if new_user:
            check("d) new account has no usable password_hash", not new_user.get("password_hash"), new_user)
            check("d) new account carries client's name", new_user.get("name") == "Ion Nou", new_user)
            check("d) new account carries client's phone", new_user.get("phone") == "0711111111", new_user)
            check("d) new account marked migration_email_sent_at", new_user.get("migration_email_sent_at") is not None, new_user)
            check("d) new account has a reset_token set", bool(new_user.get("reset_token")), new_user)
            expires = new_user.get("reset_token_expires")
            ttl_days = (expires - server.datetime.utcnow()).days if expires else None
            check("d) reset token expiry is ~30 days out (not the usual 1h)",
                  ttl_days is not None and 28 <= ttl_days <= 30, ttl_days)

        reused_user = await db.users.find_one({"email": "partial@example.com"})
        check("d) c2's existing account id unchanged (reused, not duplicated)", reused_user.get("id") == "u2", reused_user)
        check("d) c2 marked migration_email_sent_at", reused_user.get("migration_email_sent_at") is not None, reused_user)

        untouched_user = await db.users.find_one({"email": "already-migrated@example.com"})
        check("d) already-migrated account untouched (no migration_email_sent_at added)",
              untouched_user.get("migration_email_sent_at") is None, untouched_user)

        all_users_count = await db.users.count_documents({})
        check("d) no extra/duplicate accounts created", all_users_count == 3, all_users_count)
    finally:
        restore_brevo_send(original_send)
        server.BREVO_API_KEY = ""
        restore_require_admin(original_admin)


async def scenario_e_no_resend_when_already_sent():
    """(e) A client whose existing account already has migration_email_
    sent_at set is excluded up front - queued excludes them entirely, and
    no email is attempted (safe to click the button again with no spam)."""
    db = await fresh_db()
    original_admin = patch_require_admin()
    server.BREVO_API_KEY = "fake-test-key"
    sent_to = []
    original_send = patch_brevo_send(lambda email: sent_to.append(email.to[0]["email"]))
    try:
        await db.clients.insert_one(make_client(id="c1", email="already-emailed@example.com"))
        await db.users.insert_one({
            "id": "u1", "email": "already-emailed@example.com",
            "migration_email_sent_at": server.datetime.utcnow(),
        })

        bt = BackgroundTasks()
        result = await server.admin_migrate_shopify_bulk(
            request=make_request(method="POST", path="/admin/customers/migrate-shopify-bulk", with_auth=True),
            background_tasks=bt,
        )
        check("e) already-emailed client excluded -> queued == 0", result == {"queued": 0}, result)
        await bt()
        check("e) no email attempted (no spam on repeated click)", sent_to == [], sent_to)
    finally:
        restore_brevo_send(original_send)
        server.BREVO_API_KEY = ""
        restore_require_admin(original_admin)


async def scenario_f_one_failed_send_does_not_block_the_rest():
    """(f) Three brand-new candidates, Brevo raises only for the second ->
    the first and third are still created + emailed + marked sent. The
    second's account IS still created (so a later click can retry it), but
    is NOT marked migration_email_sent_at, since nothing actually went out."""
    db = await fresh_db()
    original_admin = patch_require_admin()
    server.BREVO_API_KEY = "fake-test-key"
    sent_to = []

    def _behavior(email):
        to_addr = email.to[0]["email"]
        if to_addr == "b@example.com":
            raise RuntimeError("simulated Brevo outage for this one recipient")
        sent_to.append(to_addr)

    original_send = patch_brevo_send(_behavior)
    try:
        await db.clients.insert_many([
            make_client(id="c1", email="a@example.com"),
            make_client(id="c2", email="b@example.com"),
            make_client(id="c3", email="c@example.com"),
        ])

        bt = BackgroundTasks()
        result = await server.admin_migrate_shopify_bulk(
            request=make_request(method="POST", path="/admin/customers/migrate-shopify-bulk", with_auth=True),
            background_tasks=bt,
        )
        check("f) all 3 new candidates queued", result == {"queued": 3}, result)
        await bt()

        check("f) unaffected recipients still emailed despite one failure",
              sorted(sent_to) == ["a@example.com", "c@example.com"], sent_to)

        ok_a = await db.users.find_one({"email": "a@example.com"})
        ok_c = await db.users.find_one({"email": "c@example.com"})
        failed_b = await db.users.find_one({"email": "b@example.com"})
        check("f) a) account created + marked sent", ok_a is not None and ok_a.get("migration_email_sent_at") is not None, ok_a)
        check("f) c) account created + marked sent", ok_c is not None and ok_c.get("migration_email_sent_at") is not None, ok_c)
        check("f) b) account still created despite send failure (retryable later)", failed_b is not None, failed_b)
        check("f) b) NOT marked migration_email_sent_at (nothing was actually sent)",
              failed_b is not None and failed_b.get("migration_email_sent_at") is None, failed_b)
    finally:
        restore_brevo_send(original_send)
        server.BREVO_API_KEY = ""
        restore_require_admin(original_admin)


async def main():
    await scenario_a_status_computes_three_numbers()
    await scenario_b_status_requires_admin()
    await scenario_c_migrate_bulk_requires_admin()
    await scenario_d_creates_missing_accounts_only_for_eligible_clients()
    await scenario_e_no_resend_when_already_sent()
    await scenario_f_one_failed_send_does_not_block_the_rest()

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
