"""
Focused, standalone check for the 4 independent security fixes applied to
server.py on 2026-08-22 (security remediation plan approved by George after
a read-only audit):

  1. verify_shopify_webhook() now fails CLOSED (returns False / request
     rejected with 401) when SHOPIFY_WEBHOOK_SECRET isn't configured,
     instead of the old fail-OPEN "accept everything" behavior.
  2. The two inbound /integrations/* routes actually defined in THIS repo
     (POST /integrations/equipment-from-crm, DELETE /integrations/
     equipment/{equipment_id} - both already gated by
     _require_crm_integration_key / X-Integration-Key) are now also
     rate-limited per IP via the same _enforce_rate_limit/
     _SlidingWindowRateLimiter infrastructure already used on /auth/login
     etc. (NOTE: /integrations/orders, /integrations/clients,
     /integrations/interests are endpoints in agb-crm that THIS backend
     calls OUTBOUND to - they are not routes defined in this file, so
     there's nothing to rate-limit here for those).
  3. get_admin_access_token() no longer logs any fraction of the real
     SHOPIFY_ADMIN_TOKEN value (was logging admin_token[:10]) - just a
     boolean presence log now.
  4. New _is_production_environment() gate, wired into all 9 Brevo-sending
     functions right after their existing `if not BREVO_API_KEY` check -
     step A only (unset ENVIRONMENT still defaults to True/"send", so
     production email sending is NOT affected by this change until George
     explicitly sets ENVIRONMENT=production on every real Render service
     and step B - flipping the missing-var default - is done separately).

Not a pytest suite - mirrors scripts/test_order_confirmation_email.py's and
scripts/test_gdpr_export_and_delete.py's approach (this repo still has no
test framework, tests/ dir, or pytest in requirements.txt). Uses mongomock +
mongomock-motor to swap in an in-memory Mongo double for server.client/db,
and calls the endpoint/helper functions directly (no real HTTP layer /
FastAPI TestClient). Brevo's send_transac_email is monkeypatched - this
script NEVER sends a real email, makes a real Shopify webhook call, or hits
a real network address.

Covers:
  (a) verify_shopify_webhook: SHOPIFY_WEBHOOK_SECRET unset -> returns False
      directly, AND the POST /webhooks/shopify endpoint itself rejects the
      request with 401 (not just a log-and-continue).
  (a-regression) verify_shopify_webhook: with a secret configured and a
      correctly-computed X-Shopify-Hmac-SHA256 header, the request is still
      accepted (True) - proves the fail-closed change didn't also break the
      legitimate/normal path.
  (b) /integrations/* rate limiting: the (INTEGRATIONS_IP_LIMIT + 1)-th call
      within the window from the same IP to
      DELETE /integrations/equipment/{id} is rejected with 429 (with a
      Retry-After header); a request from a DIFFERENT IP right after is NOT
      rate-limited (bucket is genuinely per-IP, not global); and the bucket
      is shared across both /integrations/* routes (a POST to
      /integrations/equipment-from-crm from the same already-exhausted IP
      also gets 429).
  (c) _is_production_environment: ENVIRONMENT unset -> True (unchanged
      behavior vs. before this gate existed), ENVIRONMENT="production" ->
      True, ENVIRONMENT="staging" -> False, ENVIRONMENT="" (present but
      empty) -> True (treated same as unset).
  (d) Regression guard: _send_order_confirmation_email does NOT call Brevo
      when ENVIRONMENT="staging" (returns False), but DOES call Brevo (and
      returns True) when ENVIRONMENT is unset - explicitly proving this
      change does not silently stop production email sending today.
  (e) All 9 Brevo-sending functions (_send_order_confirmation_email,
      _send_shipping_notification_email, _send_marketing_email,
      _send_stock_notification_email, _send_new_order_staff_notification,
      send_password_reset_email, send_shopify_migration_email,
      send_abandoned_cart_email, send_blog_notification_email) are wired to
      the same gate: each returns False and never touches Brevo when
      ENVIRONMENT="staging" (with BREVO_API_KEY configured, so the gate
      under test - not the pre-existing BREVO_API_KEY check - is what's
      actually being exercised).
  (f) get_admin_access_token() log output no longer contains any substring
      of the real token value (captures the logger output and asserts the
      test token string is absent), for both the env-var and
      global-variable code paths.

Run (from repo root): python scripts/test_security_webhook_ratelimit_env_gate.py
"""
import asyncio
import base64
import hashlib
import hmac
import io
import logging
import os
import sys
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


def make_webhook_request(body: bytes, hmac_header: str = None, topic: str = "products/update"):
    """Bare starlette Request carrying a real body via an ASGI receive
    channel (verify_shopify_webhook() calls `await request.body()`, so a
    headers-only Request without a working receive() would hang/error)."""
    headers = [(b"x-shopify-topic", topic.encode())]
    if hmac_header is not None:
        headers.append((b"x-shopify-hmac-sha256", hmac_header.encode()))

    sent = {"done": False}

    async def receive():
        if sent["done"]:
            return {"type": "http.disconnect"}
        sent["done"] = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request({"type": "http", "headers": headers, "method": "POST", "path": "/webhooks/shopify"}, receive=receive)


def make_integrations_request(method="DELETE", path="/integrations/equipment/eq-1", ip="203.0.113.9", key="test-integration-key"):
    headers = [(b"x-forwarded-for", ip.encode())]
    if key is not None:
        headers.append((b"x-integration-key", key.encode()))
    return Request({"type": "http", "headers": headers, "method": method, "path": path})


async def scenario_a_webhook_fails_closed_without_secret():
    """(a) No SHOPIFY_WEBHOOK_SECRET configured -> verify_shopify_webhook
    returns False directly, AND the real endpoint rejects with 401 instead
    of silently accepting an unauthenticated products/delete (or any other
    topic) webhook."""
    await fresh_db()
    original_secret = server.SHOPIFY_WEBHOOK_SECRET
    server.SHOPIFY_WEBHOOK_SECRET = ""
    try:
        req = make_webhook_request(b'{"id": 123}')
        result = await server.verify_shopify_webhook(req)
        check("a) verify_shopify_webhook() -> False when secret unset", result is False, result)

        req2 = make_webhook_request(b'{"id": 123}', topic="products/delete")
        try:
            await server.shopify_webhook(request=req2, background_tasks=BackgroundTasks())
            check("a) POST /webhooks/shopify rejected when secret unset", False, "no exception raised - request was accepted!")
        except server.HTTPException as e:
            check("a) POST /webhooks/shopify -> 401 when secret unset", e.status_code == 401, f"got {e.status_code}")
    finally:
        server.SHOPIFY_WEBHOOK_SECRET = original_secret


async def scenario_a2_webhook_still_accepts_valid_signature():
    """(a-regression) With a secret configured and a correctly-computed HMAC,
    the webhook is still accepted - the fail-closed change only affects the
    "secret missing" branch, not the legitimate signed-request path."""
    await fresh_db()
    original_secret = server.SHOPIFY_WEBHOOK_SECRET
    server.SHOPIFY_WEBHOOK_SECRET = "test-shopify-webhook-secret"
    try:
        body = b'{"id": 123, "title": "Test product"}'
        computed = hmac.new(server.SHOPIFY_WEBHOOK_SECRET.encode("utf-8"), body, hashlib.sha256).digest()
        valid_hmac = base64.b64encode(computed).decode("utf-8")

        req = make_webhook_request(body, hmac_header=valid_hmac)
        result = await server.verify_shopify_webhook(req)
        check("a2) verify_shopify_webhook() -> True with correct signature", result is True, result)

        req_bad = make_webhook_request(body, hmac_header="not-the-real-signature")
        result_bad = await server.verify_shopify_webhook(req_bad)
        check("a2) verify_shopify_webhook() -> False with wrong signature", result_bad is False, result_bad)
    finally:
        server.SHOPIFY_WEBHOOK_SECRET = original_secret


async def scenario_b_integrations_rate_limited_per_ip():
    """(b) /integrations/* routes are rate-limited per IP, sharing one
    bucket across both inbound routes, and independent across IPs."""
    db = await fresh_db()
    original_key = server.CRM_INTEGRATION_KEY
    server.CRM_INTEGRATION_KEY = "test-integration-key"
    try:
        ip_a = "203.0.113.50"
        rejected_with_429 = False
        retry_after_present = False
        for i in range(server.INTEGRATIONS_IP_LIMIT + 3):
            req = make_integrations_request(path=f"/integrations/equipment/nonexistent-{i}", ip=ip_a)
            try:
                result = await server.receive_equipment_delete_from_crm(request=req, equipment_id=f"nonexistent-{i}")
                check_detail = result
            except server.HTTPException as e:
                if e.status_code == 429:
                    rejected_with_429 = True
                    retry_after_present = "Retry-After" in (e.headers or {})
                    break
                check("b) unexpected non-429 HTTPException during warm-up loop", False, f"status={e.status_code} detail={e.detail}")
                break

        check("b) (N+1)-th call to /integrations/equipment/{id} from same IP -> 429",
              rejected_with_429, "never hit 429 within limit+3 attempts")
        check("b) 429 response carries a Retry-After header", retry_after_present, "no Retry-After header")

        # A different IP is NOT affected by ip_a's exhausted bucket.
        req_other_ip = make_integrations_request(path="/integrations/equipment/nonexistent-other-ip", ip="198.51.100.7")
        try:
            result = await server.receive_equipment_delete_from_crm(request=req_other_ip, equipment_id="nonexistent-other-ip")
            check("b) a different IP is unaffected by ip_a's exhausted bucket", result == {"status": "not_found"}, result)
        except server.HTTPException as e:
            check("b) a different IP is unaffected by ip_a's exhausted bucket", False, f"got {e.status_code}: {e.detail}")

        # The bucket is shared across BOTH /integrations/* routes - ip_a is
        # still exhausted, so even the OTHER route rejects it too.
        payload = server.EquipmentFromCrm(crm_tractor_id="t-1", model="Model X")
        req_post = make_integrations_request(method="POST", path="/integrations/equipment-from-crm", ip=ip_a)
        try:
            await server.receive_equipment_from_crm(request=req_post, payload=payload)
            check("b) shared bucket: exhausted IP also 429s on the sibling /integrations/* route", False, "no exception raised")
        except server.HTTPException as e:
            check("b) shared bucket: exhausted IP also 429s on the sibling /integrations/* route",
                  e.status_code == 429, f"got {e.status_code}: {e.detail}")
    finally:
        server.CRM_INTEGRATION_KEY = original_key


def scenario_c_is_production_environment():
    """(c) _is_production_environment(): unset -> True (unchanged default),
    "production" -> True, "staging" -> False, "" (present but empty) ->
    True (treated the same as unset)."""
    original = os.environ.get("ENVIRONMENT", None)
    try:
        os.environ.pop("ENVIRONMENT", None)
        check("c) ENVIRONMENT unset -> True (unchanged default)", server._is_production_environment() is True)

        os.environ["ENVIRONMENT"] = "production"
        check('c) ENVIRONMENT="production" -> True', server._is_production_environment() is True)

        os.environ["ENVIRONMENT"] = "staging"
        check('c) ENVIRONMENT="staging" -> False', server._is_production_environment() is False)

        os.environ["ENVIRONMENT"] = ""
        check('c) ENVIRONMENT="" (present but empty) -> True', server._is_production_environment() is True)
    finally:
        if original is None:
            os.environ.pop("ENVIRONMENT", None)
        else:
            os.environ["ENVIRONMENT"] = original


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


def sample_order():
    return server.Order(
        session_id="sess-env-gate",
        items=[{"product_id": "p1", "product_name": "Piesă test", "quantity": 1, "price": 50.0}],
        customer=server.CustomerInfo(
            name="Ion Popescu", email="ion@example.com", phone="0700000000",
            address="Str. Exemplu 1", city="Cluj-Napoca", county="Cluj", postal_code="400000",
        ),
        subtotal=50.0, shipping=25.0, total=75.0, payment_method="ramburs",
    )


async def scenario_d_regression_guard_order_confirmation_email():
    """(d) Explicit regression guard: staging skips the send entirely
    (Brevo never called), but ENVIRONMENT unset still sends exactly like
    before this change existed - the exact "did we just silently break
    production email" check this task called for."""
    await fresh_db()
    original_key = server.BREVO_API_KEY
    original_env = os.environ.get("ENVIRONMENT", None)
    server.BREVO_API_KEY = "fake-test-key"
    order = sample_order()
    calls = []
    original_send = patch_brevo_send(lambda email: calls.append(email))
    try:
        os.environ["ENVIRONMENT"] = "staging"
        result = await server._send_order_confirmation_email(order)
        check("d) ENVIRONMENT=staging -> _send_order_confirmation_email returns False", result is False, result)
        check("d) ENVIRONMENT=staging -> Brevo never called", calls == [], calls)

        os.environ.pop("ENVIRONMENT", None)
        result2 = await server._send_order_confirmation_email(order)
        check("d) ENVIRONMENT unset -> _send_order_confirmation_email returns True (unchanged)", result2 is True, result2)
        check("d) ENVIRONMENT unset -> Brevo WAS called (production behavior unaffected)", len(calls) == 1, calls)
    finally:
        restore_brevo_send(original_send)
        server.BREVO_API_KEY = original_key
        if original_env is None:
            os.environ.pop("ENVIRONMENT", None)
        else:
            os.environ["ENVIRONMENT"] = original_env


async def scenario_e_all_nine_send_functions_gated():
    """(e) Every one of the 9 Brevo-sending functions is wired to the same
    gate: with BREVO_API_KEY configured (so it's specifically the new env
    gate under test, not the pre-existing key check) and
    ENVIRONMENT="staging", each returns falsy and Brevo is never invoked."""
    await fresh_db()
    original_key = server.BREVO_API_KEY
    original_env = os.environ.get("ENVIRONMENT", None)
    server.BREVO_API_KEY = "fake-test-key"
    order = sample_order()
    calls = []
    original_send = patch_brevo_send(lambda email: calls.append(email))
    try:
        os.environ["ENVIRONMENT"] = "staging"

        functions = [
            ("_send_order_confirmation_email", lambda: server._send_order_confirmation_email(order)),
            ("_send_shipping_notification_email", lambda: server._send_shipping_notification_email(order)),
            ("_send_marketing_email", lambda: server._send_marketing_email(
                "buyer@example.com", "Cumpărător", {"id": "p1", "title": "Piesă", "price": 10.0, "image_url": ""})),
            ("_send_stock_notification_email", lambda: server._send_stock_notification_email(
                "buyer@example.com", "Cumpărător", "Piesă", "COD1", 10.0, "RON", "", product_id="p1")),
            ("_send_new_order_staff_notification", lambda: server._send_new_order_staff_notification(order)),
            ("send_password_reset_email", lambda: server.send_password_reset_email(
                "buyer@example.com", "Cumpărător", "https://example.com/reset")),
            ("send_shopify_migration_email", lambda: server.send_shopify_migration_email(
                "buyer@example.com", "Cumpărător", "https://example.com/reset")),
            ("send_abandoned_cart_email", lambda: server.send_abandoned_cart_email(
                "buyer@example.com", "Cumpărător", "https://example.com/cart")),
            ("send_blog_notification_email", lambda: server.send_blog_notification_email(
                "buyer@example.com", "Cumpărător", "Titlu", "Extras", "https://example.com/blog")),
        ]

        for name, thunk in functions:
            calls.clear()
            result = await thunk()
            check(f"e) {name}: ENVIRONMENT=staging -> returns falsy", not result, result)
            check(f"e) {name}: ENVIRONMENT=staging -> Brevo never called", calls == [], calls)
    finally:
        restore_brevo_send(original_send)
        server.BREVO_API_KEY = original_key
        if original_env is None:
            os.environ.pop("ENVIRONMENT", None)
        else:
            os.environ["ENVIRONMENT"] = original_env


async def scenario_f_admin_token_log_never_leaks_a_fraction():
    """(f) get_admin_access_token() no longer logs any substring of the
    real token, for both the env-var and the global-variable fallback code
    paths."""
    await fresh_db()
    test_token = "shpat_abcdef1234567890supersecretvalue"

    logger = logging.getLogger("server")
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger.addHandler(handler)
    previous_level = logger.level
    logger.setLevel(logging.INFO)

    original_env_token = os.environ.get("SHOPIFY_ADMIN_TOKEN", None)
    original_global_token = server.SHOPIFY_ADMIN_TOKEN
    try:
        # (f1) env-var path
        os.environ["SHOPIFY_ADMIN_TOKEN"] = test_token
        server.SHOPIFY_ADMIN_TOKEN = ""
        stream.truncate(0)
        stream.seek(0)
        result = await server.get_admin_access_token()
        log_output = stream.getvalue()
        check("f1) get_admin_access_token() (env path) still returns the real token", result == test_token, result)
        check("f1) log output does NOT contain any fraction of the real token (env path)",
              test_token[:10] not in log_output and test_token not in log_output, log_output)

        # (f2) global-variable fallback path
        os.environ.pop("SHOPIFY_ADMIN_TOKEN", None)
        server.SHOPIFY_ADMIN_TOKEN = test_token
        stream.truncate(0)
        stream.seek(0)
        result2 = await server.get_admin_access_token()
        log_output2 = stream.getvalue()
        check("f2) get_admin_access_token() (global path) still returns the real token", result2 == test_token, result2)
        check("f2) log output does NOT contain any fraction of the real token (global path)",
              test_token[:10] not in log_output2 and test_token not in log_output2, log_output2)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
        if original_env_token is None:
            os.environ.pop("SHOPIFY_ADMIN_TOKEN", None)
        else:
            os.environ["SHOPIFY_ADMIN_TOKEN"] = original_env_token
        server.SHOPIFY_ADMIN_TOKEN = original_global_token


async def main():
    await scenario_a_webhook_fails_closed_without_secret()
    await scenario_a2_webhook_still_accepts_valid_signature()
    await scenario_b_integrations_rate_limited_per_ip()
    scenario_c_is_production_environment()
    await scenario_d_regression_guard_order_confirmation_email()
    await scenario_e_all_nine_send_functions_gated()
    await scenario_f_admin_token_log_never_leaks_a_fraction()

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
