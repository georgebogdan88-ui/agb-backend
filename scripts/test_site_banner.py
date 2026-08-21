"""
Focused, standalone check for the site-banner feature in server.py:

  - GET /banner (get_site_banner) - PUBLIC, no auth. Polled by
    agb-webshop's homepage on every load.
  - GET /admin/banner (admin_get_site_banner) - admin-gated, full state.
  - PUT /admin/banner (admin_update_site_banner) - admin-gated, full-replace
    upsert into db.site_banner.
  - _banner_is_effectively_active - the single shared date-window helper
    both the public and admin endpoints rely on (active flag AND inside the
    optional [starts_at, ends_at] window).

Not a pytest suite - same mongomock/mongomock-motor + direct-function-call
approach as every other scripts/test_*.py file here (mirrors
scripts/test_restock_auto_notify.py's patch_require_admin stub for the
admin-gated scenarios, and scripts/test_require_admin_bff_only.py's
leave-it-unstubbed approach for the "requires auth" scenarios).

Covers:
  (a) active=true, no starts_at/ends_at -> public GET reports active with
      the stored text/style.
  (b) active=false -> public GET reports inactive, regardless of dates.
  (c) active=true but starts_at in the future -> not yet live -> inactive.
  (d) active=true but ends_at in the past -> already ended -> inactive.
  (e) active=true and now inside [starts_at, ends_at] -> active.
  (f) no document at all ever saved (fresh DB) -> public GET reports
      inactive, no error.
  (g) GET /admin/banner and PUT /admin/banner both 401 without a valid
      admin credential (auth itself is covered end-to-end by
      test_require_admin_bff_only.py; this only confirms these two routes
      actually call _require_admin).
  (h) PUT /admin/banner persists every field correctly (text/active/
      starts_at/ends_at/style/updated_at/updated_by), and a follow-up GET
      /admin/banner reflects exactly what was saved, including
      effective_active.
  (i) PUT /admin/banner rejects ends_at < starts_at and an unknown `style`
      value, in both cases without writing anything.
  (j) PUT /admin/banner writes an admin_audit_log entry.

Run (from repo root): python scripts/test_site_banner.py
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


def make_request(bearer_value="admin-token", method="GET", path="/admin/banner"):
    headers = []
    if bearer_value is not None:
        headers.append((b"authorization", f"Bearer {bearer_value}".encode()))
    scope = {"type": "http", "headers": headers, "method": method, "path": path}
    return Request(scope)


def _ms(dt):
    """Truncates to millisecond precision, matching real MongoDB's storage
    precision (and mongomock's) - a datetime.utcnow() microsecond value
    would otherwise silently lose its last 3 digits on round-trip and fail
    an exact equality check for reasons unrelated to the code under test."""
    return dt.replace(microsecond=(dt.microsecond // 1000) * 1000)


def patch_require_admin(admin_id="admin-1", admin_email="staff@example.com"):
    original = server._require_admin

    async def _stub(request):
        return {"id": admin_id, "email": admin_email, "role": "admin"}

    server._require_admin = _stub
    return original


async def scenario_a_active_no_window_is_public_active():
    """(a) active=true, no starts_at/ends_at -> public GET reports active."""
    db = await fresh_db()
    await db.site_banner.insert_one({
        "text": "Reduceri de vara -10%", "active": True,
        "starts_at": None, "ends_at": None, "style": "promo",
    })
    result = await server.get_site_banner()
    check("a) public GET reports active", result.get("active") is True, result)
    check("a) public GET returns stored text", result.get("text") == "Reduceri de vara -10%", result)
    check("a) public GET returns stored style", result.get("style") == "promo", result)
    check("a) public GET omits internal fields", "starts_at" not in result and "updated_by" not in result, result)


async def scenario_b_inactive_flag_always_wins():
    """(b) active=false -> public GET inactive, even with no dates set."""
    db = await fresh_db()
    await db.site_banner.insert_one({
        "text": "Ceva", "active": False, "starts_at": None, "ends_at": None, "style": "info",
    })
    result = await server.get_site_banner()
    check("b) public GET reports inactive when active=false", result == {"active": False}, result)


async def scenario_c_future_start_not_yet_live():
    """(c) active=true but starts_at is in the future -> not yet live."""
    db = await fresh_db()
    await db.site_banner.insert_one({
        "text": "Concediu 1-15 septembrie", "active": True,
        "starts_at": datetime.utcnow() + timedelta(days=5),
        "ends_at": None, "style": "atentie",
    })
    result = await server.get_site_banner()
    check("c) future starts_at -> inactive", result == {"active": False}, result)


async def scenario_d_past_end_already_over():
    """(d) active=true but ends_at is in the past -> already ended."""
    db = await fresh_db()
    await db.site_banner.insert_one({
        "text": "Oferta de Paste", "active": True,
        "starts_at": None,
        "ends_at": datetime.utcnow() - timedelta(days=1),
        "style": "promo",
    })
    result = await server.get_site_banner()
    check("d) past ends_at -> inactive", result == {"active": False}, result)


async def scenario_e_inside_window_is_active():
    """(e) active=true and now falls inside [starts_at, ends_at] -> active."""
    db = await fresh_db()
    await db.site_banner.insert_one({
        "text": "Suntem in concediu intre 1-15 septembrie", "active": True,
        "starts_at": datetime.utcnow() - timedelta(days=1),
        "ends_at": datetime.utcnow() + timedelta(days=1),
        "style": "info",
    })
    result = await server.get_site_banner()
    check("e) inside window -> active", result.get("active") is True, result)
    check("e) inside window -> text present", result.get("text") == "Suntem in concediu intre 1-15 septembrie", result)


async def scenario_f_no_document_ever_saved():
    """(f) fresh DB, nothing ever saved to db.site_banner -> public GET
    reports inactive cleanly, no exception."""
    await fresh_db()
    result = await server.get_site_banner()
    check("f) no document -> inactive, no error", result == {"active": False}, result)


async def scenario_g_admin_routes_require_auth():
    """(g) GET/PUT /admin/banner both 401 without a valid admin credential
    (real _require_admin left un-stubbed here)."""
    await fresh_db()
    try:
        await server.admin_get_site_banner(make_request(bearer_value=None))
        check("g) admin GET rejected without auth", False, "no exception raised")
    except server.HTTPException as e:
        check("g) admin GET -> 401 without auth", e.status_code == 401, f"got {e.status_code}")

    try:
        payload = server.BannerUpdate(text="x", active=True)
        await server.admin_update_site_banner(payload, make_request(bearer_value=None, method="PUT"))
        check("g) admin PUT rejected without auth", False, "no exception raised")
    except server.HTTPException as e:
        check("g) admin PUT -> 401 without auth", e.status_code == 401, f"got {e.status_code}")


async def scenario_h_put_persists_all_fields_and_get_reflects_them():
    """(h) PUT /admin/banner persists every field; a follow-up GET
    /admin/banner reflects exactly what was saved, including
    effective_active."""
    await fresh_db()
    original_admin = patch_require_admin(admin_id="admin-9", admin_email="george@agb.ro")
    try:
        starts_at = _ms(datetime.utcnow() - timedelta(hours=1))
        ends_at = _ms(datetime.utcnow() + timedelta(days=10))
        payload = server.BannerUpdate(
            text="  Suntem in concediu intre 1 si 15 septembrie  ",
            active=True, starts_at=starts_at, ends_at=ends_at, style="atentie",
        )
        put_result = await server.admin_update_site_banner(payload, make_request(method="PUT"))
        check("h) PUT trims text", put_result.get("text") == "Suntem in concediu intre 1 si 15 septembrie", put_result)
        check("h) PUT echoes active=True", put_result.get("active") is True, put_result)
        check("h) PUT echoes style", put_result.get("style") == "atentie", put_result)
        check("h) PUT echoes updated_by = admin email", put_result.get("updated_by") == "george@agb.ro", put_result)
        check("h) PUT reports effective_active=True (inside window)", put_result.get("effective_active") is True, put_result)
        check("h) PUT sets updated_at", isinstance(put_result.get("updated_at"), datetime), put_result)

        get_result = await server.admin_get_site_banner(make_request())
        check("h) admin GET text matches saved", get_result.get("text") == "Suntem in concediu intre 1 si 15 septembrie", get_result)
        check("h) admin GET active matches saved", get_result.get("active") is True, get_result)
        check("h) admin GET starts_at matches saved", get_result.get("starts_at") == starts_at, get_result)
        check("h) admin GET ends_at matches saved", get_result.get("ends_at") == ends_at, get_result)
        check("h) admin GET style matches saved", get_result.get("style") == "atentie", get_result)
        check("h) admin GET effective_active matches", get_result.get("effective_active") is True, get_result)

        public_result = await server.get_site_banner()
        check("h) public GET now reflects the saved active banner", public_result.get("active") is True, public_result)
        check("h) public GET text matches saved", public_result.get("text") == "Suntem in concediu intre 1 si 15 septembrie", public_result)
    finally:
        server._require_admin = original_admin


async def scenario_i_validation_rejects_bad_input_without_writing():
    """(i) ends_at < starts_at and an unknown style are both rejected with
    400, and in both cases nothing is written to db.site_banner."""
    db = await fresh_db()
    original_admin = patch_require_admin()
    try:
        # Baseline: nothing saved yet.
        check("i) baseline: no document before invalid PUTs", await db.site_banner.find_one({}) is None)

        bad_window = server.BannerUpdate(
            text="x", active=True,
            starts_at=datetime.utcnow() + timedelta(days=5),
            ends_at=datetime.utcnow(),
        )
        try:
            await server.admin_update_site_banner(bad_window, make_request(method="PUT"))
            check("i) ends_at < starts_at rejected", False, "no exception raised")
        except server.HTTPException as e:
            check("i) ends_at < starts_at -> 400", e.status_code == 400, f"got {e.status_code}")

        bad_style = server.BannerUpdate(text="x", active=True, style="does-not-exist")
        try:
            await server.admin_update_site_banner(bad_style, make_request(method="PUT"))
            check("i) unknown style rejected", False, "no exception raised")
        except server.HTTPException as e:
            check("i) unknown style -> 400", e.status_code == 400, f"got {e.status_code}")

        check("i) still no document after both rejected PUTs", await db.site_banner.find_one({}) is None)
    finally:
        server._require_admin = original_admin


async def scenario_j_put_writes_audit_log():
    """(j) A successful PUT /admin/banner writes an admin_audit_log entry
    for resource_type site_banner."""
    db = await fresh_db()
    original_admin = patch_require_admin(admin_id="admin-1", admin_email="staff@example.com")
    try:
        payload = server.BannerUpdate(text="Anunt nou", active=False)
        await server.admin_update_site_banner(payload, make_request(method="PUT"))
        entry = await db.admin_audit_log.find_one({"resource_type": "site_banner"})
        check("j) audit log entry written", entry is not None, entry)
        if entry:
            check("j) audit log action=update", entry.get("action") == "update", entry)
            check("j) audit log admin_email recorded", entry.get("admin_email") == "staff@example.com", entry)
            check("j) audit log after.text recorded", (entry.get("after") or {}).get("text") == "Anunt nou", entry)
    finally:
        server._require_admin = original_admin


async def main():
    await scenario_a_active_no_window_is_public_active()
    await scenario_b_inactive_flag_always_wins()
    await scenario_c_future_start_not_yet_live()
    await scenario_d_past_end_already_over()
    await scenario_e_inside_window_is_active()
    await scenario_f_no_document_ever_saved()
    await scenario_g_admin_routes_require_auth()
    await scenario_h_put_persists_all_fields_and_get_reflects_them()
    await scenario_i_validation_rejects_bad_input_without_writing()
    await scenario_j_put_writes_audit_log()

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
