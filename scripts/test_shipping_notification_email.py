"""
Focused, standalone check for the new shipping-notification email
(_send_shipping_notification_email in server.py) - fired from PATCH
/admin/orders/{order_id}/courier (admin_update_order_courier) the moment
agb-crm pushes a freshly-generated FAN Courier AWB for an order (see
scripts/test_courier_tracking_endpoints.py for that trigger point's own
coverage - the once-per-order queueing guarantee lives there, alongside the
rest of admin_update_order_courier's behavior).

Not a pytest suite - same mongomock/mongomock-motor + direct-function-call
approach as every other scripts/test_*.py file here (this repo still has no
test framework, tests/ dir, or pytest in requirements.txt). Mirrors
test_order_confirmation_email.py's scenario_c structure almost exactly -
same Brevo monkeypatch technique, same three send-outcome cases - since
_send_shipping_notification_email is a near-identical sibling of
_send_order_confirmation_email (same provider, same never-raise contract).

Additionally covers the one behavior order-confirmation doesn't have: a
successful send also mirrors into an in-app notification (db.notifications)
via _create_user_notification, but ONLY when the customer's email actually
has a web/app account - same "best-effort, no account = no row" contract
_create_user_notification documents itself, exercised here directly instead
of assumed.

Run (from repo root): python scripts/test_shipping_notification_email.py
"""
import asyncio
import os
import sys
from pathlib import Path

os.environ.setdefault("MONGO_URL", "mongodb://fake-for-import-only/")
os.environ.setdefault("DB_NAME", "fake_db_for_import_only")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mongomock_motor  # noqa: E402
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


def make_shipped_order(email="maria@example.com", awb="AWB-12345", service="Standard"):
    return server.Order(
        session_id="sess-1",
        items=[{"product_id": "p1", "product_name": "Piesă test", "quantity": 1, "price": 50.0}],
        customer=server.CustomerInfo(
            name="Maria Ionescu", email=email, phone="0711111111",
            address="Str. B 2", city="Iași", county="Iași", postal_code="700000",
        ),
        subtotal=50.0, shipping=25.0, total=75.0, payment_method="ramburs",
        courier_awb_number=awb, courier_service=service,
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


async def scenario_a_no_brevo_api_key():
    """(a) BREVO_API_KEY not configured -> False, no exception, no network
    call attempted."""
    await fresh_db()
    order = make_shipped_order()
    original_key = server.BREVO_API_KEY
    server.BREVO_API_KEY = ""
    try:
        result = await server._send_shipping_notification_email(order)
        check("a) no BREVO_API_KEY -> returns False", result is False, result)
    finally:
        server.BREVO_API_KEY = original_key


async def scenario_b_brevo_raises():
    """(b) Brevo call raises -> False, no exception propagates (fire-and-
    forget contract - a failed shipping email must never affect the
    already-completed AWB save)."""
    await fresh_db()
    order = make_shipped_order()
    server.BREVO_API_KEY = "fake-test-key"
    original_send = patch_brevo_send(lambda email: (_ for _ in ()).throw(RuntimeError("simulated Brevo outage")))
    try:
        result = await server._send_shipping_notification_email(order)
        check("b) Brevo raises -> returns False, no exception propagates", result is False, result)
    finally:
        restore_brevo_send(original_send)
        server.BREVO_API_KEY = ""


async def scenario_c_successful_send_content_and_in_app_notification():
    """(c) Successful send -> True, correct recipient/subject/content (AWB
    number, order id, courier service), and mirrors into an in-app
    notification for a customer who has a real account."""
    db = await fresh_db()
    await db.users.insert_one({"id": "user-1", "email": "maria@example.com", "role": "customer"})
    order = make_shipped_order(email="maria@example.com", awb="AWB-99999", service="Standard")
    server.BREVO_API_KEY = "fake-test-key"
    captured = {}

    def _capture(send_smtp_email):
        captured["email"] = send_smtp_email
        return None

    original_send = patch_brevo_send(_capture)
    try:
        result = await server._send_shipping_notification_email(order)
        check("c) successful send -> returns True", result is True, result)
        sent = captured.get("email")
        check("c) sent to customer's email", sent.to[0]["email"] == "maria@example.com", sent.to if sent else None)
        check("c) subject mentions order id", order.id in sent.subject, sent.subject if sent else None)
        check("c) body mentions AWB number", "AWB-99999" in sent.html_content, "missing")
        check("c) body mentions courier service", "Standard" in sent.html_content, "missing")

        notif = await db.notifications.find_one({"user_id": "user-1"})
        check("c) in-app notification created for account holder", notif is not None, notif)
        if notif:
            check("c) in-app notification type is order_shipped", notif.get("type") == "order_shipped", notif)
            check("c) in-app notification body mentions AWB", "AWB-99999" in (notif.get("body") or ""), notif)
    finally:
        restore_brevo_send(original_send)
        server.BREVO_API_KEY = ""


async def scenario_d_no_account_no_in_app_notification_but_still_sends_email():
    """(d) A guest-checkout customer (no web/app account with this email)
    still gets the email - _create_user_notification is best-effort and
    silently no-ops when there's no account, exactly as it documents itself
    (same contract already relied on by _send_stock_notification_email)."""
    db = await fresh_db()  # no db.users row for this email
    order = make_shipped_order(email="guest@example.com", awb="AWB-GUEST")
    server.BREVO_API_KEY = "fake-test-key"
    original_send = patch_brevo_send(lambda email: None)
    try:
        result = await server._send_shipping_notification_email(order)
        check("d) guest checkout still gets the email -> returns True", result is True, result)
        notif = await db.notifications.find_one({"user_id": {"$exists": True}})
        check("d) no in-app notification created (no account)", notif is None, notif)
    finally:
        restore_brevo_send(original_send)
        server.BREVO_API_KEY = ""


async def main():
    await scenario_a_no_brevo_api_key()
    await scenario_b_brevo_raises()
    await scenario_c_successful_send_content_and_in_app_notification()
    await scenario_d_no_account_no_in_app_notification_but_still_sends_email()

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
