"""Focused, standalone check for the B2B discount (admin_set_b2b_discount /
admin_list_b2b_customers / _get_authoritative_price / create_order) and
abandoned-cart email (admin_send_abandoned_cart_email /
admin_list_abandoned_carts / admin_abandoned_cart_email_log /
_process_one_abandoned_cart / abandoned_cart_email_loop) features.

Same pattern as this directory's other standalone scripts (see
test_stock_checkout.py's own docstring for why this repo uses this style
instead of pytest): monkeypatch server._require_admin (same technique as
test_admin_customer_account_lookup.py) and call the route handlers
directly, no HTTP layer. UNLIKE test_stock_checkout.py, this one does NOT
swap in mongomock - it runs against the real configured MONGO_URL (same as
every script here that isn't explicitly mongomock-based), using clearly
tagged throwaway data (@example.com emails, timestamped session_ids) with
full cleanup in a finally block either way.

Run (from repo root): python scripts/test_b2b_and_abandoned_carts.py
"""
import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402
from fastapi import Request, HTTPException  # noqa: E402

PASS = []
FAIL = []


def check(name, condition, detail=""):
    if condition:
        PASS.append(name)
        print(f"PASS: {name}")
    else:
        FAIL.append(name)
        print(f"FAIL: {name} - {detail}")


def make_request():
    return Request({"type": "http", "headers": [], "method": "PATCH", "path": "/admin/test"})


def patch_require_admin():
    original = server._require_admin

    async def _stub(request):
        return {"id": "admin-1", "email": "staff@example.com", "role": "admin"}

    server._require_admin = _stub
    return original


async def main():
    db = server.db
    ts = int(datetime.utcnow().timestamp())
    target_email = f"b2b-direct-{ts}@example.com"
    await db.users.insert_one({
        "id": f"u-{ts}", "email": target_email, "name": "Direct Test", "phone": "0700000000",
        "password_hash": "x", "is_shopify_customer": False, "created_at": datetime.utcnow(),
        "tokens": [],
    })

    session_id = f"abandoned-direct-{ts}"
    guest_session_id = f"guest-direct-{ts}"
    original = patch_require_admin()
    try:
        # ---- B2B discount admin endpoints ----
        result = await server.admin_set_b2b_discount(
            target_email, server.B2BDiscountUpdate(is_reseller=True, discount_percent=20), make_request()
        )
        check("a) set B2B discount returns updated user", result.get("b2b_discount_percent") == 20, result)

        try:
            await server.admin_set_b2b_discount(
                target_email, server.B2BDiscountUpdate(is_reseller=True, discount_percent=None), make_request()
            )
            check("b) missing discount_percent rejected", False, "no exception raised")
        except HTTPException as e:
            check("b) missing discount_percent rejected -> 400", e.status_code == 400, e.status_code)

        try:
            await server.admin_set_b2b_discount(
                target_email, server.B2BDiscountUpdate(is_reseller=True, discount_percent=150), make_request()
            )
            check("c) out-of-range discount rejected", False, "no exception raised")
        except HTTPException as e:
            check("c) out-of-range discount rejected -> 400", e.status_code == 400, e.status_code)

        try:
            await server.admin_set_b2b_discount(
                "nobody-xyz@example.com", server.B2BDiscountUpdate(is_reseller=True, discount_percent=10), make_request()
            )
            check("d) unknown email rejected", False, "no exception raised")
        except HTTPException as e:
            check("d) unknown email rejected -> 404", e.status_code == 404, e.status_code)

        listed = await server.admin_list_b2b_customers(make_request())
        found = [u for u in listed if u["email"] == target_email]
        check("e) list B2B includes target with correct discount", len(found) == 1 and found[0]["b2b_discount_percent"] == 20, found)

        result2 = await server.admin_set_b2b_discount(
            target_email, server.B2BDiscountUpdate(is_reseller=False, discount_percent=None), make_request()
        )
        check("f) unsetting clears discount", result2.get("is_reseller") is False and result2.get("b2b_discount_percent") is None, result2)

        # ---- B2B discount actually changes the authoritative price ----
        # (add_to_cart/create_order are thin HTTP wrappers around exactly
        # this call - covered end-to-end over real HTTP separately, see the
        # PR/session notes; this is the core pricing logic itself.)
        product = await db.shopify_products.find_one({}, {"id": 1, "title": 1, "price": 1})
        full_price = await server._get_authoritative_price(product["id"])
        discounted_price = await server._get_authoritative_price(product["id"], discount_percent=10)
        check("f2) 10% B2B discount reduces the authoritative price by exactly 10%",
              abs(discounted_price - round(full_price * 0.9, 2)) < 0.01,
              (full_price, discounted_price))
        check("f3) no discount_percent leaves price unchanged",
              await server._get_authoritative_price(product["id"]) == full_price, full_price)

        # ---- Abandoned cart admin endpoints ----
        old_time = datetime.utcnow() - timedelta(hours=30)
        await db.cart.insert_one({
            "id": f"cart-{ts}", "session_id": session_id, "product_id": product["id"],
            "product_name": product.get("title", "x"), "product_image": "", "price": 100.0, "quantity": 2,
            "created_at": old_time, "updated_at": old_time, "user_email": target_email,
        })
        await db.cart.insert_one({
            "id": f"cart-guest-{ts}", "session_id": guest_session_id, "product_id": product["id"],
            "product_name": product.get("title", "x"), "product_image": "", "price": 100.0, "quantity": 1,
            "created_at": old_time, "updated_at": old_time, "user_email": None,
        })

        sent = []

        async def fake_send(recipient_email, recipient_name, cart_url):
            sent.append((recipient_email, cart_url))
            return True

        server.send_abandoned_cart_email = fake_send

        manual_result = await server.admin_send_abandoned_cart_email(session_id, make_request())
        check("g) manual send succeeds", "Email trimis" in manual_result.get("message", ""), manual_result)
        check("h) manual send actually called the email function", len(sent) == 1 and sent[0][0] == target_email, sent)

        try:
            await server.admin_send_abandoned_cart_email(guest_session_id, make_request())
            check("i) guest cart manual send rejected", False, "no exception raised")
        except HTTPException as e:
            check("i) guest cart manual send rejected -> 400", e.status_code == 400, e.status_code)

        try:
            await server.admin_send_abandoned_cart_email("no-such-session", make_request())
            check("j) nonexistent session rejected", False, "no exception raised")
        except HTTPException as e:
            check("j) nonexistent session rejected -> 404", e.status_code == 404, e.status_code)

        abandoned = await server.admin_list_abandoned_carts(make_request())
        sids = [c["session_id"] for c in abandoned]
        check("k) list includes emailed cart, excludes guest cart", session_id in sids and guest_session_id not in sids, sids)
        entry = next(c for c in abandoned if c["session_id"] == session_id)
        check("l) items_count/total_value correct", entry["items_count"] == 2 and entry["total_value"] == 200.0, entry)

        log = await server.admin_abandoned_cart_email_log(make_request(), limit=200)
        log_entry = next((e for e in log if e["session_id"] == session_id and e["trigger"] == "manual"), None)
        check("m) email log has manual entry", log_entry is not None, log_entry)

        # ---- Scheduling logic ----
        sent.clear()
        await server._process_one_abandoned_cart(session_id, target_email, old_time)
        check("n) 30h-old cart triggers auto email #1", len(sent) == 1, sent)

        sent.clear()
        await server._process_one_abandoned_cart(session_id, target_email, old_time)
        check("o) immediate re-run does not resend", len(sent) == 0, sent)

        new_time = datetime.utcnow() - timedelta(hours=1)
        await server._process_one_abandoned_cart(session_id, target_email, new_time)
        tracking = await db.abandoned_cart_tracking.find_one({"session_id": session_id})
        check("p) fresh activity resets sequence", tracking is None or len(tracking.get("emails_sent", [])) == 0, tracking)

    finally:
        server._require_admin = original
        await db.users.delete_many({"email": target_email})
        await db.cart.delete_many({"session_id": {"$in": [session_id, guest_session_id]}})
        await db.abandoned_cart_tracking.delete_many({"session_id": session_id})
        await db.abandoned_cart_email_log.delete_many({"session_id": session_id})

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
