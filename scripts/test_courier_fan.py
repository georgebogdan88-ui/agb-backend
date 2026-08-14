"""
Focused, standalone check for courier_fan.py (new module in this repo,
adapted from E:\\CRM\\agb-crm\\backend\\courier_fan.py's login/token-cache/
track_awb logic - see that module's docstring for what was intentionally
left out, since this app only ever needs tracking, never AWB generation).

Not a pytest suite - mirrors scripts/test_checkout_invoice_toggle.py's
approach (this repo still has no test framework, tests/ dir, or pytest in
requirements.txt): a fake httpx.AsyncClient captures/answers every call
instead of hitting the real FAN Courier API.

Covers:
  (a) FAN_COURIER_USERNAME/PASSWORD unset -> track_awb raises RuntimeError
      with a clear message, doesn't crash (login never even attempted to
      run against the network).
  (b) FAN_COURIER_CLIENT_ID unset (but username/password set, so login
      succeeds) -> track_awb raises RuntimeError with a clear message.
  (c) Happy path: login + tracking call both succeed -> track_awb returns
      the raw FAN item (with "events"/"confirmation") for the requested AWB.
  (d) Token caching: a second track_awb call for a different AWB within the
      cache TTL does NOT call /login again (only the first call does).
  (e) FAN tracking endpoint returns status != "success" -> RuntimeError.
  (f) httpx.TimeoutException while calling FAN -> RuntimeError (not an
      unhandled exception), with a message mentioning the timeout.
  (g) Tracking cache: two track_awb calls for the SAME awb number in a row
      only hit the network once (second is served from _tracking_cache).

Run (from repo root): python scripts/test_courier_fan.py
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
import courier_fan  # noqa: E402

PASS = []
FAIL = []


def check(name, condition, detail=""):
    if condition:
        PASS.append(name)
        print(f"PASS: {name}")
    else:
        FAIL.append(name)
        print(f"FAIL: {name}  {detail}")


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json_data = json_data or {}
        self.text = text or str(json_data)

    def json(self):
        return self._json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)


class _FakeAsyncClient:
    """Records every request made; `handler(method, url, params)` (set by
    each scenario) decides what to answer, so different scenarios can drive
    different fake FAN behavior without redefining the whole class."""

    handler = None
    calls = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, params=None, **kwargs):
        _FakeAsyncClient.calls.append(("POST", url, params))
        return _FakeAsyncClient.handler("POST", url, params)

    async def request(self, method, url, headers=None, params=None, **kwargs):
        _FakeAsyncClient.calls.append((method, url, params))
        return _FakeAsyncClient.handler(method, url, params)


def reset_module_state():
    """courier_fan keeps in-memory caches at module level - clear them
    between scenarios so one scenario's login/tracking cache can't leak
    into the next and mask a bug (or a false pass)."""
    courier_fan._token_cache["token"] = None
    courier_fan._token_cache["expires_at"] = None
    courier_fan._tracking_cache.clear()
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.handler = None


def patch_http():
    original = courier_fan.httpx.AsyncClient
    courier_fan.httpx.AsyncClient = _FakeAsyncClient
    return original


def restore_http(original):
    courier_fan.httpx.AsyncClient = original


def successful_login_and_tracking_handler(awb="AWB123", events=None, confirmation=None):
    events = events if events is not None else [
        {"name": "Colet preluat", "location": "Depozit Cluj", "date": "2026-08-10 10:00:00"},
        {"name": "Colet livrat", "location": "Cluj-Napoca", "date": "2026-08-11 09:30:00"},
    ]

    def handler(method, url, params):
        if url.endswith("/login"):
            return _FakeResponse(200, {
                "status": "success",
                "data": {
                    "token": "fake-token",
                    "expiresAt": (datetime.utcnow() + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S"),
                },
            })
        if url.endswith("/reports/awb/tracking"):
            return _FakeResponse(200, {
                "status": "success",
                "data": [{"awb": awb, "events": events, "confirmation": confirmation}],
            })
        raise AssertionError(f"unexpected fake HTTP call: {method} {url}")
    return handler


async def scenario_a_missing_username_password():
    reset_module_state()
    original = patch_http()
    try:
        os.environ.pop("FAN_COURIER_USERNAME", None)
        os.environ.pop("FAN_COURIER_PASSWORD", None)
        os.environ["FAN_COURIER_CLIENT_ID"] = "999"
        try:
            await courier_fan.track_awb("AWB123")
            check("a) missing username/password raises", False, "no exception raised")
        except RuntimeError as e:
            check("a) missing username/password raises RuntimeError", True)
            check("a) message mentions the missing env vars", "FAN_COURIER_USERNAME" in str(e), str(e))
        check("a) no network call attempted (fails before login request)", len(_FakeAsyncClient.calls) == 0, _FakeAsyncClient.calls)
    finally:
        restore_http(original)


async def scenario_b_missing_client_id():
    reset_module_state()
    original = patch_http()
    try:
        os.environ["FAN_COURIER_USERNAME"] = "user"
        os.environ["FAN_COURIER_PASSWORD"] = "pass"
        os.environ.pop("FAN_COURIER_CLIENT_ID", None)
        _FakeAsyncClient.handler = successful_login_and_tracking_handler()
        try:
            await courier_fan.track_awb("AWB123")
            check("b) missing client id raises", False, "no exception raised")
        except RuntimeError as e:
            check("b) missing client id raises RuntimeError", True)
            check("b) message mentions FAN_COURIER_CLIENT_ID", "FAN_COURIER_CLIENT_ID" in str(e), str(e))
    finally:
        restore_http(original)


async def scenario_c_happy_path():
    reset_module_state()
    original = patch_http()
    try:
        os.environ["FAN_COURIER_USERNAME"] = "user"
        os.environ["FAN_COURIER_PASSWORD"] = "pass"
        os.environ["FAN_COURIER_CLIENT_ID"] = "999"
        _FakeAsyncClient.handler = successful_login_and_tracking_handler(
            confirmation={"name": "Ion Popescu", "date": "2026-08-11 09:31:00"},
        )
        result = await courier_fan.track_awb("AWB123")
        check("c) result has events", len(result.get("events", [])) == 2, result)
        check("c) result has confirmation", result.get("confirmation", {}).get("name") == "Ion Popescu", result)
        check("c) login call happened", any(c[1].endswith("/login") for c in _FakeAsyncClient.calls), _FakeAsyncClient.calls)
        check("c) tracking call happened", any(c[1].endswith("/reports/awb/tracking") for c in _FakeAsyncClient.calls), _FakeAsyncClient.calls)
    finally:
        restore_http(original)


async def scenario_d_token_cached_across_different_awbs():
    reset_module_state()
    original = patch_http()
    try:
        os.environ["FAN_COURIER_USERNAME"] = "user"
        os.environ["FAN_COURIER_PASSWORD"] = "pass"
        os.environ["FAN_COURIER_CLIENT_ID"] = "999"
        _FakeAsyncClient.handler = successful_login_and_tracking_handler()
        await courier_fan.track_awb("AWB-ONE")
        login_calls_after_first = sum(1 for c in _FakeAsyncClient.calls if c[1].endswith("/login"))
        await courier_fan.track_awb("AWB-TWO")
        login_calls_after_second = sum(1 for c in _FakeAsyncClient.calls if c[1].endswith("/login"))
        check("d) exactly one /login call after first track_awb", login_calls_after_first == 1, login_calls_after_first)
        check("d) still exactly one /login call after second track_awb (different AWB, cached token)",
              login_calls_after_second == 1, login_calls_after_second)
    finally:
        restore_http(original)


async def scenario_g_tracking_cache_same_awb():
    reset_module_state()
    original = patch_http()
    try:
        os.environ["FAN_COURIER_USERNAME"] = "user"
        os.environ["FAN_COURIER_PASSWORD"] = "pass"
        os.environ["FAN_COURIER_CLIENT_ID"] = "999"
        _FakeAsyncClient.handler = successful_login_and_tracking_handler()
        await courier_fan.track_awb("AWB-SAME")
        calls_after_first = len([c for c in _FakeAsyncClient.calls if c[1].endswith("/reports/awb/tracking")])
        await courier_fan.track_awb("AWB-SAME")
        calls_after_second = len([c for c in _FakeAsyncClient.calls if c[1].endswith("/reports/awb/tracking")])
        check("g) exactly one tracking call after first request", calls_after_first == 1, calls_after_first)
        check("g) still exactly one tracking call after second request for same AWB (served from cache)",
              calls_after_second == 1, calls_after_second)
    finally:
        restore_http(original)


async def scenario_e_status_not_success():
    reset_module_state()
    original = patch_http()
    try:
        os.environ["FAN_COURIER_USERNAME"] = "user"
        os.environ["FAN_COURIER_PASSWORD"] = "pass"
        os.environ["FAN_COURIER_CLIENT_ID"] = "999"

        def handler(method, url, params):
            if url.endswith("/login"):
                return _FakeResponse(200, {
                    "status": "success",
                    "data": {
                        "token": "fake-token",
                        "expiresAt": (datetime.utcnow() + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S"),
                    },
                })
            if url.endswith("/reports/awb/tracking"):
                return _FakeResponse(200, {"status": "error", "message": "AWB not found"})
            raise AssertionError(f"unexpected fake HTTP call: {method} {url}")

        _FakeAsyncClient.handler = handler
        try:
            await courier_fan.track_awb("AWB-UNKNOWN")
            check("e) status != success raises", False, "no exception raised")
        except RuntimeError as e:
            check("e) status != success raises RuntimeError", True)
            check("e) message mentions failure", "Tracking eșuat" in str(e), str(e))
    finally:
        restore_http(original)


async def scenario_f_timeout_becomes_runtime_error():
    reset_module_state()
    original = patch_http()
    try:
        os.environ["FAN_COURIER_USERNAME"] = "user"
        os.environ["FAN_COURIER_PASSWORD"] = "pass"
        os.environ["FAN_COURIER_CLIENT_ID"] = "999"

        class _TimeoutClient(_FakeAsyncClient):
            async def post(self, url, params=None, **kwargs):
                raise httpx.TimeoutException("simulated timeout")

        courier_fan.httpx.AsyncClient = _TimeoutClient
        try:
            await courier_fan.track_awb("AWB-TIMEOUT")
            check("f) timeout raises", False, "no exception raised")
        except RuntimeError as e:
            check("f) timeout raises RuntimeError (not unhandled httpx exception)", True)
            check("f) message mentions timeout", "timeout" in str(e).lower(), str(e))
    finally:
        restore_http(original)


async def main():
    await scenario_a_missing_username_password()
    await scenario_b_missing_client_id()
    await scenario_c_happy_path()
    await scenario_d_token_cached_across_different_awbs()
    await scenario_g_tracking_cache_same_awb()
    await scenario_e_status_not_success()
    await scenario_f_timeout_becomes_runtime_error()

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
