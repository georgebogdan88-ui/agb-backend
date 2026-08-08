"""
Focused, standalone check for the _require_admin() change that retires the
legacy native webshop admin session token lookup, leaving a CRM-signed BFF
admin JWT (see _verify_bff_jwt) as the only accepted credential.

Not a pytest suite - mirrors scripts/test_stock_checkout.py's approach,
since this repo still has no test framework, tests/ dir, or pytest in
requirements.txt (re-confirmed for this task: no pytest.ini, no
conftest.py, `pip show pytest` reports not installed). Uses mongomock +
mongomock-motor to swap in an in-memory Mongo double for server.client/db,
and builds a bare starlette.requests.Request from a raw ASGI scope (headers
only) to drive server._require_admin(request) directly - no FastAPI
TestClient/HTTP server needed, since _require_admin only ever reads
request.headers.

CRM_BFF_JWT_PUBLIC_KEY is set (module-level, before `import server`) to a
throwaway Ed25519 keypair's public key generated in this script, so the
"not configured -> 503" fail-closed branch does NOT fire for the scenarios
below and the intended JWT-shape / signature-verification logic is what's
actually being exercised. A SEPARATE scenario deliberately clears that env
var (patched onto the server module after import, then restored) to check
the fail-closed 503 path in isolation.

None of the required scenarios below ((a)/(b)/(c) per the task) need to
reach the database at all:
  - (a) no Authorization header fails at the Bearer-prefix check.
  - (b) a non-JWT-shaped Bearer token fails at _looks_like_jwt(), before
    _verify_bff_jwt (and therefore the DB) is ever touched.
  - (c) a JWT-shaped-but-garbage-signature token fails inside
    _verify_bff_jwt's jwt.decode() (PyJWTError on bad signature), which
    happens before the DB revocation lookup runs.
mongomock is still wired up (fresh_db()) so that scenario (d) below - a
REAL native session token for a REAL admin user, seeded exactly the way
_find_user_by_token expects - has a DB to be looked up against, to prove
the regression-relevant case directly rather than only via the structural
_looks_like_jwt check in (b).

What this script explicitly does NOT attempt (per instructions - needs
real production/staging CRM-signed keys this worktree doesn't have):
  - A genuine CRM-signed BFF JWT succeeding end-to-end. Scenario (e) below
    gets as close as this environment allows: a token signed by this
    script's OWN throwaway Ed25519 key, with correct aud/iss/scopes/exp/
    iat, verified against that SAME key - this proves the signature/claims
    logic in _verify_bff_jwt is internally consistent, but it is NOT proof
    that a real CRM-signed token (signed with CRM's actual private key,
    verified against the real CRM_BFF_JWT_PUBLIC_KEY configured in a real
    environment) succeeds. That specific path needs live staging
    credentials the coordinator has separately.

Run (from repo root): python scripts/test_require_admin_bff_only.py
"""
import asyncio
import os
import sys
import time
import uuid
from pathlib import Path

# Required module-level env vars for server.py to import at all (see
# server.py:47-49 equivalent) - fake values, no real Mongo/Atlas connection
# is ever made since AsyncIOMotorClient connects lazily and we swap it out
# for a mongomock client before any query runs.
os.environ.setdefault("MONGO_URL", "mongodb://fake-for-import-only/")
os.environ.setdefault("DB_NAME", "fake_db_for_import_only")

# Generate a throwaway Ed25519 keypair BEFORE importing server, since
# CRM_BFF_JWT_PUBLIC_KEY is read at module import time (server.py top
# level) and _require_admin's new fail-closed 503 branch would otherwise
# fire for every scenario here, masking the logic actually under test.
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

_private_key = Ed25519PrivateKey.generate()
_public_pem = _private_key.public_key().public_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PublicFormat.SubjectPublicKeyInfo,
).decode()
_private_pem = _private_key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
).decode()
os.environ["CRM_BFF_JWT_PUBLIC_KEY"] = _public_pem

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import jwt  # noqa: E402
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
    """A brand-new in-memory Mongo double per scenario, swapped into the
    server module's globals exactly like the real client/db are set up at
    import time."""
    mock_client = mongomock_motor.AsyncMongoMockClient()
    mock_db = mock_client["test_db"]
    server.client = mock_client
    server.db = mock_db
    return mock_db


def make_request(bearer_value=None):
    """Bare starlette Request built from a raw ASGI scope carrying only an
    Authorization header (or none, if bearer_value is None) -
    _require_admin only ever reads request.headers, so no app/route/HTTP
    server is needed."""
    headers = []
    if bearer_value is not None:
        headers.append((b"authorization", f"Bearer {bearer_value}".encode()))
    scope = {
        "type": "http",
        "headers": headers,
        "method": "GET",
        "path": "/admin/products",
    }
    return Request(scope)


def sign_bff_jwt(private_key_pem, *, sub="staff-1", email="staff@example.com",
                  scopes=None, aud=None, iss=None, exp_delta=300, iat_delta=0,
                  include_exp=True, include_iat=True):
    """Mint a JWT shaped like a real CRM BFF admin token, signed with the
    given private key. Defaults produce a claim set that SHOULD verify
    successfully against the matching public key; individual kwargs let
    scenarios deliberately break one aspect (wrong aud, expired, missing
    scope, ...)."""
    if scopes is None:
        scopes = ["webshop_admin"]
    now = int(time.time())
    payload = {
        "sub": sub,
        "email": email,
        "jti": str(uuid.uuid4()),
        "scopes": scopes,
    }
    if aud is not None:
        payload["aud"] = aud
    if iss is not None:
        payload["iss"] = iss
    if include_exp:
        payload["exp"] = now + exp_delta
    if include_iat:
        payload["iat"] = now + iat_delta
    return jwt.encode(payload, private_key_pem, algorithm="EdDSA")


async def scenario_a_no_auth_header():
    """(a, required) no Authorization header at all -> 401, same message as
    before this change."""
    await fresh_db()
    req = make_request(bearer_value=None)
    try:
        await server._require_admin(req)
        check("a) missing header rejected", False, "no exception raised")
    except server.HTTPException as e:
        check("a) missing header -> 401", e.status_code == 401, f"got {e.status_code}")
        check("a) missing header message unchanged", e.detail == "Token lipsă sau invalid", e.detail)


async def scenario_b_non_jwt_shaped_native_token():
    """(b, required) a Bearer token that does NOT look like a JWT (e.g. an
    opaque native-session-style hex string, no dots) now 401s. Before this
    change, this exact shape of input is what would have been routed to
    _find_user_by_token - confirm that fallback no longer runs."""
    await fresh_db()
    req = make_request(bearer_value="deadbeef1234567890deadbeef1234567890")
    try:
        await server._require_admin(req)
        check("b) non-JWT-shaped token rejected", False, "no exception raised")
    except server.HTTPException as e:
        check("b) non-JWT-shaped token -> 401", e.status_code == 401, f"got {e.status_code}")
        check("b) non-JWT-shaped token message unchanged", e.detail == "Token invalid sau expirat", e.detail)


async def scenario_c_garbage_signature_jwt():
    """(c, required) a well-formed-but-garbage JWT (three dot-separated
    base64url segments, but signed with an unrelated key so the signature
    is invalid against CRM_BFF_JWT_PUBLIC_KEY) -> 401 via _verify_bff_jwt's
    own PyJWTError handling, not by falling through to anything else."""
    await fresh_db()
    other_key = Ed25519PrivateKey.generate()
    other_pem = other_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    bad_token = sign_bff_jwt(
        other_pem, aud=server.BFF_JWT_AUDIENCE, iss=server.BFF_JWT_ISSUER,
    )
    check("c) garbage token structurally looks like a JWT",
          server._looks_like_jwt(bad_token))
    req = make_request(bearer_value=bad_token)
    try:
        await server._require_admin(req)
        check("c) garbage-signature JWT rejected", False, "no exception raised")
    except server.HTTPException as e:
        check("c) garbage-signature JWT -> 401", e.status_code == 401, f"got {e.status_code}")
        check("c) garbage-signature JWT message unchanged", e.detail == "Token invalid sau expirat", e.detail)


async def scenario_d_real_native_admin_token_now_rejected():
    """(d, extra) end-to-end regression check: seed a REAL admin user in
    the (mock) DB with a REAL native session token exactly the way
    _issue_session_token/_new_session_token_doc would, the way
    _find_user_by_token successfully resolves it (confirmed separately
    still works for customer-facing endpoints - this script doesn't touch
    that). Before this change, calling _require_admin with this token
    would have returned the admin user. Now it must 401, proving the
    regression this task targets is actually closed, not just structurally
    argued via (b)."""
    db = await fresh_db()
    native_token = server.generate_token()
    await db.users.insert_one({
        "email": "owner@example.com",
        "role": "admin",
        "tokens": [{
            "token": native_token,
            "created_at": server.datetime.utcnow(),
            "expires_at": server.datetime.utcnow() + server.timedelta(days=30),
        }],
    })
    # Sanity check: _find_user_by_token itself (unmodified, still used by
    # customer-facing endpoints) still resolves this token - proves the
    # seed is realistic and the rejection below is due to _require_admin's
    # own change, not a broken fixture.
    resolved = await server._find_user_by_token(native_token)
    check("d) fixture sanity: _find_user_by_token still resolves the seeded token",
          resolved is not None and resolved.get("role") == "admin")

    req = make_request(bearer_value=native_token)
    try:
        await server._require_admin(req)
        check("d) real native admin token now rejected by _require_admin", False,
              "admin was returned - native-token fallback still active!")
    except server.HTTPException as e:
        check("d) real native admin token -> 401", e.status_code == 401, f"got {e.status_code}")


async def scenario_e_self_signed_bff_jwt_internal_consistency():
    """(e, extra, SCOPED CAVEAT - see module docstring) a token signed by
    THIS SCRIPT'S OWN throwaway key, verified against that same key's
    public half (which is what CRM_BFF_JWT_PUBLIC_KEY is set to for this
    whole script) - correct aud/iss/scopes/exp/iat. This is the closest
    this worktree can get to "a genuine CRM-signed BFF JWT succeeds"
    without real CRM staging credentials: it proves _verify_bff_jwt's own
    verification logic (signature, audience, issuer, required claims,
    revocation lookup) is internally consistent and reachable end-to-end
    from _require_admin, but it is NOT proof that a real CRM-signed
    production token verifies - that needs live staging credentials this
    worktree does not have (coordinator to verify separately)."""
    await fresh_db()
    good_token = sign_bff_jwt(
        _private_pem, aud=server.BFF_JWT_AUDIENCE, iss=server.BFF_JWT_ISSUER,
    )
    req = make_request(bearer_value=good_token)
    try:
        result = await server._require_admin(req)
        check("e) self-signed-consistent BFF JWT accepted", result.get("role") == "admin", result)
        check("e) auth_source reflects bff path", result.get("auth_source") == "bff", result)
    except server.HTTPException as e:
        check("e) self-signed-consistent BFF JWT accepted", False,
              f"unexpectedly rejected: {e.status_code} {e.detail}")


async def scenario_f_not_configured_fails_closed_503():
    """(f, extra) with CRM_BFF_JWT_PUBLIC_KEY unset entirely, ANY bearer
    token (even a well-formed one) must fail closed with 503, never fall
    through to any other lookup. Patches server.CRM_BFF_JWT_PUBLIC_KEY
    directly (the module global _require_admin actually reads) rather than
    os.environ, since server.py already evaluated the env var at import
    time; restores it afterwards so later scenarios aren't affected order-
    dependently."""
    await fresh_db()
    original = server.CRM_BFF_JWT_PUBLIC_KEY
    server.CRM_BFF_JWT_PUBLIC_KEY = ""
    try:
        req = make_request(bearer_value="whatever.looks.like-a-jwt")
        try:
            await server._require_admin(req)
            check("f) unconfigured key fails closed", False, "no exception raised")
        except server.HTTPException as e:
            check("f) unconfigured key -> 503 (fail closed, not 401)", e.status_code == 503, f"got {e.status_code}")
    finally:
        server.CRM_BFF_JWT_PUBLIC_KEY = original


async def main():
    await scenario_a_no_auth_header()
    await scenario_b_non_jwt_shaped_native_token()
    await scenario_c_garbage_signature_jwt()
    await scenario_d_real_native_admin_token_now_rejected()
    await scenario_e_self_signed_bff_jwt_internal_consistency()
    await scenario_f_not_configured_fails_closed_503()

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
