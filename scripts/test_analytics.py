"""
Focused, standalone check for the webshop analytics feature in server.py:

  - POST /analytics/pageview (track_pageview) - public, no auth. Also
    covers the CF-IPCountry/CF-IPCity Cloudflare edge-header geolocation
    capture added alongside GET /admin/analytics/live.
  - GET /admin/analytics/sales (admin_analytics_sales) - admin-gated.
  - GET /admin/analytics/traffic (admin_analytics_traffic) - admin-gated.
  - GET /admin/analytics/live (admin_analytics_live) - admin-gated. "Who's
    on the site right now" - sessions with >=1 pageview inside
    LIVE_SESSION_WINDOW_SECONDS, most recent pageview per session only.
  - OrderCreate.analytics_session_id / create_order's best-effort
    db.analytics_conversions insert.

Not a pytest suite - mirrors scripts/test_stock_checkout.py's,
scripts/test_order_confirmation_email.py's and
scripts/test_admin_customer_account_lookup.py's approach (this repo still
has no test framework, tests/ dir, or pytest in requirements.txt). Uses
mongomock + mongomock-motor to swap in an in-memory Mongo double for
server.client/db, calls the real endpoint functions directly (no HTTP
layer/TestClient), and monkeypatches server._require_admin to a fixed-admin
stub for the admin-gated endpoints (auth itself is already covered
end-to-end by test_require_admin_bff_only.py) - except for
scenario_live_requires_admin, which deliberately leaves _require_admin
un-stubbed to confirm the endpoint really is gated.

Run (from repo root): python scripts/test_analytics.py
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

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


def make_request(headers=None):
    """The admin endpoints under test only ever pass `request` through to
    _require_admin (monkeypatched below to ignore it entirely) and never
    read it directly themselves - an empty scope is enough for those.
    track_pageview is the one exception (reads Cloudflare's CF-IPCountry/
    CF-IPCity headers directly) - `headers` lets callers set those.
    Starlette's ASGI scope wants headers as a list of (lowercase-bytes,
    bytes) tuples; `request.headers.get(...)` itself is still
    case-insensitive on the READ side regardless of how they're cased
    here, but ASGI itself requires lowercase on the wire, matching real
    HTTP/2 and most real HTTP/1.1 client behavior."""
    header_list = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({"type": "http", "headers": header_list, "method": "GET", "path": "/admin/analytics"})


def patch_require_admin(admin_id="admin-1", admin_email="staff@example.com"):
    """Replace server._require_admin with a stub that always succeeds -
    isolates the analytics aggregation logic under test from BFF-JWT auth
    (covered separately by test_require_admin_bff_only.py). Returns the
    original so callers can restore it."""
    original = server._require_admin

    async def _stub(request):
        return {"id": admin_id, "email": admin_email, "role": "admin"}

    server._require_admin = _stub
    return original


async def seed_product(db, product_id, stock=10, price=100.0, title="Piesă test"):
    await db.shopify_products.insert_one({
        "id": product_id,
        "title": title,
        "price": price,
        "stock": stock,
        "stock_status": "in_stock",
    })


# ==================== Part 2a: POST /analytics/pageview ====================

async def scenario_pageview_accepts_valid_input():
    """A valid pageview (all fields present) is accepted, returns 204, and
    is persisted with every field intact."""
    db = await fresh_db()
    payload = server.PageviewCreate(
        session_id="sess-abc",
        path="/produse/piesa-1",
        referrer="https://google.com",
        utm_source="google",
        utm_medium="cpc",
        utm_campaign="summer-sale",
    )
    response = await server.track_pageview(payload, make_request())
    check("pageview valid: returns 204", response.status_code == 204, response.status_code)

    doc = await db.analytics_pageviews.find_one({"session_id": "sess-abc"})
    check("pageview valid: persisted", doc is not None)
    check("pageview valid: path stored", doc["path"] == "/produse/piesa-1", doc)
    check("pageview valid: referrer stored", doc["referrer"] == "https://google.com", doc)
    check("pageview valid: utm_source stored", doc["utm_source"] == "google", doc)
    check("pageview valid: utm_medium stored", doc["utm_medium"] == "cpc", doc)
    check("pageview valid: utm_campaign stored", doc["utm_campaign"] == "summer-sale", doc)
    check("pageview valid: created_at is a datetime", isinstance(doc["created_at"], datetime), doc)


async def scenario_pageview_missing_optional_fields_ok():
    """Only session_id + path present (no referrer/utm_*) is still accepted
    - all the optional fields are genuinely optional, stored as None."""
    db = await fresh_db()
    payload = server.PageviewCreate(session_id="sess-minimal", path="/")
    response = await server.track_pageview(payload, make_request())
    check("pageview minimal: returns 204", response.status_code == 204, response.status_code)
    doc = await db.analytics_pageviews.find_one({"session_id": "sess-minimal"})
    check("pageview minimal: persisted", doc is not None)
    check("pageview minimal: referrer None", doc["referrer"] is None, doc)
    check("pageview minimal: utm_source None", doc["utm_source"] is None, doc)


async def scenario_pageview_rejects_missing_session_id():
    """Missing session_id -> fast 400 (not a generic 422), nothing persisted."""
    db = await fresh_db()
    payload = server.PageviewCreate(session_id=None, path="/produse")
    try:
        await server.track_pageview(payload, make_request())
        check("pageview missing session_id -> rejected", False, "no exception raised")
    except server.HTTPException as e:
        check("pageview missing session_id -> 400", e.status_code == 400, e.status_code)
    check("pageview missing session_id: nothing persisted", await db.analytics_pageviews.count_documents({}) == 0)


async def scenario_pageview_rejects_missing_path():
    """Missing path -> fast 400, nothing persisted."""
    db = await fresh_db()
    payload = server.PageviewCreate(session_id="sess-xyz", path=None)
    try:
        await server.track_pageview(payload, make_request())
        check("pageview missing path -> rejected", False, "no exception raised")
    except server.HTTPException as e:
        check("pageview missing path -> 400", e.status_code == 400, e.status_code)
    check("pageview missing path: nothing persisted", await db.analytics_pageviews.count_documents({}) == 0)


async def scenario_pageview_rejects_blank_session_id():
    """A whitespace-only session_id sanitizes down to empty -> same 400 as
    a fully-missing one (not silently accepted as a blank string)."""
    await fresh_db()
    payload = server.PageviewCreate(session_id="   ", path="/x")
    try:
        await server.track_pageview(payload, make_request())
        check("pageview blank session_id -> rejected", False, "no exception raised")
    except server.HTTPException as e:
        check("pageview blank session_id -> 400", e.status_code == 400, e.status_code)


async def scenario_pageview_captures_country_and_city_from_cf_headers():
    """Cloudflare's edge geolocation headers (CF-IPCountry / CF-IPCity),
    sent in a non-canonical case to prove the read is case-insensitive
    (same as _client_ip's X-Forwarded-For read) - captured into the
    stored doc's new country/city fields."""
    db = await fresh_db()
    payload = server.PageviewCreate(session_id="sess-geo", path="/produse")
    request = make_request(headers={"Cf-IpCountry": "RO", "CF-IPCITY": "Cluj-Napoca"})
    await server.track_pageview(payload, request)
    doc = await db.analytics_pageviews.find_one({"session_id": "sess-geo"})
    check("pageview geo: country captured", doc["country"] == "RO", doc)
    check("pageview geo: city captured", doc["city"] == "Cluj-Napoca", doc)


async def scenario_pageview_missing_cf_headers_country_none_no_crash():
    """No CF-IPCountry/CF-IPCity headers at all (e.g. local dev, or if
    Cloudflare turns out not to forward them here - NOT yet empirically
    confirmed on real traffic) -> country/city stored as None, no
    exception, still 204 - geolocation capture must never block/break the
    pageview write."""
    db = await fresh_db()
    payload = server.PageviewCreate(session_id="sess-no-geo", path="/produse")
    response = await server.track_pageview(payload, make_request())
    check("pageview no geo: still returns 204", response.status_code == 204, response.status_code)
    doc = await db.analytics_pageviews.find_one({"session_id": "sess-no-geo"})
    check("pageview no geo: country is None", doc["country"] is None, doc)
    check("pageview no geo: city is None", doc["city"] is None, doc)


# ==================== Part 1: GET /admin/analytics/sales ====================

async def scenario_sales_analytics_aggregates_correctly():
    """Seeds a small, known mix of native (db.orders) and imported Shopify
    (db.shopify_order_history) orders - some inside, some outside the
    queried [from, to] range, and a customer whose true all-time-earliest
    order is on the OTHER source and OUTSIDE the range - then checks every
    field of the GET /admin/analytics/sales response against hand-computed
    expected values."""
    db = await fresh_db()
    original_admin = patch_require_admin()
    try:
        # Native orders (db.orders) -----------------------------------
        # N1/N2: same customer (a@example.com), both in-range.
        n1 = server.Order(
            id="n1", session_id="s-n1",
            items=[{"product_id": "p1", "product_name": "Piesa A", "quantity": 2, "price": 100.0}],
            customer=server.CustomerInfo(
                name="A Test", email="a@example.com", phone="0700000001",
                address="Str. A", city="Cluj", county="Cluj", postal_code="400001",
            ),
            subtotal=200.0, shipping=25.0, total=225.0, status="pending",
            created_at=datetime(2026, 1, 5, 10, 0, 0),
        )
        n2 = server.Order(
            id="n2", session_id="s-n2",
            items=[{"product_id": "p1", "product_name": "Piesa A", "quantity": 1, "price": 100.0}],
            customer=server.CustomerInfo(
                name="A Test", email="A@Example.com", phone="0700000001",
                address="Str. A", city="Cluj", county="Cluj", postal_code="400001",
            ),
            subtotal=100.0, shipping=25.0, total=125.0, status="confirmed",
            created_at=datetime(2026, 1, 10, 10, 0, 0),
        )
        # N3: different customer (b@example.com), first-ever order, in-range.
        n3 = server.Order(
            id="n3", session_id="s-n3",
            items=[{"product_id": "p2", "product_name": "Piesa B", "quantity": 3, "price": 50.0}],
            customer=server.CustomerInfo(
                name="B Test", email="b@example.com", phone="0700000002",
                address="Str. B", city="Cluj", county="Cluj", postal_code="400002",
            ),
            subtotal=150.0, shipping=25.0, total=175.0, status="pending",
            created_at=datetime(2026, 1, 7, 10, 0, 0),
        )
        # N4: outside the queried range entirely (before `from`).
        n4 = server.Order(
            id="n4", session_id="s-n4",
            items=[{"product_id": "p1", "product_name": "Piesa A", "quantity": 1, "price": 100.0}],
            customer=server.CustomerInfo(
                name="C Test", email="c@example.com", phone="0700000003",
                address="Str. C", city="Cluj", county="Cluj", postal_code="400003",
            ),
            subtotal=100.0, shipping=25.0, total=125.0, status="pending",
            created_at=datetime(2025, 12, 1, 10, 0, 0),
        )
        for order in (n1, n2, n3, n4):
            await db.orders.insert_one(order.dict())

        # Imported Shopify orders (db.shopify_order_history) -----------
        # S1: customer d@example.com, first-ever order, in-range.
        await db.shopify_order_history.insert_one({
            "id": "shop-1", "client_id": None,
            "customer_name": "D Test", "customer_email": "d@example.com",
            "order_number": 1001, "name": "#1001",
            "created_at": datetime(2026, 1, 6, 9, 0, 0, tzinfo=timezone.utc),
            "total_price": 300.0, "currency": "RON",
            "financial_status": "paid", "fulfillment_status": "fulfilled",
            "line_items": [{"title": "Piesa C", "quantity": 1, "price": 300.0}],
        })
        # S2: a@example.com's TRUE all-time-earliest order, but on Shopify
        # and OUTSIDE the queried range - proves new_vs_returning looks at
        # the customer's global earliest order across BOTH sources, not
        # just what's inside the period or in db.orders alone.
        await db.shopify_order_history.insert_one({
            "id": "shop-2", "client_id": None,
            "customer_name": "A Test", "customer_email": "a@example.com",
            "order_number": 1000, "name": "#1000",
            "created_at": datetime(2025, 11, 1, 9, 0, 0, tzinfo=timezone.utc),
            "total_price": 50.0, "currency": "RON",
            "financial_status": "paid", "fulfillment_status": "fulfilled",
            "line_items": [{"title": "Piesa D", "quantity": 1, "price": 50.0}],
        })

        result = await server.admin_analytics_sales(
            request=make_request(), date_from="2026-01-01", date_to="2026-01-31", granularity="day",
        )

        # revenue_over_time: N1(1/5)=225, S1(1/6)=300, N3(1/7)=175, N2(1/10)=125
        rot = {b["date"]: b for b in result["revenue_over_time"]}
        check("sales: revenue_over_time has 4 day-buckets", len(result["revenue_over_time"]) == 4, result["revenue_over_time"])
        check("sales: 2026-01-05 bucket", rot.get("2026-01-05") == {"date": "2026-01-05", "revenue": 225.0, "order_count": 1}, rot.get("2026-01-05"))
        check("sales: 2026-01-06 bucket", rot.get("2026-01-06") == {"date": "2026-01-06", "revenue": 300.0, "order_count": 1}, rot.get("2026-01-06"))
        check("sales: 2026-01-07 bucket", rot.get("2026-01-07") == {"date": "2026-01-07", "revenue": 175.0, "order_count": 1}, rot.get("2026-01-07"))
        check("sales: 2026-01-10 bucket", rot.get("2026-01-10") == {"date": "2026-01-10", "revenue": 125.0, "order_count": 1}, rot.get("2026-01-10"))
        check("sales: N4 (outside range) excluded", "2025-12-01" not in rot, rot)

        # aov = (225+300+175+125)/4 = 206.25
        check("sales: aov correct", result["aov"] == 206.25, result["aov"])

        # orders_by_status: native "status" + shopify "financial_status" merged
        check(
            "sales: orders_by_status correct",
            result["orders_by_status"] == {"pending": 2, "confirmed": 1, "paid": 1},
            result["orders_by_status"],
        )

        # top_products: p1 (qty 3, revenue 300), Piesa C (qty 1, revenue 300),
        # p2 (qty 3, revenue 150) - N4 (outside range) must NOT contribute.
        top_by_key = {(p["product_id"], p["title"]): p for p in result["top_products"]}
        check("sales: top_products has 3 entries", len(result["top_products"]) == 3, result["top_products"])
        p1 = top_by_key.get(("p1", "Piesa A"))
        check("sales: p1 aggregated across N1+N2", p1 is not None and p1["quantity"] == 3 and p1["revenue"] == 300.0, p1)
        piesa_c = top_by_key.get((None, "Piesa C"))
        check("sales: Piesa C (shopify, no product_id) present", piesa_c is not None and piesa_c["quantity"] == 1 and piesa_c["revenue"] == 300.0, piesa_c)
        p2 = top_by_key.get(("p2", "Piesa B"))
        check("sales: p2 present", p2 is not None and p2["quantity"] == 3 and p2["revenue"] == 150.0, p2)
        revenues = [p["revenue"] for p in result["top_products"]]
        check("sales: top_products sorted descending by revenue", revenues == sorted(revenues, reverse=True), revenues)

        # new_vs_returning: b (new, first order in-range), d (new, first
        # order in-range) -> 2 new. a's two in-range orders (N1, N2) are
        # BOTH returning because a's true earliest order (S2) is outside
        # the range/on the other source -> 2 returning. c (N4) is entirely
        # outside the range so doesn't count either way.
        check(
            "sales: new_vs_returning correct",
            result["new_vs_returning"] == {"new": 2, "returning": 2},
            result["new_vs_returning"],
        )

        # Month granularity: everything above collapses into a single
        # 2026-01 bucket.
        result_month = await server.admin_analytics_sales(
            request=make_request(), date_from="2026-01-01", date_to="2026-01-31", granularity="month",
        )
        check(
            "sales: month granularity collapses to 1 bucket",
            result_month["revenue_over_time"] == [{"date": "2026-01", "revenue": 825.0, "order_count": 4}],
            result_month["revenue_over_time"],
        )
    finally:
        server._require_admin = original_admin


async def scenario_sales_analytics_invalid_params():
    """Bad granularity / bad date format / to before from -> clear 400s."""
    await fresh_db()
    original_admin = patch_require_admin()
    try:
        try:
            await server.admin_analytics_sales(request=make_request(), date_from="2026-01-01", date_to="2026-01-31", granularity="week")
            check("sales: bad granularity rejected", False, "no exception raised")
        except server.HTTPException as e:
            check("sales: bad granularity -> 400", e.status_code == 400, e.status_code)

        try:
            await server.admin_analytics_sales(request=make_request(), date_from="not-a-date", date_to="2026-01-31", granularity="day")
            check("sales: bad date format rejected", False, "no exception raised")
        except server.HTTPException as e:
            check("sales: bad date format -> 400", e.status_code == 400, e.status_code)

        try:
            await server.admin_analytics_sales(request=make_request(), date_from="2026-02-01", date_to="2026-01-01", granularity="day")
            check("sales: to-before-from rejected", False, "no exception raised")
        except server.HTTPException as e:
            check("sales: to-before-from -> 400", e.status_code == 400, e.status_code)
    finally:
        server._require_admin = original_admin


# ==================== Part 2b: GET /admin/analytics/traffic ====================

async def scenario_traffic_analytics_aggregates_correctly():
    """Seeds a small, known mix of pageviews + conversions (some in-range,
    some out) and checks every field of the GET /admin/analytics/traffic
    response against hand-computed expected values."""
    db = await fresh_db()
    original_admin = patch_require_admin()
    try:
        pageviews = [
            {"_id": "pv1", "session_id": "s1", "path": "/produse/x", "referrer": "https://google.com",
             "utm_source": None, "utm_medium": None, "utm_campaign": None,
             "created_at": datetime(2026, 1, 5, 8, 0, 0)},
            {"_id": "pv2", "session_id": "s1", "path": "/produse/x", "referrer": "https://google.com",
             "utm_source": None, "utm_medium": None, "utm_campaign": None,
             "created_at": datetime(2026, 1, 5, 9, 0, 0)},
            {"_id": "pv3", "session_id": "s2", "path": "/produse/y", "referrer": None,
             "utm_source": None, "utm_medium": None, "utm_campaign": None,
             "created_at": datetime(2026, 1, 6, 8, 0, 0)},
            {"_id": "pv4", "session_id": "s3", "path": "/produse/x", "referrer": "https://facebook.com",
             "utm_source": None, "utm_medium": None, "utm_campaign": None,
             "created_at": datetime(2026, 1, 7, 8, 0, 0)},
            # Outside the queried range - must not affect any result field.
            {"_id": "pv5", "session_id": "s4", "path": "/produse/z", "referrer": None,
             "utm_source": None, "utm_medium": None, "utm_campaign": None,
             "created_at": datetime(2025, 12, 1, 8, 0, 0)},
        ]
        await db.analytics_pageviews.insert_many(pageviews)

        conversions = [
            {"_id": "c1", "session_id": "s1", "order_id": "order-1", "created_at": datetime(2026, 1, 5, 8, 30, 0)},
            # Outside range - must not count.
            {"_id": "c2", "session_id": "s2", "order_id": "order-2", "created_at": datetime(2025, 12, 15, 8, 0, 0)},
        ]
        await db.analytics_conversions.insert_many(conversions)

        result = await server.admin_analytics_traffic(
            request=make_request(), date_from="2026-01-01", date_to="2026-01-31", granularity="day",
        )

        pot = {b["date"]: b["count"] for b in result["pageviews_over_time"]}
        check("traffic: pageviews_over_time has 3 day-buckets", len(result["pageviews_over_time"]) == 3, result["pageviews_over_time"])
        check("traffic: 2026-01-05 count=2", pot.get("2026-01-05") == 2, pot)
        check("traffic: 2026-01-06 count=1", pot.get("2026-01-06") == 1, pot)
        check("traffic: 2026-01-07 count=1", pot.get("2026-01-07") == 1, pot)
        check("traffic: outside-range pageview excluded", "2025-12-01" not in pot, pot)

        top_pages = {p["path"]: p["count"] for p in result["top_pages"]}
        check("traffic: top_pages /produse/x count=3", top_pages.get("/produse/x") == 3, top_pages)
        check("traffic: top_pages /produse/y count=1", top_pages.get("/produse/y") == 1, top_pages)

        top_referrers = {r["referrer"]: r["count"] for r in result["top_referrers"]}
        check("traffic: top_referrers google.com count=2", top_referrers.get("https://google.com") == 2, top_referrers)
        check("traffic: top_referrers facebook.com count=1", top_referrers.get("https://facebook.com") == 1, top_referrers)
        check("traffic: empty/None referrer excluded from top_referrers", len(result["top_referrers"]) == 2, result["top_referrers"])

        # unique_sessions: distinct session_id WITH A PAGEVIEW in range = s1,s2,s3 = 3
        check("traffic: unique_sessions == 3", result["unique_sessions"] == 3, result["unique_sessions"])
        # conversions: distinct session_id WITH A CONVERSION in range = s1 only (c2 is outside range)
        check("traffic: conversions == 1", result["conversions"] == 1, result["conversions"])
        # conversion_rate = 1/3
        check("traffic: conversion_rate == round(1/3, 4)", result["conversion_rate"] == round(1 / 3, 4), result["conversion_rate"])
    finally:
        server._require_admin = original_admin


async def scenario_traffic_analytics_zero_sessions_no_division_error():
    """No pageviews in range at all -> unique_sessions=0, conversions=0,
    conversion_rate=0 (never a ZeroDivisionError)."""
    await fresh_db()
    original_admin = patch_require_admin()
    try:
        result = await server.admin_analytics_traffic(
            request=make_request(), date_from="2026-01-01", date_to="2026-01-31", granularity="day",
        )
        check("traffic: empty range unique_sessions == 0", result["unique_sessions"] == 0, result)
        check("traffic: empty range conversions == 0", result["conversions"] == 0, result)
        check("traffic: empty range conversion_rate == 0 (no ZeroDivisionError)", result["conversion_rate"] == 0, result)
    finally:
        server._require_admin = original_admin


# ==================== Part 2b-live: GET /admin/analytics/live ====================

async def scenario_live_recent_session_shows_most_recent_pageview():
    """A session with its LAST pageview inside the live window appears,
    showing that most recent pageview's path/country/city (not an earlier
    one from the same session) plus a sane last_seen/seconds_ago."""
    db = await fresh_db()
    original_admin = patch_require_admin()
    try:
        now = datetime.utcnow()
        await db.analytics_pageviews.insert_many([
            {"_id": "pv1", "session_id": "s1", "path": "/produse/x", "country": "RO", "city": "Cluj-Napoca",
             "referrer": None, "utm_source": None, "utm_medium": None, "utm_campaign": None,
             "created_at": now - timedelta(minutes=3)},
            # s1's most recent pageview - this is the one that should win.
            {"_id": "pv2", "session_id": "s1", "path": "/produse/y", "country": "RO", "city": "Cluj-Napoca",
             "referrer": None, "utm_source": None, "utm_medium": None, "utm_campaign": None,
             "created_at": now - timedelta(minutes=1)},
        ])
        result = await server.admin_analytics_live(request=make_request())
        check("live: count == 1", result["count"] == 1, result)
        check("live: exactly one session in list", len(result["sessions"]) == 1, result)
        check(
            "live: window_seconds matches LIVE_SESSION_WINDOW_SECONDS",
            result["window_seconds"] == server.LIVE_SESSION_WINDOW_SECONDS, result,
        )
        session = result["sessions"][0]
        check("live: session_id correct", session["session_id"] == "s1", session)
        check("live: shows MOST RECENT path, not the first", session["path"] == "/produse/y", session)
        check("live: country present", session["country"] == "RO", session)
        check("live: city present", session["city"] == "Cluj-Napoca", session)
        check("live: last_seen is a datetime", isinstance(session["last_seen"], datetime), session)
        check(
            "live: seconds_ago is a small non-negative int (~60s)",
            isinstance(session["seconds_ago"], int) and 0 <= session["seconds_ago"] < 120, session,
        )
    finally:
        server._require_admin = original_admin


async def scenario_live_stale_session_excluded():
    """A session whose only pageview is older than LIVE_SESSION_WINDOW_SECONDS
    does NOT appear - it's not "live" per the agreed 5-minute-since-last-
    pageview definition."""
    db = await fresh_db()
    original_admin = patch_require_admin()
    try:
        now = datetime.utcnow()
        await db.analytics_pageviews.insert_one({
            "_id": "pv-stale", "session_id": "s-stale", "path": "/produse/old", "country": None, "city": None,
            "referrer": None, "utm_source": None, "utm_medium": None, "utm_campaign": None,
            "created_at": now - timedelta(seconds=server.LIVE_SESSION_WINDOW_SECONDS + 60),
        })
        result = await server.admin_analytics_live(request=make_request())
        check("live: stale session excluded -> count == 0", result["count"] == 0, result)
        check("live: stale session -> empty sessions list", result["sessions"] == [], result)
    finally:
        server._require_admin = original_admin


async def scenario_live_country_city_none_when_never_captured():
    """A pageview recorded without country/city (e.g. Cloudflare headers
    were absent when it was written - see scenario_pageview_missing_cf_
    headers_country_none_no_crash) still shows up live, just with
    country/city as None rather than raising/crashing the aggregation."""
    db = await fresh_db()
    original_admin = patch_require_admin()
    try:
        now = datetime.utcnow()
        await db.analytics_pageviews.insert_one({
            "_id": "pv-nogeo", "session_id": "s-nogeo", "path": "/", "country": None, "city": None,
            "referrer": None, "utm_source": None, "utm_medium": None, "utm_campaign": None,
            "created_at": now - timedelta(seconds=30),
        })
        result = await server.admin_analytics_live(request=make_request())
        check("live: no-geo session still appears", result["count"] == 1, result)
        check("live: no-geo session country is None", result["sessions"][0]["country"] is None, result)
        check("live: no-geo session city is None", result["sessions"][0]["city"] is None, result)
    finally:
        server._require_admin = original_admin


async def scenario_live_multiple_sessions_sorted_newest_first():
    """Multiple distinct live sessions -> one row each (not one row per
    pageview), sorted by last_seen descending (most recently active
    first)."""
    db = await fresh_db()
    original_admin = patch_require_admin()
    try:
        now = datetime.utcnow()
        await db.analytics_pageviews.insert_many([
            {"_id": "pv-a", "session_id": "s-a", "path": "/a", "country": None, "city": None,
             "referrer": None, "utm_source": None, "utm_medium": None, "utm_campaign": None,
             "created_at": now - timedelta(minutes=4)},
            {"_id": "pv-b", "session_id": "s-b", "path": "/b", "country": None, "city": None,
             "referrer": None, "utm_source": None, "utm_medium": None, "utm_campaign": None,
             "created_at": now - timedelta(minutes=1)},
        ])
        result = await server.admin_analytics_live(request=make_request())
        check("live: two distinct sessions -> count == 2", result["count"] == 2, result)
        ids_in_order = [s["session_id"] for s in result["sessions"]]
        check("live: sorted newest-activity-first", ids_in_order == ["s-b", "s-a"], ids_in_order)
    finally:
        server._require_admin = original_admin


async def scenario_live_no_sessions_empty_list():
    """No pageviews at all in the window -> count 0, empty sessions list,
    no error."""
    await fresh_db()
    original_admin = patch_require_admin()
    try:
        result = await server.admin_analytics_live(request=make_request())
        check("live: empty state count == 0", result["count"] == 0, result)
        check("live: empty state sessions == []", result["sessions"] == [], result)
    finally:
        server._require_admin = original_admin


async def scenario_live_requires_admin():
    """No Authorization header at all -> _require_admin's own 401 (same
    gate, same message, as every other /admin/* route) - _require_admin is
    deliberately left UN-stubbed here (unlike every other scenario in this
    file) to confirm GET /admin/analytics/live actually calls it, rather
    than just trusting the source read. Full auth-logic coverage (JWT
    verification, fail-closed 503 when unconfigured, etc.) lives in
    test_require_admin_bff_only.py, not duplicated here."""
    await fresh_db()
    request = Request({"type": "http", "headers": [], "method": "GET", "path": "/admin/analytics/live"})
    try:
        await server.admin_analytics_live(request=request)
        check("live: missing auth rejected", False, "no exception raised")
    except server.HTTPException as e:
        check("live: missing auth -> 401", e.status_code == 401, e.status_code)


# ==================== Part 2c: create_order + analytics_session_id ====================

def make_order_data(analytics_session_id=None, session_id="sess-checkout"):
    return server.OrderCreate(
        session_id=session_id,
        items=[{"product_id": "p1", "product_name": "Piesa", "quantity": 1, "price": 0.0}],
        customer=server.CustomerInfo(
            name="Ion Popescu", email="ion@example.com", phone="0700000000",
            address="Str. Exemplu 1", city="Cluj-Napoca", county="Cluj", postal_code="400000",
        ),
        subtotal=0.0, total=0.0, payment_method="ramburs",
        analytics_session_id=analytics_session_id,
    )


async def scenario_order_with_analytics_session_id_creates_conversion():
    """(d1) OrderCreate.analytics_session_id present -> create_order writes
    a matching doc into db.analytics_conversions (source_id/order_id
    correct), without needing the background tasks to run."""
    db = await fresh_db()
    await seed_product(db, "p1", stock=10, price=100.0)
    order_data = make_order_data(analytics_session_id="visitor-session-123")
    bt = BackgroundTasks()
    order = await server.create_order(order_data, make_request(), bt)

    conversion = await db.analytics_conversions.find_one({"session_id": "visitor-session-123"})
    check("order+analytics_session_id: conversion doc created", conversion is not None)
    check("order+analytics_session_id: conversion.order_id matches", conversion is not None and conversion["order_id"] == order.id, conversion)
    check("order+analytics_session_id: conversion.created_at is a datetime", conversion is not None and isinstance(conversion["created_at"], datetime), conversion)
    check("order+analytics_session_id: exactly one conversion doc", await db.analytics_conversions.count_documents({}) == 1)


async def scenario_order_without_analytics_session_id_no_conversion_no_error():
    """(d2) OrderCreate without analytics_session_id (the pre-existing/
    default shape) -> create_order succeeds normally and does NOT create
    any db.analytics_conversions doc - and, critically, doesn't raise."""
    db = await fresh_db()
    await seed_product(db, "p1", stock=10, price=100.0)
    order_data = make_order_data(analytics_session_id=None)
    bt = BackgroundTasks()
    order = await server.create_order(order_data, make_request(), bt)

    check("order without analytics_session_id: order created normally", order is not None)
    check("order without analytics_session_id: no conversion doc", await db.analytics_conversions.count_documents({}) == 0)


async def main():
    await scenario_pageview_accepts_valid_input()
    await scenario_pageview_missing_optional_fields_ok()
    await scenario_pageview_rejects_missing_session_id()
    await scenario_pageview_rejects_missing_path()
    await scenario_pageview_rejects_blank_session_id()
    await scenario_pageview_captures_country_and_city_from_cf_headers()
    await scenario_pageview_missing_cf_headers_country_none_no_crash()
    await scenario_sales_analytics_aggregates_correctly()
    await scenario_sales_analytics_invalid_params()
    await scenario_traffic_analytics_aggregates_correctly()
    await scenario_traffic_analytics_zero_sessions_no_division_error()
    await scenario_live_recent_session_shows_most_recent_pageview()
    await scenario_live_stale_session_excluded()
    await scenario_live_country_city_none_when_never_captured()
    await scenario_live_multiple_sessions_sorted_newest_first()
    await scenario_live_no_sessions_empty_list()
    await scenario_live_requires_admin()
    await scenario_order_with_analytics_session_id_creates_conversion()
    await scenario_order_without_analytics_session_id_no_conversion_no_error()

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
