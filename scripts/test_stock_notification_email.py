"""
Focused, standalone check for the stock-notification email
(_send_stock_notification_email in server.py) - fired from POST
/admin/client-interests/notify-email (admin_notify_client_interest_email),
CRM's "Interese clienți" page, "Înștiințează client" button, the moment
staff confirms an out-of-stock item a client asked about is now available.

Not a pytest suite - same mongomock/mongomock-motor + direct-function-call
approach as every other scripts/test_*.py file here (this repo still has no
test framework, tests/ dir, or pytest in requirements.txt). Mirrors
test_shipping_notification_email.py's structure almost exactly - same Brevo
monkeypatch technique, same send-outcome cases - since
_send_stock_notification_email is a near-identical sibling of
_send_shipping_notification_email (same provider, same never-raise
contract).

Additionally covers the behavior this file was actually written for: on a
successful send, _clear_fulfilled_interest_alerts must deactivate the
client's own price_alert/stock_alert toggle(s) for that product (delete the
matching db.customer_interests row(s)) - so the product/account page no
longer shows the alert as active once the client has actually been
notified. Before this existed, that toggle stayed on forever once set,
regardless of staff sending the email. Covers:
  - successful send -> matching stock_alert AND price_alert rows for that
    user+product are both cleared, "favorite" rows are left untouched.
  - failed send (Brevo raises, or BREVO_API_KEY unset) -> customer_interests
    untouched (must never clear an alert that wasn't actually fulfilled).
  - no product_id on the request (manual/legacy interest with no catalog
    id) -> customer_interests untouched, logged not guessed - there's no
    safe way to match it back to the original interest.
  - a DB hiccup while clearing (interest lookup/delete raises) must not
    turn an already-successfully-sent email into a reported failure - the
    function must still return True.

Run (from repo root): python scripts/test_stock_notification_email.py
"""
import asyncio
import os
import sys
from datetime import datetime
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


async def insert_interest(db, *, iid, user_id, product_id, itype):
    await db.customer_interests.insert_one({
        "id": iid, "user_id": user_id, "product_id": product_id, "type": itype,
        "created_at": datetime.utcnow(), "crm_synced": False, "crm_sync_error": None,
        "crm_sync_attempts": 0,
    })


async def scenario_a_no_brevo_api_key_leaves_interests_untouched():
    """(a) BREVO_API_KEY not configured -> False, no exception, and the
    client's alert toggles must stay exactly as they were (nothing was
    actually delivered)."""
    db = await fresh_db()
    await db.users.insert_one({"id": "u1", "email": "ion@example.com", "role": "customer"})
    await insert_interest(db, iid="i1", user_id="u1", product_id="p1", itype="stock_alert")

    original_key = server.BREVO_API_KEY
    server.BREVO_API_KEY = ""
    try:
        result = await server._send_stock_notification_email(
            "ion@example.com", "Ion", "Filtru ulei", "F-100", 50.0, "RON",
            "https://example.com/f100.jpg", product_id="p1",
        )
        check("a) no BREVO_API_KEY -> returns False", result is False, result)
        remaining = await db.customer_interests.count_documents({})
        check("a) interest NOT cleared on failed send", remaining == 1, remaining)
    finally:
        server.BREVO_API_KEY = original_key


async def scenario_b_brevo_raises_leaves_interests_untouched():
    """(b) Brevo call raises -> False, no exception propagates, and the
    alert toggle must not be cleared (email was never actually delivered)."""
    db = await fresh_db()
    await db.users.insert_one({"id": "u1", "email": "ion@example.com", "role": "customer"})
    await insert_interest(db, iid="i1", user_id="u1", product_id="p1", itype="stock_alert")

    server.BREVO_API_KEY = "fake-test-key"
    original_send = patch_brevo_send(lambda email: (_ for _ in ()).throw(RuntimeError("simulated Brevo outage")))
    try:
        result = await server._send_stock_notification_email(
            "ion@example.com", "Ion", "Filtru ulei", "F-100", 50.0, "RON",
            "https://example.com/f100.jpg", product_id="p1",
        )
        check("b) Brevo raises -> returns False, no exception propagates", result is False, result)
        remaining = await db.customer_interests.count_documents({})
        check("b) interest NOT cleared when send fails", remaining == 1, remaining)
    finally:
        restore_brevo_send(original_send)
        server.BREVO_API_KEY = ""


async def scenario_c_successful_send_clears_stock_and_price_alerts_not_favorite():
    """(c) Successful send with a product_id -> True, and BOTH the
    stock_alert and price_alert rows for that user+product are cleared, but
    a "favorite" row for the same user+product is left alone (it's a
    bookmark, not a notification subscription). An unrelated interest (same
    type, different product) must also survive untouched."""
    db = await fresh_db()
    await db.users.insert_one({"id": "u1", "email": "maria@example.com", "role": "customer"})
    await insert_interest(db, iid="i-stock", user_id="u1", product_id="p1", itype="stock_alert")
    await insert_interest(db, iid="i-price", user_id="u1", product_id="p1", itype="price_alert")
    await insert_interest(db, iid="i-fav", user_id="u1", product_id="p1", itype="favorite")
    await insert_interest(db, iid="i-other", user_id="u1", product_id="p2", itype="stock_alert")

    server.BREVO_API_KEY = "fake-test-key"
    captured = {}

    def _capture(send_smtp_email):
        captured["email"] = send_smtp_email
        return None

    original_send = patch_brevo_send(_capture)
    try:
        result = await server._send_stock_notification_email(
            "maria@example.com", "Maria", "Filtru ulei", "F-100", 50.0, "RON",
            "https://example.com/f100.jpg", product_id="p1",
        )
        check("c) successful send -> returns True", result is True, result)
        sent = captured.get("email")
        check("c) sent to customer's email", sent.to[0]["email"] == "maria@example.com", sent.to if sent else None)

        check(
            "c) stock_alert cleared",
            await db.customer_interests.find_one({"id": "i-stock"}) is None,
        )
        check(
            "c) price_alert cleared too (same product, no way to tell which one was meant)",
            await db.customer_interests.find_one({"id": "i-price"}) is None,
        )
        check(
            "c) favorite (bookmark) left untouched",
            await db.customer_interests.find_one({"id": "i-fav"}) is not None,
        )
        check(
            "c) unrelated product's stock_alert left untouched",
            await db.customer_interests.find_one({"id": "i-other"}) is not None,
        )

        notif = await db.notifications.find_one({"user_id": "u1"})
        check("c) in-app notification still created", notif is not None, notif)
    finally:
        restore_brevo_send(original_send)
        server.BREVO_API_KEY = ""


async def scenario_d_no_product_id_skips_clearing():
    """(d) No product_id on the request (manual staff-noted interest, or a
    legacy interest synced before product_id existed) -> email still sends
    successfully, but there's no safe way to match it back to a
    customer_interests row, so nothing is cleared - the existing alert(s)
    for that user must survive untouched."""
    db = await fresh_db()
    await db.users.insert_one({"id": "u1", "email": "maria@example.com", "role": "customer"})
    await insert_interest(db, iid="i-stock", user_id="u1", product_id="p1", itype="stock_alert")

    server.BREVO_API_KEY = "fake-test-key"
    original_send = patch_brevo_send(lambda email: None)
    try:
        result = await server._send_stock_notification_email(
            "maria@example.com", "Maria", "Filtru ulei", "F-100", None, None, None,
            product_id=None,
        )
        check("d) no product_id -> still returns True (email sent)", result is True, result)
        remaining = await db.customer_interests.count_documents({})
        check("d) no product_id -> nothing cleared (can't match safely)", remaining == 1, remaining)
    finally:
        restore_brevo_send(original_send)
        server.BREVO_API_KEY = ""


async def scenario_e_clearing_db_error_does_not_turn_success_into_failure():
    """(e) A DB error while clearing the interest (e.g. delete_many raises)
    must be swallowed by _clear_fulfilled_interest_alerts itself - the
    email was already sent successfully, so the function must still report
    True to the caller (staff shouldn't see a false "send failed" error for
    something that has nothing to do with delivery)."""
    db = await fresh_db()
    await db.users.insert_one({"id": "u1", "email": "maria@example.com", "role": "customer"})
    await insert_interest(db, iid="i-stock", user_id="u1", product_id="p1", itype="stock_alert")

    server.BREVO_API_KEY = "fake-test-key"
    original_send = patch_brevo_send(lambda email: None)

    class _ExplodingInterests:
        def delete_many(self, *args, **kwargs):
            raise RuntimeError("simulated Mongo hiccup")

    original_collection = db.customer_interests
    try:
        server.db.customer_interests = _ExplodingInterests()
        result = await server._send_stock_notification_email(
            "maria@example.com", "Maria", "Filtru ulei", "F-100", 50.0, "RON",
            None, product_id="p1",
        )
        check("e) clearing DB error doesn't turn a sent email into a failure", result is True, result)
    finally:
        server.db.customer_interests = original_collection
        restore_brevo_send(original_send)
        server.BREVO_API_KEY = ""
        remaining = await db.customer_interests.count_documents({})
        check("e) interest untouched when the delete itself failed", remaining == 1, remaining)


async def main():
    await scenario_a_no_brevo_api_key_leaves_interests_untouched()
    await scenario_b_brevo_raises_leaves_interests_untouched()
    await scenario_c_successful_send_clears_stock_and_price_alerts_not_favorite()
    await scenario_d_no_product_id_skips_clearing()
    await scenario_e_clearing_db_error_does_not_turn_success_into_failure()

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
