"""
Focused, standalone check for infisical_bootstrap.load_secrets_from_infisical(),
added 2026-08-22 to let this backend optionally pull its secrets from
Infisical (project "AGB Agroparts Solution") instead of a plain .env file,
while being a complete no-op fallback to today's exact behavior whenever the
new mechanism isn't configured (the only state production/Render is in right
now - INFISICAL_CLIENT_ID/INFISICAL_CLIENT_SECRET are deliberately NOT set
there yet).

This never talks to the real Infisical API - infisical_sdk.InfisicalSDKClient
is monkeypatched in every scenario that reaches it, so there is no network
call, no real credentials, and nothing is written back to .env.

Does NOT import server.py (infisical_bootstrap.py has no dependency on it,
and importing server would pull in Mongo/FastAPI/etc. for no benefit here) -
imports infisical_bootstrap directly.

Covers:
  (a) INFISICAL_CLIENT_ID/INFISICAL_CLIENT_SECRET both absent from os.environ
      -> load_secrets_from_infisical() returns 0, os.environ is completely
      unchanged, and infisical_sdk.InfisicalSDKClient is NEVER instantiated
      (proves the short-circuit happens before any SDK/network involvement,
      not just that the end result happens to match).
  (a2) Same, but with only ONE of the two set (CLIENT_ID present, SECRET
      missing, and vice versa) -> same short-circuit, still 0 instantiations.
  (b) Both set, InfisicalSDKClient/login/list_secrets fully mocked to return
      2 fake secrets -> both keys land in os.environ with the exact values
      from the mock response, and the function's return value (count of NEW
      keys actually written) is 2.
  (c) One of the two mock secret keys is already present in os.environ
      BEFORE the call (simulating a local .env override) -> that key's
      existing local value is preserved, NOT overwritten by the Infisical
      mock value; the other (not pre-set) key IS populated from Infisical;
      the return value reflects only the 1 key that was actually newly
      written.
  (d) client.auth.universal_auth.login(...) raises (simulated network/auth
      failure) -> load_secrets_from_infisical() does NOT raise, returns 0,
      logs a warning (captured and asserted, without crashing), and
      os.environ is left completely unchanged for the keys that would have
      been populated had the call succeeded.
  (d2) Same shape of check, but the exception is raised by
      client.secrets.list_secrets(...) instead of login(...) - the whole
      Infisical interaction is wrapped in one try/except, not just the login
      call.
  (e) Regression/observability: on the success path (b), an INFO-level log
      record mentioning the count of secrets loaded is emitted (best-effort
      visibility check - the value actually reaching Render's log stream
      depends on server.py's logging.basicConfig() call, which happens later
      in module import order; this only proves the log call itself fires
      with the right count, not stdout visibility).

Run (from repo root): python scripts/test_infisical_bootstrap.py
"""
import io
import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import infisical_bootstrap  # noqa: E402

PASS = []
FAIL = []


def check(name, condition, detail=""):
    if condition:
        PASS.append(name)
        print(f"PASS: {name}")
    else:
        FAIL.append(name)
        print(f"FAIL: {name}  {detail}")


INFISICAL_ENV_KEYS = [
    "INFISICAL_CLIENT_ID",
    "INFISICAL_CLIENT_SECRET",
    "INFISICAL_PROJECT_ID",
    "INFISICAL_ENVIRONMENT",
    "INFISICAL_SECRET_PATH",
]

# Test-only fake secret keys - never real production variable names, so a
# leaked/failed cleanup can't be confused with anything real.
FAKE_SECRET_KEYS = ["TEST_INFISICAL_SECRET_ALPHA", "TEST_INFISICAL_SECRET_BETA"]


def _snapshot_env(keys):
    return {k: os.environ.get(k) for k in keys}


def _restore_env(snapshot):
    for k, v in snapshot.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _clear(keys):
    for k in keys:
        os.environ.pop(k, None)


def make_mock_response(pairs):
    """pairs: list of (secretKey, secretValue) tuples -> object shaped like
    the real ListSecretsResponse (a .secrets list of objects exposing
    .secretKey/.secretValue attributes, matching infisical_sdk's actual
    BaseSecret dataclass field names - verified by reading the installed
    infisical_sdk package's api_types.py directly, not guessed)."""
    return SimpleNamespace(secrets=[SimpleNamespace(secretKey=k, secretValue=v) for k, v in pairs])


def scenario_a_not_configured_no_action():
    """(a) Neither INFISICAL_CLIENT_ID nor INFISICAL_CLIENT_SECRET set ->
    complete no-op: return 0, os.environ untouched, SDK class never
    instantiated."""
    env_snapshot = _snapshot_env(INFISICAL_ENV_KEYS + FAKE_SECRET_KEYS)
    _clear(INFISICAL_ENV_KEYS + FAKE_SECRET_KEYS)
    before = dict(os.environ)
    try:
        with patch("infisical_sdk.InfisicalSDKClient") as mock_cls:
            result = infisical_bootstrap.load_secrets_from_infisical()
            check("a) returns 0 when both INFISICAL_CLIENT_ID/SECRET are unset", result == 0, result)
            check("a) InfisicalSDKClient never instantiated when unconfigured", mock_cls.call_count == 0, mock_cls.call_count)
        after = dict(os.environ)
        check("a) os.environ completely unchanged", before == after, "environ was mutated")
    finally:
        _restore_env(env_snapshot)


def scenario_a2_partial_config_no_action():
    """(a2) Only one of the two credentials set (either direction) -> still
    a complete no-op, same as fully unset."""
    env_snapshot = _snapshot_env(INFISICAL_ENV_KEYS + FAKE_SECRET_KEYS)
    try:
        # client_id only
        _clear(INFISICAL_ENV_KEYS + FAKE_SECRET_KEYS)
        os.environ["INFISICAL_CLIENT_ID"] = "fake-client-id"
        with patch("infisical_sdk.InfisicalSDKClient") as mock_cls:
            result = infisical_bootstrap.load_secrets_from_infisical()
            check("a2) CLIENT_ID set alone -> returns 0", result == 0, result)
            check("a2) CLIENT_ID set alone -> SDK never instantiated", mock_cls.call_count == 0, mock_cls.call_count)

        # client_secret only
        _clear(INFISICAL_ENV_KEYS + FAKE_SECRET_KEYS)
        os.environ["INFISICAL_CLIENT_SECRET"] = "fake-client-secret"
        with patch("infisical_sdk.InfisicalSDKClient") as mock_cls:
            result = infisical_bootstrap.load_secrets_from_infisical()
            check("a2) CLIENT_SECRET set alone -> returns 0", result == 0, result)
            check("a2) CLIENT_SECRET set alone -> SDK never instantiated", mock_cls.call_count == 0, mock_cls.call_count)
    finally:
        _restore_env(env_snapshot)


def scenario_b_full_mock_populates_environ():
    """(b) Both credentials set, SDK fully mocked -> the 2 fake secrets from
    the mock response land in os.environ with correct keys/values, return
    value is 2."""
    env_snapshot = _snapshot_env(INFISICAL_ENV_KEYS + FAKE_SECRET_KEYS)
    _clear(INFISICAL_ENV_KEYS + FAKE_SECRET_KEYS)
    os.environ["INFISICAL_CLIENT_ID"] = "fake-client-id"
    os.environ["INFISICAL_CLIENT_SECRET"] = "fake-client-secret"
    try:
        mock_response = make_mock_response([
            (FAKE_SECRET_KEYS[0], "alpha-value-from-infisical"),
            (FAKE_SECRET_KEYS[1], "beta-value-from-infisical"),
        ])
        mock_instance = MagicMock()
        mock_instance.secrets.list_secrets.return_value = mock_response

        with patch("infisical_sdk.InfisicalSDKClient", return_value=mock_instance) as mock_cls:
            result = infisical_bootstrap.load_secrets_from_infisical()

            check("b) InfisicalSDKClient instantiated exactly once", mock_cls.call_count == 1, mock_cls.call_count)
            check("b) login() called with the configured client_id/client_secret",
                  mock_instance.auth.universal_auth.login.call_args.kwargs
                  == {"client_id": "fake-client-id", "client_secret": "fake-client-secret"},
                  mock_instance.auth.universal_auth.login.call_args)
            check("b) list_secrets() called with default project/environment/path",
                  mock_instance.secrets.list_secrets.call_args.kwargs == {
                      "project_id": infisical_bootstrap.DEFAULT_INFISICAL_PROJECT_ID,
                      "environment_slug": infisical_bootstrap.DEFAULT_INFISICAL_ENVIRONMENT,
                      "secret_path": infisical_bootstrap.DEFAULT_INFISICAL_SECRET_PATH,
                  }, mock_instance.secrets.list_secrets.call_args)

        check("b) returns 2 (both mock secrets newly written)", result == 2, result)
        check("b) first secret key present with correct value",
              os.environ.get(FAKE_SECRET_KEYS[0]) == "alpha-value-from-infisical", os.environ.get(FAKE_SECRET_KEYS[0]))
        check("b) second secret key present with correct value",
              os.environ.get(FAKE_SECRET_KEYS[1]) == "beta-value-from-infisical", os.environ.get(FAKE_SECRET_KEYS[1]))
    finally:
        _restore_env(env_snapshot)


def scenario_c_local_value_not_overwritten():
    """(c) A key already present in os.environ before the call keeps its
    local value (setdefault semantics) - the other, not pre-set, key IS
    populated from the mock. Return value counts only the truly-new key."""
    env_snapshot = _snapshot_env(INFISICAL_ENV_KEYS + FAKE_SECRET_KEYS)
    _clear(INFISICAL_ENV_KEYS + FAKE_SECRET_KEYS)
    os.environ["INFISICAL_CLIENT_ID"] = "fake-client-id"
    os.environ["INFISICAL_CLIENT_SECRET"] = "fake-client-secret"
    # Pre-set the first fake secret locally, simulating a value already in
    # .env/the process environment before Infisical bootstrap runs.
    os.environ[FAKE_SECRET_KEYS[0]] = "pre-existing-local-value"
    try:
        mock_response = make_mock_response([
            (FAKE_SECRET_KEYS[0], "alpha-value-from-infisical"),
            (FAKE_SECRET_KEYS[1], "beta-value-from-infisical"),
        ])
        mock_instance = MagicMock()
        mock_instance.secrets.list_secrets.return_value = mock_response

        with patch("infisical_sdk.InfisicalSDKClient", return_value=mock_instance):
            result = infisical_bootstrap.load_secrets_from_infisical()

        check("c) pre-existing local value is NOT overwritten by Infisical",
              os.environ.get(FAKE_SECRET_KEYS[0]) == "pre-existing-local-value", os.environ.get(FAKE_SECRET_KEYS[0]))
        check("c) key not previously set IS populated from Infisical",
              os.environ.get(FAKE_SECRET_KEYS[1]) == "beta-value-from-infisical", os.environ.get(FAKE_SECRET_KEYS[1]))
        check("c) return value counts only the 1 newly-written key", result == 1, result)
    finally:
        _restore_env(env_snapshot)


def scenario_d_login_exception_does_not_crash():
    """(d) login() raises -> function swallows it, returns 0, logs a
    warning, os.environ left unchanged for keys that would have been
    populated."""
    env_snapshot = _snapshot_env(INFISICAL_ENV_KEYS + FAKE_SECRET_KEYS)
    _clear(INFISICAL_ENV_KEYS + FAKE_SECRET_KEYS)
    os.environ["INFISICAL_CLIENT_ID"] = "fake-client-id"
    os.environ["INFISICAL_CLIENT_SECRET"] = "fake-client-secret"
    try:
        mock_instance = MagicMock()
        mock_instance.auth.universal_auth.login.side_effect = ConnectionError("simulated network failure")

        logger = logging.getLogger("infisical_bootstrap")
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logger.addHandler(handler)
        previous_level = logger.level
        logger.setLevel(logging.INFO)
        try:
            with patch("infisical_sdk.InfisicalSDKClient", return_value=mock_instance):
                result = infisical_bootstrap.load_secrets_from_infisical()
            log_output = stream.getvalue()
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous_level)

        check("d) does not raise, returns 0 when login() fails", result == 0, result)
        check("d) no fake secret keys were written to os.environ",
              all(k not in os.environ for k in FAKE_SECRET_KEYS), dict(os.environ))
        check("d) a warning was logged mentioning the failure",
              "esuat" in log_output or "failed" in log_output.lower(), log_output)
    finally:
        _restore_env(env_snapshot)


def scenario_d2_list_secrets_exception_does_not_crash():
    """(d2) Same as (d), but the exception comes from list_secrets() instead
    of login() - the whole interaction is guarded, not just the login call."""
    env_snapshot = _snapshot_env(INFISICAL_ENV_KEYS + FAKE_SECRET_KEYS)
    _clear(INFISICAL_ENV_KEYS + FAKE_SECRET_KEYS)
    os.environ["INFISICAL_CLIENT_ID"] = "fake-client-id"
    os.environ["INFISICAL_CLIENT_SECRET"] = "fake-client-secret"
    try:
        mock_instance = MagicMock()
        mock_instance.secrets.list_secrets.side_effect = RuntimeError("simulated 404 project not found")

        with patch("infisical_sdk.InfisicalSDKClient", return_value=mock_instance):
            result = infisical_bootstrap.load_secrets_from_infisical()

        check("d2) does not raise, returns 0 when list_secrets() fails", result == 0, result)
        check("d2) no fake secret keys were written to os.environ",
              all(k not in os.environ for k in FAKE_SECRET_KEYS), dict(os.environ))
    finally:
        _restore_env(env_snapshot)


def scenario_e_success_logs_info_with_count():
    """(e) On success, an INFO log record is emitted mentioning the count of
    secrets loaded (2, matching scenario b's mock)."""
    env_snapshot = _snapshot_env(INFISICAL_ENV_KEYS + FAKE_SECRET_KEYS)
    _clear(INFISICAL_ENV_KEYS + FAKE_SECRET_KEYS)
    os.environ["INFISICAL_CLIENT_ID"] = "fake-client-id"
    os.environ["INFISICAL_CLIENT_SECRET"] = "fake-client-secret"
    try:
        mock_response = make_mock_response([
            (FAKE_SECRET_KEYS[0], "alpha-value-from-infisical"),
            (FAKE_SECRET_KEYS[1], "beta-value-from-infisical"),
        ])
        mock_instance = MagicMock()
        mock_instance.secrets.list_secrets.return_value = mock_response

        logger = logging.getLogger("infisical_bootstrap")
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logger.addHandler(handler)
        previous_level = logger.level
        logger.setLevel(logging.INFO)
        try:
            with patch("infisical_sdk.InfisicalSDKClient", return_value=mock_instance):
                infisical_bootstrap.load_secrets_from_infisical()
            log_output = stream.getvalue()
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous_level)

        check("e) success path logs the count of secrets loaded (2)", "2" in log_output, log_output)
    finally:
        _restore_env(env_snapshot)


def main():
    scenario_a_not_configured_no_action()
    scenario_a2_partial_config_no_action()
    scenario_b_full_mock_populates_environ()
    scenario_c_local_value_not_overwritten()
    scenario_d_login_exception_does_not_crash()
    scenario_d2_list_secrets_exception_does_not_crash()
    scenario_e_success_logs_info_with_count()

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
