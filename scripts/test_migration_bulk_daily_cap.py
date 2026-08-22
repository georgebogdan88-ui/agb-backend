"""
Focused, standalone check for the DAILY_MIGRATION_EMAIL_CAP throttle added to
POST /admin/customers/migrate-shopify-bulk (admin_migrate_shopify_bulk) on
2026-08-22, at George's explicit request: no matter how many times (or with
how many eligible candidates) staff presses the "migrate" button in CRM, at
most DAILY_MIGRATION_EMAIL_CAP (50) migration emails may actually go out in
any rolling 24h window - counted directly off the existing
db.users.migration_email_sent_at field (already the per-recipient anti-
resend marker), NOT a new "day number"/counter document.

Sibling to scripts/test_shopify_migration_status_and_bulk.py, which already
covers base eligibility (password_hash set / migration_email_sent_at set ->
excluded), auth gating, and per-recipient send-failure isolation - this file
does not re-derive those from scratch beyond one explicit regression check
(scenario f) and instead focuses purely on the new cap arithmetic:

  (a) 0 sent in the last 24h, 80 eligible candidates -> exactly 50 sent,
      queued=50, eligible_total=80, remaining_after_this_batch=30.
  (b) 45 already sent within the last 24h (seeded directly on db.users), 30
      newly-eligible candidates -> only the remaining 5 slots are sent, not
      all 30.
  (c) 50 already-sent db.users docs exist, but 30 of them are 25+ hours old
      (outside the rolling 24h window) - only the 20 within-window ones
      count against the cap, so remaining capacity reflects that (not the
      full seeded 50).
  (d) Cap already fully exhausted (50 sent within the last 24h) -> 0 new
      candidates queued, background_tasks.add_task is NEVER called (checked
      directly against the BackgroundTasks object's task list, not just
      inferred from the response), daily_cap_reached=true in the response.
  (e) Eligible candidates (20) fewer than the remaining cap (50 - 0 sent) ->
      all 20 are sent, nothing invented to fill the cap.
  (f) Regression: base eligibility filtering (password_hash set -> excluded;
      migration_email_sent_at set -> excluded) is unaffected by the cap
      logic, still exactly as before.

Same mongomock/mongomock-motor + direct-function-call approach, and the same
real starlette.background.BackgroundTasks()-then-await-it technique, as
scripts/test_shopify_migration_status_and_bulk.py. Brevo's send_transac_email
is monkeypatched - this script NEVER sends a real email or makes a real
network call.

Run (from repo root): python scripts/test_migration_bulk_daily_cap.py
"""
import asyncio
import os
import sys
from pathlib import Path
from datetime import timedelta

os.environ.setdefault("MONGO_URL", "mongodb://fake-for-import-only/")
os.environ.setdefault("DB_NAME", "fake_db_for_import_only")
# Same rationale as test_shopify_migration_status_and_bulk.py: force
# production so send_shopify_migration_email's actual send path (via the
# monkeypatched Brevo client below) is exercised regardless of the shell's
# ENVIRONMENT.
os.environ["ENVIRONMENT"] = "production"

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


def make_request(method="POST", path="/admin/customers/migrate-shopify-bulk", with_auth=True):
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


def make_client(*, id, email, name=""):
    return {
        "id": id,
        "shopify_customer_id": id,
        "name": name,
        "email": email,
        "email_normalized": email.strip().lower(),
        "phone": "",
        "address": {"address": "", "city": "", "county": "", "postal_code": ""},
        "source": "shopify",
    }


async def seed_eligible_clients(db, n, prefix):
    """n brand-new db.clients rows, no matching db.users doc at all -> all
    trivially eligible (no password_hash, never emailed)."""
    await db.clients.insert_many([
        make_client(id=f"{prefix}{i}", email=f"{prefix}{i}@example.com", name=f"Client {prefix}{i}")
        for i in range(n)
    ])


async def seed_already_sent_users(db, n, prefix, hours_ago):
    """n db.users docs with migration_email_sent_at set `hours_ago` hours in
    the past - simulates prior migration-email sends, used purely to drive
    the rolling-24h cap count, independent of whether a matching db.clients
    row exists."""
    sent_at = server.datetime.utcnow() - timedelta(hours=hours_ago)
    await db.users.insert_many([
        {
            "id": f"{prefix}{i}",
            "email": f"{prefix}{i}@example.com",
            "migration_email_sent_at": sent_at,
        }
        for i in range(n)
    ])


async def scenario_a_under_cap_sends_exactly_cap():
    """(a) 0 sent in last 24h, 80 eligible -> sends exactly 50 (the cap),
    queued=50, eligible_total=80, remaining_after_this_batch=30."""
    db = await fresh_db()
    original_admin = patch_require_admin()
    server.BREVO_API_KEY = "fake-test-key"
    sent_to = []
    original_send = patch_brevo_send(lambda email: sent_to.append(email.to[0]["email"]))
    try:
        await seed_eligible_clients(db, 80, "a")

        bt = BackgroundTasks()
        result = await server.admin_migrate_shopify_bulk(
            request=make_request(), background_tasks=bt,
        )
        check("a) queued == 50 (the daily cap)", result.get("queued") == 50, result)
        check("a) eligible_total == 80", result.get("eligible_total") == 80, result)
        check("a) remaining_after_this_batch == 30", result.get("remaining_after_this_batch") == 30, result)
        check("a) daily_cap_reached is False (capacity was available)", result.get("daily_cap_reached") is False, result)

        await bt()
        check("a) exactly 50 emails actually sent, not 80", len(sent_to) == 50, len(sent_to))
        marked = await db.users.count_documents({"migration_email_sent_at": {"$ne": None}})
        check("a) exactly 50 users marked migration_email_sent_at", marked == 50, marked)
    finally:
        restore_brevo_send(original_send)
        server.BREVO_API_KEY = ""
        restore_require_admin(original_admin)


async def scenario_b_partial_cap_remaining():
    """(b) 45 already sent within the last 24h, 30 newly-eligible candidates
    -> only the remaining 5 slots are sent, not all 30."""
    db = await fresh_db()
    original_admin = patch_require_admin()
    server.BREVO_API_KEY = "fake-test-key"
    sent_to = []
    original_send = patch_brevo_send(lambda email: sent_to.append(email.to[0]["email"]))
    try:
        await seed_already_sent_users(db, 45, "already", hours_ago=3)
        await seed_eligible_clients(db, 30, "b")

        bt = BackgroundTasks()
        result = await server.admin_migrate_shopify_bulk(
            request=make_request(), background_tasks=bt,
        )
        check("b) queued == 5 (50 - 45 already sent), not 30", result.get("queued") == 5, result)
        check("b) eligible_total == 30", result.get("eligible_total") == 30, result)
        check("b) remaining_after_this_batch == 25", result.get("remaining_after_this_batch") == 25, result)

        await bt()
        check("b) exactly 5 emails actually sent", len(sent_to) == 5, sent_to)
    finally:
        restore_brevo_send(original_send)
        server.BREVO_API_KEY = ""
        restore_require_admin(original_admin)


async def scenario_c_outside_window_not_counted():
    """(c) 50 already-sent db.users docs exist total, but 30 of them are 25+
    hours old (outside the rolling 24h window) - only the 20 within-window
    ones should count against the cap, leaving 30 slots (not 0)."""
    db = await fresh_db()
    original_admin = patch_require_admin()
    server.BREVO_API_KEY = "fake-test-key"
    sent_to = []
    original_send = patch_brevo_send(lambda email: sent_to.append(email.to[0]["email"]))
    try:
        await seed_already_sent_users(db, 20, "recent", hours_ago=5)     # within window
        await seed_already_sent_users(db, 30, "stale", hours_ago=25)      # outside window
        await seed_eligible_clients(db, 100, "c")

        bt = BackgroundTasks()
        result = await server.admin_migrate_shopify_bulk(
            request=make_request(), background_tasks=bt,
        )
        check("c) only the 20 within-window sends count -> queued == 30", result.get("queued") == 30, result)
        check("c) eligible_total == 100", result.get("eligible_total") == 100, result)
        check("c) remaining_after_this_batch == 70", result.get("remaining_after_this_batch") == 70, result)

        await bt()
        check("c) exactly 30 emails actually sent", len(sent_to) == 30, len(sent_to))
    finally:
        restore_brevo_send(original_send)
        server.BREVO_API_KEY = ""
        restore_require_admin(original_admin)


async def scenario_d_cap_exhausted_no_background_task():
    """(d) 50 already sent within the last 24h (cap fully exhausted) -> 0
    new candidates queued, background_tasks.add_task is NEVER invoked at all
    (checked directly against the BackgroundTasks object's internal task
    list), daily_cap_reached=true."""
    db = await fresh_db()
    original_admin = patch_require_admin()
    server.BREVO_API_KEY = "fake-test-key"
    sent_to = []
    original_send = patch_brevo_send(lambda email: sent_to.append(email.to[0]["email"]))
    try:
        await seed_already_sent_users(db, 50, "already", hours_ago=1)
        await seed_eligible_clients(db, 30, "d")

        bt = BackgroundTasks()
        result = await server.admin_migrate_shopify_bulk(
            request=make_request(), background_tasks=bt,
        )
        check("d) queued == 0", result.get("queued") == 0, result)
        check("d) eligible_total == 30 (still computed/reported)", result.get("eligible_total") == 30, result)
        check("d) remaining_after_this_batch == 30 (nothing queued)", result.get("remaining_after_this_batch") == 30, result)
        check("d) daily_cap_reached is True", result.get("daily_cap_reached") is True, result)
        check("d) background_tasks.add_task was never called (empty task list)", len(bt.tasks) == 0, bt.tasks)

        await bt()  # no-op, nothing queued
        check("d) no emails sent", sent_to == [], sent_to)
    finally:
        restore_brevo_send(original_send)
        server.BREVO_API_KEY = ""
        restore_require_admin(original_admin)


async def scenario_e_eligible_fewer_than_remaining_cap():
    """(e) 20 eligible candidates, 50 slots remaining (0 sent so far) -> all
    20 are sent, nothing invented to fill the remaining cap."""
    db = await fresh_db()
    original_admin = patch_require_admin()
    server.BREVO_API_KEY = "fake-test-key"
    sent_to = []
    original_send = patch_brevo_send(lambda email: sent_to.append(email.to[0]["email"]))
    try:
        await seed_eligible_clients(db, 20, "e")

        bt = BackgroundTasks()
        result = await server.admin_migrate_shopify_bulk(
            request=make_request(), background_tasks=bt,
        )
        check("e) queued == 20 (all eligible, cap not the limiter)", result.get("queued") == 20, result)
        check("e) eligible_total == 20", result.get("eligible_total") == 20, result)
        check("e) remaining_after_this_batch == 0", result.get("remaining_after_this_batch") == 0, result)
        check("e) daily_cap_reached is False", result.get("daily_cap_reached") is False, result)

        await bt()
        check("e) exactly 20 emails sent, not padded/invented", len(sent_to) == 20, len(sent_to))
    finally:
        restore_brevo_send(original_send)
        server.BREVO_API_KEY = ""
        restore_require_admin(original_admin)


async def scenario_f_base_eligibility_unaffected_by_cap():
    """(f) Regression: password_hash-set clients and already-emailed clients
    remain excluded from the candidate list regardless of the cap logic -
    same behavior as before this change."""
    db = await fresh_db()
    original_admin = patch_require_admin()
    server.BREVO_API_KEY = "fake-test-key"
    sent_to = []
    original_send = patch_brevo_send(lambda email: sent_to.append(email.to[0]["email"]))
    try:
        await db.clients.insert_many([
            make_client(id="f1", email="migrated@example.com"),
            make_client(id="f2", email="already-emailed@example.com"),
            make_client(id="f3", email="fresh@example.com"),
        ])
        await db.users.insert_one({"id": "uf1", "email": "migrated@example.com", "password_hash": "real-hash"})
        await db.users.insert_one({
            "id": "uf2", "email": "already-emailed@example.com",
            "migration_email_sent_at": server.datetime.utcnow() - timedelta(hours=2),
        })

        bt = BackgroundTasks()
        result = await server.admin_migrate_shopify_bulk(
            request=make_request(), background_tasks=bt,
        )
        # uf2's migration_email_sent_at also counts toward the rolling-24h
        # cap total (sent_last_24h=1), but that's incidental here - the
        # point of this scenario is that both f1 and f2 are excluded from
        # the *candidate* list itself, well before the cap slice happens.
        check("f) only the fresh client is eligible -> queued == 1", result.get("queued") == 1, result)
        check("f) eligible_total == 1", result.get("eligible_total") == 1, result)

        await bt()
        check("f) only the fresh client actually emailed", sent_to == ["fresh@example.com"], sent_to)
    finally:
        restore_brevo_send(original_send)
        server.BREVO_API_KEY = ""
        restore_require_admin(original_admin)


async def main():
    await scenario_a_under_cap_sends_exactly_cap()
    await scenario_b_partial_cap_remaining()
    await scenario_c_outside_window_not_counted()
    await scenario_d_cap_exhausted_no_background_task()
    await scenario_e_eligible_fewer_than_remaining_cap()
    await scenario_f_base_eligibility_unaffected_by_cap()

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
