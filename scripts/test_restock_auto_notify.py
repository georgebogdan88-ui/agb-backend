"""
Focused, standalone check for the automatic "back in stock" notification
(_notify_restocked_customers in server.py) and its two trigger points:

  1. _apply_product_update (server.py) - the single shared helper behind
     every staff-driven product edit (single PUT /admin/products/{id},
     shared-patch bulk PUT /admin/products/bulk, and the Shopify-style
     inline grid PUT /admin/products/bulk-save). Exercised here via
     admin_update_product (the single-product endpoint) since all three
     funnel through the exact same _apply_product_update hook - a bug in
     the hook itself would show up identically regardless of which of the
     three calls it.
  2. update_single_product (server.py) - the live Shopify webhook handler
     for products/create and products/update, a second, independent write
     path into the same `stock` field that bypasses _apply_product_update
     entirely (confirmed in code, see its own inline comment).

No manual staff "Înștiințează client" click required for either path -
before this existed, that was the ONLY way a client with an active
stock_alert got emailed.

Not a pytest suite - same mongomock/mongomock-motor + direct-function-call
approach as every other scripts/test_*.py file here. Mirrors
scripts/test_stock_notification_email.py's Brevo-monkeypatch technique and
scripts/test_order_confirmation_email.py's real starlette
BackgroundTasks()-then-await-it approach for exercising queued
fire-and-forget tasks the same way the real ASGI request/response cycle
does.

Covers:
  (a) stock 0 -> 5 on a product with two active stock_alert subscribers ->
      both get emailed (via the single-product PUT endpoint / background
      tasks path).
  (b) stock 5 -> 8 (never touched 0) -> must NOT trigger any notification,
      even though `stock` changed and even though there ARE active
      subscribers - only a genuine 0/None -> >=1 transition should fire.
  (c) a product with zero active stock_alert interests restocking ->
      no error, no email attempted, function returns cleanly.
  (d) one subscriber's send fails (simulated Brevo outage) -> the next
      subscriber in the list still gets notified (failure isolation).
  (e) stock None -> 3 (product never had a `stock` field at all) also
      counts as a restock and triggers notifications.
  (f) same 0 -> positive transition arriving via the Shopify webhook path
      (update_single_product), which writes `stock` through a completely
      separate update_one(upsert=True) call that bypasses
      _apply_product_update - confirms this second path is covered too.
  (g) a "favorite" or "price_alert" interest on the same product must NOT
      receive a restock email - only "stock_alert" rows are notified.

Run (from repo root): python scripts/test_restock_auto_notify.py
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


def make_admin_request():
    """None of the endpoints under test read anything from `request` beyond
    passing it through to _require_admin (stubbed below) and _write_audit_log
    (fine on an empty scope) - same helper as test_product_seo_fields.py."""
    return Request({"type": "http", "headers": [], "method": "PUT", "path": "/admin/products/p1"})


def patch_require_admin(admin_id="admin-1", admin_email="staff@example.com"):
    original = server._require_admin

    async def _stub(request):
        return {"id": admin_id, "email": admin_email, "role": "admin"}

    server._require_admin = _stub
    return original


def base_product(**overrides):
    doc = {
        "handle": "placeholder-handle", "description": "", "title": "Produs test",
        "title_normalized": "produs test", "price": 100.0, "currency": "RON",
        "images": [], "tags": [], "compatible_models": [], "collections": [],
        "complementary_product_ids": [], "equivalent_product_ids": [],
        "is_featured": False, "sku": "SKU-1", "image_url": "https://example.com/img.jpg",
    }
    doc.update(overrides)
    return doc


async def insert_interest(db, *, iid, user_id, product_id, itype="stock_alert"):
    await db.customer_interests.insert_one({
        "id": iid, "user_id": user_id, "product_id": product_id, "type": itype,
    })


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


async def scenario_a_zero_to_positive_notifies_all_active_subscribers():
    """(a) stock 0 -> 5 via the single-product admin PUT endpoint -> both
    active stock_alert subscribers get emailed, each interest row cleared
    afterwards (reusing _send_stock_notification_email's own behavior)."""
    db = await fresh_db()
    await db.shopify_products.insert_one(base_product(id="p1", stock=0, title="Filtru ulei"))
    await db.users.insert_one({"id": "u1", "email": "ion@example.com", "name": "Ion"})
    await db.users.insert_one({"id": "u2", "email": "maria@example.com", "name": "Maria"})
    await insert_interest(db, iid="i1", user_id="u1", product_id="p1")
    await insert_interest(db, iid="i2", user_id="u2", product_id="p1")

    original_admin = patch_require_admin()
    server.BREVO_API_KEY = "fake-test-key"
    sent_to = []
    original_send = patch_brevo_send(lambda email: sent_to.append(email.to[0]["email"]))
    try:
        bt = BackgroundTasks()
        updated = await server.admin_update_product(
            "p1", request=make_admin_request(), product_data=server.ProductUpdate(stock=5),
            background_tasks=bt,
        )
        check("a) stock actually updated to 5", updated.stock == 5, updated.stock)
        await bt()  # runs the queued _notify_restocked_customers task

        check("a) both subscribers emailed", sorted(sent_to) == ["ion@example.com", "maria@example.com"], sent_to)
        check("a) i1 cleared after successful send", await db.customer_interests.find_one({"id": "i1"}) is None)
        check("a) i2 cleared after successful send", await db.customer_interests.find_one({"id": "i2"}) is None)
    finally:
        restore_brevo_send(original_send)
        server.BREVO_API_KEY = ""
        server._require_admin = original_admin


async def scenario_b_ordinary_increase_does_not_trigger():
    """(b) stock 5 -> 8 (already positive before the edit) must NOT send
    anything, even with an active subscriber sitting right there."""
    db = await fresh_db()
    await db.shopify_products.insert_one(base_product(id="p2", stock=5, title="Curea transmisie"))
    await db.users.insert_one({"id": "u1", "email": "ion@example.com", "name": "Ion"})
    await insert_interest(db, iid="i1", user_id="u1", product_id="p2")

    original_admin = patch_require_admin()
    server.BREVO_API_KEY = "fake-test-key"
    sent_to = []
    original_send = patch_brevo_send(lambda email: sent_to.append(email.to[0]["email"]))
    try:
        bt = BackgroundTasks()
        updated = await server.admin_update_product(
            "p2", request=make_admin_request(), product_data=server.ProductUpdate(stock=8),
            background_tasks=bt,
        )
        check("b) stock actually updated to 8", updated.stock == 8, updated.stock)
        await bt()

        check("b) no email sent on an ordinary 5->8 increase", sent_to == [], sent_to)
        check("b) interest NOT cleared (nothing was sent)", await db.customer_interests.find_one({"id": "i1"}) is not None)
    finally:
        restore_brevo_send(original_send)
        server.BREVO_API_KEY = ""
        server._require_admin = original_admin


async def scenario_c_no_active_interests_is_a_clean_noop():
    """(c) Product restocks (0 -> 3) but has zero customer_interests rows at
    all - must not raise and must not attempt any send."""
    db = await fresh_db()
    await db.shopify_products.insert_one(base_product(id="p3", stock=0, title="Rulment"))

    original_admin = patch_require_admin()
    server.BREVO_API_KEY = "fake-test-key"
    sent_to = []
    original_send = patch_brevo_send(lambda email: sent_to.append(email.to[0]["email"]))
    try:
        bt = BackgroundTasks()
        updated = await server.admin_update_product(
            "p3", request=make_admin_request(), product_data=server.ProductUpdate(stock=3),
            background_tasks=bt,
        )
        check("c) stock actually updated to 3", updated.stock == 3, updated.stock)
        try:
            await bt()
            ran_clean = True
        except Exception as e:  # noqa: BLE001
            ran_clean = False
            print(f"  unexpected exception: {e}")
        check("c) no interests -> runs clean, no exception", ran_clean)
        check("c) no email attempted", sent_to == [], sent_to)
    finally:
        restore_brevo_send(original_send)
        server.BREVO_API_KEY = ""
        server._require_admin = original_admin


async def scenario_d_one_failed_send_does_not_block_the_rest():
    """(d) Three active subscribers, the Brevo call raises only for the
    second recipient -> the first and third must still be notified."""
    db = await fresh_db()
    await db.shopify_products.insert_one(base_product(id="p4", stock=0, title="Anvelopa"))
    await db.users.insert_one({"id": "u1", "email": "a@example.com", "name": "A"})
    await db.users.insert_one({"id": "u2", "email": "b@example.com", "name": "B"})
    await db.users.insert_one({"id": "u3", "email": "c@example.com", "name": "C"})
    await insert_interest(db, iid="i1", user_id="u1", product_id="p4")
    await insert_interest(db, iid="i2", user_id="u2", product_id="p4")
    await insert_interest(db, iid="i3", user_id="u3", product_id="p4")

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
        bt = BackgroundTasks()
        await server.admin_update_product(
            "p4", request=make_admin_request(), product_data=server.ProductUpdate(stock=4),
            background_tasks=bt,
        )
        await bt()

        check("d) unaffected subscribers still notified despite one failure",
              sorted(sent_to) == ["a@example.com", "c@example.com"], sent_to)
        check("d) failed recipient's interest NOT cleared", await db.customer_interests.find_one({"id": "i2"}) is not None)
        check("d) successful recipients' interests cleared",
              await db.customer_interests.find_one({"id": "i1"}) is None
              and await db.customer_interests.find_one({"id": "i3"}) is None)
    finally:
        restore_brevo_send(original_send)
        server.BREVO_API_KEY = ""
        server._require_admin = original_admin


async def scenario_e_missing_stock_field_counts_as_zero():
    """(e) Product never had a `stock` field at all (None) -> setting it to
    3 must still be treated as a restock, same as an explicit 0."""
    db = await fresh_db()
    doc = base_product(id="p5", title="Piesa fara stoc setat")
    doc.pop("stock", None)  # genuinely absent, not even 0
    await db.shopify_products.insert_one(doc)
    await db.users.insert_one({"id": "u1", "email": "ion@example.com", "name": "Ion"})
    await insert_interest(db, iid="i1", user_id="u1", product_id="p5")

    original_admin = patch_require_admin()
    server.BREVO_API_KEY = "fake-test-key"
    sent_to = []
    original_send = patch_brevo_send(lambda email: sent_to.append(email.to[0]["email"]))
    try:
        bt = BackgroundTasks()
        await server.admin_update_product(
            "p5", request=make_admin_request(), product_data=server.ProductUpdate(stock=3),
            background_tasks=bt,
        )
        await bt()
        check("e) missing stock field treated as a restock trigger", sent_to == ["ion@example.com"], sent_to)
    finally:
        restore_brevo_send(original_send)
        server.BREVO_API_KEY = ""
        server._require_admin = original_admin


async def scenario_f_shopify_webhook_path_also_triggers():
    """(f) The independent Shopify-webhook write path (update_single_product)
    also fires the same notification on a 0 -> positive transition, since it
    bypasses _apply_product_update entirely (separate update_one/upsert
    call)."""
    db = await fresh_db()
    await db.shopify_products.insert_one(base_product(id="999", stock=0, title="Piesa Shopify"))
    await db.users.insert_one({"id": "u1", "email": "webhook-client@example.com", "name": "Webhook Client"})
    await insert_interest(db, iid="i1", user_id="u1", product_id="999")

    fake_graphql_node = {
        "id": "gid://shopify/Product/999",
        "title": "Piesa Shopify",
        "handle": "piesa-shopify",
        "description": "",
        "tags": [],
        "productType": "Nou",
        "vendor": "AGB",
        "priceRange": {"minVariantPrice": {"amount": "150.0", "currencyCode": "RON"}},
        "images": {"edges": [{"node": {"url": "https://example.com/img2.jpg"}}]},
        "variants": {"edges": [{"node": {"id": "v1", "sku": "SKU-999", "quantityAvailable": 7}}]},
    }

    class _FakeResponse:
        def json(self):
            return {"data": {"product": fake_graphql_node}}

    server.BREVO_API_KEY = "fake-test-key"
    sent_to = []
    original_send = patch_brevo_send(lambda email: sent_to.append(email.to[0]["email"]))
    try:
        with patch.object(server.httpx.AsyncClient, "post", new=AsyncMock(return_value=_FakeResponse())):
            result = await server.update_single_product("999")
        check("f) webhook update reports success", result is True, result)

        persisted = await db.shopify_products.find_one({"id": "999"})
        check("f) stock actually persisted to 7", persisted.get("stock") == 7, persisted.get("stock"))

        # update_single_product fires the notification via asyncio.create_task
        # (it's already fully detached/background itself - no BackgroundTasks
        # object to await here) - yield control so that task gets to run.
        await asyncio.sleep(0.05)

        check("f) Shopify webhook restock also notifies the subscriber", sent_to == ["webhook-client@example.com"], sent_to)
    finally:
        restore_brevo_send(original_send)
        server.BREVO_API_KEY = ""


async def scenario_g_only_stock_alert_type_is_notified():
    """(g) A "favorite" bookmark and a "price_alert" on the same product
    must NOT receive a restock email - only the "stock_alert" row should."""
    db = await fresh_db()
    await db.shopify_products.insert_one(base_product(id="p7", stock=0, title="Piesa mixta"))
    await db.users.insert_one({"id": "u1", "email": "stock-only@example.com", "name": "Stock Only"})
    await db.users.insert_one({"id": "u2", "email": "favorite-only@example.com", "name": "Favorite Only"})
    await db.users.insert_one({"id": "u3", "email": "price-only@example.com", "name": "Price Only"})
    await insert_interest(db, iid="i1", user_id="u1", product_id="p7", itype="stock_alert")
    await insert_interest(db, iid="i2", user_id="u2", product_id="p7", itype="favorite")
    await insert_interest(db, iid="i3", user_id="u3", product_id="p7", itype="price_alert")

    original_admin = patch_require_admin()
    server.BREVO_API_KEY = "fake-test-key"
    sent_to = []
    original_send = patch_brevo_send(lambda email: sent_to.append(email.to[0]["email"]))
    try:
        bt = BackgroundTasks()
        await server.admin_update_product(
            "p7", request=make_admin_request(), product_data=server.ProductUpdate(stock=2),
            background_tasks=bt,
        )
        await bt()
        check("g) only the stock_alert subscriber is emailed", sent_to == ["stock-only@example.com"], sent_to)
        check("g) favorite row untouched", await db.customer_interests.find_one({"id": "i2"}) is not None)
        check("g) price_alert row untouched (never sent, never cleared)", await db.customer_interests.find_one({"id": "i3"}) is not None)
    finally:
        restore_brevo_send(original_send)
        server.BREVO_API_KEY = ""
        server._require_admin = original_admin


async def main():
    await scenario_a_zero_to_positive_notifies_all_active_subscribers()
    await scenario_b_ordinary_increase_does_not_trigger()
    await scenario_c_no_active_interests_is_a_clean_noop()
    await scenario_d_one_failed_send_does_not_block_the_rest()
    await scenario_e_missing_stock_field_counts_as_zero()
    await scenario_f_shopify_webhook_path_also_triggers()
    await scenario_g_only_stock_alert_type_is_notified()

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
