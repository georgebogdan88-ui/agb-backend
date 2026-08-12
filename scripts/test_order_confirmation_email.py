"""
Focused, standalone check for the new order-confirmation email
(_send_order_confirmation_email in server.py) and its trigger point in
POST /orders (create_order).

Not a pytest suite - mirrors scripts/test_stock_checkout.py's and
scripts/test_product_seo_fields.py's approach (this repo still has no test
framework, tests/ dir, or pytest in requirements.txt). Uses mongomock +
mongomock-motor to swap in an in-memory Mongo double for server.client/db,
and calls server.create_order() directly (no HTTP layer/TestClient) with a
real starlette.background.BackgroundTasks() instance, which is awaited
explicitly after the endpoint returns to actually run the fire-and-forget
tasks it queued (create_order() itself only calls .add_task(), same as the
real ASGI request/response cycle - Starlette runs queued background tasks
only after the response has already been sent).

Covers:
  (a) order creation succeeds and returns normally even when the Brevo
      send call inside _send_order_confirmation_email raises - proves the
      customer-facing response/DB write can NEVER be affected by an email
      failure, matching the same never-raise contract as sync_order_to_crm.
  (b) the confirmation-email background task actually gets queued by
      create_order (alongside sync_order_to_crm, without replacing it).
  (c) _send_order_confirmation_email in isolation: returns True on a
      successful (mocked) Brevo send, returns False (never raises) when
      the Brevo call raises, and returns False (never raises) when
      BREVO_API_KEY isn't configured.

Brevo's send_transac_email is monkeypatched on the sib_api_v3_sdk module
itself (TransactionalEmailsApi.send_transac_email) - _send_order_
confirmation_email imports sib_api_v3_sdk lazily inside the function body,
but that's the same cached module object from sys.modules, so patching the
class method here affects the real call site with no network access ever
made either way.

Run (from repo root): python scripts/test_order_confirmation_email.py
"""
import asyncio
import os
import sys
from pathlib import Path

os.environ.setdefault("MONGO_URL", "mongodb://fake-for-import-only/")
os.environ.setdefault("DB_NAME", "fake_db_for_import_only")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mongomock_motor  # noqa: E402
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


async def seed_product(db, product_id, stock=10, price=100.0, title="Piesă test"):
    await db.shopify_products.insert_one({
        "id": product_id,
        "title": title,
        "price": price,
        "stock": stock,
        "stock_status": "in_stock",
    })


def make_order_data(product_id="p1", quantity=2):
    return server.OrderCreate(
        session_id="sess-1",
        items=[{"product_id": product_id, "quantity": quantity}],
        customer=server.CustomerInfo(
            name="Ion Popescu",
            email="ion@example.com",
            phone="0700000000",
            address="Str. Exemplu 1",
            city="Cluj-Napoca",
            county="Cluj",
            postal_code="400000",
        ),
        subtotal=0.0,
        total=0.0,
        payment_method="ramburs",
    )


def patch_brevo_send(behavior):
    """behavior: callable(send_smtp_email) -> None, invoked in place of the
    real network call. Returns the original method so callers can restore
    it."""
    import sib_api_v3_sdk

    original = sib_api_v3_sdk.TransactionalEmailsApi.send_transac_email

    def _stub(self, send_smtp_email, **kwargs):
        return behavior(send_smtp_email)

    sib_api_v3_sdk.TransactionalEmailsApi.send_transac_email = _stub
    return original


def restore_brevo_send(original):
    import sib_api_v3_sdk
    sib_api_v3_sdk.TransactionalEmailsApi.send_transac_email = original


async def scenario_a_order_succeeds_even_if_email_send_raises():
    """(a) create_order() returns a normal, correct Order and the order is
    persisted in the DB, even though the Brevo call inside the background
    email task raises when it's actually run afterward."""
    db = await fresh_db()
    await seed_product(db, "p1", stock=10, price=100.0)
    original_key = server.BREVO_API_KEY
    server.BREVO_API_KEY = "fake-test-key"
    original_send = patch_brevo_send(lambda email: (_ for _ in ()).throw(RuntimeError("simulated Brevo outage")))
    try:
        order_data = make_order_data("p1", quantity=2)
        bt = BackgroundTasks()
        order = await server.create_order(order_data, bt)

        check("a) order response returned normally", order is not None)
        check("a) order total computed correctly", order.total == 225.0, order.total)  # 2*100 + 25 shipping
        check("a) order persisted in DB", (await db.orders.find_one({"id": order.id})) is not None)
        check("a) stock decremented (order fully processed)", (await db.shopify_products.find_one({"id": "p1"}))["stock"] == 8)

        # Now actually run the queued background tasks (sync_order_to_crm +
        # _send_order_confirmation_email), exactly as Starlette would after
        # sending the response - must not raise despite the mocked Brevo
        # failure.
        await bt()
        check("a) background tasks (incl. failing email send) ran without raising", True)

        # The order document itself must be completely unaffected by the
        # email failure - no fields mutated by the email path.
        doc_after = await db.orders.find_one({"id": order.id})
        check("a) order doc unaffected by email failure", doc_after["total"] == 225.0, doc_after)
    finally:
        restore_brevo_send(original_send)
        server.BREVO_API_KEY = original_key


async def scenario_b_confirmation_email_task_is_queued_alongside_crm_sync():
    """(b) create_order() queues sync_order_to_crm, _send_order_confirmation_email,
    AND _send_new_order_staff_notification as background tasks - none of the
    three replaces or crowds out the others."""
    db = await fresh_db()
    await seed_product(db, "p1", stock=10, price=50.0)
    order_data = make_order_data("p1", quantity=1)
    bt = BackgroundTasks()
    await server.create_order(order_data, bt)

    queued_funcs = [task.func for task in bt.tasks]
    check("b) sync_order_to_crm queued", server.sync_order_to_crm in queued_funcs, queued_funcs)
    check("b) _send_order_confirmation_email queued", server._send_order_confirmation_email in queued_funcs, queued_funcs)
    check("b) _send_new_order_staff_notification queued", server._send_new_order_staff_notification in queued_funcs, queued_funcs)
    check("b) exactly three background tasks queued", len(bt.tasks) == 3, len(bt.tasks))


async def scenario_c_send_function_isolated_behaviors():
    """(c) _send_order_confirmation_email in isolation: True on success,
    False (never raises) on a Brevo-side exception, False (never raises)
    when BREVO_API_KEY is unset."""
    await fresh_db()
    order = server.Order(
        session_id="sess-2",
        items=[{"product_id": "p1", "product_name": "Piesă test", "quantity": 1, "price": 50.0}],
        customer=server.CustomerInfo(
            name="Maria Ionescu", email="maria@example.com", phone="0711111111",
            address="Str. B 2", city="Iași", county="Iași", postal_code="700000",
        ),
        subtotal=50.0, shipping=25.0, total=75.0, payment_method="ramburs",
    )

    # (c1) BREVO_API_KEY not configured -> False, no exception, no network call attempted.
    original_key = server.BREVO_API_KEY
    server.BREVO_API_KEY = ""
    try:
        result = await server._send_order_confirmation_email(order)
        check("c1) no BREVO_API_KEY -> returns False", result is False, result)
    finally:
        server.BREVO_API_KEY = original_key

    # (c2) Brevo call raises -> False, no exception propagates.
    server.BREVO_API_KEY = "fake-test-key"
    original_send = patch_brevo_send(lambda email: (_ for _ in ()).throw(RuntimeError("simulated failure")))
    try:
        result = await server._send_order_confirmation_email(order)
        check("c2) Brevo raises -> returns False, no exception propagates", result is False, result)
    finally:
        restore_brevo_send(original_send)
        server.BREVO_API_KEY = original_key

    # (c3) Brevo call succeeds -> True, and email/subject/content carry the
    # expected order data.
    server.BREVO_API_KEY = "fake-test-key"
    captured = {}

    def _capture(send_smtp_email):
        captured["email"] = send_smtp_email
        return None

    original_send = patch_brevo_send(_capture)
    try:
        result = await server._send_order_confirmation_email(order)
        check("c3) successful send -> returns True", result is True, result)
        sent = captured.get("email")
        check("c3) sent to customer's email", sent.to[0]["email"] == "maria@example.com", sent.to if sent else None)
        check("c3) subject mentions order id", order.id in sent.subject, sent.subject if sent else None)
        check("c3) body mentions item name", "Piesă test" in sent.html_content, "missing")
        check("c3) body mentions total", "75.00 RON" in sent.html_content, "missing")
    finally:
        restore_brevo_send(original_send)
        server.BREVO_API_KEY = original_key


async def main():
    await scenario_a_order_succeeds_even_if_email_send_raises()
    await scenario_b_confirmation_email_task_is_queued_alongside_crm_sync()
    await scenario_c_send_function_isolated_behaviors()

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
