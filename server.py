from fastapi import FastAPI, APIRouter, HTTPException, Query, BackgroundTasks, Request, UploadFile, File, Form, Body
from fastapi.responses import HTMLResponse, FileResponse, Response
from fastapi.encoders import jsonable_encoder
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import OperationFailure
import os
import logging
import html
from pathlib import Path
from pydantic import BaseModel, Field
from typing import List, Optional, Literal, Dict, Deque, Set
import uuid
from datetime import datetime, timedelta, timezone
import httpx
import re
import json
import unicodedata
import asyncio
import hashlib
import hmac
import secrets
import bcrypt
import jwt
import time
import random
from collections import defaultdict, deque
import cloudinary
import cloudinary.uploader
import io
from PIL import Image
import bleach

import courier_fan

ROOT_DIR = Path(__file__).parent
# Load .env but don't override existing environment variables (important for Render deployment)
load_dotenv(ROOT_DIR / '.env', override=False)

# MongoDB connection
# maxPoolSize explicit (was left at the driver default of 100) - this
# process shares a ~500-connection Atlas M0 budget with agb-crm, and 100
# idle-capable connections from a single-worker process is more than this
# app's actual concurrency needs (each request holds a connection only
# briefly, being I/O-bound async). 20 leaves headroom for agb-crm and for
# any future horizontal scaling of this service. Configurable via
# MONGO_MAX_POOL_SIZE for staging load testing; defaults to 20 so
# production is unaffected unless the env var is explicitly set there.
mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url, maxPoolSize=int(os.environ.get("MONGO_MAX_POOL_SIZE", "20")))
db = client[os.environ['DB_NAME']]

# Shopify Configuration - loaded from environment
SHOPIFY_STORE = os.environ.get('SHOPIFY_STORE', '43ca3c-3.myshopify.com')
SHOPIFY_STOREFRONT_TOKEN = os.environ.get('SHOPIFY_STOREFRONT_TOKEN', '')
SHOPIFY_API_VERSION = os.environ.get('SHOPIFY_API_VERSION', '2024-01')
SHOPIFY_WEBHOOK_SECRET = os.environ.get('SHOPIFY_WEBHOOK_SECRET', '')

# Shopify Admin API OAuth Configuration
SHOPIFY_CLIENT_ID = os.environ.get('SHOPIFY_CLIENT_ID', '38d338d6b94e38743c88c38ece3b6b21')
SHOPIFY_CLIENT_SECRET = os.environ.get('SHOPIFY_CLIENT_SECRET', '')
SHOPIFY_ADMIN_TOKEN = os.environ.get('SHOPIFY_ADMIN_TOKEN', '')  # Will be set after OAuth

# Brevo Email Configuration
BREVO_API_KEY = os.environ.get('BREVO_API_KEY', '')

# CRM Integration Configuration (fire-and-forget order sync to agb-crm)
CRM_API_URL = os.environ.get('CRM_API_URL', '')
CRM_INTEGRATION_KEY = os.environ.get('CRM_INTEGRATION_KEY', '')

# BFF (Backend-for-Frontend) admin auth: CRM signs short-lived (5-15 min)
# Ed25519 JWTs after a staff member authenticates on the CRM side, and this
# backend only ever VERIFIES them (see _verify_bff_jwt) with the matching
# public key. This is now the ONLY accepted credential type for /admin/*
# (and the other _require_admin-gated routes) - see _require_admin. The
# legacy native webshop admin session token fallback has been retired.
# This backend never signs/issues these JWTs - that happens exclusively on
# CRM. MANDATORY, not optional: if unset, _require_admin fails closed with
# 503 on every gated route, with no fallback - every environment that runs
# this code (staging AND production) must have this set to the real public
# key matching CRM's signing key before deploying.
CRM_BFF_JWT_PUBLIC_KEY = os.environ.get('CRM_BFF_JWT_PUBLIC_KEY', '')
# Separate shared secret from CRM_INTEGRATION_KEY above (that one is for the
# unrelated /integrations/* channel) - authenticates CRM's call to
# POST /api/internal/revoke-bff-admin. See _require_crm_bff_service_key.
CRM_BFF_SERVICE_KEY = os.environ.get('CRM_BFF_SERVICE_KEY', '')

# Auto-sync configuration
AUTO_SYNC_INTERVAL_MINUTES = int(os.environ.get('AUTO_SYNC_INTERVAL_MINUTES', '5'))  # Default 5 minutes

# Cloudinary configuration - legacy, only still used by the one-off
# Shopify-CDN -> Cloudinary bulk migration below (POST /admin/migrate-images,
# db.shopify_products has 0 cdn.shopify.com image_url left as of this
# writing). New admin image uploads go straight to Cloudflare Images
# instead - see CLOUDFLARE_* below and admin_upload_image().
cloudinary.config(
    cloud_name=os.environ.get('CLOUDINARY_CLOUD_NAME', ''),
    api_key=os.environ.get('CLOUDINARY_API_KEY', ''),
    api_secret=os.environ.get('CLOUDINARY_API_SECRET', ''),
    secure=True,
)

# Cloudflare Images configuration (admin product image uploads + the
# Cloudinary -> Cloudflare product-catalog migration, see
# scripts/migrate_to_cloudflare_images.py for the original one-off bulk
# migration this reuses the exact same upload mechanism from).
CLOUDFLARE_ACCOUNT_ID = os.environ.get('CLOUDFLARE_ACCOUNT_ID', '')
CLOUDFLARE_API_TOKEN = os.environ.get('CLOUDFLARE_API_TOKEN', '')
CLOUDFLARE_IMAGES_UPLOAD_URL = "https://api.cloudflare.com/client/v4/accounts/{account_id}/images/v1"
# Cloudflare's built-in "public" variant only scales an image down to fit
# 1366x768 - it does NOT crop. A custom variant named "square"
# (fit=cover, 1000x1000 - center-crops to fill a square) was created in the
# Cloudflare account for the original migration and is reused here so newly
# uploaded images look consistent with the rest of the already-migrated
# catalog (same variant name as DELIVERY_VARIANT in
# scripts/migrate_to_cloudflare_images.py - keep both in sync if it ever
# changes).
CLOUDFLARE_IMAGE_VARIANT = "square"

# Create the main app without a prefix
app = FastAPI()

# Create a router with the /api prefix
api_router = APIRouter(prefix="/api")

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Sync status
sync_status = {
    "is_syncing": False,
    "total_synced": 0,
    "last_sync": None,
    "error": None
}

# ==================== MODELS ====================

class Product(BaseModel):
    id: str
    title: str
    handle: str
    description: str
    description_normalized: Optional[str] = ""
    technical_specs: Optional[str] = None
    title_normalized: Optional[str] = ""
    # Storefront SEO metadata (<title>/<meta name="description">) - purely
    # editorial, admin-set overrides of what would otherwise be derived from
    # title/description on the storefront. None means "not set", storefront
    # falls back to its own defaults.
    meta_title: Optional[str] = None
    meta_description: Optional[str] = None
    price: float
    currency: str = "RON"
    image_url: Optional[str] = None
    images: List[str] = []
    tags: List[str] = []
    product_type: Optional[str] = None
    vendor: Optional[str] = None
    stock: int = 0
    stock_status: Optional[str] = None
    sku: Optional[str] = None
    compatible_models: List[str] = []
    collections: List[str] = []
    complementary_product_ids: List[str] = []
    equivalent_product_ids: List[str] = []
    is_featured: bool = False
    # When this product doc was first created (manual admin create, or first
    # picked up by sync_all_products()) / last had a real content change
    # (admin edit, or a Shopify resync that actually picked up different
    # data - NOT a resync that just re-confirms identical data). Optional
    # because products synced before this field existed won't have it until
    # scripts/backfill_product_timestamps.py runs; None is a legitimate,
    # if hopefully transient, value here.
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    # Utilaje de vânzare only (product_type == "Utilaje") - a used-equipment
    # spec sheet, same shape as a marketplace listing (year/hours/power/
    # transmission/tires/max speed). Left unset for spare-parts products.
    equipment_year: Optional[int] = None
    equipment_hours: Optional[int] = None
    equipment_power_hp: Optional[int] = None
    equipment_transmission: Optional[str] = None
    equipment_front_tire: Optional[str] = None
    equipment_front_tire_wear: Optional[str] = None
    equipment_rear_tire: Optional[str] = None
    equipment_rear_tire_wear: Optional[str] = None
    equipment_max_speed: Optional[int] = None
    # Internal-only flag for the gradual Cloudinary -> Cloudflare Images
    # rollout (phase 2). Never set through this API - toggled directly in
    # Mongo on a hand-picked sample of products. `exclude=True` so it's
    # read from the raw Mongo doc (by `apply_cloudflare_rollout()`, before
    # the doc is turned into a `Product`) but never leaks into the JSON
    # response, same pattern as `title_en`/`description_en` on the
    # feat/i18n-product-translations branch.
    cloudflare_rollout: Optional[bool] = Field(default=None, exclude=True)

class CartItem(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str
    product_id: str
    product_name: str
    product_image: str
    price: float
    quantity: int = 1
    created_at: datetime = Field(default_factory=datetime.utcnow)

class CartItemCreate(BaseModel):
    session_id: str
    product_id: str
    product_name: str
    product_image: str
    price: float
    quantity: int = 1

class CartItemUpdate(BaseModel):
    quantity: int

class CustomerInfo(BaseModel):
    name: str
    email: str
    phone: str
    address: str
    city: str
    county: str
    postal_code: str
    notes: Optional[str] = ""
    # Company/invoice fields (checkout "issue on company" toggle) - mirrors
    # the exact naming already used on the User account model above
    # (is_company/company_name/cui/reg_com/administrator/company_address_*)
    # and on agb-webshop's cont/profil page, so nothing needs translating
    # across the shared contract. All optional/backward-compatible: a
    # personal (is_company=False) order looks identical to before.
    is_company: bool = False
    company_name: Optional[str] = None
    cui: Optional[str] = None
    reg_com: Optional[str] = None
    administrator: Optional[str] = None
    company_address_strada: Optional[str] = None
    company_address_numar: Optional[str] = None
    company_address_bloc: Optional[str] = None
    company_address_scara: Optional[str] = None
    company_address_ap: Optional[str] = None
    company_address_oras: Optional[str] = None
    company_address_judet: Optional[str] = None
    company_address_cod_postal: Optional[str] = None

class Order(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str
    items: List[dict]
    customer: CustomerInfo
    subtotal: float
    shipping: float = 25.0
    total: float
    status: str = "pending"
    payment_method: str = "ramburs"
    created_at: datetime = Field(default_factory=datetime.utcnow)
    crm_synced: bool = False
    crm_sync_error: Optional[str] = None
    crm_sync_attempts: int = 0
    # Set whenever an admin edits this order's items after it was already
    # created (and, if crm_synced, already pushed to CRM) - tracks that the
    # CRM copy needs its lines overwritten to match. Independent of
    # crm_synced/crm_sync_* above, which are about the one-time initial
    # create sync only.
    crm_items_dirty: bool = False
    crm_items_sync_error: Optional[str] = None
    crm_items_sync_attempts: int = 0
    # Set by PATCH /admin/orders/{order_id}/courier, pushed fire-and-forget
    # from agb-crm's generate_awb once staff creates a FAN Courier AWB for
    # this order - lets the customer see their AWB number and live delivery
    # status on their own account page (GET /auth/orders/{order_id}/
    # courier-tracking). None/absent until an AWB has actually been
    # generated for this order.
    courier_awb_number: Optional[str] = None
    courier_service: Optional[str] = None

class OrderCreate(BaseModel):
    session_id: str
    items: List[dict]
    customer: CustomerInfo
    subtotal: float
    shipping: float = 25.0
    total: float
    payment_method: str = "ramburs"
    # Optional link back to the anonymous browsing session that converted
    # (agb-webshop's localStorage-persisted `agb_analytics_session_id`, see
    # POST /analytics/pageview) - entirely backward-compatible: omitted by
    # any pre-existing webshop/mobile client, in which case create_order
    # simply skips recording a conversion (see below).
    analytics_session_id: Optional[str] = None

# ==================== AUTH MODELS ====================

# Literal version string for the Terms of Service / Privacy Policy text
# shown at registration. Bump this (and only this) whenever that text
# changes materially, so consent_terms_version on existing users keeps
# reflecting exactly what they agreed to - never rewrite/backfill past
# users' stored version when bumping it.
CURRENT_TERMS_VERSION = "2026-08-09"


class UserRegister(BaseModel):
    email: str
    password: str
    name: str
    phone: str
    # Required explicit GDPR consent to Terms + Privacy Policy. Defaults to
    # False (rather than being a plain required field) so that both an
    # omitted field AND an explicit `false` hit the same clear Romanian
    # error message below, instead of the omitted case falling through to
    # FastAPI's generic 422 validation error.
    terms_accepted: bool = False

class UserLogin(BaseModel):
    email: str
    password: str

class ShopifyCustomerLogin(BaseModel):
    """Login with existing Shopify customer account"""
    email: str
    password: str

class UserUpdate(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    address: Optional[str] = None
    address_strada: Optional[str] = None
    address_numar: Optional[str] = None
    address_bloc: Optional[str] = None
    address_scara: Optional[str] = None
    address_ap: Optional[str] = None
    city: Optional[str] = None
    county: Optional[str] = None
    postal_code: Optional[str] = None
    # Company fields
    is_company: Optional[bool] = None
    company_name: Optional[str] = None
    cui: Optional[str] = None
    reg_com: Optional[str] = None
    administrator: Optional[str] = None
    company_address: Optional[str] = None
    company_address_strada: Optional[str] = None
    company_address_numar: Optional[str] = None
    company_address_bloc: Optional[str] = None
    company_address_scara: Optional[str] = None
    company_address_ap: Optional[str] = None
    company_address_oras: Optional[str] = None
    company_address_judet: Optional[str] = None
    company_address_cod_postal: Optional[str] = None
    notify_news_email: Optional[bool] = None

# ==================== EQUIPMENT MODELS ====================

class Equipment(BaseModel):
    """Model for customer's equipment/tractors"""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    brand: Optional[str] = None  # Marca
    model: str  # Ex: John Deere 6150R
    chassis_serial: Optional[str] = None  # Serie șasiu
    engine_serial: Optional[str] = None  # Serie motor
    engine_type: Optional[str] = None  # Model motor
    transmission_type: Optional[str] = None  # Model cutie viteze
    front_axle_model: Optional[str] = None  # Model punte față
    features: Optional[List[str]] = None  # Echipări selectate
    created_at: datetime = Field(default_factory=datetime.utcnow)

class EquipmentCreate(BaseModel):
    """Create equipment request"""
    brand: Optional[str] = None
    model: str
    chassis_serial: Optional[str] = None
    engine_serial: Optional[str] = None
    engine_type: Optional[str] = None
    transmission_type: Optional[str] = None
    front_axle_model: Optional[str] = None
    features: Optional[List[str]] = None

class EquipmentUpdate(BaseModel):
    """Update equipment request"""
    brand: Optional[str] = None
    model: Optional[str] = None
    chassis_serial: Optional[str] = None
    engine_serial: Optional[str] = None
    engine_type: Optional[str] = None
    transmission_type: Optional[str] = None
    front_axle_model: Optional[str] = None
    features: Optional[List[str]] = None

class EquipmentFromCrm(BaseModel):
    """Inbound payload for POST /integrations/equipment-from-crm - the
    reverse direction of sync_equipment_to_crm: CRM staff add/edit a
    tractor on a client record and it should land on that client's web/
    mobile account equipment list, if they have one. Field names already
    match our own Equipment fields 1:1 (no renaming needed on this side)."""
    client_phone: Optional[str] = None
    client_email: Optional[str] = None
    client_name: Optional[str] = None
    crm_tractor_id: str
    brand: Optional[str] = None
    model: str
    chassis_serial: Optional[str] = None
    engine_serial: Optional[str] = None
    engine_type: Optional[str] = None
    transmission_type: Optional[str] = None
    front_axle_model: Optional[str] = None
    features: Optional[List[str]] = None

class CustomerInterestCreate(BaseModel):
    """Add-interest request body for POST /auth/interests (favorite /
    price alert / stock alert toggle on a product page)."""
    product_id: str
    type: Literal["favorite", "price_alert", "stock_alert"]

class UserResponse(BaseModel):
    id: str
    email: str
    name: str
    phone: str
    address: Optional[str] = None
    city: Optional[str] = None
    county: Optional[str] = None
    postal_code: Optional[str] = None
    is_company: bool = False
    company_name: Optional[str] = None
    cui: Optional[str] = None
    reg_com: Optional[str] = None
    company_address: Optional[str] = None
    created_at: datetime
    consent_accepted_at: Optional[datetime] = None
    consent_terms_version: Optional[str] = None

# Password hashing helper
async def hash_password(password: str) -> str:
    """Hash password using bcrypt. Offloaded to a thread - bcrypt is
    CPU-bound (~100-300ms) and this process runs a single Uvicorn worker,
    so calling it synchronously would block the entire event loop (every
    other concurrent request, not just this one) for that duration."""
    return await asyncio.to_thread(
        lambda: bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    )

async def verify_password(password: str, hashed: str) -> bool:
    """Verify password against bcrypt hash. Offloaded to a thread - see
    hash_password() above for why."""
    if not hashed:
        return False
    return await asyncio.to_thread(bcrypt.checkpw, password.encode(), hashed.encode())

def generate_token() -> str:
    """Generate a simple auth token"""
    return str(uuid.uuid4()) + "-" + str(uuid.uuid4())

# Hard cap on concurrent logged-in devices per account. Each `db.users`
# document stores its active session tokens in a `tokens` array (max length
# MAX_DEVICE_TOKENS); a 4th simultaneous login auto-evicts the oldest
# session rather than being rejected (see _issue_session_token).
MAX_DEVICE_TOKENS = 3

# TTL for session tokens minted from this deploy onward (see
# _new_session_token_doc). Configurable via SESSION_TOKEN_TTL_DAYS;
# defaults to 30 days - this is a customer-facing storefront, not an
# internal admin console, so sessions don't need to be as short-lived as
# e.g. a back-office tool's.
SESSION_TOKEN_TTL_DAYS = int(os.environ.get("SESSION_TOKEN_TTL_DAYS", "30"))


def _new_session_token_doc() -> dict:
    """Build a new `tokens[]` entry for a freshly-issued session.

    Sessions minted from this deploy onward are stored as OBJECTS
    (`{"token", "created_at", "expires_at"}`), not bare strings, so they
    carry a real, enforced expiry - see _find_user_by_token for how expiry
    is actually checked. Sessions issued before this deploy remain bare
    strings in `tokens[]` and are intentionally left alone (never
    retroactively expired - see _find_user_by_token's docstring): this
    only changes what NEW logins/registrations get from here on.
    """
    now = datetime.utcnow()
    return {
        "token": generate_token(),
        "created_at": now,
        "expires_at": now + timedelta(days=SESSION_TOKEN_TTL_DAYS),
    }


async def _issue_session_token(email: str) -> str:
    """Mint and persist a new session token for the given user. Enforces
    MAX_DEVICE_TOKENS concurrent devices per account by evicting the oldest
    session once the cap is reached, rather than rejecting the new login -
    a 4th device logging in silently kicks out the least-recently-added
    session (that device's next API call gets a normal 401, same as any
    other invalid/expired token). Only call this after credentials have
    already been verified for `email`.

    Atomic by construction: `$push` with `$each`/`$slice` is a single
    update_one, so concurrent logins for the same account can't race into
    an over-sized array. `$slice` works the same regardless of whether the
    array holds legacy bare-string entries, new object entries, or a mix
    of both (it just keeps the last MAX_DEVICE_TOKENS elements, whatever
    their type) - so an account transitioning from all-legacy to
    mixed-to-all-new tokens over several logins evicts correctly at every
    step.
    """
    token_doc = _new_session_token_doc()
    await db.users.update_one(
        {"email": email},
        {"$push": {"tokens": {"$each": [token_doc], "$slice": -MAX_DEVICE_TOKENS}}},
    )
    return token_doc["token"]


async def _find_user_by_token(token: str, allow_shopify_access_token: bool = False) -> Optional[dict]:
    """Resolve a bearer token to a user doc.

    Matches, in a single query, ALL of:
    - a legacy bare-string entry in `tokens[]` (pre-this-deploy sessions -
      still never expire; forcing them to would silently log out every
      real customer with an active session the moment this ships), OR
    - a legacy singular `token` field for any account the one-time startup
      migration hasn't converted yet (belt-and-braces safety net - in
      steady state every account should already have `tokens`), OR
    - a new-format object entry in `tokens[]` (`{"token", "expires_at",
      ...}`) whose `expires_at` hasn't passed yet - once it has, that
      token simply stops matching here (effectively expired) without any
      separate cleanup job needing to run.

    When `allow_shopify_access_token` is set, also matches on the user's
    stored Shopify customer access token, matching the handful of endpoints
    (equipment CRUD) that have always accepted either credential type as a
    bearer token.
    """
    or_clauses = [
        {"tokens": token},
        {"token": token},
        {"tokens": {"$elemMatch": {"token": token, "expires_at": {"$gt": datetime.utcnow()}}}},
    ]
    if allow_shopify_access_token:
        or_clauses.append({"shopify_access_token": token})
    user = await db.users.find_one({"$or": or_clauses})
    if user and user.get("is_deleted"):
        # A deleted account's session tokens are cleared on deletion (see
        # delete_current_user_account), so this only ever matches via a
        # still-live shopify_access_token that deletion doesn't invalidate -
        # exactly the bypass this guard exists to close. Treat a deleted
        # account as unauthenticated everywhere, not just at /auth/login.
        return None
    return user

# ==================== RATE LIMITING ====================
# In-memory abuse-prevention limiter for the auth endpoints (brute-force
# login guard, registration/forgot-password spam guard) and for the 4
# admin-only sync/notification triggers hardened in the previous security
# pass. Deliberately NOT slowapi/Redis/any new dependency: this service
# always runs as a single uvicorn worker on the current Render tier
# (worker=1 was re-confirmed deliberately after multi-worker measured
# worse on this tier - see Procfile), so a single in-process dict is both
# sufficient and inherently consistent - there's only ever one process's
# state to keep straight. State lives purely in memory and resets on every
# restart/redeploy - acceptable here because this is abuse mitigation
# layered on top of real checks (password hash, admin role), never the
# only line of defense.

class _SlidingWindowRateLimiter:
    """Per-key sliding-window hit counter. Safe without locks: every
    operation here is plain synchronous dict/deque manipulation with no
    `await` in between reading and mutating state, so an asyncio task can
    never be pre-empted mid-update (and this only ever runs in a single
    process/worker anyway - see module note above)."""

    def __init__(self):
        self._hits: Dict[str, Deque[float]] = defaultdict(deque)

    def hit(self, key: str, limit: int, window_seconds: float) -> Optional[int]:
        """Record one attempt for `key`. Returns None if `key` is still
        within `limit` attempts over the trailing `window_seconds`.
        Otherwise returns the whole number of seconds until the oldest
        attempt in the window ages out (a reasonable Retry-After value) -
        the over-limit attempt itself is NOT recorded, so a caller that
        keeps retrying immediately doesn't keep pushing the window forward
        forever."""
        now = time.monotonic()
        q = self._hits[key]
        cutoff = now - window_seconds
        while q and q[0] < cutoff:
            q.popleft()

        # Cheap, probabilistic housekeeping so keys that go permanently
        # quiet (e.g. a one-off attacker IP) eventually get evicted instead
        # of sitting in memory forever - not worth a dedicated background
        # task for something this low-stakes.
        if random.random() < 0.001:
            self._sweep()

        if len(q) >= limit:
            retry_after = q[0] + window_seconds - now
            return max(1, int(retry_after) + 1)

        q.append(now)
        return None

    def _sweep(self, max_age_seconds: float = 3600) -> None:
        """Drop any key whose hits are all older than `max_age_seconds`
        (1 hour = the largest window any limiter below uses)."""
        now = time.monotonic()
        cutoff = now - max_age_seconds
        stale = []
        for key, q in self._hits.items():
            while q and q[0] < cutoff:
                q.popleft()
            if not q:
                stale.append(key)
        for key in stale:
            del self._hits[key]


_rate_limiter = _SlidingWindowRateLimiter()

# Tunable limits - (max attempts, window in seconds) per bucket. See the
# call sites below for exactly what's used as the key.
LOGIN_IP_EMAIL_LIMIT, LOGIN_IP_EMAIL_WINDOW_SECONDS = 10, 15 * 60   # 10 / 15 min per IP+email combo
LOGIN_IP_LIMIT, LOGIN_IP_WINDOW_SECONDS = 30, 15 * 60               # 30 / 15 min per IP (catches email-rotation)
REGISTER_IP_LIMIT, REGISTER_IP_WINDOW_SECONDS = 5, 60 * 60          # 5 / hour per IP
FORGOT_PASSWORD_IP_LIMIT, FORGOT_PASSWORD_IP_WINDOW_SECONDS = 5, 60 * 60  # 5 / hour per IP
ADMIN_ACTION_LIMIT, ADMIN_ACTION_WINDOW_SECONDS = 10, 60 * 60       # 10 / hour per admin, per protected action
ACCOUNT_DELETE_LIMIT, ACCOUNT_DELETE_WINDOW_SECONDS = 5, 15 * 60    # 5 / 15 min per account (password re-check)


def _client_ip(request: Request) -> str:
    """Best-effort real client IP to key rate limits on. This service sits
    behind Render's edge proxy, which sets X-Forwarded-For to the original
    client IP; uvicorn here is NOT started with --forwarded-allow-ips (see
    Procfile), so request.client.host alone would just be Render's own
    internal proxy address - identical for every request - which would
    make an IP-based limit either a no-op (an attacker could never be
    singled out) or, worse, a limit shared by every real user at once. This
    reads X-Forwarded-For directly instead and only falls back to
    request.client.host when there's no such header (e.g. local dev with
    no proxy in front)."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _enforce_rate_limit(key: str, limit: int, window_seconds: float, message: str) -> None:
    """Raise HTTP 429 (with a Retry-After header) if `key` is already over
    `limit` hits within `window_seconds`; otherwise records this attempt
    and returns normally."""
    retry_after = _rate_limiter.hit(key, limit, window_seconds)
    if retry_after is not None:
        raise HTTPException(
            status_code=429,
            detail=message,
            headers={"Retry-After": str(retry_after)},
        )

# ==================== ADMIN AUDIT LOG ====================
# Append-only trail of every admin action that mutates data (product/order/
# equipment-option writes, image uploads, bulk imports, etc) - see
# _write_audit_log below and its call sites at each admin write endpoint.
# db.admin_audit_log has deliberately no update/delete route anywhere in
# this file - GET /admin/audit-log is the only way to read it back.

_AUDIT_SECRET_KEY_RE = re.compile(
    r"password|passwd|pwd|token|secret|api[_-]?key|authorization", re.IGNORECASE
)
_AUDIT_PII_KEY_RE = re.compile(
    r"email|phone|telefon|address|adresa", re.IGNORECASE
)
_AUDIT_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _mask_audit_email(value: str) -> str:
    """"j.popescu@example.ro" -> "j***@***.ro" - keeps just enough to be
    recognizable to someone who already knows the address, without storing
    it in clear text in the audit trail."""
    local, _, domain = value.partition("@")
    first = local[0] if local else "*"
    tld = domain.rsplit(".", 1)[-1] if "." in domain else "***"
    return f"{first}***@***.{tld}"


def _mask_audit_pii(value: str) -> str:
    """Generic partial mask for any other PII-looking string (phone number,
    free-text address, etc) - keeps only the last 2 characters visible."""
    value = value.strip()
    if len(value) <= 2:
        return "***"
    return ("*" * (len(value) - 2)) + value[-2:]


def _sanitize_audit_value(key: str, value):
    if isinstance(value, dict):
        return _sanitize_audit_payload(value)
    if isinstance(value, list):
        return [_sanitize_audit_value(key, v) for v in value]
    if isinstance(value, str):
        if _AUDIT_EMAIL_RE.match(value):
            return _mask_audit_email(value)
        if key and _AUDIT_PII_KEY_RE.search(key):
            return _mask_audit_pii(value)
    return value


def _sanitize_audit_payload(data: Optional[dict]) -> Optional[dict]:
    """Applied to every `before`/`after` snapshot right before it's written
    to db.admin_audit_log: drops any secret-looking field (password/token/
    api key/etc) entirely, and masks any customer-PII-looking field (email/
    phone/address - by key name, plus any string anywhere that simply looks
    like an email address regardless of its key) so the audit trail never
    stores a password, token, or a customer's contact details in clear
    text. This is defense-in-depth on top of callers already being expected
    to only pass the specific fields that changed, never a full document."""
    if not data:
        return data
    sanitized = {}
    for k, v in data.items():
        if _AUDIT_SECRET_KEY_RE.search(k):
            continue
        sanitized[k] = _sanitize_audit_value(k, v)
    return sanitized


async def _write_audit_log(
    request: Request,
    admin: dict,
    action: str,
    resource_type: str,
    resource_id: Optional[str] = None,
    before: Optional[dict] = None,
    after: Optional[dict] = None,
    result: str = "success",
    reason: Optional[str] = None,
) -> None:
    """Best-effort write of one admin-audit-log entry. Must never fail or
    block the admin action it's logging - any error writing the log itself
    (e.g. a transient Mongo issue) is caught and logged to the app logger,
    never re-raised, so a problem here can't turn an otherwise-successful
    admin action into a 500 for the admin. `before`/`after` should only ever
    contain the fields that actually changed (never a full document) - see
    _sanitize_audit_payload for how they're scrubbed of secrets/PII before
    being persisted."""
    try:
        await db.admin_audit_log.insert_one({
            "id": str(uuid.uuid4()),
            "timestamp": datetime.utcnow(),
            "admin_id": admin.get("id"),
            "admin_email": admin.get("email"),
            "action": action,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "before": _sanitize_audit_payload(before),
            "after": _sanitize_audit_payload(after),
            "result": result,
            "reason": reason,
            "request_id": str(uuid.uuid4()),
            "ip": _client_ip(request),
        })
    except Exception:
        logger.exception(
            f"Failed to write admin audit log entry (action={action}, "
            f"resource_type={resource_type}, resource_id={resource_id})"
        )


@api_router.get("/admin/audit-log")
async def admin_list_audit_log(
    request: Request,
    action: Optional[str] = None,
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
    admin_id: Optional[str] = None,
    limit: int = 100,
    skip: int = 0,
):
    """Read-only, paginated view of db.admin_audit_log, newest first. Same
    admin-auth tier as every other /admin/* endpoint - no separate role.
    There is deliberately no update/delete route for this collection
    anywhere in this file, so this GET is the only way in or out of it aside
    from _write_audit_log's inserts - append-only by omission."""
    await _require_admin(request)

    query: dict = {}
    if action:
        query["action"] = action
    if resource_type:
        query["resource_type"] = resource_type
    if resource_id:
        query["resource_id"] = resource_id
    if admin_id:
        query["admin_id"] = admin_id

    total = await db.admin_audit_log.count_documents(query)
    cursor = db.admin_audit_log.find(query).sort("timestamp", -1).skip(skip).limit(limit)
    entries = await cursor.to_list(limit)
    for entry in entries:
        entry.pop("_id", None)

    return {"items": entries, "total": total}

# ==================== SEARCH HELPERS ====================

def normalize_text(text: str) -> str:
    """Remove diacritics and normalize text for better search"""
    if not text:
        return ""
    normalized = unicodedata.normalize('NFD', text)
    ascii_text = ''.join(c for c in normalized if unicodedata.category(c) != 'Mn')
    return ascii_text.lower()

def extract_compatible_models(description: str) -> List[str]:
    """Extract tractor/equipment model numbers from product description - full model names between commas"""
    if not description:
        return []
    
    models = set()
    
    # Normalize "PR" to "Premium" for consistent indexing
    description_normalized = re.sub(r'\b(\d{4})\s*PR\b', r'\1 Premium', description, flags=re.IGNORECASE)
    
    # First, try to split by comma and extract individual models
    # This handles format like "6810, 6910, 6910S" or "6820, 6920, 6920S"
    parts = description_normalized.split(',')
    for part in parts:
        part = part.strip()
        # Check if this looks like a model number
        if re.match(r'^[\d]{3,4}[A-Za-z]*\s*[A-Za-z]*$', part):
            # Clean up spaces
            model = part.replace(' ', '')
            if len(model) >= 3 and len(model) <= 20:
                models.add(model)
        # Also check for SE models
        elif re.match(r'^SE\d{4}$', part, re.IGNORECASE):
            models.add(part.upper())
    
    # Pattern 1: Extract full model names between commas (e.g., "6150 M", "7530 Premium")
    # Look for patterns like ", 6150 M ," or ", 7530 Premium ,"
    comma_pattern = r',\s*(\d{4}\s*[A-Za-z]*(?:\s+[A-Za-z]+)?)\s*,'
    comma_matches = re.findall(comma_pattern, description_normalized, re.IGNORECASE)
    for match in comma_matches:
        # Clean up: remove extra spaces and format properly
        model = ' '.join(match.split())  # Normalize spaces
        model = model.replace(' ', '')  # Remove spaces for storage (e.g., "6150 M" -> "6150M")
        if len(model) >= 3:
            models.add(model)
    
    # Pattern 2: Standard model patterns with full text
    patterns = [
        r'\b(\d{4}\s*[A-Z])\b',           # e.g., "6150 M" or "6150M"
        r'\b(\d{4}\s*Premium)\b',          # e.g., "7530 Premium"
        r'\b(\d{4}\s*[A-Z]\s*Premium)\b',  # e.g., "6150 M Premium"
        r'\b(SE\s*\d{4})\b',               # e.g., "SE6400"
        r'\b(\d{4}[RMESTXDNJHL]?)\b',      # e.g., "6630", "6630R", "6920S", "6920"
        r'\b(\d{3,4}[A-Z]{0,2})\b',        # e.g., "6920", "6920S", "5045D"
        r'\b(\d{4}[A-Z]{1,3}\d{2,4})\b',   # engine codes, e.g. "6068HL470", "4045HL474"
    ]
    
    for pattern in patterns:
        matches = re.findall(pattern, description_normalized, re.IGNORECASE)
        for match in matches:
            # Clean up: remove extra spaces
            model = match.replace(' ', '')
            if len(model) >= 3 and len(model) <= 15:
                # Exclude things that are clearly not models (years, random numbers)
                if not model.isdigit() or (len(model) == 4 and 1000 <= int(model) <= 9999):
                    models.add(model)
    
    return list(models)  # No limit on models

# ==================== SHOPIFY SYNC ====================

async def fetch_shopify_products_page(after: Optional[str] = None) -> dict:
    """Fetch a single page of products from Shopify"""
    graphql_query = """
    query getProducts($first: Int!, $after: String) {
        products(first: $first, after: $after) {
            edges {
                node {
                    id
                    title
                    handle
                    description
                    tags
                    productType
                    vendor
                    priceRange {
                        minVariantPrice {
                            amount
                            currencyCode
                        }
                    }
                    images(first: 10) {
                        edges {
                            node {
                                url
                            }
                        }
                    }
                    variants(first: 1) {
                        edges {
                            node {
                                id
                                sku
                                quantityAvailable
                                availableForSale
                            }
                        }
                    }
                }
            }
            pageInfo {
                hasNextPage
                endCursor
            }
        }
    }
    """
    
    variables = {"first": 250, "after": after}
    
    url = f"https://{SHOPIFY_STORE}/api/{SHOPIFY_API_VERSION}/graphql.json"
    headers = {
        "X-Shopify-Storefront-Access-Token": SHOPIFY_STOREFRONT_TOKEN,
        "Content-Type": "application/json"
    }
    
    async with httpx.AsyncClient() as http_client:
        response = await http_client.post(
            url,
            json={"query": graphql_query, "variables": variables},
            headers=headers,
            timeout=60.0
        )
        
        if response.status_code != 200:
            raise Exception(f"Shopify API error: {response.text}")
        
        return response.json()

def parse_shopify_node(node: dict) -> dict:
    """Parse a Shopify product node into our format"""
    # Get all images
    images = []
    if node.get("images", {}).get("edges"):
        for edge in node["images"]["edges"]:
            images.append(edge["node"]["url"])
    
    image_url = images[0] if images else None
    
    stock = 0
    sku = None
    available_for_sale = False
    if node.get("variants", {}).get("edges"):
        variant = node["variants"]["edges"][0]["node"]
        stock = variant.get("quantityAvailable") or 0
        sku = variant.get("sku")
        available_for_sale = variant.get("availableForSale", False)
    
    price = 0.0
    currency = "RON"
    if node.get("priceRange", {}).get("minVariantPrice"):
        price = float(node["priceRange"]["minVariantPrice"]["amount"])
        currency = node["priceRange"]["minVariantPrice"]["currencyCode"]
    
    title = node.get("title", "")
    description = node.get("description", "")
    tags = node.get("tags", [])
    
    # Extract compatible models from description AND tags
    compatible_models = extract_compatible_models(description)
    
    # Also extract models from tags
    for tag in tags:
        tag_models = extract_compatible_models(tag)
        for m in tag_models:
            if m not in compatible_models:
                compatible_models.append(m)
    
    product_id = node["id"].replace("gid://shopify/Product/", "")
    
    # Determine stock status
    # "În stoc furnizor" for products with 0 stock but availableForSale=true (Continue selling enabled)
    stock_status = "in_stock" if stock > 0 else "out_of_stock"
    tags = node.get("tags", [])
    
    # If stock is 0 but product is still availableForSale, it means "Continue selling when out of stock" is enabled
    if stock == 0 and available_for_sale:
        stock_status = "supplier_stock"
    
    # Also check if product has supplier stock mention in description or tags
    desc_lower = description.lower()
    if stock == 0 and ("contactati pentru oferta" in desc_lower or 
                       "stoc furnizor" in desc_lower or 
                       "pretul actual poate varia" in desc_lower or
                       "la comanda" in desc_lower or
                       "disponibil la comanda" in desc_lower):
        stock_status = "supplier_stock"
    
    # Also check for specific product types that are usually available on order
    product_type = node.get("productType", "")
    if stock == 0 and product_type and product_type.lower() in ["nou", "aftermarket"]:
        # Check if it's a new product without stock - likely supplier stock
        if price > 0:
            stock_status = "supplier_stock"
    
    return {
        "id": product_id,
        "title": title,
        "handle": node.get("handle", ""),
        "description": description,
        "description_normalized": normalize_text(description),
        "title_normalized": normalize_text(title),
        "price": price,
        "currency": currency,
        "image_url": image_url,
        "images": images,  # All product images
        "tags": tags,
        "product_type": product_type,
        "vendor": node.get("vendor"),
        "stock": stock,
        "stock_status": stock_status,  # "in_stock", "out_of_stock", "supplier_stock"
        "sku": sku,
        "compatible_models": compatible_models,
        "synced_at": datetime.utcnow()
    }

def _extract_next_page_info(link_header: str) -> Optional[str]:
    """Extract the page_info belonging specifically to the rel="next" entry.

    Shopify's Link header can contain BOTH rel="previous" AND rel="next" on
    any page after the first, comma-separated in one string, e.g.:
      <...page_info=AAA...>; rel="previous", <...page_info=BBB...>; rel="next"
    A regex applied to the whole header (`page_info=(...).*rel="next"`) is
    greedy and matches the FIRST page_info in the string (the previous one),
    not the one actually tied to rel="next" - which silently paginates
    backwards and loops forever. Splitting on "," and only inspecting the
    segment that contains rel="next" avoids that.
    """
    if not link_header:
        return None
    for segment in link_header.split(","):
        if 'rel="next"' not in segment:
            continue
        match = re.search(r'page_info=([^>&]+)', segment)
        if match:
            return match.group(1)
    return None


async def _fetch_all_collection_product_ids(http_client: httpx.AsyncClient, collection_id, headers: dict) -> list:
    """Fetch every product id in a collection, following Shopify's Link-header
    pagination. The old inline version only ever made one request per
    collection (?limit=250) and silently dropped anything past the first
    250 products - this fixes that."""
    product_ids = []
    page_info = None
    while True:
        products_url = f"https://{SHOPIFY_STORE}/admin/api/2024-01/collections/{collection_id}/products.json?limit=250"
        if page_info:
            products_url += f"&page_info={page_info}"

        products_response = await http_client.get(products_url, headers=headers, timeout=60.0)
        if products_response.status_code != 200:
            break

        products_data = products_response.json()
        for product in products_data.get("products", []):
            product_ids.append(str(product.get("id")))

        next_page_info = _extract_next_page_info(products_response.headers.get("Link", ""))
        if not next_page_info or next_page_info == page_info:
            break
        page_info = next_page_info
        await asyncio.sleep(0.15)

    return product_ids


async def fetch_shopify_collections() -> list:
    """Fetch all collections from Shopify Admin API with their products"""
    collections = []
    
    # Use Admin API to get collections with products
    admin_token = os.environ.get('SHOPIFY_ADMIN_TOKEN', '') or SHOPIFY_ADMIN_TOKEN
    if not admin_token:
        logger.warning("SHOPIFY_ADMIN_TOKEN not set, cannot fetch collections")
        return []
    
    headers = {
        "X-Shopify-Access-Token": admin_token,
        "Content-Type": "application/json"
    }
    
    async with httpx.AsyncClient() as http_client:
        # First get all collections
        page_info = None
        while True:
            # Use REST API for collections list
            url = f"https://{SHOPIFY_STORE}/admin/api/2024-01/custom_collections.json?limit=250"
            if page_info:
                url += f"&page_info={page_info}"
            
            response = await http_client.get(url, headers=headers, timeout=60.0)
            
            if response.status_code != 200:
                logger.error(f"Error fetching collections: {response.text}")
                break
            
            data = response.json()
            
            for collection in data.get("custom_collections", []):
                collection_id = collection.get("id")
                collection_title = collection.get("title", "")

                product_ids = await _fetch_all_collection_product_ids(http_client, collection_id, headers)

                collections.append({
                    "title": collection_title,
                    "handle": collection.get("handle", ""),
                    "product_ids": product_ids
                })

                logger.info(f"Collection '{collection_title}' has {len(product_ids)} products")
                await asyncio.sleep(0.1)  # Rate limiting

            # Check for pagination via Link header
            next_page_info = _extract_next_page_info(response.headers.get("Link", ""))
            if not next_page_info or next_page_info == page_info:
                break
            page_info = next_page_info

            await asyncio.sleep(0.2)
        
        # Also get smart collections
        url = f"https://{SHOPIFY_STORE}/admin/api/2024-01/smart_collections.json?limit=250"
        response = await http_client.get(url, headers=headers, timeout=60.0)
        
        if response.status_code == 200:
            data = response.json()
            for collection in data.get("smart_collections", []):
                collection_id = collection.get("id")
                collection_title = collection.get("title", "")

                product_ids = await _fetch_all_collection_product_ids(http_client, collection_id, headers)

                collections.append({
                    "title": collection_title,
                    "handle": collection.get("handle", ""),
                    "product_ids": product_ids
                })

                logger.info(f"Smart Collection '{collection_title}' has {len(product_ids)} products")
                await asyncio.sleep(0.1)
    
    return collections

async def sync_collections_to_products():
    """Sync collection names to products in database"""
    logger.info("Starting collections sync...")
    
    collections = await fetch_shopify_collections()
    logger.info(f"Found {len(collections)} collections")
    
    # Create a mapping of product_id -> collection names
    product_collections = {}
    for collection in collections:
        for product_id in collection["product_ids"]:
            if product_id not in product_collections:
                product_collections[product_id] = []
            product_collections[product_id].append(collection["title"])
    
    # Update products in batches
    updates = 0
    for product_id, collection_names in product_collections.items():
        result = await db.shopify_products.update_one(
            {"id": product_id},
            {"$set": {"collections": collection_names, "collections_normalized": [normalize_text(c) for c in collection_names]}}
        )
        if result.modified_count > 0:
            updates += 1
    
    logger.info(f"Updated {updates} products with collection info")
    return updates

# Fields sync_all_products() itself derives fresh from Shopify on every run
# (via parse_shopify_node(), plus the preserved-image/complementary/featured/
# equivalent overrides applied right before the comparison). Deliberately
# excludes bookkeeping-only fields (synced_at, created_at, updated_at) and
# admin-curated fields that are always copied verbatim from the existing doc
# when present (complementary_product_ids, equivalent_product_ids,
# is_featured, cf_image_id, cf_image_url, cloudflare_rollout) - those can
# never differ here, so comparing them would be a no-op at best.
_PRODUCT_SYNC_COMPARISON_FIELDS = [
    "title", "handle", "description", "description_normalized", "title_normalized",
    "price", "currency", "image_url", "images", "tags", "product_type", "vendor",
    "stock", "stock_status", "sku", "compatible_models",
]

def _product_sync_content_changed(new_product: dict, existing_doc: dict) -> bool:
    """Whether a freshly-parsed Shopify product actually differs from what's
    already stored in db.shopify_products, on the fields sync_all_products()
    derives from Shopify. Used to decide whether a periodic auto-resync
    should bump `updated_at`: re-confirming identical data (the common case,
    since most products don't change between two syncs) must NOT look like a
    fresh edit, but picking up a real Shopify-side change (price, stock,
    description, ...) should."""
    for field in _PRODUCT_SYNC_COMPARISON_FIELDS:
        if new_product.get(field) != existing_doc.get(field):
            return True
    return False

async def sync_all_products():
    """Sync ALL products from Shopify to MongoDB"""
    global sync_status
    
    if sync_status["is_syncing"]:
        return
    
    sync_status["is_syncing"] = True
    sync_status["total_synced"] = 0
    sync_status["error"] = None
    
    try:
        # Preserve admin-curated complementary-product links and "Produse
        # recomandate" picks across the delete+reinsert below -
        # parse_shopify_node() never produces either field (Shopify's bulk
        # product query doesn't return them), so without this the periodic
        # auto-sync would silently wipe every link set via the admin UI or
        # scripts/backfill_complementary_products.py, and silently un-feature
        # every product an admin curated, within one sync cycle. (No need to
        # preserve an explicit `is_featured: False` - that's already the
        # default for anything freshly parsed.)
        preserved_complementary = {
            p["id"]: p["complementary_product_ids"]
            for p in await db.shopify_products.find(
                {"source": {"$ne": "manual"}, "complementary_product_ids": {"$exists": True, "$ne": []}},
                {"id": 1, "complementary_product_ids": 1}
            ).to_list(None)
        }
        preserved_featured = {
            p["id"]
            for p in await db.shopify_products.find(
                {"source": {"$ne": "manual"}, "is_featured": True},
                {"id": 1}
            ).to_list(None)
        }
        # Same reasoning as preserved_complementary above, for the "Echivalente"
        # (same part, different brand) links curated via the admin UI.
        preserved_equivalent = {
            p["id"]: p["equivalent_product_ids"]
            for p in await db.shopify_products.find(
                {"source": {"$ne": "manual"}, "equivalent_product_ids": {"$exists": True, "$ne": []}},
                {"id": 1, "equivalent_product_ids": 1}
            ).to_list(None)
        }

        # Preserve Cloudinary-hosted images across the delete+reinsert below.
        # Product images were migrated off Shopify's CDN onto Cloudinary in a
        # one-off bulk migration (see admin_migrate_images below), plus a
        # subsequent recrop correction applied to ~15,298 images - but
        # parse_shopify_node() always parses fresh, raw cdn.shopify.com URLs
        # straight from Shopify. Without this, a full resync would silently
        # overwrite every migrated/recropped Cloudinary URL with the original
        # Shopify CDN URL, undoing both the migration and the recrop.
        #
        # Same reasoning for cf_image_id/cf_image_url/cloudflare_rollout -
        # populated by scripts/migrate_to_cloudflare_images.py and by
        # admin_upload_image() (see the Cloudflare Images upload endpoint
        # above), never produced by parse_shopify_node(). Without preserving
        # these too, every periodic auto-sync (AUTO_SYNC_INTERVAL_MINUTES)
        # would silently wipe the Cloudflare rollout state for every synced
        # product, undoing both the original migration and any per-product
        # fix applied through admin_upload_image().
        preserved_images = {
            p["id"]: {
                "image_url": p.get("image_url"),
                "images": p.get("images") or [],
                "cf_image_id": p.get("cf_image_id"),
                "cf_image_url": p.get("cf_image_url"),
                "cloudflare_rollout": p.get("cloudflare_rollout"),
            }
            for p in await db.shopify_products.find(
                {
                    "source": {"$ne": "manual"},
                    "$or": [
                        {"image_url": {"$regex": "res.cloudinary.com"}},
                        {"images": {"$regex": "res.cloudinary.com"}},
                        {"cf_image_url": {"$exists": True, "$ne": None}},
                    ],
                },
                {"id": 1, "image_url": 1, "images": 1, "cf_image_id": 1, "cf_image_url": 1, "cloudflare_rollout": 1}
            ).to_list(None)
        }

        # Preserve created_at/updated_at across the delete+reinsert below, and
        # capture enough of each existing doc's own Shopify-derived fields to
        # tell whether this sync actually changed anything for it (see
        # _product_sync_content_changed() below). Without this, every single
        # periodic auto-sync (AUTO_SYNC_INTERVAL_MINUTES) would look like a
        # fresh edit of all ~15,000 products, making updated_at useless for
        # "what did an admin actually touch recently" sorting/display.
        existing_products_by_id = {
            p["id"]: p
            for p in await db.shopify_products.find(
                {"source": {"$ne": "manual"}},
                {
                    "id": 1, "title": 1, "handle": 1, "description": 1,
                    "description_normalized": 1, "title_normalized": 1,
                    "price": 1, "currency": 1, "image_url": 1, "images": 1,
                    "tags": 1, "product_type": 1, "vendor": 1, "stock": 1,
                    "stock_status": 1, "sku": 1, "compatible_models": 1,
                    "created_at": 1, "updated_at": 1, "synced_at": 1,
                },
            ).to_list(None)
        }

        # Clear existing Shopify-synced products, but keep manually-created
        # products (source="manual") - those aren't part of the Shopify catalog
        # and would be permanently lost if wiped here.
        await db.shopify_products.delete_many({"source": {"$ne": "manual"}})

        # Single timestamp for everything created_at/updated_at stamps during
        # this whole sync run, rather than a slightly different one per
        # product - matches how sync_status["last_sync"] is stamped once at
        # the end below.
        sync_run_at = datetime.utcnow()

        after = None
        total_products = 0
        batch = []

        while True:
            logger.info(f"Fetching products... (total so far: {total_products})")

            data = await fetch_shopify_products_page(after)
            edges = data.get("data", {}).get("products", {}).get("edges", [])
            page_info = data.get("data", {}).get("products", {}).get("pageInfo", {})

            for edge in edges:
                node = edge["node"]
                product = parse_shopify_node(node)
                if product["id"] in preserved_complementary:
                    product["complementary_product_ids"] = preserved_complementary[product["id"]]
                if product["id"] in preserved_featured:
                    product["is_featured"] = True
                if product["id"] in preserved_equivalent:
                    product["equivalent_product_ids"] = preserved_equivalent[product["id"]]
                if product["id"] in preserved_images:
                    preserved = preserved_images[product["id"]]
                    product["image_url"] = preserved["image_url"]
                    product["images"] = preserved["images"]
                    if preserved["cf_image_id"] is not None:
                        product["cf_image_id"] = preserved["cf_image_id"]
                    if preserved["cf_image_url"] is not None:
                        product["cf_image_url"] = preserved["cf_image_url"]
                    if preserved["cloudflare_rollout"] is not None:
                        product["cloudflare_rollout"] = preserved["cloudflare_rollout"]

                existing_doc = existing_products_by_id.get(product["id"])
                if existing_doc is None:
                    # Genuinely new to the catalog since the last sync.
                    product["created_at"] = sync_run_at
                    product["updated_at"] = sync_run_at
                else:
                    product["created_at"] = (
                        existing_doc.get("created_at")
                        or existing_doc.get("synced_at")
                        or sync_run_at
                    )
                    if _product_sync_content_changed(product, existing_doc):
                        product["updated_at"] = sync_run_at
                    else:
                        product["updated_at"] = existing_doc.get("updated_at") or product["created_at"]

                batch.append(product)
                total_products += 1
            
            # Insert in batches of 500
            if len(batch) >= 500:
                await db.shopify_products.insert_many(batch)
                sync_status["total_synced"] = total_products
                logger.info(f"Inserted batch, total: {total_products}")
                batch = []
            
            if not page_info.get("hasNextPage"):
                break
            
            after = page_info.get("endCursor")
            
            # Small delay to avoid rate limiting
            await asyncio.sleep(0.2)
        
        # Insert remaining products
        if batch:
            await db.shopify_products.insert_many(batch)
        
        # Create indexes for fast search
        await db.shopify_products.create_index([("title_normalized", "text"), ("description_normalized", "text")])
        await db.shopify_products.create_index("title_normalized")
        await db.shopify_products.create_index("description_normalized")
        await db.shopify_products.create_index("product_type")
        await db.shopify_products.create_index("compatible_models")
        await db.shopify_products.create_index("collections")  # New index for collections
        # created_at/updated_at indexes are created once in startup_event()
        # below (try/except-wrapped there so an index hiccup never blocks
        # server boot) rather than re-created on every sync run here.

        sync_status["total_synced"] = total_products
        sync_status["last_sync"] = datetime.utcnow().isoformat()
        logger.info(f"Sync complete! Total products: {total_products}")
        
        # Sync collections in background
        try:
            await sync_collections_to_products()
            logger.info("Collections synced to products")
        except Exception as ce:
            logger.error(f"Error syncing collections: {ce}")
        
    except Exception as e:
        sync_status["error"] = str(e)
        logger.error(f"Sync error: {e}")
    finally:
        sync_status["is_syncing"] = False

# ==================== PRODUCT ENDPOINTS ====================

@api_router.get("/")
async def root():
    return {"message": "AGB Agroparts API - Connected to Shopify"}

@api_router.get("/sync/status")
async def get_sync_status():
    """Get current sync status"""
    product_count = await db.shopify_products.count_documents({})
    return {
        **sync_status,
        "products_in_db": product_count
    }

@api_router.post("/sync/start")
async def start_sync(request: Request, background_tasks: BackgroundTasks):
    """Start syncing all products from Shopify"""
    admin = await _require_admin(request)
    _enforce_rate_limit(
        f"admin:sync-start:{admin['id']}", ADMIN_ACTION_LIMIT, ADMIN_ACTION_WINDOW_SECONDS,
        "Prea multe sincronizări pornite recent. Încearcă din nou mai târziu.",
    )
    if sync_status["is_syncing"]:
        return {"message": "Sincronizare deja în curs", "status": sync_status}

    background_tasks.add_task(sync_all_products)
    return {"message": "Sincronizare pornită! Verificați /api/sync/status pentru progres"}

@api_router.post("/sync/collections")
async def sync_collections(request: Request, background_tasks: BackgroundTasks):
    """Sync only collections to existing products"""
    admin = await _require_admin(request)
    _enforce_rate_limit(
        f"admin:sync-collections:{admin['id']}", ADMIN_ACTION_LIMIT, ADMIN_ACTION_WINDOW_SECONDS,
        "Prea multe sincronizări pornite recent. Încearcă din nou mai târziu.",
    )
    background_tasks.add_task(sync_collections_to_products)
    return {"message": "Sincronizare colecții pornită!"}

@api_router.get("/collections")
async def get_collections():
    """Get all unique collections from products"""
    pipeline = [
        {"$unwind": "$collections"},
        {"$group": {"_id": "$collections", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}}
    ]
    result = await db.shopify_products.aggregate(pipeline).to_list(100)
    return [{"name": r["_id"], "product_count": r["count"]} for r in result]

SORT_FIELDS = {
    "price_asc": ("price", 1),
    "price_desc": ("price", -1),
    "title_asc": ("title_normalized", 1),
    # Used by the admin product list (GET /admin/products?sort=...) to show
    # newest-created / most-recently-modified products first. Also usable on
    # the public GET /products (opt-in only - the default, unsorted/relevance
    # behavior there is unchanged). Products synced/created before this field
    # existed sort as if created_at/updated_at were missing (Mongo puts
    # missing-field docs first in ascending sort / last in descending sort)
    # until scripts/backfill_product_timestamps.py backfills them.
    "created_at_desc": ("created_at", -1),
    "updated_at_desc": ("updated_at", -1),
}


def _term_to_spaced_regex(term: str) -> str:
    """Turns a search term into a regex fragment that also matches the same
    characters with an optional space inserted at each letter->digit
    transition (e.g. "8r410" -> "8r\\s*410").

    `compatible_models` stores John Deere's native R/RT/RX/M/T-series codes
    with a space between the series letters and the model number (e.g.
    "8R 410", "9RT 470", "6M 105", "6R 110" - confirmed against real data).
    A customer typing "8r410" with no space would otherwise never match
    "8R 410" contiguously, while typing "8r 410" happens to work today only
    because it becomes two independent terms. This makes both spellings
    behave the same, matching with zero or more spaces at that boundary.

    Only the letter->digit transition gets the optional space. The reverse
    (digit->letter, e.g. a hypothetical "410 R") does not occur in the real
    data - suffixes like "R"/"RT" are always glued directly to the model
    number (e.g. "8320R", "8310R") - so it is intentionally left alone.
    """
    parts = []
    for i, ch in enumerate(term):
        if i > 0 and term[i - 1].isalpha() and ch.isdigit():
            parts.append(r"\s*")
        parts.append(re.escape(ch))
    return "".join(parts)


def build_products_query(
    search: Optional[str],
    product_type: Optional[str],
    collection: Optional[str],
    vendor: Optional[str] = None,
) -> dict:
    """Builds the MongoDB filter dict for the storefront catalog/search -
    shared by GET /products (paginated results), GET /products/search-count
    (total match count for the "N produse găsite" indicator), and
    GET /products/vendors (the "Producător" filter bar's option list), so
    all three always agree on what counts as a match.

    `vendor` powers the storefront's "Producător" filter bar: when set, it's
    ANDed in like product_type/collection above. GET /products/vendors
    deliberately calls this WITHOUT vendor even when a vendor is selected -
    the filter bar's OWN option list must keep showing every producător
    present in the current search/category results, not collapse down to
    just the one already selected."""
    query = {}

    if product_type:
        query["product_type"] = product_type

    if collection:
        # `collections` is a list field per product; querying it with a
        # scalar matches documents where the array contains that value.
        query["collections"] = collection

    if vendor:
        query["vendor"] = vendor

    if search:
        # Normalize search terms and handle "Premium" variations
        # Convert "6930 Premium" to search for both "6930Premium" and "6930PR"
        premium_pattern = re.compile(r'(\d{4})\s*Premium', re.IGNORECASE)
        premium_matches = premium_pattern.findall(search)

        search_terms = [normalize_text(term) for term in search.split() if term.strip()]

        # Remove "premium" from search terms if it was part of a model number
        if premium_matches:
            search_terms = [t for t in search_terms if t.lower() != 'premium']

        # Re-merge a split series/model code (e.g. user typed "8r 410" as two
        # words) back into one term ("8r410") before building regexes below.
        #
        # Without this, "8r" and "410" would become two independent \b..\b
        # conditions ANDed together, each allowed to match a *different*
        # element of the `compatible_models` array - e.g. a product listing
        # both "8R 340" and "8RT 410" (but not "8R 410") would wrongly match,
        # since "8R 340" satisfies the "8r" condition and "8RT 410"
        # separately satisfies the "410" condition, even though neither
        # element is actually "8R 410". That's a real false positive
        # (verified against production data - see fix/search-model-spacing).
        #
        # Merging first makes "8r 410" go through the exact same single-term
        # path as an already-glued "8r410" (see _term_to_spaced_regex below),
        # which requires the letters and digits to be adjacent (with only an
        # optional space between them), so both spellings return identical,
        # correctly-adjacent results.
        #
        # Scope is deliberately narrow to avoid merging unrelated ordinary
        # two-word searches: only triggers when a term made purely of
        # digits+letters (e.g. "8r", "9rt", "6m" - a partial series code) is
        # immediately followed by a purely-numeric term.
        merged_terms = []
        i = 0
        while i < len(search_terms):
            current = search_terms[i]
            next_term = search_terms[i + 1] if i + 1 < len(search_terms) else None
            if (
                next_term
                and re.match(r'^\d+[a-z]+$', current, re.IGNORECASE)
                and re.match(r'^\d+$', next_term)
            ):
                merged_terms.append(current + next_term)
                i += 2
            else:
                merged_terms.append(current)
                i += 1
        search_terms = merged_terms

        if search_terms:
            # Build regex patterns for each term
            regex_conditions = []
            for term in search_terms:
                # Check if term looks like a model number (4 digits optionally followed by letters)
                is_model_search = bool(re.match(r'^\d{4}[a-z]*$', term, re.IGNORECASE))

                if is_model_search:
                    # For model numbers, search more precisely
                    # Match exact model or model followed by space/end (not followed by more letters)
                    # This prevents "6210" from matching "6210R" but allows "6210" to match "6210 M" or "6210"
                    model_regex = f"^{term}(?![A-Za-z])"  # Negative lookahead: not followed by letters

                    # Also search for Premium variant if this model was part of "XXXX Premium" search
                    model_conditions = [
                        {"title_normalized": {"$regex": f"\\b{term}\\b", "$options": "i"}},
                        {"description_normalized": {"$regex": f"\\b{term}\\b", "$options": "i"}},
                        {"compatible_models": {"$regex": model_regex, "$options": "i"}},
                    ]

                    # If searching for a model that was part of "Premium" search, also look for Premium/PR variants
                    if term in [m.lower() for m in premium_matches]:
                        model_conditions.extend([
                            {"compatible_models": {"$regex": f"^{term}Premium", "$options": "i"}},
                            {"compatible_models": {"$regex": f"^{term}PR", "$options": "i"}},
                        ])

                    regex_conditions.append({"$or": model_conditions})
                else:
                    # For regular terms (non-model numbers): always anchor
                    # the START of the term on a word boundary, regardless
                    # of length.
                    #
                    # This used to be split by length - short terms (<=4
                    # chars) got full word boundaries (\b...\b), longer terms
                    # searched as a raw, unanchored substring. That split was
                    # itself meant to fix false positives (e.g. "usa"
                    # matching inside "caUzA" - see commit 14deda9), but it
                    # only patched the <=4 char case. Part codes like
                    # "AL17256" are 7+ chars, so they fell into the unbounded
                    # branch, and a search for "AL17256" would also match
                    # "AL172568", "AL172562", ... (any longer code sharing
                    # the same prefix) - a real wrong-part-ordered risk for a
                    # webshop selling by exact code.
                    #
                    # Whether the END is also anchored depends on the term:
                    #   - Terms with a digit are treated as part/model codes
                    #     (e.g. "AL17256"), any length, and get a full
                    #     \b{term}\b on both ends, so "AL17256" cannot match
                    #     inside "AL172568".
                    #   - Purely alphabetic terms of 5+ chars are treated as
                    #     ordinary Romanian words and only get the term
                    #     anchored on the left (\b{term}, no trailing \b), so
                    #     inflected forms still match - e.g. "hidraulic"
                    #     still finds "hidraulica", "rulment" still finds
                    #     "rulmenti", "filtru"/"motor" still find "filtrul"/
                    #     "motorului" while still excluding unrelated
                    #     compound words like "prefiltru"/"servomotor" (no
                    #     boundary right before "filtru"/"motor" there).
                    #   - Purely alphabetic terms under 5 chars (e.g. "AL",
                    #     "RE", "SE", "AR", "VPJ", "VPD", "VPH") get a full
                    #     \b{term}\b on both ends too: these are exactly the
                    #     short manufacturer/part-family code prefixes real
                    #     customers search for, and without the trailing
                    #     anchor they'd also match as a left-anchored prefix
                    #     of unrelated ordinary words (e.g. "SE" as a prefix
                    #     of "seria", which appears in most descriptions),
                    #     making the search return almost the entire catalog
                    #     for a 2-3 letter query. This restores the original,
                    #     safe both-ends-anchored behaviour for those terms.
                    #
                    # Additionally, terms that mix letters and digits (e.g.
                    # "8r410", "9rt470") get an optional space inserted at
                    # the letter->digit boundary via _term_to_spaced_regex,
                    # so they match native-spaced codes like "8R 410" in
                    # `compatible_models` just as well as the glued form -
                    # see that helper's docstring for details.
                    has_digit = bool(re.search(r'\d', term))
                    term_pattern = _term_to_spaced_regex(term) if has_digit else term
                    if has_digit or len(term) < 5:
                        term_regex = f"\\b{term_pattern}\\b"
                    else:
                        term_regex = f"\\b{term_pattern}"
                    regex_conditions.append({
                        "$or": [
                            {"title_normalized": {"$regex": term_regex, "$options": "i"}},
                            {"description_normalized": {"$regex": term_regex, "$options": "i"}},
                            {"compatible_models": {"$regex": term_regex, "$options": "i"}},
                            {"sku": {"$regex": term_regex, "$options": "i"}},
                            {"collections_normalized": {"$regex": term_regex, "$options": "i"}}
                        ]
                    })

            if regex_conditions:
                query["$and"] = regex_conditions

    return query


def apply_cloudflare_rollout(doc: dict) -> dict:
    """Gradual, per-product rollout of Cloudflare Images (phase 2 of the
    Cloudinary -> Cloudflare migration - see scripts/migrate_to_cloudflare_
    images.py for phase 1, which only populated `cf_image_id`/`cf_image_url`
    on each product doc without changing what's served).

    Only swaps the primary `image_url` (and, if it was pointing at the same
    URL, `images[0]`) for `cf_image_url` when a product doc has BOTH
    `cloudflare_rollout: True` (set directly in Mongo, never via this API -
    on a small hand-picked sample only) AND a populated `cf_image_url`
    (i.e. the migration actually ran for that product). Any other product -
    no flag, or flag set but migration incomplete for some reason - is
    returned completely unchanged, still serving Cloudinary exactly as
    before. The rest of `images[]` (secondary gallery photos) is left alone
    either way - only the primary image is in scope for this phase.

    Returns a shallow copy; never mutates `doc` in place. Meant to be
    called on the raw Mongo dict before constructing a `Product` from it,
    same pattern as `localize_product_doc()` on
    feat/i18n-product-translations for the `?lang=` param."""
    if not doc.get("cloudflare_rollout"):
        return doc

    cf_image_url = doc.get("cf_image_url")
    if not cf_image_url:
        return doc

    doc = dict(doc)
    old_image_url = doc.get("image_url")
    doc["image_url"] = cf_image_url

    images = doc.get("images")
    if images and images[0] == old_image_url:
        doc["images"] = [cf_image_url] + list(images[1:])

    return doc


@api_router.get("/products/search-count")
async def get_products_search_count(
    search: Optional[str] = None,
    product_type: Optional[str] = None,
    collection: Optional[str] = None,
    vendor: Optional[str] = None,
):
    """Total count of products matching the same filters as GET /products -
    powers the "N produse găsite" indicator on the storefront catalog/search
    page (which itself only loads a page at a time via infinite scroll, so
    it has no way to know the true total on its own). Named distinctly from
    the pre-existing unfiltered GET /products/count (used elsewhere for the
    Shopify-sync total) rather than overloading it."""
    query = build_products_query(search, product_type, collection, vendor)
    total = await db.shopify_products.count_documents(query)
    return {"total": total}


@api_router.get("/products", response_model=List[Product])
async def get_products(
    search: Optional[str] = None,
    product_type: Optional[str] = None,
    collection: Optional[str] = None,
    vendor: Optional[str] = None,
    sort: Optional[str] = None,
    limit: int = 1000,
    skip: int = 0
):
    """
    Get products with search from local database.
    First run /api/sync/start to sync all 15,000+ products from Shopify.
    """
    try:
        # Check if we have products in DB
        product_count = await db.shopify_products.count_documents({})

        if product_count == 0:
            # Fallback to Shopify API if no local products
            return await get_products_from_shopify(search, limit)

        query = build_products_query(search, product_type, collection, vendor)

        # Execute query
        cursor = db.shopify_products.find(query)
        if sort and sort in SORT_FIELDS:
            field, direction = SORT_FIELDS[sort]
            cursor = cursor.sort(field, direction)
        cursor = cursor.skip(skip).limit(limit)
        products = await cursor.to_list(limit)

        # Sort by relevance if searching and no explicit sort was requested -
        # an explicit sort (price/title) takes priority over relevance.
        if search and not sort:
            search_normalized = normalize_text(search)
            products.sort(
                key=lambda p: (
                    -10 if search_normalized in p.get("title_normalized", "") else 0,
                    -5 if search_normalized in p.get("description_normalized", "") else 0,
                    -1 if p.get("stock", 0) > 0 else 0
                )
            )

        return [Product(**apply_cloudflare_rollout(p)) for p in products]

    except Exception as e:
        logger.error(f"Error fetching products: {e}")
        raise HTTPException(status_code=500, detail=str(e))

async def get_products_from_shopify(search: Optional[str], limit: int) -> List[Product]:
    """Fallback: get products directly from Shopify"""
    all_products = []
    after = None
    
    while len(all_products) < limit:
        data = await fetch_shopify_products_page(after)
        edges = data.get("data", {}).get("products", {}).get("edges", [])
        page_info = data.get("data", {}).get("products", {}).get("pageInfo", {})
        
        for edge in edges:
            product = parse_shopify_node(edge["node"])
            all_products.append(product)
        
        if not page_info.get("hasNextPage") or len(all_products) >= limit:
            break
        
        after = page_info.get("endCursor")
    
    # Apply search filter locally
    if search:
        search_terms = [normalize_text(term) for term in search.split() if term.strip()]
        filtered = []
        for p in all_products:
            matches_all = True
            for term in search_terms:
                if term not in p.get("title_normalized", "") and term not in p.get("description_normalized", ""):
                    matches_all = False
                    break
            if matches_all:
                filtered.append(p)
        all_products = filtered
    
    return [Product(**p) for p in all_products[:limit]]

@api_router.get("/products/featured", response_model=List[Product])
async def get_featured_products(limit: int = 10):
    """Get featured products for the homepage "Produse recomandate" section.

    Admin-curated picks (`is_featured: True`, set via the admin UI) always
    take priority and are listed first. If there are fewer curated picks
    than `limit` (including none at all, e.g. before an admin has curated
    anything), the remainder is backfilled using the old automatic logic
    (in-stock items first, then out-of-stock), excluding anything already
    included as a curated pick so nothing is duplicated. Curated picks are
    never bumped by the automatic fallback."""
    try:
        product_count = await db.shopify_products.count_documents({})

        if product_count > 0:
            curated = await db.shopify_products.find(
                {"is_featured": True}
            ).sort("title_normalized", 1).limit(limit).to_list(limit)

            products = list(curated)
            remaining = limit - len(products)

            if remaining > 0:
                curated_ids = [p["id"] for p in curated]

                more = await db.shopify_products.find(
                    {"stock": {"$gt": 0}, "id": {"$nin": curated_ids}}
                ).limit(remaining).to_list(remaining)
                products.extend(more)
                remaining -= len(more)

                if remaining > 0:
                    excluded_ids = curated_ids + [p["id"] for p in more]
                    more_out_of_stock = await db.shopify_products.find(
                        {"stock": 0, "id": {"$nin": excluded_ids}}
                    ).limit(remaining).to_list(remaining)
                    products.extend(more_out_of_stock)

            return [Product(**apply_cloudflare_rollout(p)) for p in products]
        else:
            # Fallback to Shopify
            return await get_products_from_shopify(None, limit)

    except Exception as e:
        logger.error(f"Error fetching featured products: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@api_router.get("/products/count")
async def get_products_count():
    """Get total product count"""
    count = await db.shopify_products.count_documents({})
    return {"total": count, "synced": sync_status["last_sync"]}

@api_router.get("/products/types")
async def get_product_types():
    """Get available product types from database"""
    try:
        types = await db.shopify_products.distinct("product_type")
        # Filter out None/empty
        types = [t for t in types if t]
        return {"types": types}
    except:
        return {
            "types": [
                "Dezmembrari",
                "Piese noi",
                "Hidraulica",
                "Motor",
                "Transmisie",
                "Electrice",
                "Filtre"
            ]
        }

@api_router.get("/products/vendors")
async def get_product_vendors(
    search: Optional[str] = None,
    product_type: Optional[str] = None,
    collection: Optional[str] = None,
):
    """Get distinct vendor/brand names from the database.

    With no params (the admin product form's Marcă field, and the CRM's own
    caller) this is the full catalog's vendor list - unchanged from before.
    With search/product_type/collection (the storefront's "Producător"
    filter bar), reuses build_products_query - the exact same filter
    GET /products and GET /products/search-count already use - so the
    returned vendors are exactly the ones actually present among current
    search/category results, never an option that would filter to zero
    products. Deliberately does NOT also filter by `vendor` itself (that's
    for the caller to pass separately if needed) - the vendor OPTIONS list
    must ignore any already-selected vendor, or picking one would leave it
    as the only remaining choice."""
    query = build_products_query(search, product_type, collection)
    vendors = await db.shopify_products.distinct("vendor", query)
    vendors = sorted(v for v in vendors if v)
    return {"vendors": vendors}

@api_router.get("/products/{product_id}/complementary")
async def get_complementary_products(product_id: str):
    """Get complementary and related products.

    Prefers the native, admin-managed `complementary_product_ids` field on
    the product's own db.shopify_products doc (set via the admin UI, or by
    the one-off scripts/backfill_complementary_products.py backfill) - this
    lets George manually curate the "Posibil să ai nevoie și de" section
    instead of relying solely on Shopify's metafield. Falls back to the
    original live Shopify metafield query (unchanged) for any product that
    hasn't been given native links yet. `related` products stay
    Shopify-metafield-only in both cases - that field is out of scope here,
    so a native-resolved response always returns an empty `related` list.
    """
    local_product = await db.shopify_products.find_one({"id": product_id})
    complementary_ids = (local_product or {}).get("complementary_product_ids") or []

    if complementary_ids:
        complementary = []
        for ref_id in complementary_ids:
            ref_product = await db.shopify_products.find_one({"id": ref_id})
            if not ref_product:
                continue
            ref_product = apply_cloudflare_rollout(ref_product)
            complementary.append({
                "id": ref_product.get("id"),
                "variant_id": None,
                "title": ref_product.get("title", ""),
                "handle": ref_product.get("handle", ""),
                "description": ref_product.get("description", ""),
                "price": ref_product.get("price", 0.0),
                "currency": ref_product.get("currency", "RON"),
                "image_url": ref_product.get("image_url"),
                "stock": ref_product.get("stock", 0),
                "stock_status": ref_product.get("stock_status"),
                "sku": ref_product.get("sku"),
                "recommended_quantity": 1,
            })
        return {"complementary": complementary, "related": []}

    try:
        # Fetch product with metafields from Shopify
        graphql_query = """
        query getProductWithMetafields($id: ID!) {
            product(id: $id) {
                id
                complementaryProducts: metafield(namespace: "shopify--discovery--product_recommendation", key: "complementary_products") {
                    value
                    type
                    references(first: 10) {
                        edges {
                            node {
                                ... on Product {
                                    id
                                    title
                                    handle
                                    description
                                    priceRange {
                                        minVariantPrice {
                                            amount
                                            currencyCode
                                        }
                                    }
                                    images(first: 1) {
                                        edges {
                                            node {
                                                url
                                            }
                                        }
                                    }
                                    variants(first: 1) {
                                        edges {
                                            node {
                                                id
                                                sku
                                                quantityAvailable
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
                relatedProducts: metafield(namespace: "shopify--discovery--product_recommendation", key: "related_products") {
                    value
                    type
                    references(first: 10) {
                        edges {
                            node {
                                ... on Product {
                                    id
                                    title
                                    handle
                                    description
                                    priceRange {
                                        minVariantPrice {
                                            amount
                                            currencyCode
                                        }
                                    }
                                    images(first: 1) {
                                        edges {
                                            node {
                                                url
                                            }
                                        }
                                    }
                                    variants(first: 1) {
                                        edges {
                                            node {
                                                id
                                                sku
                                                quantityAvailable
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
        """
        
        variables = {"id": f"gid://shopify/Product/{product_id}"}
        
        url = f"https://{SHOPIFY_STORE}/api/{SHOPIFY_API_VERSION}/graphql.json"
        headers = {
            "Content-Type": "application/json",
            "X-Shopify-Storefront-Access-Token": SHOPIFY_STOREFRONT_TOKEN
        }
        
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json={"query": graphql_query, "variables": variables}, headers=headers)
            data = response.json()
            
            logger.info(f"Complementary products response for {product_id}: {data}")
            
            product_data = data.get("data", {}).get("product", {})
            
            complementary = []
            related = []
            
            # Parse complementary products
            comp_metafield = product_data.get("complementaryProducts")
            if comp_metafield and comp_metafield.get("references"):
                for edge in comp_metafield["references"].get("edges", []):
                    node = edge.get("node", {})
                    if node:
                        prod = parse_metafield_product(node)
                        if prod:
                            complementary.append(prod)
            
            # Parse related products
            rel_metafield = product_data.get("relatedProducts")
            if rel_metafield and rel_metafield.get("references"):
                for edge in rel_metafield["references"].get("edges", []):
                    node = edge.get("node", {})
                    if node:
                        prod = parse_metafield_product(node)
                        if prod:
                            related.append(prod)
            
            return {
                "complementary": complementary,
                "related": related
            }
            
    except Exception as e:
        logger.error(f"Error fetching complementary products: {e}")
        return {"complementary": [], "related": []}

@api_router.get("/products/{product_id}/equivalents")
async def get_equivalent_products(product_id: str):
    """Get "same part, different brand" equivalent products.

    Mirrors get_complementary_products() above, but reads the native,
    admin-managed `equivalent_product_ids` field instead of
    `complementary_product_ids`, and has no Shopify-metafield fallback -
    equivalence (same part number, different manufacturer/brand - e.g. John
    Deere vs. Vapormatic vs. FP Diesel vs. Reliance vs. Mahle for the same
    DZ110417 kit) is a purely admin-curated relationship with no Shopify
    concept to fall back to. If `equivalent_product_ids` is empty, this
    simply returns an empty list rather than querying Shopify.
    """
    local_product = await db.shopify_products.find_one({"id": product_id})
    equivalent_ids = (local_product or {}).get("equivalent_product_ids") or []

    equivalents = []
    for ref_id in equivalent_ids:
        ref_product = await db.shopify_products.find_one({"id": ref_id})
        if not ref_product:
            continue
        ref_product = apply_cloudflare_rollout(ref_product)
        equivalents.append({
            "id": ref_product.get("id"),
            "variant_id": None,
            "title": ref_product.get("title", ""),
            "handle": ref_product.get("handle", ""),
            "description": ref_product.get("description", ""),
            "price": ref_product.get("price", 0.0),
            "currency": ref_product.get("currency", "RON"),
            "image_url": ref_product.get("image_url"),
            "stock": ref_product.get("stock", 0),
            "stock_status": ref_product.get("stock_status"),
            "sku": ref_product.get("sku"),
            "vendor": ref_product.get("vendor"),
            "recommended_quantity": 1,
        })
    return {"equivalents": equivalents}

def parse_metafield_product(node: dict) -> dict:
    """Parse a product node from metafield references"""
    try:
        image_url = None
        if node.get("images", {}).get("edges"):
            image_url = node["images"]["edges"][0]["node"]["url"]
        
        stock = 0
        sku = None
        variant_id = None
        if node.get("variants", {}).get("edges"):
            variant = node["variants"]["edges"][0]["node"]
            stock = variant.get("quantityAvailable") or 0
            sku = variant.get("sku")
            variant_id = variant.get("id", "").replace("gid://shopify/ProductVariant/", "")
        
        price = 0.0
        currency = "RON"
        if node.get("priceRange", {}).get("minVariantPrice"):
            price = float(node["priceRange"]["minVariantPrice"]["amount"])
            currency = node["priceRange"]["minVariantPrice"]["currencyCode"]
        
        product_id = node["id"].replace("gid://shopify/Product/", "")
        
        return {
            "id": product_id,
            "variant_id": variant_id,
            "title": node.get("title", ""),
            "handle": node.get("handle", ""),
            "description": node.get("description", "")[:100] if node.get("description") else "",
            "price": price,
            "currency": currency,
            "image_url": image_url,
            "stock": stock,
            "sku": sku,
            "recommended_quantity": 1
        }
    except Exception as e:
        logger.error(f"Error parsing metafield product: {e}")
        return None

@api_router.get("/products/{product_id}", response_model=Product)
async def get_product(product_id: str):
    """Get a single product by ID"""
    try:
        # First try local DB
        product = await db.shopify_products.find_one({"id": product_id})

        if product:
            return Product(**apply_cloudflare_rollout(product))

        # Fallback to Shopify API
        graphql_query = """
        query getProduct($id: ID!) {
            product(id: $id) {
                id
                title
                handle
                description
                tags
                productType
                vendor
                priceRange {
                    minVariantPrice {
                        amount
                        currencyCode
                    }
                }
                images(first: 5) {
                    edges {
                        node {
                            url
                        }
                    }
                }
                variants(first: 1) {
                    edges {
                        node {
                            id
                            sku
                            quantityAvailable
                        }
                    }
                }
            }
        }
        """
        
        full_id = f"gid://shopify/Product/{product_id}"
        
        url = f"https://{SHOPIFY_STORE}/api/{SHOPIFY_API_VERSION}/graphql.json"
        headers = {
            "X-Shopify-Storefront-Access-Token": SHOPIFY_STOREFRONT_TOKEN,
            "Content-Type": "application/json"
        }
        
        async with httpx.AsyncClient() as http_client:
            response = await http_client.post(
                url,
                json={"query": graphql_query, "variables": {"id": full_id}},
                headers=headers,
                timeout=30.0
            )
            
            data = response.json()
            
            if not data.get("data", {}).get("product"):
                raise HTTPException(status_code=404, detail="Produs negăsit")
            
            product = parse_shopify_node(data["data"]["product"])
            return Product(**product)
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching product: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ==================== CART ENDPOINTS ====================

async def _get_authoritative_price(product_id: str) -> float:
    """Never trust a price submitted by a client (webshop or mobile) - look
    it up from the product catalog instead. Used at every point where a
    cart/order is created or priced, so a client can't add an item at an
    arbitrary price by editing the request. Raises 400 if the product_id
    doesn't exist, so a fabricated product_id can't be used to inject a
    fake line item either."""
    product = await db.shopify_products.find_one({"id": product_id}, {"price": 1})
    if not product or product.get("price") is None:
        raise HTTPException(status_code=400, detail=f"Produs inexistent: {product_id}")
    return product["price"]

@api_router.get("/cart/{session_id}", response_model=List[CartItem])
async def get_cart(session_id: str):
    """Get cart items for a session"""
    items = await db.cart.find({"session_id": session_id}).to_list(100)
    return [CartItem(**item) for item in items]

@api_router.post("/cart", response_model=CartItem)
async def add_to_cart(item: CartItemCreate):
    """Add item to cart"""
    item.price = await _get_authoritative_price(item.product_id)
    existing = await db.cart.find_one({
        "session_id": item.session_id,
        "product_id": item.product_id
    })
    
    if existing:
        new_quantity = existing["quantity"] + item.quantity
        await db.cart.update_one(
            {"id": existing["id"]},
            {"$set": {"quantity": new_quantity}}
        )
        existing["quantity"] = new_quantity
        return CartItem(**existing)
    
    cart_item = CartItem(**item.dict())
    await db.cart.insert_one(cart_item.dict())
    return cart_item

@api_router.put("/cart/{item_id}", response_model=CartItem)
async def update_cart_item(item_id: str, update: CartItemUpdate):
    """Update cart item quantity"""
    if update.quantity <= 0:
        await db.cart.delete_one({"id": item_id})
        raise HTTPException(status_code=200, detail="Articol eliminat din coș")
    
    result = await db.cart.find_one_and_update(
        {"id": item_id},
        {"$set": {"quantity": update.quantity}},
        return_document=True
    )
    
    if not result:
        raise HTTPException(status_code=404, detail="Articol negăsit în coș")
    
    return CartItem(**result)

@api_router.delete("/cart/{item_id}")
async def remove_from_cart(item_id: str):
    """Remove item from cart"""
    result = await db.cart.delete_one({"id": item_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Articol negăsit în coș")
    return {"message": "Articol eliminat din coș"}

@api_router.delete("/cart/session/{session_id}")
async def clear_cart(session_id: str):
    """Clear all items from cart for a session"""
    await db.cart.delete_many({"session_id": session_id})
    return {"message": "Coș golit"}

# ==================== ORDER ENDPOINTS ====================

def _build_crm_order_items_payload(items: List[dict]) -> List[dict]:
    """Shared by sync_order_to_crm (create) and sync_order_update_to_crm
    (item edits) so the two can't map fields differently."""
    return [
        {
            "denumire": item.get("product_name"),
            "cod_prod": item.get("product_id"),
            "cantitate": item.get("quantity"),
            "pret_unitar_cu_tva": item.get("price"),
        }
        for item in items
    ]


async def sync_order_to_crm(order: Order):
    """Fire-and-forget: push a newly created webshop order into agb-crm.

    Must never raise - any failure (missing config, timeout, connection
    error, 4xx/5xx) is logged and swallowed so it can't affect the order
    that was already saved/returned to the client.
    """
    if not CRM_API_URL or not CRM_INTEGRATION_KEY:
        logger.error("CRM sync skipped for order %s: CRM_API_URL/CRM_INTEGRATION_KEY not configured", order.id)
        return

    customer_payload = {
        "nume": order.customer.name,
        "email": order.customer.email,
        "telefon": order.customer.phone,
        "adresa_strada": order.customer.address,
        "adresa_oras": order.customer.city,
        "adresa_judet": order.customer.county,
        "adresa_cod_postal": order.customer.postal_code,
    }
    # Company/invoice fields only when the customer opted to have the order
    # invoiced on their company at checkout - omitted entirely (not sent as
    # null/empty) for personal orders, so the payload shape for is_company=
    # False orders is byte-identical to before this field was added. Mirrors
    # the shape already used for the /admin/customer-account lookup payload
    # above (denumire_societate/cui/reg_com/administrator/company_address).
    if order.customer.is_company:
        customer_payload.update({
            "denumire_societate": order.customer.company_name,
            "cui": order.customer.cui,
            "reg_com": order.customer.reg_com,
            "administrator": order.customer.administrator,
            "company_address": {
                "strada": order.customer.company_address_strada,
                "numar": order.customer.company_address_numar,
                "bloc": order.customer.company_address_bloc,
                "scara": order.customer.company_address_scara,
                "ap": order.customer.company_address_ap,
                "oras": order.customer.company_address_oras,
                "judet": order.customer.company_address_judet,
                "cod_postal": order.customer.company_address_cod_postal,
            },
        })

    payload = {
        "source": "webshop",
        "source_order_id": order.id,
        "payment_method": order.payment_method,
        "note": order.customer.notes,
        "customer": customer_payload,
        "items": _build_crm_order_items_payload(order.items),
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as http_client:
            response = await http_client.post(
                f"{CRM_API_URL}/integrations/orders",
                json=payload,
                headers={"X-Integration-Key": CRM_INTEGRATION_KEY},
            )
            if response.status_code >= 400:
                error_message = f"HTTP {response.status_code} - {response.text}"
                logger.error(
                    "CRM sync failed for order %s: %s",
                    order.id, error_message,
                )
                await db.orders.update_one(
                    {"id": order.id},
                    {
                        "$set": {"crm_synced": False, "crm_sync_error": error_message},
                        "$inc": {"crm_sync_attempts": 1},
                    },
                )
                new_attempts = order.crm_sync_attempts + 1
                if new_attempts >= 10:
                    logger.error(f"CRM SYNC FAILED PERMANENTLY for order {order.id} after 10 attempts - needs manual sync")
            else:
                await db.orders.update_one(
                    {"id": order.id},
                    {"$set": {"crm_synced": True, "crm_sync_error": None}},
                )
    except Exception as e:
        logger.error("CRM sync failed for order %s: %s", order.id, e)
        await db.orders.update_one(
            {"id": order.id},
            {
                "$set": {"crm_synced": False, "crm_sync_error": str(e)},
                "$inc": {"crm_sync_attempts": 1},
            },
        )
        new_attempts = order.crm_sync_attempts + 1
        if new_attempts >= 10:
            logger.error(f"CRM SYNC FAILED PERMANENTLY for order {order.id} after 10 attempts - needs manual sync")


async def sync_order_update_to_crm(order: Order):
    """Fire-and-forget: push an admin's item edit (add/remove/adjust
    products) on an already-CRM-synced order, overwriting its lines there.
    Same never-raise contract as sync_order_to_crm.

    Only called when the order's initial create-sync (crm_synced) already
    succeeded - if it hasn't landed yet, the reconciliation loop's retry of
    that pending create reads the order's current items fresh from the DB
    each time, so it already picks up any edit made before the create even
    lands. Nothing extra needed in that case."""
    if not CRM_API_URL or not CRM_INTEGRATION_KEY:
        logger.error("CRM items sync skipped for order %s: CRM_API_URL/CRM_INTEGRATION_KEY not configured", order.id)
        return

    payload = {
        "source": "webshop",
        "items": _build_crm_order_items_payload(order.items),
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as http_client:
            response = await http_client.put(
                f"{CRM_API_URL}/integrations/orders/{order.id}",
                json=payload,
                headers={"X-Integration-Key": CRM_INTEGRATION_KEY},
            )
            if response.status_code >= 400:
                error_message = f"HTTP {response.status_code} - {response.text}"
                logger.error("CRM items sync failed for order %s: %s", order.id, error_message)
                await db.orders.update_one(
                    {"id": order.id},
                    {
                        "$set": {"crm_items_dirty": True, "crm_items_sync_error": error_message},
                        "$inc": {"crm_items_sync_attempts": 1},
                    },
                )
            else:
                await db.orders.update_one(
                    {"id": order.id},
                    {"$set": {"crm_items_dirty": False, "crm_items_sync_error": None}},
                )
    except Exception as e:
        logger.error("CRM items sync failed for order %s: %s", order.id, e)
        await db.orders.update_one(
            {"id": order.id},
            {
                "$set": {"crm_items_dirty": True, "crm_items_sync_error": str(e)},
                "$inc": {"crm_items_sync_attempts": 1},
            },
        )


class _InsufficientStockAbort(Exception):
    """Raised inside a stock-reservation transaction callback to force
    with_transaction() to abort (not commit, not retry) as soon as any line
    item fails its conditional stock check. Never raised outside that
    callback - caught immediately around the with_transaction() call, never
    allowed to bubble further."""

    def __init__(self, product_ids: List[str]):
        self.product_ids = product_ids


# Substrings that identify "this Mongo deployment doesn't support
# multi-document transactions" as opposed to some other, genuine
# OperationFailure we should NOT swallow. Real pymongo against a standalone
# (non-replica-set) mongod raises OperationFailure with a message like
# "Transaction numbers are only allowed on a replica set member or mongos".
# mongomock-motor (used in tests - see scripts/test_stock_checkout.py)
# doesn't implement sessions at all and raises NotImplementedError with
# "Mongomock does not support sessions yet" instead - different exception
# type, so it's listed separately below, but detected the same way.
_NO_TRANSACTION_SUPPORT_MARKERS = ("replica set", "mongos", "does not support sessions")


def _looks_like_no_transaction_support(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in _NO_TRANSACTION_SUPPORT_MARKERS)


async def _decrement_stock_once(quantities: Dict[str, int], session) -> List[str]:
    """Try to atomically check-and-decrement every product in `quantities`
    (product_id -> total quantity requested across the order's line items).

    Each product's check-and-decrement is a single conditional
    find_one_and_update: the filter requires stock >= requested quantity,
    and the update $inc's stock down by that quantity in the same
    operation, so a single product's decrement is always race-safe against
    concurrent checkouts on its own, transaction or not. When stock lands
    exactly on 0, stock_status also flips to "out_of_stock" in the same
    pass - unless it's "supplier_stock" (backorder-from-supplier, still
    orderable at 0 stock - see _shopify_product_to_dict), which must not be
    overwritten by a plain zero-stock decrement.

    Returns the list of product_ids that did NOT have enough stock (or
    don't exist). Does not itself decide what to do about a non-empty
    result - see the two callers below (transaction path just lets the
    caller abort; no-session fallback path compensates manually)."""
    insufficient: List[str] = []
    for product_id, qty in quantities.items():
        result = await db.shopify_products.find_one_and_update(
            {"id": product_id, "stock": {"$gte": qty}},
            {"$inc": {"stock": -qty}},
            session=session,
            return_document=True,
        )
        if result is None:
            insufficient.append(product_id)
            continue
        if result["stock"] == 0 and result.get("stock_status") != "supplier_stock":
            await db.shopify_products.update_one(
                {"id": product_id},
                {"$set": {"stock_status": "out_of_stock"}},
                session=session,
            )
    return insufficient


async def _compensate_stock(quantities: Dict[str, int], succeeded: List[str]) -> None:
    """No-session fallback only: undo the decrements already applied for
    `succeeded` product_ids after a later item in the same order failed its
    check. Not needed on the transaction path - an aborted transaction
    never persists any of its writes, so there's nothing to undo there."""
    for product_id in succeeded:
        await db.shopify_products.update_one(
            {"id": product_id},
            {"$inc": {"stock": quantities[product_id]}},
        )
        # Best-effort: if this put stock back above 0, flip stock_status
        # back off "out_of_stock" so a rolled-back item doesn't get stuck
        # looking unavailable. Not atomic with the $inc above (fallback
        # path only - see _reserve_stock_for_order), but this is a rollback
        # of our own just-made change, not a customer-facing race.
        product = await db.shopify_products.find_one({"id": product_id}, {"stock": 1, "stock_status": 1})
        if product and product.get("stock", 0) > 0 and product.get("stock_status") == "out_of_stock":
            await db.shopify_products.update_one(
                {"id": product_id},
                {"$set": {"stock_status": "in_stock"}},
            )


async def _reserve_stock_for_order(items: List[dict]) -> None:
    """Atomically check-and-decrement stock for every line item of an
    order, all-or-nothing: if ANY line item doesn't have enough stock, NO
    line item's stock ends up decremented. Raises HTTPException(409) naming
    the out-of-stock product(s) if the reservation can't be made.

    Primary strategy: a single multi-document Mongo transaction wrapping
    every line item's conditional find_one_and_update (see
    _decrement_stock_once). This process's Mongo connection is an Atlas
    cluster (see the maxPoolSize comment near the top of this file, which
    explicitly says this process shares an Atlas M0 budget with agb-crm) -
    every Atlas cluster, including the free M0 tier, is provisioned as a
    replica set, so multi-document transactions are available and are the
    right tool here: wrap all of an order's per-item decrements in one
    transaction and let a failed item abort the whole thing, rather than
    hand-rolling compensating rollback for the common case.

    Defensive fallback: if this ever runs against a Mongo deployment that
    is NOT a replica set (e.g. a bare local mongod in some future dev
    setup - this should never happen against the real Atlas deployment),
    the driver raises OperationFailure the moment the transaction's first
    operation runs. Rather than let checkout hard-fail with a 500 in that
    case, that specific condition is caught once and we fall back to
    sequential per-item atomic find_one_and_update calls with manual
    compensating rollback (re-$inc any already-decremented items) if a
    later item fails. Each individual item's check-and-decrement is still
    race-safe on this fallback path since find_one_and_update is always
    atomic per-document - the only thing the fallback can't guarantee is
    that a concurrent checkout can never interleave BETWEEN two items of
    the SAME multi-item order (a vanishingly narrow window, and still never
    results in oversell of any single product, only a slightly late
    rollback of one already-decremented item)."""
    quantities: Dict[str, int] = {}
    for item in items:
        product_id = item.get("product_id")
        quantity = int(item.get("quantity", 1))
        # A zero/negative quantity here isn't just meaningless, it's
        # actively dangerous against the $inc-based decrement below: a
        # negative quantity would make {"stock": {"$gte": qty}} match
        # almost anything and turn "$inc stock by -qty" into a stock
        # INCREASE, letting a crafted request inflate a product's stock.
        # Reject before any stock is touched.
        if quantity <= 0:
            raise HTTPException(
                status_code=400,
                detail=f"Cantitate invalidă pentru produsul {product_id}.",
            )
        quantities[product_id] = quantities.get(product_id, 0) + quantity

    insufficient: List[str] = []

    async def _txn_callback(session):
        nonlocal insufficient
        insufficient = await _decrement_stock_once(quantities, session=session)
        if insufficient:
            # Abort - do not commit any of this callback's writes. Not
            # labeled TransientTransactionError, so with_transaction()
            # propagates it immediately without retrying.
            raise _InsufficientStockAbort(insufficient)

    try:
        async with await client.start_session() as session:
            await session.with_transaction(_txn_callback)
    except _InsufficientStockAbort as abort:
        insufficient = abort.product_ids
    except (OperationFailure, NotImplementedError) as e:
        if not _looks_like_no_transaction_support(e):
            raise
        logger.warning(
            "Stock reservation: Mongo deployment doesn't support multi-document "
            "transactions (%s) - falling back to sequential per-item reservation "
            "with manual compensating rollback.", e,
        )
        # No session: every find_one_and_update below is individually
        # atomic and commits immediately (there's no transaction to abort),
        # so unlike the transaction path we must explicitly undo whichever
        # items DID succeed if any other item in the same order failed.
        insufficient = await _decrement_stock_once(quantities, session=None)
        if insufficient:
            succeeded = [pid for pid in quantities if pid not in insufficient]
            await _compensate_stock(quantities, succeeded)

    if insufficient:
        products = await db.shopify_products.find(
            {"id": {"$in": insufficient}}, {"id": 1, "title": 1}
        ).to_list(len(insufficient))
        titles_by_id = {p["id"]: p.get("title") for p in products}
        names = [titles_by_id.get(pid) or pid for pid in insufficient]
        raise HTTPException(
            status_code=409,
            detail=f"Stoc epuizat de un alt client pentru: {', '.join(names)}.",
        )


_PAYMENT_METHOD_LABELS = {
    "ramburs": "Ramburs la livrare",
    "card": "Card online",
    "online": "Plată online",
}


async def _send_order_confirmation_email(order: Order) -> bool:
    """Send an order-confirmation email to the customer via Brevo - same
    provider/call pattern as send_password_reset_email above (only the
    template content differs). Fire-and-forget by convention: the ONLY
    caller (create_order, via background_tasks.add_task) never awaits this
    directly, so a False return here just gets logged, never surfaced to
    the customer whose order already succeeded."""
    try:
        if not BREVO_API_KEY:
            logger.warning("BREVO_API_KEY not set - skipping order confirmation email for order %s", order.id)
            return False

        import sib_api_v3_sdk

        configuration = sib_api_v3_sdk.Configuration()
        configuration.api_key['api-key'] = BREVO_API_KEY
        api_instance = sib_api_v3_sdk.TransactionalEmailsApi(sib_api_v3_sdk.ApiClient(configuration))

        items_rows = "".join(
            f"""
                <tr>
                    <td style="padding: 8px; border-bottom: 1px solid #eee;">{html.escape(str(item.get('product_name', '')))}</td>
                    <td style="padding: 8px; border-bottom: 1px solid #eee; text-align: center;">{html.escape(str(item.get('quantity', 1)))}</td>
                    <td style="padding: 8px; border-bottom: 1px solid #eee; text-align: right;">{item.get('price', 0):.2f} RON</td>
                </tr>
            """
            for item in order.items
        )
        payment_method_label = _PAYMENT_METHOD_LABELS.get(order.payment_method, order.payment_method)

        html_content = f"""
        <html>
        <body style="font-family: Arial, sans-serif; background-color: #f5f5f5; padding: 20px;">
            <div style="max-width: 600px; margin: 0 auto; background-color: #ffffff; border-radius: 10px; overflow: hidden;">
                <div style="background-color: #367c2b; padding: 20px; text-align: center;">
                    <h1 style="color: #ffffff; margin: 0;">🚜 AGB Agroparts</h1>
                </div>
                <div style="padding: 30px;">
                    <h2 style="color: #333;">Îți mulțumim pentru comandă!</h2>
                    <p style="color: #666; line-height: 1.6;">Am înregistrat comanda ta cu numărul <strong>{html.escape(order.id)}</strong>. Te vom contacta în cel mai scurt timp pentru confirmare și livrare.</p>
                    <table style="width: 100%; border-collapse: collapse; margin-top: 20px;">
                        <thead>
                            <tr>
                                <th style="text-align: left; padding: 8px; border-bottom: 2px solid #367c2b; color: #333;">Produs</th>
                                <th style="text-align: center; padding: 8px; border-bottom: 2px solid #367c2b; color: #333;">Cant.</th>
                                <th style="text-align: right; padding: 8px; border-bottom: 2px solid #367c2b; color: #333;">Preț</th>
                            </tr>
                        </thead>
                        <tbody>
                            {items_rows}
                        </tbody>
                    </table>
                    <div style="margin-top: 20px; text-align: right; color: #333;">
                        <p style="margin: 4px 0;">Subtotal: {order.subtotal:.2f} RON</p>
                        <p style="margin: 4px 0;">Transport: {order.shipping:.2f} RON</p>
                        <p style="margin: 4px 0; font-size: 18px; font-weight: bold;">Total: {order.total:.2f} RON</p>
                    </div>
                    <p style="color: #666; line-height: 1.6; margin-top: 20px;">Metodă de plată: <strong>{html.escape(payment_method_label)}</strong></p>
                    <p style="color: #666; line-height: 1.6; margin-top: 20px;">Îți mulțumim că ai ales AGB Agroparts!</p>
                </div>
                <div style="background-color: #f0f0f0; padding: 15px; text-align: center; font-size: 12px; color: #999;">
                    <p>AGB Agroparts Solution S.R.L.</p>
                </div>
            </div>
        </body>
        </html>
        """

        send_smtp_email = sib_api_v3_sdk.SendSmtpEmail(
            to=[{"email": order.customer.email, "name": order.customer.name or order.customer.email}],
            sender={"email": "noreply@agb-agroparts.ro", "name": "AGB Agroparts"},
            subject=f"✅ Confirmare comandă #{order.id} - AGB Agroparts",
            html_content=html_content
        )

        api_instance.send_transac_email(send_smtp_email)
        logger.info(f"Order confirmation email sent to {order.customer.email} for order {order.id}")
        return True

    except Exception as e:
        logger.error(f"Error sending order confirmation email for order {order.id}: {e}")
        return False


# Fixed staff inbox for new-order alerts - so whoever's on duty knows a new
# order came in without needing to be logged into the CRM. Same address the
# Brevo account itself is registered under (agbagroparts.solution@yahoo.com).
STAFF_ORDER_NOTIFICATION_EMAIL = "agbagroparts.solution@yahoo.com"


async def _send_new_order_staff_notification(order: Order) -> bool:
    """Alerts AGB staff (STAFF_ORDER_NOTIFICATION_EMAIL) that a new order came
    in - same provider/call pattern as _send_order_confirmation_email above,
    just a different recipient/content: this one leads with the customer's
    own contact details (name/phone/email) since staff need to know WHO to
    reach, not just what was ordered. Fire-and-forget, same as the customer
    email - the ONLY caller (create_order) never awaits this directly."""
    try:
        if not BREVO_API_KEY:
            logger.warning("BREVO_API_KEY not set - skipping new-order staff notification for order %s", order.id)
            return False

        import sib_api_v3_sdk

        configuration = sib_api_v3_sdk.Configuration()
        configuration.api_key['api-key'] = BREVO_API_KEY
        api_instance = sib_api_v3_sdk.TransactionalEmailsApi(sib_api_v3_sdk.ApiClient(configuration))

        items_rows = "".join(
            f"""
                <tr>
                    <td style="padding: 8px; border-bottom: 1px solid #eee;">{html.escape(str(item.get('product_name', '')))}</td>
                    <td style="padding: 8px; border-bottom: 1px solid #eee; text-align: center;">{html.escape(str(item.get('quantity', 1)))}</td>
                    <td style="padding: 8px; border-bottom: 1px solid #eee; text-align: right;">{item.get('price', 0):.2f} RON</td>
                </tr>
            """
            for item in order.items
        )
        payment_method_label = _PAYMENT_METHOD_LABELS.get(order.payment_method, order.payment_method)
        customer = order.customer

        html_content = f"""
        <html>
        <body style="font-family: Arial, sans-serif; background-color: #f5f5f5; padding: 20px;">
            <div style="max-width: 600px; margin: 0 auto; background-color: #ffffff; border-radius: 10px; overflow: hidden;">
                <div style="background-color: #367c2b; padding: 20px; text-align: center;">
                    <h1 style="color: #ffffff; margin: 0;">🔔 Comandă nouă</h1>
                </div>
                <div style="padding: 30px;">
                    <h2 style="color: #333;">Comanda #{html.escape(order.id)}</h2>
                    <table style="width: 100%; border-collapse: collapse; margin-bottom: 20px;">
                        <tr><td style="padding: 4px 0; color: #666;">Client:</td><td style="padding: 4px 0; color: #333;"><strong>{html.escape(customer.name)}</strong></td></tr>
                        <tr><td style="padding: 4px 0; color: #666;">Telefon:</td><td style="padding: 4px 0; color: #333;">{html.escape(customer.phone)}</td></tr>
                        <tr><td style="padding: 4px 0; color: #666;">Email:</td><td style="padding: 4px 0; color: #333;">{html.escape(customer.email)}</td></tr>
                        <tr><td style="padding: 4px 0; color: #666;">Adresă:</td><td style="padding: 4px 0; color: #333;">{html.escape(customer.address)}, {html.escape(customer.city)}, {html.escape(customer.county)} {html.escape(customer.postal_code)}</td></tr>
                    </table>
                    <table style="width: 100%; border-collapse: collapse;">
                        <thead>
                            <tr>
                                <th style="text-align: left; padding: 8px; border-bottom: 2px solid #367c2b; color: #333;">Produs</th>
                                <th style="text-align: center; padding: 8px; border-bottom: 2px solid #367c2b; color: #333;">Cant.</th>
                                <th style="text-align: right; padding: 8px; border-bottom: 2px solid #367c2b; color: #333;">Preț</th>
                            </tr>
                        </thead>
                        <tbody>
                            {items_rows}
                        </tbody>
                    </table>
                    <div style="margin-top: 20px; text-align: right; color: #333;">
                        <p style="margin: 4px 0;">Subtotal: {order.subtotal:.2f} RON</p>
                        <p style="margin: 4px 0;">Transport: {order.shipping:.2f} RON</p>
                        <p style="margin: 4px 0; font-size: 18px; font-weight: bold;">Total: {order.total:.2f} RON</p>
                    </div>
                    <p style="color: #666; line-height: 1.6; margin-top: 20px;">Metodă de plată: <strong>{html.escape(payment_method_label)}</strong></p>
                    {f'<p style="color: #666; line-height: 1.6;">Note: {html.escape(customer.notes)}</p>' if customer.notes else ''}
                </div>
            </div>
        </body>
        </html>
        """

        send_smtp_email = sib_api_v3_sdk.SendSmtpEmail(
            to=[{"email": STAFF_ORDER_NOTIFICATION_EMAIL}],
            sender={"email": "noreply@agb-agroparts.ro", "name": "AGB Agroparts"},
            subject=f"🔔 Comandă nouă #{order.id} - {customer.name}",
            html_content=html_content
        )

        api_instance.send_transac_email(send_smtp_email)
        logger.info(f"New-order staff notification sent to {STAFF_ORDER_NOTIFICATION_EMAIL} for order {order.id}")
        return True

    except Exception as e:
        logger.error(f"Error sending new-order staff notification for order {order.id}: {e}")
        return False


@api_router.post("/orders", response_model=Order)
async def create_order(order_data: OrderCreate, background_tasks: BackgroundTasks):
    """Create a new order"""
    if not order_data.items:
        raise HTTPException(status_code=400, detail="Comanda trebuie să aibă cel puțin un produs.")
    for item in order_data.items:
        item["price"] = await _get_authoritative_price(item.get("product_id"))
    order_data.subtotal = sum(item["price"] * item.get("quantity", 1) for item in order_data.items)
    order_data.shipping = 25.0
    order_data.total = order_data.subtotal + order_data.shipping
    # Atomic check-and-decrement of stock for every line item, all-or-
    # nothing across the whole order - must happen after pricing (so a bad
    # product_id already 400'd via _get_authoritative_price above) and
    # before the order is actually persisted, so a rejected order never
    # gets written to db.orders at all.
    await _reserve_stock_for_order(order_data.items)
    order = Order(**order_data.dict())
    await db.orders.insert_one(order.dict())
    await db.cart.delete_many({"session_id": order_data.session_id})

    # Best-effort conversion link for the traffic/conversion analytics
    # (GET /admin/analytics/traffic) - only when the checkout actually sent
    # one (pre-existing webshop/mobile clients never will). Deliberately a
    # plain synchronous DB insert rather than a background task: it's a
    # single small local-DB write (not an outbound network call like
    # sync_order_to_crm/the confirmation email), so there's no latency
    # reason to defer it, and doing it inline means it's reliably done by
    # the time this response returns. Wrapped in try/except so a failure
    # here (e.g. a transient DB hiccup) can never affect the order response
    # - same fire-and-forget philosophy as sync_order_to_crm.
    if order_data.analytics_session_id:
        try:
            await db.analytics_conversions.insert_one({
                "_id": str(uuid.uuid4()),
                "session_id": order_data.analytics_session_id,
                "order_id": order.id,
                "created_at": datetime.utcnow(),
            })
        except Exception:
            logger.exception("Failed to record analytics conversion for order %s", order.id)

    background_tasks.add_task(sync_order_to_crm, order)
    background_tasks.add_task(_send_order_confirmation_email, order)
    background_tasks.add_task(_send_new_order_staff_notification, order)
    return order

# NOTE: must stay registered *before* GET /orders/{session_id} below - same
# literal-path-before-wildcard ordering gotcha as elsewhere in this file,
# otherwise "mobile" would be swallowed as a session_id.
@api_router.get("/orders/mobile")
async def get_mobile_orders(request: Request, limit: int = 50):
    """Get orders created from the mobile app"""
    await _require_admin(request)
    orders = await db.mobile_orders.find().sort("created_at", -1).limit(limit).to_list(limit)
    for order in orders:
        order["_id"] = str(order["_id"])
    return orders

@api_router.get("/orders/{session_id}", response_model=List[Order])
async def get_orders(session_id: str):
    """Get orders for a session"""
    orders = await db.orders.find({"session_id": session_id}).sort("created_at", -1).to_list(100)
    return [Order(**order) for order in orders]

@api_router.get("/order/{order_id}", response_model=Order)
async def get_order(order_id: str):
    """Get a single order by ID"""
    order = await db.orders.find_one({"id": order_id})
    if not order:
        raise HTTPException(status_code=404, detail="Comandă negăsită")
    return Order(**order)

# ==================== SHOPIFY CHECKOUT ====================

class CheckoutRequest(BaseModel):
    items: List[dict]  # [{product_id, variant_id, quantity}]
    email: Optional[str] = None

@api_router.post("/checkout/create")
async def create_shopify_checkout(request: CheckoutRequest):
    """Create a Shopify checkout and return the checkout URL"""
    try:
        # First, we need to get variant IDs for each product from Shopify
        line_items = []
        
        for item in request.items:
            product_id = item.get("product_id", "")
            quantity = item.get("quantity", 1)
            
            # Always fetch variant from Shopify directly
            graphql_query = """
            query getProduct($id: ID!) {
                product(id: $id) {
                    variants(first: 1) {
                        edges {
                            node {
                                id
                            }
                        }
                    }
                }
            }
            """
            
            full_id = f"gid://shopify/Product/{product_id}"
            url = f"https://{SHOPIFY_STORE}/api/{SHOPIFY_API_VERSION}/graphql.json"
            headers = {
                "X-Shopify-Storefront-Access-Token": SHOPIFY_STOREFRONT_TOKEN,
                "Content-Type": "application/json"
            }
            
            async with httpx.AsyncClient() as http_client:
                response = await http_client.post(
                    url,
                    json={"query": graphql_query, "variables": {"id": full_id}},
                    headers=headers,
                    timeout=30.0
                )
                data = response.json()
                logger.info(f"Shopify product response for {product_id}: {data}")
                
                product_data = data.get("data", {}).get("product", {})
                if product_data:
                    variants = product_data.get("variants", {}).get("edges", [])
                    if variants:
                        variant_id = variants[0].get("node", {}).get("id", "")
                        if variant_id:
                            line_items.append({
                                "variantId": variant_id,
                                "quantity": quantity
                            })
                            logger.info(f"Added variant {variant_id} for product {product_id}")
        
        if not line_items:
            raise HTTPException(status_code=400, detail="Nu s-au găsit produse valide pentru checkout")
        
        # Create cart using Storefront API (new Cart API)
        cart_mutation = """
        mutation cartCreate($input: CartInput!) {
            cartCreate(input: $input) {
                cart {
                    id
                    checkoutUrl
                    cost {
                        totalAmount {
                            amount
                            currencyCode
                        }
                    }
                }
                userErrors {
                    code
                    field
                    message
                }
            }
        }
        """
        
        # Convert line items to cart format
        cart_lines = []
        for item in line_items:
            cart_lines.append({
                "merchandiseId": item["variantId"],
                "quantity": item["quantity"]
            })
        
        cart_input = {
            "lines": cart_lines
        }
        
        if request.email:
            cart_input["buyerIdentity"] = {"email": request.email}
        
        url = f"https://{SHOPIFY_STORE}/api/{SHOPIFY_API_VERSION}/graphql.json"
        headers = {
            "X-Shopify-Storefront-Access-Token": SHOPIFY_STOREFRONT_TOKEN,
            "Content-Type": "application/json"
        }
        
        async with httpx.AsyncClient() as http_client:
            response = await http_client.post(
                url,
                json={
                    "query": cart_mutation,
                    "variables": {"input": cart_input}
                },
                headers=headers,
                timeout=30.0
            )
            
            data = response.json()
            logger.info(f"Cart response: {data}")
            
            # Check for GraphQL errors
            if "errors" in data:
                error_msgs = [e.get("message", "") for e in data.get("errors", [])]
                logger.error(f"GraphQL errors: {error_msgs}")
                raise HTTPException(status_code=500, detail=f"Eroare Shopify: {'; '.join(error_msgs)}")
            
            cart_data = data.get("data", {}).get("cartCreate", {})
            user_errors = cart_data.get("userErrors", [])
            
            if user_errors:
                error_msg = "; ".join([e.get("message", "") for e in user_errors])
                raise HTTPException(status_code=400, detail=f"Eroare cart: {error_msg}")
            
            cart = cart_data.get("cart", {})
            
            if not cart or not cart.get("checkoutUrl"):
                raise HTTPException(status_code=500, detail="Nu s-a putut crea checkout-ul")
            
            return {
                "checkout_id": cart.get("id"),
                "checkout_url": cart.get("checkoutUrl"),
                "total": cart.get("cost", {}).get("totalAmount", {})
            }
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating checkout: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ==================== AUTH ENDPOINTS ====================

async def sync_account_to_crm(user: dict) -> None:
    """Fire-and-forget: push a newly registered account (webshop or mobile -
    both go through this same /auth/register endpoint) into agb-crm as a
    client record.

    Must never raise - any failure (missing config, timeout, connection
    error, 4xx/5xx) is logged and swallowed so it can't affect the account
    that was already created and returned to the client. Mirrors
    sync_order_to_crm / sync_interest_to_crm.
    """
    if not CRM_API_URL or not CRM_INTEGRATION_KEY:
        logger.error("CRM sync skipped for new account %s: CRM_API_URL/CRM_INTEGRATION_KEY not configured", user.get("id"))
        return

    is_company = bool(user.get("is_company"))
    company_street = (user.get("company_address_strada") or "").strip()

    payload = {
        "nume": user.get("name"),
        "email": user.get("email"),
        "telefon": user.get("phone"),
        "denumire_societate": user.get("company_name") if is_company else None,
        "cui": user.get("cui") if is_company else None,
        "adresa_strada": user.get("address_strada"),
        "adresa_numar": user.get("address_numar"),
        "adresa_bloc": user.get("address_bloc"),
        "adresa_scara": user.get("address_scara"),
        "adresa_ap": user.get("address_ap"),
        "adresa_oras": user.get("city"),
        "adresa_judet": user.get("county"),
        "adresa_cod_postal": user.get("postal_code"),
        "company_address": {
            "strada": user.get("company_address_strada"),
            "numar": user.get("company_address_numar"),
            "bloc": user.get("company_address_bloc"),
            "scara": user.get("company_address_scara"),
            "ap": user.get("company_address_ap"),
            "oras": user.get("company_address_oras"),
            "judet": user.get("company_address_judet"),
            "cod_postal": user.get("company_address_cod_postal"),
        } if is_company and company_street else None,
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as http_client:
            response = await http_client.post(
                f"{CRM_API_URL}/integrations/clients",
                json=payload,
                headers={"X-Integration-Key": CRM_INTEGRATION_KEY},
            )
            if response.status_code >= 400:
                logger.error(
                    "CRM account sync failed for user %s: HTTP %s - %s",
                    user.get("id"), response.status_code, response.text,
                )
    except Exception as e:
        logger.error("CRM account sync failed for user %s: %s", user.get("id"), e)


@api_router.post("/auth/register")
async def register_user(user_data: UserRegister, background_tasks: BackgroundTasks, request: Request):
    """Register a new user - fully local account, independent of Shopify"""
    _enforce_rate_limit(
        f"register:ip:{_client_ip(request)}", REGISTER_IP_LIMIT, REGISTER_IP_WINDOW_SECONDS,
        "Prea multe conturi create de la această adresă. Încearcă din nou mai târziu.",
    )

    email = user_data.email.lower().strip()

    if len(user_data.password) < 6:
        raise HTTPException(status_code=400, detail="Parola trebuie să aibă minim 6 caractere")

    if not user_data.terms_accepted:
        raise HTTPException(
            status_code=400,
            detail="Trebuie să accepți Termenii și Politica de confidențialitate.",
        )

    existing_user = await db.users.find_one({"email": email})
    if existing_user:
        raise HTTPException(status_code=400, detail="Adresa de email este deja înregistrată")

    user_id = str(uuid.uuid4())
    token_doc = _new_session_token_doc()
    local_token = token_doc["token"]
    created_at = datetime.utcnow()

    user = {
        "id": user_id,
        "email": email,
        "password_hash": await hash_password(user_data.password),
        "name": user_data.name,
        "phone": user_data.phone,
        "address": None,
        "address_strada": None,
        "address_numar": None,
        "address_bloc": None,
        "address_scara": None,
        "address_ap": None,
        "city": None,
        "county": None,
        "postal_code": None,
        "is_company": False,
        "company_name": None,
        "cui": None,
        "reg_com": None,
        "administrator": None,
        "company_address": None,
        "company_address_strada": None,
        "company_address_numar": None,
        "company_address_bloc": None,
        "company_address_scara": None,
        "company_address_ap": None,
        "company_address_oras": None,
        "company_address_judet": None,
        "company_address_cod_postal": None,
        "tokens": [token_doc],
        "is_shopify_customer": False,
        "notify_news_email": True,
        "created_at": created_at,
        # GDPR consent to Terms + Privacy Policy, recorded at registration
        # time only - see CURRENT_TERMS_VERSION above. Only ever set here,
        # on NEW registrations; existing users predating this field simply
        # have it absent/None and are never backfilled or forced to
        # re-consent at login.
        "consent_accepted_at": created_at,
        "consent_terms_version": CURRENT_TERMS_VERSION,
    }

    try:
        await db.users.insert_one(user)
    except Exception as e:
        # Guards against a race with the unique index on email (two concurrent
        # registrations for the same address slipping past the find_one check above)
        if "duplicate key" in str(e).lower():
            raise HTTPException(status_code=400, detail="Adresa de email este deja înregistrată")
        raise

    background_tasks.add_task(sync_account_to_crm, user)

    return {
        "token": local_token,
        "user": {
            "id": user_id,
            "email": email,
            "name": user_data.name,
            "phone": user_data.phone,
            "is_company": False,
            "is_shopify_customer": False,
            "created_at": created_at
        }
    }

class ForgotPasswordRequest(BaseModel):
    email: str

class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str


def _public_base_url() -> str:
    return os.environ.get('RENDER_EXTERNAL_URL', 'http://localhost:8002').rstrip('/')


async def send_password_reset_email(recipient_email: str, recipient_name: str, reset_url: str) -> bool:
    """Send a password reset email via Brevo (same provider/style as
    send_blog_notification_email further down this file)."""
    try:
        if not BREVO_API_KEY:
            logger.warning("BREVO_API_KEY not set - skipping password reset email")
            return False

        import sib_api_v3_sdk

        configuration = sib_api_v3_sdk.Configuration()
        configuration.api_key['api-key'] = BREVO_API_KEY
        api_instance = sib_api_v3_sdk.TransactionalEmailsApi(sib_api_v3_sdk.ApiClient(configuration))

        html_content = f"""
        <html>
        <body style="font-family: Arial, sans-serif; background-color: #f5f5f5; padding: 20px;">
            <div style="max-width: 600px; margin: 0 auto; background-color: #ffffff; border-radius: 10px; overflow: hidden;">
                <div style="background-color: #367c2b; padding: 20px; text-align: center;">
                    <h1 style="color: #ffffff; margin: 0;">🚜 AGB Agroparts</h1>
                </div>
                <div style="padding: 30px;">
                    <h2 style="color: #333;">Resetare parolă</h2>
                    <p style="color: #666; line-height: 1.6;">Am primit o cerere de resetare a parolei pentru contul tău. Link-ul de mai jos este valabil 1 oră.</p>
                    <div style="text-align: center; margin-top: 30px;">
                        <a href="{reset_url}" style="background-color: #367c2b; color: #ffffff; padding: 15px 30px; text-decoration: none; border-radius: 5px; font-weight: bold;">
                            Resetează parola
                        </a>
                    </div>
                    <p style="color: #999; font-size: 13px; margin-top: 30px;">Dacă nu ai cerut tu resetarea, poți ignora acest email.</p>
                </div>
                <div style="background-color: #f0f0f0; padding: 15px; text-align: center; font-size: 12px; color: #999;">
                    <p>AGB Agroparts Solution S.R.L.</p>
                </div>
            </div>
        </body>
        </html>
        """

        send_smtp_email = sib_api_v3_sdk.SendSmtpEmail(
            to=[{"email": recipient_email, "name": recipient_name or recipient_email}],
            sender={"email": "noreply@agb-agroparts.ro", "name": "AGB Agroparts"},
            subject="🔑 Resetare parolă - AGB Agroparts",
            html_content=html_content
        )

        api_instance.send_transac_email(send_smtp_email)
        logger.info(f"Password reset email sent to {recipient_email}")
        return True

    except Exception as e:
        logger.error(f"Error sending password reset email to {recipient_email}: {e}")
        return False


@api_router.post("/auth/forgot-password")
async def forgot_password(request: ForgotPasswordRequest, http_request: Request):
    """Send a local password reset email. Always returns a generic success
    message regardless of whether the address is registered, to avoid
    leaking which emails have accounts."""
    _enforce_rate_limit(
        f"forgot-password:ip:{_client_ip(http_request)}", FORGOT_PASSWORD_IP_LIMIT, FORGOT_PASSWORD_IP_WINDOW_SECONDS,
        "Prea multe cereri de resetare a parolei de la această adresă. Încearcă din nou mai târziu.",
    )
    email = request.email.lower().strip()
    user = await db.users.find_one({"email": email})

    if user:
        reset_token = secrets.token_urlsafe(32)
        await db.users.update_one(
            {"email": email},
            {"$set": {
                "reset_token": reset_token,
                "reset_token_expires": datetime.utcnow() + timedelta(hours=1),
            }}
        )
        webshop_public_url = os.environ.get('WEBSHOP_PUBLIC_URL', 'http://localhost:3000').rstrip('/')
        reset_url = f"{webshop_public_url}/cont/reseteaza-parola?token={reset_token}"
        await send_password_reset_email(email, user.get("name", ""), reset_url)

    return {"message": "Dacă adresa există în sistem, ai primit un email cu instrucțiuni de resetare"}


@api_router.post("/auth/reset-password")
async def reset_password(request: ResetPasswordRequest):
    """Consume a reset token minted by /auth/forgot-password and set a new password."""
    if len(request.new_password) < 6:
        raise HTTPException(status_code=400, detail="Parola trebuie să aibă minim 6 caractere")

    user = await db.users.find_one({"reset_token": request.token})
    if not user:
        raise HTTPException(status_code=400, detail="Link de resetare invalid")

    expires = user.get("reset_token_expires")
    if not expires or expires < datetime.utcnow():
        raise HTTPException(status_code=400, detail="Link de resetare expirat. Cere unul nou.")

    # A password reset invalidates every existing session for this account
    # (all devices get logged out and must sign in again with the new
    # password) - this endpoint never hands the caller a fresh session token
    # to keep using, so there's no "device" to preserve here anyway.
    await db.users.update_one(
        {"id": user["id"]},
        {
            "$set": {"password_hash": await hash_password(request.new_password), "tokens": []},
            "$unset": {"reset_token": "", "reset_token_expires": "", "token": ""},
        }
    )

    return {"message": "Parola a fost schimbată cu succes"}


@api_router.get("/reset-password", response_class=HTMLResponse)
async def reset_password_page(token: str = ""):
    """Minimal, self-contained password reset form. No separate web frontend
    exists yet for the future storefront, so this is served directly by the
    API — same idiom as /privacy-policy below."""
    html_content = f"""
    <!DOCTYPE html>
    <html lang="ro">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Resetare parolă - AGB Agroparts</title>
        <style>
            body {{ font-family: Arial, sans-serif; max-width: 420px; margin: 60px auto; padding: 20px; }}
            h1 {{ color: #367c2b; font-size: 22px; }}
            label {{ display: block; margin-top: 16px; color: #333; font-size: 14px; }}
            input {{ width: 100%; padding: 10px; margin-top: 6px; border: 1px solid #ccc; border-radius: 6px; box-sizing: border-box; }}
            button {{ margin-top: 24px; width: 100%; background-color: #367c2b; color: #fff; border: none; padding: 12px; border-radius: 6px; font-size: 15px; cursor: pointer; }}
            #message {{ margin-top: 16px; font-size: 14px; }}
            .error {{ color: #b00020; }}
            .success {{ color: #367c2b; }}
        </style>
    </head>
    <body>
        <h1>🚜 Resetare parolă</h1>
        <form id="reset-form">
            <label for="password">Parolă nouă</label>
            <input type="password" id="password" minlength="6" required>
            <label for="confirm">Confirmă parola</label>
            <input type="password" id="confirm" minlength="6" required>
            <button type="submit">Schimbă parola</button>
        </form>
        <div id="message"></div>
        <script>
            const token = {token!r};
            const form = document.getElementById('reset-form');
            const messageEl = document.getElementById('message');
            form.addEventListener('submit', async (e) => {{
                e.preventDefault();
                const password = document.getElementById('password').value;
                const confirm = document.getElementById('confirm').value;
                messageEl.className = '';
                if (password !== confirm) {{
                    messageEl.textContent = 'Parolele nu coincid.';
                    messageEl.className = 'error';
                    return;
                }}
                try {{
                    const resp = await fetch('/api/auth/reset-password', {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{ token: token, new_password: password }})
                    }});
                    const data = await resp.json();
                    if (resp.ok) {{
                        messageEl.textContent = data.message;
                        messageEl.className = 'success';
                        form.style.display = 'none';
                    }} else {{
                        messageEl.textContent = data.detail || 'A apărut o eroare.';
                        messageEl.className = 'error';
                    }}
                }} catch (err) {{
                    messageEl.textContent = 'A apărut o eroare de rețea.';
                    messageEl.className = 'error';
                }}
            }});
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)

async def _legacy_shopify_login_and_migrate(email: str, password: str, existing_user: Optional[dict]) -> dict:
    """Fallback path for accounts created before local auth existed (or any
    email not yet in our `users` collection): verify the password against
    Shopify exactly as the app always has, and on success silently persist a
    local bcrypt hash of the password just submitted. From then on this
    account authenticates locally via `_authenticate_user` and no longer
    needs Shopify to log in.
    """
    mutation = """
    mutation customerAccessTokenCreate($input: CustomerAccessTokenCreateInput!) {
        customerAccessTokenCreate(input: $input) {
            customerAccessToken {
                accessToken
                expiresAt
            }
            customerUserErrors {
                code
                field
                message
            }
        }
    }
    """

    headers = {
        "Content-Type": "application/json",
        "X-Shopify-Storefront-Access-Token": SHOPIFY_STOREFRONT_TOKEN
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"https://{SHOPIFY_STORE}/api/{SHOPIFY_API_VERSION}/graphql.json",
            json={"query": mutation, "variables": {"input": {"email": email, "password": password}}},
            headers=headers
        )

        data = response.json()
        logger.info(f"Shopify customer login response: {data}")

        result = data.get("data", {}).get("customerAccessTokenCreate", {})
        errors = result.get("customerUserErrors", [])

        if errors:
            raise HTTPException(status_code=401, detail="Email sau parolă incorectă")

        customer_token = result.get("customerAccessToken", {})
        if not customer_token or not customer_token.get("accessToken"):
            raise HTTPException(status_code=401, detail="Email sau parolă incorectă")

        shopify_access_token = customer_token.get("accessToken")

    # Get customer details from Shopify
    customer_query = """
    query getCustomer($customerAccessToken: String!) {
        customer(customerAccessToken: $customerAccessToken) {
            id
            email
            firstName
            lastName
            phone
            defaultAddress {
                address1
                address2
                city
                province
                zip
                country
                company
            }
            orders(first: 10) {
                edges {
                    node {
                        id
                        orderNumber
                        totalPrice {
                            amount
                            currencyCode
                        }
                        processedAt
                        fulfillmentStatus
                    }
                }
            }
        }
    }
    """

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"https://{SHOPIFY_STORE}/api/{SHOPIFY_API_VERSION}/graphql.json",
            json={
                "query": customer_query,
                "variables": {"customerAccessToken": shopify_access_token}
            },
            headers=headers
        )

        customer_data = response.json()
        customer = customer_data.get("data", {}).get("customer")

        if not customer:
            raise HTTPException(status_code=401, detail="Nu s-au putut obține datele contului")

    # Create or update local user linked to Shopify
    shopify_customer_id = customer.get("id", "").replace("gid://shopify/Customer/", "")

    default_address = customer.get("defaultAddress") or {}

    user_update_data = {
        "email": email,
        "name": f"{customer.get('firstName', '')} {customer.get('lastName', '')}".strip() or email.split('@')[0],
        "phone": customer.get("phone") or "",
        "address": default_address.get("address1") or "",
        "city": default_address.get("city") or "",
        "county": default_address.get("province") or "",
        "postal_code": default_address.get("zip") or "",
        "is_company": bool(default_address.get("company")),
        "company_name": default_address.get("company") or None,
        # Silent migration: from this point on, this account has a local
        # password and no longer needs the Shopify fallback above.
        "password_hash": await hash_password(password),
        "is_shopify_customer": True,
        "shopify_customer_id": shopify_customer_id,
        "shopify_access_token": shopify_access_token,
        "updated_at": datetime.utcnow()
    }

    if existing_user:
        # Preserve locally-saved data that Shopify doesn't have
        preserved_fields = ['cui', 'reg_com', 'company_address', 'is_company', 'company_name']
        for field in preserved_fields:
            if not user_update_data.get(field) and existing_user.get(field):
                user_update_data[field] = existing_user[field]

        local_fields = ['phone', 'address', 'city', 'county', 'postal_code']
        for field in local_fields:
            if not user_update_data.get(field) and existing_user.get(field):
                user_update_data[field] = existing_user[field]

        await db.users.update_one(
            {"email": email},
            {"$set": user_update_data}
        )
        user_id = existing_user["id"]
        created_at = existing_user["created_at"]
        # Enforce the concurrent-device cap on this (potentially long-lived)
        # account the same as any other login path.
        local_token = await _issue_session_token(email)
    else:
        user_id = str(uuid.uuid4())
        user_update_data["id"] = user_id
        user_update_data["created_at"] = datetime.utcnow()
        created_at = user_update_data["created_at"]
        # Brand new account - no existing sessions, so no cap check needed.
        token_doc = _new_session_token_doc()
        local_token = token_doc["token"]
        user_update_data["tokens"] = [token_doc]
        await db.users.insert_one(user_update_data)

    # Extract Shopify orders (only available on this legacy/Shopify-verified path)
    shopify_orders = []
    orders_edges = customer.get("orders", {}).get("edges", [])
    for edge in orders_edges:
        order = edge.get("node", {})
        shopify_orders.append({
            "order_number": order.get("orderNumber"),
            "total": float(order.get("totalPrice", {}).get("amount", 0)),
            "currency": order.get("totalPrice", {}).get("currencyCode", "RON"),
            "date": order.get("processedAt"),
            "status": order.get("fulfillmentStatus") or "UNFULFILLED"
        })

    # Re-fetch rather than hand-assembling the response from user_update_data -
    # that dict never carried the split address/company fields, and this way
    # the response can't drift from _serialize_user's shape (see its docstring).
    migrated_user = await db.users.find_one({"id": user_id})
    return {
        "token": local_token,
        "user": _serialize_user(migrated_user),
        "shopify_orders": shopify_orders
    }


def _serialize_user(user: dict) -> dict:
    """Public-facing shape of a user doc - shared by /auth/me, /auth/login and
    PUT /auth/me so the three response bodies can't silently drift apart
    (a field present on one and missing on another has bitten us before -
    see the earlier missing-`role` bug)."""
    return {
        "id": user["id"],
        "email": user["email"],
        "name": user.get("name"),
        "phone": user.get("phone"),
        "address": user.get("address"),
        "address_strada": user.get("address_strada"),
        "address_numar": user.get("address_numar"),
        "address_bloc": user.get("address_bloc"),
        "address_scara": user.get("address_scara"),
        "address_ap": user.get("address_ap"),
        "city": user.get("city"),
        "county": user.get("county"),
        "postal_code": user.get("postal_code"),
        "is_company": user.get("is_company", False),
        "company_name": user.get("company_name"),
        "cui": user.get("cui"),
        "reg_com": user.get("reg_com"),
        "administrator": user.get("administrator"),
        "company_address": user.get("company_address"),
        "company_address_strada": user.get("company_address_strada"),
        "company_address_numar": user.get("company_address_numar"),
        "company_address_bloc": user.get("company_address_bloc"),
        "company_address_scara": user.get("company_address_scara"),
        "company_address_ap": user.get("company_address_ap"),
        "company_address_oras": user.get("company_address_oras"),
        "company_address_judet": user.get("company_address_judet"),
        "company_address_cod_postal": user.get("company_address_cod_postal"),
        "is_shopify_customer": user.get("is_shopify_customer", False),
        "created_at": user["created_at"],
        "role": user.get("role", "customer"),
        # Defaults True for accounts predating this field, matching the
        # unconditional-send behavior send_blog_notification_to_matching_users
        # had before this opt-out existed - adding the field never silently
        # unsubscribes anyone already receiving these emails.
        "notify_news_email": user.get("notify_news_email", True),
        # GDPR consent - absent/None for accounts created before this field
        # existed (never backfilled), populated for accounts registered
        # from now on. See CURRENT_TERMS_VERSION near UserRegister.
        "consent_accepted_at": user.get("consent_accepted_at"),
        "consent_terms_version": user.get("consent_terms_version"),
    }


async def _authenticate_user(email: str, password: str, request: Request) -> dict:
    """Authenticate a customer for login. Checks the local password hash
    first; only falls back to Shopify (and silently migrates) for accounts
    that don't have one yet. See `_legacy_shopify_login_and_migrate`.

    Shared by both /auth/login and /auth/shopify-login (the latter is kept
    only for backward compatibility with older app builds but is exactly
    as brute-forceable, so rate limiting lives here once rather than being
    duplicated - and possibly forgotten - on each caller).
    """
    email = email.lower().strip()
    ip = _client_ip(request)
    # Coarser, IP-only bucket first: every login attempt from this IP counts
    # against it regardless of which email was tried, so rotating through
    # many emails from one IP can't be used to dodge the tighter per-email
    # bucket below entirely.
    _enforce_rate_limit(
        f"login:ip:{ip}", LOGIN_IP_LIMIT, LOGIN_IP_WINDOW_SECONDS,
        "Prea multe încercări de autentificare de la această adresă. Încearcă din nou mai târziu.",
    )
    _enforce_rate_limit(
        f"login:ip-email:{ip}:{email}", LOGIN_IP_EMAIL_LIMIT, LOGIN_IP_EMAIL_WINDOW_SECONDS,
        "Prea multe încercări de autentificare pentru acest cont. Încearcă din nou mai târziu.",
    )
    existing_user = await db.users.find_one({"email": email})

    # Deleted accounts (see POST /auth/me/delete) are rejected outright,
    # before even checking the password - checked BEFORE the password_hash
    # branch below so this always produces the specific message rather than
    # ever falling through to the generic "Email sau parolă incorectă". In
    # practice deletion also anonymizes `email`, so this only matches if
    # someone is somehow still looking the account up by its current
    # (post-deletion) email - belt-and-suspenders alongside the unusable
    # password hash that deletion also sets (see that endpoint's docstring
    # for why both exist).
    if existing_user and existing_user.get("is_deleted"):
        raise HTTPException(
            status_code=403,
            detail="Acest cont a fost șters și nu se mai poate autentifica. Creează un cont nou dacă dorești să continui.",
        )

    if existing_user and existing_user.get("password_hash"):
        if not await verify_password(password, existing_user["password_hash"]):
            raise HTTPException(status_code=401, detail="Email sau parolă incorectă")

        local_token = await _issue_session_token(email)

        return {
            "token": local_token,
            "user": _serialize_user(existing_user),
            # Full order history stays available via GET /auth/orders (local)
            # and GET /auth/shopify-orders (Shopify-linked accounts only).
            "shopify_orders": [],
        }

    return await _legacy_shopify_login_and_migrate(email, password, existing_user)


@api_router.post("/auth/login")
async def login_user(credentials: UserLogin, request: Request):
    """Login a user - local password first, Shopify fallback for legacy accounts"""
    return await _authenticate_user(credentials.email, credentials.password, request)

@api_router.get("/auth/me")
async def get_current_user(request: Request):
    """Get current user by token"""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Token lipsă sau invalid")
    
    token = auth_header.replace("Bearer ", "")
    user = await _find_user_by_token(token)
    
    if not user:
        raise HTTPException(status_code=401, detail="Token invalid sau expirat")

    return _serialize_user(user)


@api_router.get("/auth/me/export")
async def export_current_user_data(request: Request):
    """GDPR Art. 15/20 data export: everything this account holds, as a
    single downloadable JSON file. Deliberately reuses the exact same auth
    pattern and underlying queries as GET /auth/me, GET /auth/orders and
    GET /auth/equipment (and the query pattern of GET /admin/customer-
    interests, scoped to this user) so this can't silently drift from what
    those endpoints already show - see _serialize_user's docstring for why
    that matters here."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Token lipsă sau invalid")

    token = auth_header.replace("Bearer ", "")
    user = await _find_user_by_token(token)

    if not user:
        raise HTTPException(status_code=401, detail="Token invalid sau expirat")

    # comenzi - identical query to GET /auth/orders, full order objects
    # (not a summary), so nothing about a past order is left out.
    orders = await db.orders.find({"customer.email": user["email"]}).sort("created_at", -1).to_list(100)
    comenzi = [Order(**order) for order in orders]

    # utilaje - same shape/cleanup as GET /auth/equipment (see
    # _clean_equipment_list).
    utilaje = _clean_equipment_list(user.get("equipment", []))

    # favorite - same collection/idea as GET /admin/customer-interests,
    # scoped to this user only, with the same product-enrichment fields.
    interests = await db.customer_interests.find({"user_id": user["id"]}).sort("created_at", -1).to_list(1000)
    product_ids = list({i["product_id"] for i in interests})
    products_by_id = {}
    if product_ids:
        async for p in db.shopify_products.find({"id": {"$in": product_ids}}):
            products_by_id[p["id"]] = p
    favorite = []
    for i in interests:
        product = products_by_id.get(i["product_id"])
        favorite.append({
            "id": i["id"],
            "product_id": i["product_id"],
            "type": i["type"],
            "created_at": i["created_at"],
            "product_title": product.get("title") if product else None,
            "product_price": product.get("price") if product else None,
            "product_currency": product.get("currency") if product else None,
            "product_image_url": product.get("image_url") if product else None,
            "product_stock_status": product.get("stock_status") if product else None,
        })

    export_payload = {
        "profil": _serialize_user(user),
        "comenzi": comenzi,
        "utilaje": utilaje,
        "favorite": favorite,
        "exportat_la": datetime.utcnow(),
    }

    filename = f"date-personale-agb-{datetime.utcnow().strftime('%Y-%m-%d')}.json"
    body = json.dumps(jsonable_encoder(export_payload), ensure_ascii=False, indent=2)
    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

# ==================== SHOPIFY CUSTOMER AUTH ====================

@api_router.post("/auth/shopify-login")
async def shopify_customer_login(credentials: ShopifyCustomerLogin, request: Request):
    """Kept for backward compatibility with older app builds that call this
    route specifically; behaves identically to /auth/login now (including
    the same login rate limiting - see _authenticate_user)."""
    return await _authenticate_user(credentials.email, credentials.password, request)

def _build_user_profile_update_dict(user: dict, update_data: UserUpdate) -> dict:
    """Build the Mongo $set dict for a partial user-profile update: only
    fields actually present (non-None) in `update_data`, plus recomputing
    the derived address/company_address combined strings whenever their
    split fields are among the changed ones.

    Shared by PUT /auth/me (self-service, the customer editing their own
    account) and PATCH /admin/customer-account/{email} (staff correcting a
    customer's standing account, e.g. after a checkout typo) so this
    ~50-line combination logic lives in exactly one place instead of
    drifting between the two call sites.

    `user` must be the CURRENT (pre-update) user doc - needed to fall back
    to the existing value of any split field that ISN'T itself part of this
    particular update, when recomputing the combined address/company_address
    string. address/company_address stay in sync as a derived combo whenever
    the split fields are sent - a handful of older call sites (checkout
    prefill, admin order display, the CRM order sync payload) still read the
    single combined field, so it can't just go stale the moment someone
    starts using the new split fields instead."""
    update_dict = {}
    for field, value in update_data.dict().items():
        if value is not None:
            update_dict[field] = value

    def _combine_address(strada: str, numar: str, bloc: str, scara: str, ap: str) -> str:
        parts = [" ".join(filter(None, [strada, numar])).strip()]
        if bloc:
            parts.append(f"Bl. {bloc}")
        if scara:
            parts.append(f"Sc. {scara}")
        if ap:
            parts.append(f"Ap. {ap}")
        return ", ".join(p for p in parts if p)

    address_fields = ("address_strada", "address_numar", "address_bloc", "address_scara", "address_ap")
    if any(k in update_dict for k in address_fields):
        update_dict["address"] = _combine_address(
            update_dict.get("address_strada", user.get("address_strada")) or "",
            update_dict.get("address_numar", user.get("address_numar")) or "",
            update_dict.get("address_bloc", user.get("address_bloc")) or "",
            update_dict.get("address_scara", user.get("address_scara")) or "",
            update_dict.get("address_ap", user.get("address_ap")) or "",
        )
    company_address_fields = (
        "company_address_strada", "company_address_numar",
        "company_address_bloc", "company_address_scara", "company_address_ap",
    )
    if any(k in update_dict for k in company_address_fields):
        update_dict["company_address"] = _combine_address(
            update_dict.get("company_address_strada", user.get("company_address_strada")) or "",
            update_dict.get("company_address_numar", user.get("company_address_numar")) or "",
            update_dict.get("company_address_bloc", user.get("company_address_bloc")) or "",
            update_dict.get("company_address_scara", user.get("company_address_scara")) or "",
            update_dict.get("company_address_ap", user.get("company_address_ap")) or "",
        )

    return update_dict


@api_router.put("/auth/me")
async def update_current_user(request: Request, update_data: UserUpdate, background_tasks: BackgroundTasks):
    """Update current user profile"""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Token lipsă sau invalid")

    token = auth_header.replace("Bearer ", "")
    user = await _find_user_by_token(token)

    if not user:
        raise HTTPException(status_code=401, detail="Token invalid sau expirat")

    update_dict = _build_user_profile_update_dict(user, update_data)

    if update_dict:
        await db.users.update_one(
            {"id": user["id"]},
            {"$set": update_dict}
        )

    # Fetch updated user
    updated_user = await db.users.find_one({"id": user["id"]})

    # Sync to CRM (fire-and-forget, never blocks/fails the response above) -
    # uses the freshly-fetched post-update document so the new address/company
    # data actually reaches CRM instead of the stale pre-update one.
    background_tasks.add_task(sync_account_to_crm, updated_user)

    return _serialize_user(updated_user)


class AccountDeleteRequest(BaseModel):
    password: str


@api_router.post("/auth/me/delete")
async def delete_current_user_account(request: Request, delete_data: AccountDeleteRequest):
    """GDPR 'right to be forgotten' account deletion - anonymizes and
    deactivates the LOGIN ACCOUNT (db.users) ONLY.

    Scope is deliberate: Romanian fiscal law requires retaining invoice/
    order records for ~10 years, and GDPR Art. 17(3)(b) explicitly permits
    NOT erasing data still needed for legal/fiscal compliance. Every order
    already snapshots its own customer/company/invoice data onto itself
    independently of the live account (see CustomerInfo's company_*
    fields) specifically so historical orders stay accurate even after the
    account that placed them is deleted. So this handler must NEVER touch
    db.orders, and must NEVER trigger a CRM sync/notification of any kind -
    if a future change "fixes" that, it would violate the fiscal retention
    requirement above. Chosen POST (not DELETE-with-body) to match this
    repo's existing convention for other sensitive/destructive auth actions
    that take a JSON body (POST /auth/reset-password, /auth/logout-all).

    Requires re-entering the CURRENT password (re-authentication, not just
    an already-valid session token) since this is irreversible-in-practice.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Token lipsă sau invalid")

    token = auth_header.replace("Bearer ", "")
    user = await _find_user_by_token(token)

    if not user:
        raise HTTPException(status_code=401, detail="Token invalid sau expirat")

    _enforce_rate_limit(
        f"account-delete:{user['id']}", ACCOUNT_DELETE_LIMIT, ACCOUNT_DELETE_WINDOW_SECONDS,
        "Prea multe încercări. Încearcă din nou mai târziu.",
    )

    if not await verify_password(delete_data.password, user.get("password_hash") or ""):
        raise HTTPException(status_code=403, detail="Parolă incorectă. Contul nu a fost șters.")

    # Guaranteed-unique placeholder - db.users has a unique index on
    # `email` (see startup_event), so this can never collide with it or
    # with another deleted account's placeholder. Also frees up the
    # original email for a brand new registration, which is the expected
    # behaviour of "delete my account".
    anonymized_email = f"deleted-{uuid.uuid4()}@deleted.local"
    # Fresh random (never-typed-by-anyone) password, hashed the same way a
    # real password would be - makes the stored hash unusable even if
    # someone somehow learned the new anonymized email. Belt-and-suspenders
    # with the explicit is_deleted flag checked in _authenticate_user below
    # (no prior "deactivated account" pattern existed in this codebase to
    # match instead, per George's instructions - so both guards are used).
    unusable_password_hash = await hash_password(secrets.token_urlsafe(32))

    await db.users.update_one(
        {"id": user["id"]},
        {
            "$set": {
                "name": "Cont șters",
                "email": anonymized_email,
                "phone": None,
                "address": None,
                "address_strada": None,
                "address_numar": None,
                "address_bloc": None,
                "address_scara": None,
                "address_ap": None,
                "city": None,
                "county": None,
                "postal_code": None,
                "company_name": None,
                "cui": None,
                "reg_com": None,
                "administrator": None,
                "company_address": None,
                "company_address_strada": None,
                "company_address_numar": None,
                "company_address_bloc": None,
                "company_address_scara": None,
                "company_address_ap": None,
                "company_address_oras": None,
                "company_address_judet": None,
                "company_address_cod_postal": None,
                "password_hash": unusable_password_hash,
                # All sessions revoked - an already-logged-in device can't
                # keep using this account after deletion either.
                "tokens": [],
                "is_deleted": True,
                "deleted_at": datetime.utcnow(),
                # Also revoke the Shopify customer access token as a login
                # credential: _find_user_by_token(allow_shopify_access_token=True)
                # would otherwise keep matching this "deleted" account on it
                # (that helper now also checks is_deleted directly, but this
                # closes the same hole at the source instead of relying on
                # a single guard). is_shopify_customer/shopify_customer_id
                # are dropped too - no reason to keep advertising this
                # (now-anonymized) account as Shopify-linked.
                "shopify_access_token": None,
                "shopify_customer_id": None,
                "is_shopify_customer": False,
                # Equipment (machine serial/model records) has no
                # equivalent fiscal-retention basis to db.orders - unlike
                # orders, nothing legally requires AGB to keep a deleted
                # customer's self-reported equipment list. Cleared here so
                # "right to be forgotten" actually forgets it, not just the
                # profile fields above.
                "equipment": [],
                # consent_accepted_at/consent_terms_version intentionally
                # left untouched - harmless historical record of when this
                # (now-anonymized) account originally consented.
            },
            "$unset": {"token": ""},
        },
    )

    return {"message": "Contul a fost șters cu succes"}


@api_router.post("/auth/logout")
async def logout_user(request: Request):
    """Logout the current device only: free up this token's slot in the
    `tokens` array so the user can log back in elsewhere without hitting the
    device cap, without touching that user's other active sessions.

    `tokens[]` now holds a mix of legacy bare-string entries and new
    object entries (see _new_session_token_doc/_find_user_by_token), and a
    single `$pull` condition can't match both a scalar-equality entry and
    an embedded-document field match at the same time - so this runs two
    separate $pull operations, one per shape. Both always run rather than
    looking up the token's shape first (cheap, and idempotent - whichever
    one doesn't match this token is just a no-op)."""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header.replace("Bearer ", "")
        # Legacy bare-string entries.
        await db.users.update_one(
            {"tokens": token},
            {"$pull": {"tokens": token}}
        )
        # New-format object entries.
        await db.users.update_one(
            {"tokens": {"$elemMatch": {"token": token}}},
            {"$pull": {"tokens": {"token": token}}}
        )
        # Legacy safety net: also clear it if this account still had it
        # stored as a single un-migrated `token` field (see
        # _find_user_by_token / the startup migration).
        await db.users.update_one(
            {"token": token},
            {"$unset": {"token": ""}}
        )
    return {"message": "Deconectat cu succes"}


@api_router.post("/auth/logout-all")
async def logout_all_devices(request: Request):
    """Logout every device/session for the current account at once (full
    session revocation) - clears `tokens[]` entirely, same as what
    /auth/reset-password already does as a side effect of changing the
    password. Requires a currently-valid token to call, same as any other
    authenticated endpoint."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Token lipsă sau invalid")

    token = auth_header.replace("Bearer ", "")
    user = await _find_user_by_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Token invalid sau expirat")

    await db.users.update_one(
        {"id": user["id"]},
        {"$set": {"tokens": []}, "$unset": {"token": ""}},
    )
    return {"message": "Toate sesiunile au fost deconectate cu succes"}

@api_router.get("/auth/orders")
async def get_user_orders(request: Request):
    """Get orders for authenticated user"""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Token lipsă sau invalid")
    
    token = auth_header.replace("Bearer ", "")
    user = await _find_user_by_token(token)
    
    if not user:
        raise HTTPException(status_code=401, detail="Token invalid sau expirat")
    
    # Get orders by user email
    orders = await db.orders.find({"customer.email": user["email"]}).sort("created_at", -1).to_list(100)
    return [Order(**order) for order in orders]

@api_router.get("/auth/order-history")
async def get_user_order_history(request: Request):
    """Merged order history for the logged-in customer: native webshop
    orders (already available via GET /auth/orders above) plus their
    historical Shopify orders (matched by email, from the full-store import
    - see _run_shopify_full_orders_import), so "Comenzile mele" isn't
    missing everything ordered before the new checkout existed."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Token lipsă sau invalid")
    token = auth_header.replace("Bearer ", "")
    user = await _find_user_by_token(token, allow_shopify_access_token=True)
    if not user:
        raise HTTPException(status_code=401, detail="Token invalid sau expirat")

    email = (user.get("email") or "").strip().lower()

    native_orders = await db.orders.find({"customer.email": user["email"]}).to_list(200)
    shopify_orders = []
    clients_by_id = {}
    if email:
        # Match by customer_email directly (full-store import) OR via a
        # resolved client_id (older customer-scoped import may only have
        # client_id set, not customer_email, on some records - see
        # _shopify_order_to_merged's docstring).
        or_conditions = [{"customer_email": {"$regex": f"^{re.escape(email)}$", "$options": "i"}}]
        client = await db.clients.find_one({"email_normalized": email})
        if client:
            or_conditions.append({"client_id": client["id"]})
            clients_by_id[client["id"]] = client
        shopify_orders = await db.shopify_order_history.find({"$or": or_conditions}).to_list(200)

    merged = [_native_order_to_merged(o) for o in native_orders] + [
        _shopify_order_to_merged(o, clients_by_id) for o in shopify_orders
    ]
    merged.sort(key=_merged_order_sort_key, reverse=True)
    return merged

@api_router.get("/auth/shopify-orders")
async def get_user_shopify_orders(request: Request):
    """Get orders from Shopify for authenticated user - includes fulfillment status"""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Token lipsă sau invalid")
    
    token = auth_header.replace("Bearer ", "")
    user = await _find_user_by_token(token)
    
    if not user:
        raise HTTPException(status_code=401, detail="Token invalid sau expirat")
    
    # Check if user has Shopify access token
    shopify_access_token = user.get("shopify_access_token")
    
    if not shopify_access_token:
        # Return mobile orders from our database if no Shopify connection
        mobile_orders = await db.mobile_orders.find(
            {"customer_email": user["email"]}
        ).sort("created_at", -1).to_list(50)
        
        return [{
            "id": str(order.get("shopify_order_id", order.get("_id"))),
            "order_number": order.get("shopify_order_number", "N/A"),
            "order_name": order.get("shopify_order_name", f"#{order.get('shopify_order_number', 'N/A')}"),
            "total_price": order.get("total_price", "0.00"),
            "currency": order.get("currency", "RON"),
            "created_at": order.get("created_at").isoformat() if order.get("created_at") else None,
            "fulfillment_status": "UNFULFILLED",
            "status_display": "În așteptare",
            "items_count": order.get("items_count", 0),
            "payment_method": order.get("payment_method", "N/A")
        } for order in mobile_orders]
    
    # Query Shopify for orders
    headers = {
        "Content-Type": "application/json",
        "X-Shopify-Storefront-Access-Token": SHOPIFY_STOREFRONT_TOKEN
    }
    
    orders_query = """
    query getCustomerOrders($customerAccessToken: String!) {
        customer(customerAccessToken: $customerAccessToken) {
            orders(first: 50, sortKey: PROCESSED_AT, reverse: true) {
                edges {
                    node {
                        id
                        orderNumber
                        name
                        totalPrice {
                            amount
                            currencyCode
                        }
                        processedAt
                        fulfillmentStatus
                        financialStatus
                        lineItems(first: 10) {
                            edges {
                                node {
                                    title
                                    quantity
                                }
                            }
                        }
                    }
                }
            }
        }
    }
    """
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"https://{SHOPIFY_STORE}/api/{SHOPIFY_API_VERSION}/graphql.json",
                json={
                    "query": orders_query,
                    "variables": {"customerAccessToken": shopify_access_token}
                },
                headers=headers,
                timeout=30.0
            )
            
            data = response.json()
            customer = data.get("data", {}).get("customer")
            
            if not customer:
                # Token might be expired, return mobile orders instead
                mobile_orders = await db.mobile_orders.find(
                    {"customer_email": user["email"]}
                ).sort("created_at", -1).to_list(50)
                
                return [{
                    "id": str(order.get("shopify_order_id", order.get("_id"))),
                    "order_number": order.get("shopify_order_number", "N/A"),
                    "order_name": order.get("shopify_order_name", f"#{order.get('shopify_order_number', 'N/A')}"),
                    "total_price": order.get("total_price", "0.00"),
                    "currency": order.get("currency", "RON"),
                    "created_at": order.get("created_at").isoformat() if order.get("created_at") else None,
                    "fulfillment_status": "UNFULFILLED",
                    "status_display": "În așteptare",
                    "items_count": order.get("items_count", 0),
                    "payment_method": order.get("payment_method", "N/A")
                } for order in mobile_orders]
            
            orders_edges = customer.get("orders", {}).get("edges", [])
            
            # Map fulfillment status to Romanian display text
            status_map = {
                "FULFILLED": "Trimisă",
                "PARTIALLY_FULFILLED": "Parțial trimisă",
                "UNFULFILLED": "În așteptare",
                "ON_HOLD": "În așteptare",
                "SCHEDULED": "Programată",
                "PENDING_FULFILLMENT": "În procesare",
                None: "În așteptare"
            }
            
            shopify_orders = []
            for edge in orders_edges:
                order = edge.get("node", {})
                fulfillment_status = order.get("fulfillmentStatus")
                line_items = order.get("lineItems", {}).get("edges", [])
                
                shopify_orders.append({
                    "id": order.get("id", "").replace("gid://shopify/Order/", ""),
                    "order_number": order.get("orderNumber"),
                    "order_name": order.get("name"),
                    "total_price": order.get("totalPrice", {}).get("amount", "0.00"),
                    "currency": order.get("totalPrice", {}).get("currencyCode", "RON"),
                    "created_at": order.get("processedAt"),
                    "fulfillment_status": fulfillment_status or "UNFULFILLED",
                    "status_display": status_map.get(fulfillment_status, "În așteptare"),
                    "financial_status": order.get("financialStatus"),
                    "items_count": len(line_items),
                    "items": [
                        {
                            "title": item.get("node", {}).get("title"),
                            "quantity": item.get("node", {}).get("quantity")
                        } for item in line_items
                    ]
                })
            
            return shopify_orders
            
    except Exception as e:
        logger.error(f"Error fetching Shopify orders: {e}")
        # Fallback to mobile orders
        mobile_orders = await db.mobile_orders.find(
            {"customer_email": user["email"]}
        ).sort("created_at", -1).to_list(50)
        
        return [{
            "id": str(order.get("shopify_order_id", order.get("_id"))),
            "order_number": order.get("shopify_order_number", "N/A"),
            "order_name": order.get("shopify_order_name", f"#{order.get('shopify_order_number', 'N/A')}"),
            "total_price": order.get("total_price", "0.00"),
            "currency": order.get("currency", "RON"),
            "created_at": order.get("created_at").isoformat() if order.get("created_at") else None,
            "fulfillment_status": "UNFULFILLED",
            "status_display": "În așteptare",
            "items_count": order.get("items_count", 0),
            "payment_method": order.get("payment_method", "N/A")
        } for order in mobile_orders]

# ==================== ADMIN ENDPOINTS ====================

# `product.description` is stored here as the source of truth and later
# rendered by the storefront (agb-webshop) via dangerouslySetInnerHTML - so
# it must be sanitized server-side, at write time, not just trusted client
# input. A strict allowlist (bleach, not hand-rolled regex - HTML
# sanitization is notoriously easy to get subtly wrong that way) keeps only
# basic formatting tags; script/style/iframe/on*-event-handler attributes/
# javascript: URIs etc. are all stripped.
_DESCRIPTION_ALLOWED_TAGS = ["p", "br", "b", "strong", "i", "em", "ul", "ol", "li", "a"]
_DESCRIPTION_ALLOWED_ATTRS = {"a": ["href"]}
_DESCRIPTION_ALLOWED_PROTOCOLS = ["http", "https", "mailto"]

def sanitize_description_html(raw: Optional[str]) -> str:
    """Strips anything outside the basic-formatting allowlist from a
    product description before it's ever written to the database. Called
    from every code path that writes `description` (admin_create_product,
    _apply_product_update - shared by the single-product PUT and all bulk
    update/save endpoints)."""
    if not raw:
        return raw or ""
    return bleach.clean(
        raw,
        tags=_DESCRIPTION_ALLOWED_TAGS,
        attributes=_DESCRIPTION_ALLOWED_ATTRS,
        protocols=_DESCRIPTION_ALLOWED_PROTOCOLS,
        strip=True,
    )

class ProductCreate(BaseModel):
    title: str
    description: str = ""
    technical_specs: Optional[str] = None
    meta_title: Optional[str] = None
    meta_description: Optional[str] = None
    price: float
    currency: str = "RON"
    image_url: Optional[str] = None
    images: List[str] = []
    tags: List[str] = []
    product_type: Optional[str] = None
    vendor: Optional[str] = None
    stock: int = 0
    stock_status: Optional[str] = None
    sku: Optional[str] = None
    compatible_models: List[str] = []
    category: Optional[str] = None  # e.g. "motor", "transmisie" - combined with product_type to derive `collections`
    complementary_product_ids: List[str] = []
    equivalent_product_ids: List[str] = []
    is_featured: bool = False
    equipment_year: Optional[int] = None
    equipment_hours: Optional[int] = None
    equipment_power_hp: Optional[int] = None
    equipment_transmission: Optional[str] = None
    equipment_front_tire: Optional[str] = None
    equipment_front_tire_wear: Optional[str] = None
    equipment_rear_tire: Optional[str] = None
    equipment_rear_tire_wear: Optional[str] = None
    equipment_max_speed: Optional[int] = None

class ProductUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    technical_specs: Optional[str] = None
    meta_title: Optional[str] = None
    meta_description: Optional[str] = None
    price: Optional[float] = None
    currency: Optional[str] = None
    image_url: Optional[str] = None
    images: Optional[List[str]] = None
    tags: Optional[List[str]] = None
    product_type: Optional[str] = None
    vendor: Optional[str] = None
    stock: Optional[int] = None
    stock_status: Optional[str] = None
    sku: Optional[str] = None
    compatible_models: Optional[List[str]] = None
    category: Optional[str] = None
    complementary_product_ids: Optional[List[str]] = None
    equivalent_product_ids: Optional[List[str]] = None
    is_featured: Optional[bool] = None
    equipment_year: Optional[int] = None
    equipment_hours: Optional[int] = None
    equipment_power_hp: Optional[int] = None
    equipment_transmission: Optional[str] = None
    equipment_front_tire: Optional[str] = None
    equipment_front_tire_wear: Optional[str] = None
    equipment_rear_tire: Optional[str] = None
    equipment_rear_tire_wear: Optional[str] = None
    equipment_max_speed: Optional[int] = None

class ProductBulkUpdate(BaseModel):
    ids: List[str]
    patch: ProductUpdate

class ProductBulkComplementaryAdd(BaseModel):
    ids: List[str]
    add: List[str]

class ProductBulkEquivalentAdd(BaseModel):
    ids: List[str]
    add: List[str]

class ProductBulkSaveItem(BaseModel):
    id: str
    patch: ProductUpdate

class ProductBulkSave(BaseModel):
    updates: List[ProductBulkSaveItem]

class ProductEquivalentsPreviewRequest(BaseModel):
    product_ids: List[str]

class ProductEquivalentLinkItem(BaseModel):
    product_id: str
    equivalent_ids: List[str]

class ProductEquivalentsApplyRequest(BaseModel):
    links: List[ProductEquivalentLinkItem]

def slugify(text: str) -> str:
    slug = normalize_text(text)
    slug = re.sub(r'[^a-z0-9]+', '-', slug).strip('-')
    return slug or str(uuid.uuid4())[:8]

NEW_COLLECTION = "PIESE NOI"
DEZMEMBRARE_COLLECTION = "PIESE DIN DEZMEMBRARE"
NEW_CATEGORY_PREFIX = "Piese noi "
DEZMEMBRARE_CATEGORY_PREFIX = "Piese din dezmembrare "

def build_collections(product_type: Optional[str], category: Optional[str], existing: List[str]) -> List[str]:
    """Derives the `collections` array (used for storefront category
    browsing) from the admin's Tip ("Nou"/"Dezmembrari") + Categorie
    ("motor", "transmisie"...) picks, matching the naming convention the
    Shopify sync already uses ("PIESE NOI" + "Piese noi motor"). Anything
    else already on the product (e.g. "HOME", "Recommended products
    (Seguno)") is preserved as-is rather than wiped."""
    preserved = [
        c for c in existing
        if c not in (NEW_COLLECTION, DEZMEMBRARE_COLLECTION)
        and not c.startswith(NEW_CATEGORY_PREFIX)
        and not c.startswith(DEZMEMBRARE_CATEGORY_PREFIX)
    ]

    derived = []
    if product_type == "Nou":
        derived.append(NEW_COLLECTION)
        if category:
            derived.append(f"{NEW_CATEGORY_PREFIX}{category}")
    elif product_type == "Dezmembrari":
        derived.append(DEZMEMBRARE_COLLECTION)
        if category:
            derived.append(f"{DEZMEMBRARE_CATEGORY_PREFIX}{category}")

    return preserved + derived

# ==================== OEM/part code extraction (equivalent matching) ====================
# Backs "Adaugă echivalente" (POST /admin/products/equivalents/preview +
# /apply below) - there's no structured OEM-code field on Product, the code
# is embedded as free text inside `title` (e.g. "Carcasa Termostate John
# Deere R522776"). This is a best-effort heuristic only: it feeds a
# human-reviewed preview step, never a direct write, so false positives/
# negatives here are not destructive - they just mean a proposed match is
# missing, or a bit noisy for staff to eyeball out.

# Chars stripped from a token's boundaries before testing it as a code -
# purely stray punctuation that can end up attached to a word by title
# formatting (parens, quotes, list punctuation), NOT '.' or '/' since those
# can be part of a real code itself (e.g. "644724.1", "72/384-21") and
# stripping them here would corrupt the code.
_OEM_CODE_BOUNDARY_STRIP = '()[]{}"\';:'

# A "code-like" token: optionally up to 4 leading letters, then at least one
# digit, then 2+ more letters/digits/./-  characters. Deliberately permissive
# (also matches spec-like tokens such as "25Cm3" or short values) - the
# preview/confirm step is the real safety net, not this regex; the only
# extra guard here is the minimum overall length below, which filters out
# the shortest/noisiest false positives (e.g. "10A") without needing to
# understand which tokens are units vs. codes.
_OEM_CODE_TOKEN_RE = re.compile(r'^[A-Za-z]{0,4}\d[A-Za-z0-9./-]{2,}$')
_OEM_CODE_MIN_LEN = 4

def _clean_oem_token(token: str) -> str:
    return token.strip(_OEM_CODE_BOUNDARY_STRIP)

def _is_oem_code_token(token: str) -> bool:
    token = _clean_oem_token(token)
    if len(token) < _OEM_CODE_MIN_LEN:
        return False
    return bool(_OEM_CODE_TOKEN_RE.match(token))

def _tokenize_title_for_oem_code(title: str) -> List[str]:
    # Split on whitespace AND commas - some titles list cross-reference
    # codes for multiple brands comma-separated rather than just
    # space-separated (e.g. "... AL79782, AL175855, AL175835, AL201127").
    return [t for t in re.split(r'[\s,]+', (title or '').strip()) if t]

def extract_oem_code(title: str) -> Optional[str]:
    """Best-effort extraction of the PRIMARY OEM/manufacturer part code
    embedded in a product title. Handles the patterns seen across the
    catalog:
      - trailing single code: "Carcasa Termostate John Deere R522776"
        -> "R522776"
      - trailing cross-reference LIST of codes for different brands of the
        same part, space- or comma-separated: "... AL79782, AL175855,
        AL175835, AL201127" -> "AL79782" (the FIRST/leftmost of the trailing
        block is the primary code, the rest are alternate manufacturer
        codes for that same part - not returned here, but they'll surface
        naturally as their own products' primary codes when THEY are looked
        up, so no information is lost by only returning one).
      - leading code instead of trailing: "R169152 Inel etanșare
        transmisie" -> "R169152", "L111005 Garnitura" -> "L111005".
      - codes containing '.' or '/' punctuation: "644724.1", "72/384-21".

    Trailing is checked before leading (the dominant pattern in the
    catalog); if a trailing code-like run exists, a leading one is never
    considered even if it's also present. Returns None if neither end of
    the title has a code-like token.
    """
    tokens = _tokenize_title_for_oem_code(title)
    if not tokens:
        return None

    trailing_run: List[str] = []
    for tok in reversed(tokens):
        if _is_oem_code_token(tok):
            trailing_run.append(_clean_oem_token(tok))
        else:
            break
    if trailing_run:
        trailing_run.reverse()
        return trailing_run[0]

    leading_run: List[str] = []
    for tok in tokens:
        if _is_oem_code_token(tok):
            leading_run.append(_clean_oem_token(tok))
        else:
            break
    if leading_run:
        return leading_run[0]

    return None

# ==================== BFF ADMIN JWT (CRM-signed, verify-only) ====================
# CRM signs short-lived (5-15 min) Ed25519 JWTs for a staff member's admin
# session and this backend verifies them as the ONLY accepted credential
# for /admin/* (and the other _require_admin-gated routes) - see
# _require_admin below. The legacy native webshop admin session token
# fallback has been retired; a native token is never sufficient here
# anymore, regardless of the account's role. This backend never signs/
# mints these tokens itself.

BFF_JWT_AUDIENCE = "agb-backend-admin"
BFF_JWT_ISSUER = "agb-crm-bff"
_JWT_SEGMENT_RE = re.compile(r'^[A-Za-z0-9_-]+$')


def _looks_like_jwt(token: str) -> bool:
    """Cheap structural check (3 non-empty base64url segments separated by
    '.') used to route a bearer token to the BFF-JWT verification path
    instead of the native-token lookup, without waiting for jwt.decode to
    fail first. Native webshop session tokens are opaque uuid4-derived hex
    strings (see _new_session_token_doc) and never contain a '.', so they
    always fail this check and fall through unchanged."""
    parts = token.split(".")
    return len(parts) == 3 and all(p and _JWT_SEGMENT_RE.match(p) for p in parts)


async def _is_bff_session_revoked(staff_user_id: str, issued_at: datetime) -> bool:
    """True if CRM revoked this staff user's BFF admin sessions at/after the
    moment this particular token was issued (see POST
    /api/internal/revoke-bff-admin below, which inserts the record this
    checks against). A revocation only invalidates tokens issued at or
    before the revocation instant - a *new* token CRM mints for the same
    staff user afterwards is unaffected."""
    doc = await db.bff_revoked_sessions.find_one({
        "staff_user_id": staff_user_id,
        "revoked_at": {"$gte": issued_at},
    })
    return doc is not None


async def _verify_bff_jwt(token: str) -> dict:
    """Verify a CRM-issued BFF admin JWT (Ed25519 / EdDSA) and return it in
    the same shape the native-token path in _require_admin returns
    ({"id", "email", "role": "admin"}), plus "jti"/"auth_source" so existing
    code that only ever read admin["id"]/admin.get("email") (e.g. the audit
    log) keeps working unmodified no matter which auth path was used.

    Every failure mode (missing public key, bad signature, expired, wrong
    audience/issuer, missing/malformed claims, revoked) raises the same
    generic HTTPException(401) with no internal detail exposed - callers
    (i.e. _require_admin) can treat this function as all-or-nothing."""
    if not CRM_BFF_JWT_PUBLIC_KEY:
        # Mechanism not provisioned in this environment. _require_admin
        # already guards the call site on this same env var so this branch
        # shouldn't normally be reached, but fail closed here too rather
        # than ever risk treating an unverifiable token as valid.
        raise HTTPException(status_code=401, detail="Token invalid sau expirat")

    try:
        payload = jwt.decode(
            token,
            CRM_BFF_JWT_PUBLIC_KEY,
            algorithms=["EdDSA"],
            audience=BFF_JWT_AUDIENCE,
            issuer=BFF_JWT_ISSUER,
            # exp/iat aren't required by PyJWT's own defaults (a token
            # simply omitting "exp" would otherwise be accepted with no
            # expiry check at all) - explicitly require both: exp because
            # every BFF token is supposed to be short-lived, and iat
            # because _is_bff_session_revoked needs it as the "issued at"
            # instant to compare against a revocation. aud/iss presence is
            # already implicitly required by passing audience=/issuer=
            # above (PyJWT rejects a token missing either claim, confirmed
            # by test - see the coordinator report for this task).
            options={"require": ["exp", "iat"]},
        )
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Token invalid sau expirat")

    sub = payload.get("sub")
    email = payload.get("email")
    jti = payload.get("jti")
    scopes = payload.get("scopes")
    iat = payload.get("iat")

    if (
        not isinstance(sub, str) or not sub
        or not isinstance(email, str) or not email
        or not isinstance(jti, str) or not jti
        or not isinstance(scopes, list)
        or "webshop_admin" not in scopes
    ):
        raise HTTPException(status_code=401, detail="Token invalid sau expirat")

    issued_at = datetime.utcfromtimestamp(iat)
    if await _is_bff_session_revoked(sub, issued_at):
        raise HTTPException(status_code=401, detail="Token invalid sau expirat")

    return {"id": sub, "email": email, "role": "admin", "jti": jti, "auth_source": "bff"}


async def _require_admin(request: Request) -> dict:
    """Resolve the bearer token to a user and confirm they have the admin
    role. There's no self-serve way to become admin - the role is only ever
    set directly in the database for the store owner's own account.

    The only accepted credential is a CRM-signed BFF admin JWT (see
    _verify_bff_jwt) - the legacy native webshop admin session token lookup
    that used to run as a fallback has been retired entirely, so a native
    token (or anything else that isn't a valid BFF JWT) is never sufficient
    here anymore.

    If CRM_BFF_JWT_PUBLIC_KEY isn't configured in this environment, this
    fails CLOSED with 503 (same fail-closed pattern used elsewhere in this
    file for other "not configured" cases, e.g. _require_crm_bff_service_key
    further down) rather than ever falling through to another lookup - an
    unconfigured verification key must never be reachable by "just don't
    send a JWT" the way a bad token would be blocked.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Token lipsă sau invalid")

    token = auth_header.replace("Bearer ", "")

    if not CRM_BFF_JWT_PUBLIC_KEY:
        raise HTTPException(status_code=503, detail="Admin auth not configured")

    if not _looks_like_jwt(token):
        raise HTTPException(status_code=401, detail="Token invalid sau expirat")

    return await _verify_bff_jwt(token)

@api_router.get("/admin/products")
async def admin_list_products(
    request: Request,
    search: Optional[str] = None,
    sort: Optional[str] = None,
    limit: int = 100,
    skip: int = 0,
):
    """List/search the full product catalog (originally Shopify-imported
    products and manually-created ones alike - both are now owned by this
    database, see sync_all_products()). Returns a total count alongside the
    page of results so the admin list can render real page-number
    pagination instead of silently truncating at one page.

    Search reuses `build_products_query` - the same word-split, word-boundary,
    Premium/PR-normalizing, flexible-model-spacing search logic as the public
    GET /products - so admins find the same results customers do (e.g.
    "8r410" and "8r 410" both match "8R 410" in compatible_models, "6630pr"
    and "6630 premium" both match). No product_type/collection filter is
    passed since this endpoint doesn't accept those params, and unlike the
    storefront, admin intentionally has no default stock/status filter -
    it must be able to find and edit out-of-stock products too.

    `sort` accepts the same values as the public GET /products (see
    SORT_FIELDS - e.g. "created_at_desc", "updated_at_desc", "price_asc",
    "price_desc", "title_asc") and defaults to the pre-existing
    title_normalized-ascending order when omitted or unrecognized, so
    existing admin callers are unaffected."""
    await _require_admin(request)

    query = build_products_query(search, None, None)

    total = await db.shopify_products.count_documents(query)
    cursor = db.shopify_products.find(query)
    if sort and sort in SORT_FIELDS:
        field, direction = SORT_FIELDS[sort]
        cursor = cursor.sort(field, direction)
    else:
        cursor = cursor.sort("title_normalized", 1)
    cursor = cursor.skip(skip).limit(limit)
    products = await cursor.to_list(limit)
    return {"items": [Product(**p) for p in products], "total": total}

@api_router.get("/admin/customer-interests")
async def admin_list_customer_interests(
    request: Request,
    type: Optional[str] = None,
    limit: int = 50,
    skip: int = 0,
):
    """Admin-only list of every customer's favorite / price-alert / stock-alert
    selections (most recent first), enriched with the product and user info
    needed for manual follow-up. Pure capture-and-display - no automated
    notification is sent when a price changes or stock returns."""
    await _require_admin(request)

    query = {}
    if type:
        query["type"] = type

    cursor = db.customer_interests.find(query).sort("created_at", -1).skip(skip).limit(limit)
    interests = await cursor.to_list(limit)

    # Batch-resolve products/users for this page in two queries total,
    # rather than one query per row.
    product_ids = list({i["product_id"] for i in interests})
    user_ids = list({i["user_id"] for i in interests})

    products_by_id = {}
    if product_ids:
        async for p in db.shopify_products.find({"id": {"$in": product_ids}}):
            products_by_id[p["id"]] = p

    users_by_id = {}
    if user_ids:
        async for u in db.users.find({"id": {"$in": user_ids}}):
            users_by_id[u["id"]] = u

    enriched = []
    for i in interests:
        product = products_by_id.get(i["product_id"])
        user = users_by_id.get(i["user_id"])
        enriched.append({
            "id": i["id"],
            "user_id": i["user_id"],
            "product_id": i["product_id"],
            "type": i["type"],
            "created_at": i["created_at"],
            "product_title": product.get("title") if product else None,
            "product_price": product.get("price") if product else None,
            "product_currency": product.get("currency") if product else None,
            "product_image_url": product.get("image_url") if product else None,
            "product_stock_status": product.get("stock_status") if product else None,
            "user_name": user.get("name") if user else None,
            "user_email": user.get("email") if user else None,
            "user_phone": user.get("phone") if user else None,
        })

    return enriched

def _product_audit_summary(product: dict) -> dict:
    """Compact identifying/business summary of a product doc, for audit-log
    create/delete snapshots - deliberately excludes the large free-text
    description/images payloads rather than dumping the whole document."""
    return {
        "title": product.get("title"),
        "sku": product.get("sku"),
        "price": product.get("price"),
        "currency": product.get("currency"),
        "stock": product.get("stock"),
        "stock_status": product.get("stock_status"),
        "product_type": product.get("product_type"),
        "vendor": product.get("vendor"),
    }


def _diff_changed_fields(existing: dict, updated: dict, candidate_fields) -> tuple:
    """Returns (before, after) containing only the entries from
    `candidate_fields` whose value actually differs between `existing` and
    `updated` - used to build audit-log before/after snapshots with just
    what changed, not the whole document."""
    before, after = {}, {}
    for field in candidate_fields:
        old_value = existing.get(field)
        new_value = updated.get(field)
        if old_value != new_value:
            before[field] = old_value
            after[field] = new_value
    return before, after


@api_router.post("/admin/products")
async def admin_create_product(request: Request, product_data: ProductCreate):
    admin = await _require_admin(request)

    product_id = f"local-{uuid.uuid4()}"
    now = datetime.utcnow()
    collections = build_collections(product_data.product_type, product_data.category, [])
    sanitized_description = sanitize_description_html(product_data.description)
    product = {
        "id": product_id,
        "title": product_data.title,
        "handle": slugify(product_data.title),
        "description": sanitized_description,
        "technical_specs": product_data.technical_specs,
        "description_normalized": normalize_text(sanitized_description),
        "title_normalized": normalize_text(product_data.title),
        "meta_title": product_data.meta_title,
        "meta_description": product_data.meta_description,
        "price": product_data.price,
        "currency": product_data.currency,
        "image_url": product_data.image_url,
        "images": _filter_valid_image_urls(product_data.images),
        "tags": product_data.tags,
        "product_type": product_data.product_type,
        "vendor": product_data.vendor,
        "stock": product_data.stock,
        "stock_status": product_data.stock_status,
        "sku": product_data.sku,
        "compatible_models": product_data.compatible_models,
        "collections": collections,
        "collections_normalized": [normalize_text(c) for c in collections],
        "complementary_product_ids": product_data.complementary_product_ids,
        "equivalent_product_ids": product_data.equivalent_product_ids,
        "is_featured": product_data.is_featured,
        "equipment_year": product_data.equipment_year,
        "equipment_hours": product_data.equipment_hours,
        "equipment_power_hp": product_data.equipment_power_hp,
        "equipment_transmission": product_data.equipment_transmission,
        "equipment_front_tire": product_data.equipment_front_tire,
        "equipment_front_tire_wear": product_data.equipment_front_tire_wear,
        "equipment_rear_tire": product_data.equipment_rear_tire,
        "equipment_rear_tire_wear": product_data.equipment_rear_tire_wear,
        "equipment_max_speed": product_data.equipment_max_speed,
        "source": "manual",
        "created_at": now,
        "updated_at": now,
    }
    await db.shopify_products.insert_one(product)

    await _write_audit_log(
        request, admin, action="product.create", resource_type="product",
        resource_id=product_id, after=_product_audit_summary(product),
    )

    return Product(**product)

_CLOUDFLARE_DELIVERY_URL_RE = re.compile(r"^https://imagedelivery\.net/[^/]+/([^/]+)/[^/]+$")

def _extract_cloudflare_image_id(url: Optional[str]) -> Optional[str]:
    """Pulls the Cloudflare Images id out of one of our own delivery URLs
    (https://imagedelivery.net/{account_hash}/{image_id}/{variant}), or
    returns None if `url` isn't a Cloudflare Images delivery URL at all
    (e.g. a Cloudinary URL or some other externally-pasted link). Used by
    _apply_product_update() below to keep cf_image_id/cf_image_url in sync
    whenever an update changes image_url directly."""
    if not url:
        return None
    match = _CLOUDFLARE_DELIVERY_URL_RE.match(url)
    return match.group(1) if match else None


def _filter_valid_image_urls(images: List[str]) -> List[str]:
    """Server-side defense in depth against a real recurring bug: the CRM's
    admin form used to (and, on a stale cached bundle, still could) join the
    "extra images" field on commas - and Cloudinary URLs embed commas in
    their own transformation path (".../upload/ar_1:1,c_crop,g_center/..."),
    so a single pasted URL could get shredded into 2-3 broken fragments that
    aren't URLs at all (confirmed live on 8 real products, on two separate
    occasions - the second one a day after the frontend fix shipped, on a
    browser tab that had been open since before the deploy). A non-URL
    fragment can never be a valid image regardless of which client sent it,
    so it's dropped here rather than stored - this protects every caller
    (including any admin browser tab still running old JS from before a
    frontend fix), not just the currently-deployed frontend build."""
    return [u for u in images if isinstance(u, str) and u.startswith(("http://", "https://"))]


async def _apply_product_update(product_id: str, product_data: ProductUpdate) -> Optional[dict]:
    """Applies a partial ProductUpdate to a single product by id - shared by
    the single-product and bulk update endpoints so the two code paths can't
    silently diverge. Returns the updated document, or None if no product
    with that id exists (caller decides how to report that)."""
    existing = await db.shopify_products.find_one({"id": product_id})
    if not existing:
        return None

    update_dict = {k: v for k, v in product_data.dict().items() if v is not None}
    category = update_dict.pop("category", None)
    if "images" in update_dict:
        update_dict["images"] = _filter_valid_image_urls(update_dict["images"])

    # Keep cf_image_id/cf_image_url in sync whenever this update actually
    # changes image_url. This endpoint is the ONLY way the admin webshop's
    # ProductForm saves a new image (its upload call never sends product_id,
    # so the /admin/upload-image sync added for the L79232 fix never fires
    # in that flow) - without this, apply_cloudflare_rollout() would keep
    # overwriting image_url with the OLD cf_image_url on every read for any
    # product with cloudflare_rollout=True, silently reverting whatever
    # image was just saved here. See apply_cloudflare_rollout() above for
    # the read-side half of this.
    if "image_url" in update_dict and update_dict["image_url"] != existing.get("image_url"):
        new_image_url = update_dict["image_url"]
        cf_image_id = _extract_cloudflare_image_id(new_image_url)
        if cf_image_id:
            # Already a Cloudflare delivery URL (e.g. pasted from a prior
            # upload) - sync cf_image_id/cf_image_url to match it exactly
            # and make sure rollout is on, so apply_cloudflare_rollout()
            # serves exactly this image on every subsequent read.
            update_dict["cf_image_id"] = cf_image_id
            update_dict["cf_image_url"] = new_image_url
            update_dict["cloudflare_rollout"] = True
        else:
            # Not a Cloudflare URL (external link, Cloudinary, etc) - turn
            # rollout off so apply_cloudflare_rollout() doesn't silently
            # overwrite this newly-saved image_url with a stale/unrelated
            # cf_image_url on the next read. cf_image_id/cf_image_url are
            # deliberately left untouched (not cleared) in case rollout is
            # re-enabled for this product later.
            update_dict["cloudflare_rollout"] = False
    if "title" in update_dict:
        update_dict["title_normalized"] = normalize_text(update_dict["title"])
        update_dict["handle"] = slugify(update_dict["title"])
    if "description" in update_dict:
        update_dict["description"] = sanitize_description_html(update_dict["description"])
        update_dict["description_normalized"] = normalize_text(update_dict["description"])
    # ProductForm always resubmits the full Tip+Categorie selection together,
    # so it's safe to always rebuild `collections` here rather than trying
    # to detect whether either one actually changed.
    final_type = update_dict.get("product_type", existing.get("product_type"))
    update_dict["collections"] = build_collections(final_type, category, existing.get("collections", []))
    update_dict["collections_normalized"] = [normalize_text(c) for c in update_dict["collections"]]
    # Once locally edited, this product is locally-owned from now on - if a
    # full resync ever runs again, it won't get silently reverted back to
    # its old Shopify-imported values.
    update_dict["source"] = "manual"
    # This function is only ever reached via an explicit admin edit (single
    # or bulk) - unlike sync_all_products()'s periodic auto-resync, which
    # deliberately does NOT bump updated_at when it's just re-confirming
    # unchanged data - so every call here is a real edit and always stamps
    # updated_at, regardless of which fields the patch actually touched.
    update_dict["updated_at"] = datetime.utcnow()

    if update_dict:
        await db.shopify_products.update_one({"id": product_id}, {"$set": update_dict})

    return await db.shopify_products.find_one({"id": product_id})

# NOTE: this must stay registered *before* PUT /admin/products/{product_id}
# below - otherwise Starlette's routing would match "bulk" against the
# {product_id} path parameter first, since it's a plain string segment.
@api_router.put("/admin/products/bulk")
async def admin_bulk_update_products(request: Request, bulk_data: ProductBulkUpdate):
    """Applies the same partial update to many products at once (e.g. the
    admin product list's Shopify-style bulk-edit), instead of the frontend
    issuing one PUT per product. Ids that don't match any product are
    reported back in `not_found` rather than failing the whole request."""
    admin = await _require_admin(request)

    if len(bulk_data.ids) > 500:
        raise HTTPException(
            status_code=400,
            detail="Poți actualiza cel mult 500 de produse într-o singură cerere",
        )

    updated_count = 0
    not_found: List[str] = []
    for product_id in bulk_data.ids:
        updated = await _apply_product_update(product_id, bulk_data.patch)
        if updated is None:
            not_found.append(product_id)
        else:
            updated_count += 1

    # One audit entry for the whole bulk request rather than one per product
    # - `patch` (the shared fields being $set on every matched id) plus the
    # affected id lists is what changed here, not a per-product before/after
    # diff (500 individual diffs would be disproportionate for what's a
    # single admin action).
    await _write_audit_log(
        request, admin, action="product.bulk_update", resource_type="product",
        after={
            "patch": {k: v for k, v in bulk_data.patch.dict().items() if v is not None},
            "product_ids": bulk_data.ids,
            "updated_count": updated_count,
            "not_found": not_found,
        },
    )

    return {"updated": updated_count, "not_found": not_found}

# NOTE: same literal-path-before-wildcard reasoning as /admin/products/bulk
# above - this must stay registered before PUT /admin/products/{product_id}.
# (In this particular case the extra "/complementary-add" segment means it
# could never actually match the single-segment {product_id} route anyway,
# but keeping it grouped with the other /admin/products/bulk* routes here
# avoids relying on that.)
@api_router.put("/admin/products/bulk/complementary-add")
async def admin_bulk_add_complementary_products(request: Request, bulk_data: ProductBulkComplementaryAdd):
    """Adds the ids in `add` to the `complementary_product_ids` list of every
    product in `ids`, ADDITIVELY - unlike /admin/products/bulk (which applies
    a ProductUpdate patch via $set, and would therefore overwrite/wipe out
    each product's existing complementary_product_ids). Backs the admin
    webshop's "add complementary products in bulk" action: e.g. select every
    oil-filter product as `ids`, pick "Engine Oil" as the single id in `add`,
    and it gets appended (deduplicated) to all of them in one request. Ids in
    `ids` that don't match any product are reported back in `not_found`
    rather than failing the whole request."""
    admin = await _require_admin(request)

    if not bulk_data.ids or not bulk_data.add:
        raise HTTPException(
            status_code=400,
            detail="ids și add nu pot fi liste goale",
        )

    if len(bulk_data.ids) > 500:
        raise HTTPException(
            status_code=400,
            detail="Poți actualiza cel mult 500 de produse într-o singură cerere",
        )

    if len(bulk_data.add) > 500:
        raise HTTPException(
            status_code=400,
            detail="Poți adăuga cel mult 500 de produse complementare într-o singură cerere",
        )

    cursor = db.shopify_products.find({"id": {"$in": bulk_data.ids}}, {"id": 1})
    found_docs = await cursor.to_list(500)
    found_ids = {doc["id"] for doc in found_docs}
    not_found = [product_id for product_id in bulk_data.ids if product_id not in found_ids]

    if found_ids:
        # This is an explicit admin edit, same as _apply_product_update()
        # (which this endpoint deliberately bypasses to get $addToSet
        # instead of $set semantics) - so it must stamp updated_at the same
        # way, regardless of whether any id in `add` was already present.
        await db.shopify_products.update_many(
            {"id": {"$in": list(found_ids)}},
            {
                "$addToSet": {"complementary_product_ids": {"$each": bulk_data.add}},
                "$set": {"updated_at": datetime.utcnow()},
            },
        )

    # Single audit entry for the whole bulk request - see the same reasoning
    # in admin_bulk_update_products above.
    await _write_audit_log(
        request, admin, action="product.bulk_complementary_add", resource_type="product",
        after={
            "add": bulk_data.add,
            "product_ids": bulk_data.ids,
            "updated_count": len(found_ids),
            "not_found": not_found,
        },
    )

    return {"updated": len(found_ids), "not_found": not_found}

# NOTE: same literal-path-before-wildcard reasoning as /admin/products/bulk
# above - this must stay registered before PUT /admin/products/{product_id}.
@api_router.put("/admin/products/bulk/equivalent-add")
async def admin_bulk_add_equivalent_products(request: Request, bulk_data: ProductBulkEquivalentAdd):
    """Adds the ids in `add` to the `equivalent_product_ids` list of every
    product in `ids`, ADDITIVELY - unlike /admin/products/bulk (which applies
    a ProductUpdate patch via $set, and would therefore overwrite/wipe out
    each product's existing equivalent_product_ids). Exact same shape/
    semantics as /admin/products/bulk/complementary-add, just for the
    equivalent_product_ids field instead of complementary_product_ids. Ids in
    `ids` that don't match any product are reported back in `not_found`
    rather than failing the whole request."""
    admin = await _require_admin(request)

    if not bulk_data.ids or not bulk_data.add:
        raise HTTPException(
            status_code=400,
            detail="ids și add nu pot fi liste goale",
        )

    if len(bulk_data.ids) > 500:
        raise HTTPException(
            status_code=400,
            detail="Poți actualiza cel mult 500 de produse într-o singură cerere",
        )

    if len(bulk_data.add) > 500:
        raise HTTPException(
            status_code=400,
            detail="Poți adăuga cel mult 500 de produse echivalente într-o singură cerere",
        )

    cursor = db.shopify_products.find({"id": {"$in": bulk_data.ids}}, {"id": 1})
    found_docs = await cursor.to_list(500)
    found_ids = {doc["id"] for doc in found_docs}
    not_found = [product_id for product_id in bulk_data.ids if product_id not in found_ids]

    if found_ids:
        # Same reasoning as /admin/products/bulk/complementary-add above.
        await db.shopify_products.update_many(
            {"id": {"$in": list(found_ids)}},
            {
                "$addToSet": {"equivalent_product_ids": {"$each": bulk_data.add}},
                "$set": {"updated_at": datetime.utcnow()},
            },
        )

    # Single audit entry for the whole bulk request - see the same reasoning
    # in admin_bulk_update_products above.
    await _write_audit_log(
        request, admin, action="product.bulk_equivalent_add", resource_type="product",
        after={
            "add": bulk_data.add,
            "product_ids": bulk_data.ids,
            "updated_count": len(found_ids),
            "not_found": not_found,
        },
    )

    return {"updated": len(found_ids), "not_found": not_found}

# Projection used by the preview endpoint below - only the fields needed to
# extract a code, compare vendors, and show a staff member enough to eyeball
# a candidate match, over what can be the WHOLE catalog (~15k products) in
# one query.
_PRODUCT_EQUIVALENTS_PREVIEW_PROJECTION = {
    "id": 1, "title": 1, "vendor": 1, "price": 1, "currency": 1,
    "sku": 1, "image_url": 1, "stock": 1, "equivalent_product_ids": 1,
}

@api_router.post("/admin/products/equivalents/preview")
async def admin_preview_product_equivalents(request: Request, payload: ProductEquivalentsPreviewRequest):
    """Read-only first step of "Adaugă echivalente": for each of the
    staff-selected `product_ids` (e.g. a freshly-created batch), extracts
    its OEM/part code from `title` (see extract_oem_code above) and looks
    for OTHER products anywhere in the catalog sharing that exact code
    (case-insensitive) but with a DIFFERENT `vendor` - the "same part,
    different brand" signal the storefront's GET /products/{id}/equivalents
    already reads from `equivalent_product_ids`. Already-linked pairs (ids
    already present in a product's own `equivalent_product_ids`) are
    excluded from `matches` since there'd be no point proposing them again.

    Makes NO database writes - this only returns proposed matches for a
    human to confirm/deselect via POST /admin/products/equivalents/apply
    below, which is the only endpoint of this pair that actually writes.

    Extraction + matching runs once over the whole catalog and is indexed
    in memory, rather than re-scanning the full collection once per
    selected product (which would be `len(product_ids)` separate full
    scans) - this is a single query either way.
    """
    await _require_admin(request)

    if not payload.product_ids:
        raise HTTPException(status_code=400, detail="product_ids nu poate fi o listă goală")

    if len(payload.product_ids) > 500:
        raise HTTPException(
            status_code=400,
            detail="Poți previzualiza cel mult 500 de produse într-o singură cerere",
        )

    all_docs = await db.shopify_products.find({}, _PRODUCT_EQUIVALENTS_PREVIEW_PROJECTION).to_list(None)

    docs_by_id: Dict[str, dict] = {}
    code_by_id: Dict[str, Optional[str]] = {}
    code_index: Dict[str, List[str]] = {}
    for doc in all_docs:
        product_id = doc.get("id")
        if not product_id:
            continue
        docs_by_id[product_id] = doc
        code = extract_oem_code(doc.get("title", ""))
        code_by_id[product_id] = code
        if code:
            code_index.setdefault(code.lower(), []).append(product_id)

    results = []
    for product_id in payload.product_ids:
        doc = docs_by_id.get(product_id)
        if not doc:
            results.append({
                "product_id": product_id,
                "title": None,
                "extracted_code": None,
                "matches": [],
                "not_found": True,
            })
            continue

        code = code_by_id.get(product_id)
        matches = []
        if code:
            existing_equivalents = set(doc.get("equivalent_product_ids") or [])
            product_vendor = (doc.get("vendor") or "").strip().lower()
            for candidate_id in code_index.get(code.lower(), []):
                if candidate_id == product_id or candidate_id in existing_equivalents:
                    continue
                candidate = docs_by_id[candidate_id]
                candidate_vendor = (candidate.get("vendor") or "").strip().lower()
                if candidate_vendor == product_vendor:
                    continue
                matches.append({
                    "id": candidate.get("id"),
                    "title": candidate.get("title"),
                    "vendor": candidate.get("vendor"),
                    "price": candidate.get("price"),
                    "currency": candidate.get("currency"),
                    "sku": candidate.get("sku"),
                    "image_url": candidate.get("image_url"),
                    "stock": candidate.get("stock"),
                })

        results.append({
            "product_id": product_id,
            "title": doc.get("title"),
            "extracted_code": code,
            "matches": matches,
            "not_found": False,
        })

    return {"results": results}

@api_router.post("/admin/products/equivalents/apply")
async def admin_apply_product_equivalents(request: Request, payload: ProductEquivalentsApplyRequest):
    """Writes the equivalent-product links a staff member confirmed from
    POST /admin/products/equivalents/preview's proposed `matches` - this is
    explicitly whatever subset they kept after deselecting any they didn't
    want, NOT a re-run of the matching itself; it only writes exactly the
    `links` it's given and never queries by title/code.

    Shape is deliberately different from /admin/products/bulk/equivalent-add
    (which applies the SAME `add` ids to every product in `ids` - one set of
    ids fanned out to many products): here each `links` item carries its OWN
    distinct `equivalent_ids` for that one `product_id` (product A matches X
    and Y, product B matches Z), since that's what a real preview-confirm
    batch looks like.

    Bidirectional by design (unlike /admin/products/bulk/complementary-add,
    which is intentionally one-directional): for every {product_id,
    equivalent_ids} pair, `equivalent_ids` is $addToSet'd onto product_id's
    own equivalent_product_ids, AND product_id is $addToSet'd onto EACH of
    those equivalent products' own equivalent_product_ids - "same part,
    different brand" is inherently a symmetric relationship, so a link from
    A to B must always imply the reverse link from B to A. $addToSet makes
    re-applying the same links (or overlapping ones) idempotent either
    direction.

    Ids that don't match any existing product (on either side of a pair)
    are reported back in `not_found` rather than failing the whole request,
    same convention as the other bulk endpoints above.
    """
    admin = await _require_admin(request)

    if not payload.links:
        raise HTTPException(status_code=400, detail="links nu poate fi o listă goală")

    if len(payload.links) > 500:
        raise HTTPException(
            status_code=400,
            detail="Poți lega cel mult 500 de produse într-o singură cerere",
        )

    for link in payload.links:
        if len(link.equivalent_ids) > 500:
            raise HTTPException(
                status_code=400,
                detail="Poți lega cel mult 500 de produse echivalente pentru un singur produs",
            )

    # Every id this request touches, in either role (product_id or one of
    # its equivalent_ids - the bidirectional write below touches both sides)
    # - collected up front so existence can be checked with a single query
    # instead of one per link/id.
    all_ids: Set[str] = set()
    for link in payload.links:
        if not link.equivalent_ids:
            continue
        all_ids.add(link.product_id)
        all_ids.update(link.equivalent_ids)

    found_ids: Set[str] = set()
    if all_ids:
        cursor = db.shopify_products.find({"id": {"$in": list(all_ids)}}, {"id": 1})
        found_docs = await cursor.to_list(len(all_ids))
        found_ids = {doc["id"] for doc in found_docs}

    not_found: Set[str] = set()
    updated_pairs = 0
    now = datetime.utcnow()

    for link in payload.links:
        if not link.equivalent_ids:
            continue

        if link.product_id not in found_ids:
            not_found.add(link.product_id)
            continue

        valid_equivalent_ids = [eid for eid in link.equivalent_ids if eid in found_ids]
        for eid in link.equivalent_ids:
            if eid not in found_ids:
                not_found.add(eid)

        if not valid_equivalent_ids:
            continue

        await db.shopify_products.update_one(
            {"id": link.product_id},
            {
                "$addToSet": {"equivalent_product_ids": {"$each": valid_equivalent_ids}},
                "$set": {"updated_at": now},
            },
        )
        # Reverse direction - one $addToSet per equivalent id, since each
        # one only ever gets this single link.product_id added (unlike the
        # forward write above, there's no shared "$each" value across many
        # docs here).
        for eid in valid_equivalent_ids:
            await db.shopify_products.update_one(
                {"id": eid},
                {
                    "$addToSet": {"equivalent_product_ids": link.product_id},
                    "$set": {"updated_at": now},
                },
            )
        updated_pairs += len(valid_equivalent_ids)

    # Single audit entry for the whole confirmed batch - each link's own
    # requested equivalent_ids (what staff actually confirmed), not a
    # per-product before/after diff, same reasoning as the other bulk
    # endpoints above.
    await _write_audit_log(
        request, admin, action="product.equivalents_apply", resource_type="product",
        after={
            "links": [
                {"product_id": link.product_id, "equivalent_ids": link.equivalent_ids}
                for link in payload.links
            ],
            "updated_pairs": updated_pairs,
            "not_found": sorted(not_found),
        },
    )

    return {"updated_pairs": updated_pairs, "not_found": sorted(not_found)}

# NOTE: same reasoning as /admin/products/bulk above - "bulk-save" and
# "by-ids" must stay registered before PUT/DELETE /admin/products/{product_id}
# below, otherwise Starlette would match them against the {product_id} path
# parameter first.
@api_router.put("/admin/products/bulk-save")
async def admin_bulk_save_products(request: Request, bulk_data: ProductBulkSave):
    """Shopify-style inline grid editor: each selected product carries its
    own distinct patch (product A's price changes to X, product B's to Y,
    etc), unlike /admin/products/bulk which applies one shared patch to many
    ids. Reuses _apply_product_update per item so the update logic can't
    diverge from the single-product/shared-bulk code paths. Ids that don't
    match any product are reported back in `not_found` rather than failing
    the whole request."""
    admin = await _require_admin(request)

    if len(bulk_data.updates) > 500:
        raise HTTPException(
            status_code=400,
            detail="Poți actualiza cel mult 500 de produse într-o singură cerere",
        )

    updated_count = 0
    not_found: List[str] = []
    for item in bulk_data.updates:
        updated = await _apply_product_update(item.id, item.patch)
        if updated is None:
            not_found.append(item.id)
        else:
            updated_count += 1

    # One audit entry for the whole grid-save request - each item's own
    # patch (the fields it actually submitted), not a per-product before/
    # after diff (same reasoning as the other bulk endpoints above).
    await _write_audit_log(
        request, admin, action="product.bulk_save", resource_type="product",
        after={
            "updates": [
                {"id": item.id, "patch": {k: v for k, v in item.patch.dict().items() if v is not None}}
                for item in bulk_data.updates
            ],
            "updated_count": updated_count,
            "not_found": not_found,
        },
    )

    return {"updated": updated_count, "not_found": not_found}

@api_router.get("/admin/products/by-ids")
async def admin_get_products_by_ids(request: Request, ids: str):
    """Batch-fetches full Product objects for a comma-separated list of ids
    in one request, so the admin grid-edit page can load details for a set
    of selected products without issuing one GET per product. Order of the
    returned list isn't guaranteed to match `ids` - the frontend re-associates
    by id."""
    await _require_admin(request)

    id_list = [i for i in ids.split(",") if i]

    if len(id_list) > 500:
        raise HTTPException(
            status_code=400,
            detail="Poți solicita cel mult 500 de produse într-o singură cerere",
        )

    cursor = db.shopify_products.find({"id": {"$in": id_list}})
    products = await cursor.to_list(500)
    return [Product(**p) for p in products]

@api_router.put("/admin/products/{product_id}")
async def admin_update_product(product_id: str, request: Request, product_data: ProductUpdate):
    admin = await _require_admin(request)

    # Fetched separately (on top of _apply_product_update()'s own internal
    # existence check) purely so the pre-update values are available here to
    # build the audit-log before/after diff - see _diff_changed_fields.
    existing = await db.shopify_products.find_one({"id": product_id})
    if not existing:
        raise HTTPException(status_code=404, detail="Produs inexistent")

    updated = await _apply_product_update(product_id, product_data)
    if updated is None:
        raise HTTPException(status_code=404, detail="Produs inexistent")

    submitted = product_data.dict()
    candidate_fields = [k for k, v in submitted.items() if v is not None and k != "category"]
    if submitted.get("product_type") is not None or submitted.get("category") is not None:
        # Not a stored field itself (see _apply_product_update) - it's what
        # actually changes on the document when either of these is set.
        candidate_fields.append("collections")
    before, after = _diff_changed_fields(existing, updated, candidate_fields)

    await _write_audit_log(
        request, admin, action="product.update", resource_type="product",
        resource_id=product_id, before=before, after=after,
    )

    return Product(**updated)

@api_router.delete("/admin/products/{product_id}")
async def admin_delete_product(product_id: str, request: Request, reason: Optional[str] = Body(default=None, embed=True)):
    """Permanently deletes a product. Irreversible, so a genuine reason
    (>= 3 characters after trimming) is required - passed in the JSON body
    as {"reason": "..."} - and recorded on the resulting audit-log entry."""
    admin = await _require_admin(request)

    if not reason or len(reason.strip()) < 3:
        raise HTTPException(
            status_code=400,
            detail="Este necesar un motiv (minim 3 caractere) pentru ștergerea unui produs",
        )

    existing = await db.shopify_products.find_one({"id": product_id})
    if not existing:
        raise HTTPException(status_code=404, detail="Produs inexistent")

    await db.shopify_products.delete_one({"id": product_id})

    await _write_audit_log(
        request, admin, action="product.delete", resource_type="product",
        resource_id=product_id, before=_product_audit_summary(existing),
        reason=reason.strip(),
    )

    return {"message": "Produs șters"}

async def _upload_to_cloudflare_images(http_client: httpx.AsyncClient, filename: str, content: bytes) -> dict:
    """Same upload call as upload_to_cloudflare() in
    scripts/migrate_to_cloudflare_images.py, adapted to httpx's async
    client (the migration script uses the sync one since it's a standalone
    CLI tool). Raises on any non-success response."""
    url = CLOUDFLARE_IMAGES_UPLOAD_URL.format(account_id=CLOUDFLARE_ACCOUNT_ID)
    headers = {"Authorization": f"Bearer {CLOUDFLARE_API_TOKEN}"}
    resp = await http_client.post(
        url, headers=headers, files={"file": (filename, content)}, timeout=60.0
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"Cloudflare upload a eșuat ({resp.status_code}): {resp.text[:300]}")
    payload = resp.json()
    if not payload.get("success"):
        raise RuntimeError(f"Cloudflare a raportat eșec: {payload.get('errors')}")
    return payload["result"]


def _pick_cloudflare_delivery_url(cf_result: dict) -> Optional[str]:
    """Identical logic to pick_delivery_url() in
    scripts/migrate_to_cloudflare_images.py - swap the trailing variant
    segment of whatever URL Cloudflare returns for CLOUDFLARE_IMAGE_VARIANT,
    so this always serves the same center-cropped square variant every
    already-migrated product uses."""
    variants = cf_result.get("variants") or []
    if not variants:
        return None
    base = variants[0].rsplit("/", 1)[0]
    return f"{base}/{CLOUDFLARE_IMAGE_VARIANT}"


# `file.content_type` below is just a client-supplied header - trivially
# spoofable (e.g. a .php/.svg/.html file renamed with an "image/jpeg"
# Content-Type) - so it's only used as a cheap early rejection, never as
# the actual security check. MAX_UPLOAD_IMAGE_BYTES/_ALLOWED_UPLOAD_IMAGE_FORMATS
# below back it up with a real size cap and Pillow actually opening/decoding
# the file to confirm it's one of a small allowlist of real image formats.
MAX_UPLOAD_IMAGE_BYTES = 10 * 1024 * 1024  # 10MB
_ALLOWED_UPLOAD_IMAGE_FORMATS = {"JPEG", "PNG", "WEBP"}


@api_router.post("/admin/upload-image")
async def admin_upload_image(
    request: Request,
    file: UploadFile = File(...),
    product_id: Optional[str] = Form(None),
):
    """Upload a product image (e.g. exported from Canva) to Cloudflare
    Images - same account/variant ("square", fit=cover, 1000x1000) as the
    original Cloudinary -> Cloudflare bulk migration
    (scripts/migrate_to_cloudflare_images.py) - and return its delivery URL,
    for pasting into the image fields above. No longer touches Cloudinary
    at all (see CLOUDINARY_* config above, still kept only for the legacy
    /admin/migrate-images endpoint).

    If `product_id` is given (the admin panel editing an existing product,
    as opposed to drafting a brand new one), this ALSO atomically updates
    that product's `cf_image_id`/`cf_image_url` to the newly uploaded image
    and sets `cloudflare_rollout: True` - fixing the L79232 class of bug,
    where a re-uploaded `image_url` and the product's `cf_image_id`/
    `cf_image_url` could drift out of sync (see apply_cloudflare_rollout()
    above) because this endpoint used to only return a bare URL with no way
    to know which product it belonged to. `image_url` (and `images[0]` if it
    mirrored the old `image_url`) is set to the same new Cloudflare URL too,
    so the product looks right immediately regardless of its
    `cloudflare_rollout` flag or whatever later reads `apply_cloudflare_rollout()`.
    `product_id` is optional and backward compatible - omitting it just
    uploads and returns the URL without touching any product, same as
    before."""
    admin = await _require_admin(request)

    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Fișierul trebuie să fie o imagine")

    if not CLOUDFLARE_ACCOUNT_ID or not CLOUDFLARE_API_TOKEN:
        logger.error("Cloudflare upload error: CLOUDFLARE_ACCOUNT_ID/CLOUDFLARE_API_TOKEN missing")
        raise HTTPException(status_code=502, detail="Încărcarea imaginii a eșuat")

    existing_product = None
    if product_id:
        existing_product = await db.shopify_products.find_one(
            {"id": product_id}, {"image_url": 1, "images": 1}
        )
        if not existing_product:
            raise HTTPException(status_code=404, detail="Produs inexistent")

    # Read in bounded chunks and bail out as soon as the cap is exceeded,
    # instead of first buffering an arbitrarily large upload fully into
    # memory before checking its size.
    chunks = bytearray()
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        chunks.extend(chunk)
        if len(chunks) > MAX_UPLOAD_IMAGE_BYTES:
            raise HTTPException(
                status_code=400,
                detail=f"Imaginea depășește dimensiunea maximă permisă ({MAX_UPLOAD_IMAGE_BYTES // (1024 * 1024)}MB)",
            )
    contents = bytes(chunks)

    if not contents:
        raise HTTPException(status_code=400, detail="Fișierul este gol")

    # Verify the REAL file content (magic bytes/actual decoded format), not
    # just the client-supplied filename/Content-Type header - both are
    # trivial to spoof. Image.verify() raises on anything that isn't a
    # genuine, undamaged image of a format Pillow recognizes.
    try:
        with Image.open(io.BytesIO(contents)) as probe_image:
            probe_image.verify()
        with Image.open(io.BytesIO(contents)) as format_image:
            detected_format = (format_image.format or "").upper()
    except Exception:
        raise HTTPException(status_code=400, detail="Fișierul nu este o imagine validă")

    if detected_format not in _ALLOWED_UPLOAD_IMAGE_FORMATS:
        raise HTTPException(
            status_code=400,
            detail="Tip de imagine nepermis - sunt acceptate doar JPEG, PNG sau WEBP",
        )

    try:
        async with httpx.AsyncClient() as http_client:
            cf_result = await _upload_to_cloudflare_images(
                http_client, file.filename or "upload.jpg", contents
            )
    except Exception as e:
        logger.error(f"Cloudflare upload error: {e}")
        raise HTTPException(status_code=502, detail="Încărcarea imaginii a eșuat")

    cf_image_id = cf_result.get("id")
    cf_image_url = _pick_cloudflare_delivery_url(cf_result)
    if not cf_image_url:
        logger.error(f"Cloudflare upload error: no variants in response for image {cf_image_id}")
        raise HTTPException(status_code=502, detail="Încărcarea imaginii a eșuat")

    if existing_product is not None:
        update = {
            "cf_image_id": cf_image_id,
            "cf_image_url": cf_image_url,
            "image_url": cf_image_url,
            # Re-enable the normal Cloudflare-serving path for this product.
            # Covers both the L79232-style case (was explicitly set to
            # False as a one-off mitigation for a stale cf_image_url, now
            # freshly back in sync) and brand-new/never-migrated products
            # (flag unset entirely) - either way, cf_image_url is now
            # guaranteed to match image_url, so there's no reason to keep
            # serving Cloudinary/an unrelated stale Cloudflare image.
            "cloudflare_rollout": True,
            # Directly changes this product's image, so it counts as a real
            # edit the same way _apply_product_update()'s PUT does.
            "updated_at": datetime.utcnow(),
        }
        old_image_url = existing_product.get("image_url")
        images = existing_product.get("images") or []
        if images and images[0] == old_image_url:
            update["images"] = [cf_image_url] + list(images[1:])
        await db.shopify_products.update_one({"id": product_id}, {"$set": update})

        await _write_audit_log(
            request, admin, action="product.image_upload", resource_type="product",
            resource_id=product_id,
            before={"image_url": old_image_url},
            after={"image_url": cf_image_url},
        )

    return {"url": cf_image_url, "cf_image_id": cf_image_id, "cf_image_url": cf_image_url}

# ==================== IMAGE MIGRATION (Shopify CDN -> Cloudinary) ====================
# One-off bulk migration so product images no longer depend on Shopify's CDN
# staying up (the whole point of leaving Shopify). Resumable: progress is
# tracked per source URL in db.image_migrations, so a re-run skips anything
# already done and only retries what's pending/failed.

image_migration_status = {
    "is_running": False,
    "total": 0,
    "done": 0,
    "failed": 0,
    "last_run": None,
    "error": None,
}

async def _migrate_single_image(url: str, semaphore: asyncio.Semaphore):
    async with semaphore:
        existing = await db.image_migrations.find_one({"_id": url})
        if existing and existing.get("status") == "done":
            image_migration_status["done"] += 1
            return

        try:
            # Cloudinary fetches directly from the given URL server-side -
            # no need to download it ourselves first.
            result = await asyncio.to_thread(
                cloudinary.uploader.upload, url, folder="agb-agroparts/products-import"
            )
            await db.image_migrations.update_one(
                {"_id": url},
                {"$set": {
                    "cloudinary_url": result["secure_url"],
                    "status": "done",
                    "error": None,
                    "migrated_at": datetime.utcnow(),
                }},
                upsert=True,
            )
            image_migration_status["done"] += 1
        except Exception as e:
            await db.image_migrations.update_one(
                {"_id": url},
                {"$set": {"status": "failed", "error": str(e), "migrated_at": datetime.utcnow()}},
                upsert=True,
            )
            image_migration_status["failed"] += 1

async def _run_image_migration(limit: Optional[int] = None):
    global image_migration_status
    if image_migration_status["is_running"]:
        return
    image_migration_status.update({"is_running": True, "error": None, "done": 0, "failed": 0})

    try:
        cursor = db.shopify_products.find({}, {"image_url": 1, "images": 1})
        urls = set()
        async for doc in cursor:
            for u in (doc.get("images") or []):
                if u and "cdn.shopify.com" in u:
                    urls.add(u)
            image_url = doc.get("image_url")
            if image_url and "cdn.shopify.com" in image_url:
                urls.add(image_url)

        urls = list(urls)
        if limit:
            urls = urls[:limit]
        image_migration_status["total"] = len(urls)

        semaphore = asyncio.Semaphore(10)
        await asyncio.gather(*(_migrate_single_image(u, semaphore) for u in urls))

        # Rewrite product image fields using whatever's mapped so far -
        # partial progress (e.g. a limited test run) still updates the
        # matching products instead of waiting for 100% completion.
        mapping = {}
        async for m in db.image_migrations.find({"status": "done", "_id": {"$in": urls}}):
            mapping[m["_id"]] = m["cloudinary_url"]

        cursor = db.shopify_products.find(
            {"$or": [{"image_url": {"$in": urls}}, {"images": {"$in": urls}}]},
            {"id": 1, "image_url": 1, "images": 1},
        )
        async for doc in cursor:
            update = {}
            old_image_url = doc.get("image_url")
            if old_image_url in mapping:
                update["image_url"] = mapping[old_image_url]
            old_images = doc.get("images") or []
            new_images = [mapping.get(u, u) for u in old_images]
            if new_images != old_images:
                update["images"] = new_images
            if update:
                await db.shopify_products.update_one({"id": doc["id"]}, {"$set": update})

        image_migration_status["last_run"] = datetime.utcnow().isoformat()
    except Exception as e:
        image_migration_status["error"] = str(e)
        logger.error(f"Image migration error: {e}")
    finally:
        image_migration_status["is_running"] = False

@api_router.post("/admin/migrate-images")
async def admin_migrate_images(request: Request, background_tasks: BackgroundTasks, limit: Optional[int] = None):
    """Kick off the Shopify-CDN -> Cloudinary image migration in the
    background. Pass `limit` to test on a handful of images first."""
    admin = await _require_admin(request)
    if image_migration_status["is_running"]:
        raise HTTPException(status_code=409, detail="Migrarea rulează deja")
    background_tasks.add_task(_run_image_migration, limit)
    # This only logs that the migration was TRIGGERED with these params - the
    # actual per-image writes happen later in the background task above, well
    # after this request has already returned, so there's no meaningful
    # per-image before/after to attach to a single request-scoped log entry
    # here (see db.image_migrations / GET /admin/migrate-images/status for
    # the actual run's own progress/outcome tracking).
    await _write_audit_log(
        request, admin, action="images.migrate_trigger", resource_type="image_migration",
        after={"limit": limit},
    )
    return {"message": "Migrare pornită"}

@api_router.get("/admin/migrate-images/status")
async def admin_migrate_images_status(request: Request):
    await _require_admin(request)
    return image_migration_status

@api_router.get("/admin/orders")
async def admin_list_orders(request: Request, limit: int = 100, skip: int = 0):
    """List orders placed through the webshop's own checkout (db.orders).
    Does not include Shopify/mobile-app orders (db.mobile_orders) - those are
    thin references without full line-item/customer detail."""
    await _require_admin(request)
    cursor = db.orders.find({}).sort("created_at", -1).skip(skip).limit(limit)
    orders = await cursor.to_list(limit)
    for order in orders:
        order.pop("_id", None)
    return orders

async def _fetch_native_and_shopify_orders_raw():
    """Shared raw fetch behind GET /admin/orders/history and the
    GET /admin/analytics/sales aggregation below: native webshop/mobile
    orders (db.orders) plus the imported historical Shopify catalog
    (db.shopify_order_history), plus a batch-fetched {client_id: client_doc}
    map for the older customer-scoped-import Shopify records that only
    carry client_id (see _shopify_order_to_merged). Extracted so every
    caller reuses the exact same two-collection dataset/fetch instead of
    re-implementing this ad hoc."""
    native_orders = await db.orders.find({}).to_list(5000)
    shopify_orders = await db.shopify_order_history.find({}).to_list(5000)

    client_ids = list({o["client_id"] for o in shopify_orders if o.get("client_id")})
    clients_by_id = {}
    if client_ids:
        async for c in db.clients.find({"id": {"$in": client_ids}}):
            clients_by_id[c["id"]] = c

    return native_orders, shopify_orders, clients_by_id


# NOTE: must stay registered *before* GET /admin/orders/{order_id} below -
# same literal-path-before-wildcard ordering gotcha as /admin/products/bulk,
# otherwise "history" would be swallowed as an order_id.
@api_router.get("/admin/orders/history")
async def admin_list_order_history(
    request: Request,
    search: Optional[str] = None,
    source: Optional[str] = None,
    limit: int = 100,
    skip: int = 0,
):
    """Unified order history across both sources this store has ever had:
    native webshop checkouts (db.orders) and the full historical Shopify
    catalog (db.shopify_order_history - see the full-store import further
    down), each entry tagged with `source`. In-process merge/sort rather
    than a cross-collection DB-level paginated query - this store's total
    order count is in the hundreds, not a scale where that matters."""
    await _require_admin(request)

    native_orders, shopify_orders, clients_by_id = await _fetch_native_and_shopify_orders_raw()

    merged = [_native_order_to_merged(o) for o in native_orders] + [
        _shopify_order_to_merged(o, clients_by_id) for o in shopify_orders
    ]

    if source in ("native", "shopify"):
        merged = [o for o in merged if o["source"] == source]

    if search:
        term = normalize_text(search)

        def _matches(o: dict) -> bool:
            haystack = normalize_text(
                " ".join(filter(None, [
                    o.get("customer_name"),
                    o.get("customer_email"),
                    str(o.get("order_number") or ""),
                ]))
            )
            return term in haystack

        merged = [o for o in merged if _matches(o)]

    merged.sort(key=_merged_order_sort_key, reverse=True)

    total = len(merged)
    return {"items": merged[skip: skip + limit], "total": total}


# NOTE: must stay registered *after* GET /admin/orders above - same
# literal-path-before-wildcard ordering gotcha as /admin/products/bulk.
@api_router.get("/admin/orders/{order_id}")
async def admin_get_order(request: Request, order_id: str):
    """Single order, for the admin order detail/edit page."""
    await _require_admin(request)
    order = await db.orders.find_one({"id": order_id})
    if not order:
        raise HTTPException(status_code=404, detail="Comanda nu a fost găsită")
    order.pop("_id", None)
    return order


class OrderItemInput(BaseModel):
    product_id: str
    product_name: str
    product_image: str
    price: float
    quantity: int


class OrderItemsUpdate(BaseModel):
    items: List[OrderItemInput]
    # Only actually required when this update ELIMINATES an item that was
    # previously on the order (see the removed_product_ids check below) -
    # not for pure add/quantity-adjust edits. Irreversible in the sense that
    # the removed line simply isn't on the order anymore once saved.
    reason: Optional[str] = None


@api_router.put("/admin/orders/{order_id}/items")
async def admin_update_order_items(
    request: Request,
    order_id: str,
    payload: OrderItemsUpdate,
    background_tasks: BackgroundTasks,
):
    """Add/remove/adjust the products on an order - e.g. staff noticing the
    customer will also need an installation part they didn't order. Only
    allowed while the order is still "pending" (not yet processed) - once
    it's moved past that, editing locks, same spirit as Shopify's own order
    editing restrictions."""
    admin = await _require_admin(request)

    order_doc = await db.orders.find_one({"id": order_id})
    if not order_doc:
        raise HTTPException(status_code=404, detail="Comanda nu a fost găsită")
    if order_doc.get("status") != "pending":
        raise HTTPException(
            status_code=409,
            detail="Comanda nu mai poate fi editată (nu mai este în așteptare).",
        )
    if not payload.items:
        raise HTTPException(status_code=400, detail="Comanda trebuie să aibă cel puțin un produs.")

    old_items = order_doc.get("items", [])
    items = [item.dict() for item in payload.items]

    # This endpoint replaces the ENTIRE items list rather than patching
    # individual lines, so any old product_id missing from the new list has
    # been eliminated from the order, not just quantity-adjusted - that's
    # the "removal" case that requires a reason.
    old_product_ids = {i.get("product_id") for i in old_items}
    new_product_ids = {i["product_id"] for i in items}
    removed_product_ids = old_product_ids - new_product_ids
    if removed_product_ids and (not payload.reason or len(payload.reason.strip()) < 3):
        raise HTTPException(
            status_code=400,
            detail="Este necesar un motiv (minim 3 caractere) pentru eliminarea unui produs din comandă",
        )

    subtotal = sum(item["price"] * item["quantity"] for item in items)
    total = subtotal + order_doc.get("shipping", 25.0)

    update_dict: dict = {"items": items, "subtotal": subtotal, "total": total}
    was_crm_synced = bool(order_doc.get("crm_synced"))
    if was_crm_synced:
        update_dict["crm_items_dirty"] = True
        update_dict["crm_items_sync_error"] = None

    await db.orders.update_one({"id": order_id}, {"$set": update_dict})
    updated = await db.orders.find_one({"id": order_id})
    updated.pop("_id", None)

    await _write_audit_log(
        request, admin, action="order.items_update", resource_type="order",
        resource_id=order_id,
        before={"items": old_items, "subtotal": order_doc.get("subtotal"), "total": order_doc.get("total")},
        after={"items": items, "subtotal": subtotal, "total": total},
        reason=payload.reason.strip() if removed_product_ids else None,
    )

    if was_crm_synced:
        background_tasks.add_task(sync_order_update_to_crm, Order(**updated))

    return updated


class OrderCourierUpdate(BaseModel):
    courier_awb_number: str
    courier_service: Optional[str] = None


@api_router.patch("/admin/orders/{order_id}/courier")
async def admin_update_order_courier(
    request: Request,
    order_id: str,
    payload: OrderCourierUpdate,
):
    """Receives the FAN Courier AWB number for an order once staff generates
    it in agb-crm (fire-and-forget push from CRM's generate_awb - see
    routes_courier.py there). Sets courier_awb_number/courier_service on the
    order so the customer can see their AWB and live tracking status on
    their own account page (GET /auth/orders/{order_id}/courier-tracking
    below). Same admin-auth pattern as the neighboring PUT
    /admin/orders/{order_id}/items above - no separate mechanism for this
    one endpoint."""
    admin = await _require_admin(request)

    order_doc = await db.orders.find_one({"id": order_id})
    if not order_doc:
        raise HTTPException(status_code=404, detail="Comanda nu a fost găsită")

    update_dict = {
        "courier_awb_number": payload.courier_awb_number,
        "courier_service": payload.courier_service,
    }
    await db.orders.update_one({"id": order_id}, {"$set": update_dict})

    await _write_audit_log(
        request, admin, action="order.courier_update", resource_type="order",
        resource_id=order_id,
        before={
            "courier_awb_number": order_doc.get("courier_awb_number"),
            "courier_service": order_doc.get("courier_service"),
        },
        after=update_dict,
    )

    updated = await db.orders.find_one({"id": order_id})
    updated.pop("_id", None)
    return updated


class OrderCustomerUpdate(BaseModel):
    """Partial update to an order's frozen `customer` snapshot (CustomerInfo)
    - every field is optional, only fields actually present (non-None) in
    the request body get changed. Deliberately omits `email` (the
    account-identifying/lookup field on CustomerInfo, not something a
    checkout typo lands in) and `notes` (free-text order note, unrelated to
    contact/address/invoice correctness)."""
    name: Optional[str] = None
    phone: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    county: Optional[str] = None
    postal_code: Optional[str] = None
    is_company: Optional[bool] = None
    company_name: Optional[str] = None
    cui: Optional[str] = None
    reg_com: Optional[str] = None
    administrator: Optional[str] = None
    company_address_strada: Optional[str] = None
    company_address_numar: Optional[str] = None
    company_address_bloc: Optional[str] = None
    company_address_scara: Optional[str] = None
    company_address_ap: Optional[str] = None
    company_address_oras: Optional[str] = None
    company_address_judet: Optional[str] = None
    company_address_cod_postal: Optional[str] = None


@api_router.patch("/admin/orders/{order_id}/customer")
async def admin_update_order_customer(
    request: Request,
    order_id: str,
    payload: OrderCustomerUpdate,
):
    """Corrects a typo the customer made in their contact/address/invoice
    details at checkout (e.g. a wrong phone digit, a misspelled street) -
    edits the frozen `customer` snapshot on this ONE order only. Does NOT
    touch the customer's standing account (db.users) - see PATCH
    /admin/customer-account/{email} for fixing that so future orders and
    the account page reflect the correction too.

    Only updates fields actually present (non-None) in the request body -
    a partial update, same spirit as PUT /auth/me - not a full overwrite of
    the customer sub-object. Same admin-auth pattern as the neighboring PUT
    /admin/orders/{order_id}/items and PATCH /admin/orders/{order_id}/
    courier above.

    Note: this does NOT re-push the corrected details to CRM for orders
    that already synced there (unlike the items endpoint's crm_items_dirty
    mechanism) - out of scope for this fix; CRM-side correction is handled
    separately if/when needed."""
    admin = await _require_admin(request)

    order_doc = await db.orders.find_one({"id": order_id})
    if not order_doc:
        raise HTTPException(status_code=404, detail="Comanda nu a fost găsită")

    changed_fields = {k: v for k, v in payload.dict().items() if v is not None}
    if not changed_fields:
        order_doc.pop("_id", None)
        return order_doc

    old_customer = order_doc.get("customer") or {}
    before = {k: old_customer.get(k) for k in changed_fields}

    update_dict = {f"customer.{k}": v for k, v in changed_fields.items()}
    await db.orders.update_one({"id": order_id}, {"$set": update_dict})

    await _write_audit_log(
        request, admin, action="order.customer_update", resource_type="order",
        resource_id=order_id,
        before=before,
        after=changed_fields,
    )

    updated = await db.orders.find_one({"id": order_id})
    updated.pop("_id", None)
    return updated


@api_router.get("/auth/orders/{order_id}/courier-tracking")
async def get_order_courier_tracking(order_id: str, request: Request):
    """Live FAN Courier delivery status for one of the logged-in customer's
    own orders - powers the AWB/tracking link shown on their account page
    (sent to them separately via WhatsApp/email). Same auth pattern as GET
    /auth/orders above, plus an explicit ownership check (order must belong
    to this customer's email) so one customer can never see another's
    tracking by guessing an order_id.

    Returns the same shape agb-crm already uses for AWB tracking (see
    courier_fan.track_awb / routes_courier.py there):
    {"events": [{"name", "location", "date"}, ...], "confirmation":
    {"name", "date"} | None}.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Token lipsă sau invalid")

    token = auth_header.replace("Bearer ", "")
    user = await _find_user_by_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Token invalid sau expirat")

    order = await db.orders.find_one({"id": order_id, "customer.email": user["email"]})
    if not order:
        raise HTTPException(status_code=404, detail="Comandă negăsită")

    awb = order.get("courier_awb_number")
    if not awb:
        raise HTTPException(status_code=404, detail="Comanda nu are AWB generat")

    try:
        result = await courier_fan.track_awb(awb)
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))

    return {
        "events": result.get("events") or [],
        "confirmation": result.get("confirmation"),
    }

# ==================== CLIENTS (Shopify customer import) ====================
# One-time bulk import of every existing Shopify customer + their full order
# history into our own db.clients / db.shopify_order_history collections, so
# the webshop admin panel can show a "Clienti" section without depending on
# Shopify staying online. This is NOT a continuous sync - Shopify remains the
# system of record for customers until it's deactivated, at which point a
# SECOND run (passing `since` = the `cutoff_recorded` value returned by this
# run's status once it's done) picks up only what changed since then.
# Idempotent by design: re-running upserts by shopify_customer_id / Shopify
# order id, it never duplicates - safe to run repeatedly.

SHOPIFY_ADMIN_API_VERSION = "2024-01"

clients_import_status = {
    "is_running": False,
    "since_used": None,
    "limit_used": None,
    "total_customers": 0,
    "done_customers": 0,
    "orders_imported": 0,
    "failed_customers": 0,
    "cutoff_recorded": None,
    "last_run": None,
    "error": None,
}


def _parse_shopify_datetime(value: Optional[str]) -> Optional[datetime]:
    """Shopify returns ISO8601 with a fixed offset (e.g. -04:00), which
    datetime.fromisoformat handles natively - no extra dependency needed."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        logger.warning(f"Could not parse Shopify datetime: {value}")
        return None


async def _fetch_shopify_customer_orders(http_client: httpx.AsyncClient, headers: dict, customer_id: str) -> list:
    """Fetch a single customer's FULL order history (status=any, so it
    includes open/closed/cancelled orders alike), following Shopify's
    Link-header pagination - same pattern as _fetch_all_collection_product_ids
    above."""
    orders = []
    page_info = None
    while True:
        url = (
            f"https://{SHOPIFY_STORE}/admin/api/{SHOPIFY_ADMIN_API_VERSION}/customers/"
            f"{customer_id}/orders.json?status=any&limit=250"
        )
        if page_info:
            url += f"&page_info={page_info}"

        response = await http_client.get(url, headers=headers, timeout=60.0)
        if response.status_code != 200:
            logger.error(f"Error fetching orders for Shopify customer {customer_id}: {response.text}")
            break

        data = response.json()
        orders.extend(data.get("orders", []))

        next_page_info = _extract_next_page_info(response.headers.get("Link", ""))
        if not next_page_info or next_page_info == page_info:
            break
        page_info = next_page_info
        await asyncio.sleep(0.15)

    return orders


async def _run_clients_import(since: Optional[str], limit: Optional[int]):
    """Background job: page through Shopify's customers.json (optionally
    filtered to only customers updated at/after `since`), and for each one
    upsert its profile plus its complete order history. `limit` caps the
    total number of customers processed, for a quick sanity-check run."""
    global clients_import_status
    if clients_import_status["is_running"]:
        return

    run_started_at = datetime.utcnow()
    clients_import_status.update({
        "is_running": True,
        "since_used": since,
        "limit_used": limit,
        "total_customers": 0,
        "done_customers": 0,
        "orders_imported": 0,
        "failed_customers": 0,
        "cutoff_recorded": None,
        "error": None,
    })

    run_status = "completed"
    run_error = None

    try:
        admin_token = os.environ.get('SHOPIFY_ADMIN_TOKEN', '') or SHOPIFY_ADMIN_TOKEN
        if not admin_token:
            raise RuntimeError("SHOPIFY_ADMIN_TOKEN not configured")

        headers = {
            "X-Shopify-Access-Token": admin_token,
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient() as http_client:
            customers = []
            page_info = None
            while True:
                url = f"https://{SHOPIFY_STORE}/admin/api/{SHOPIFY_ADMIN_API_VERSION}/customers.json?limit=250"
                if since:
                    url += f"&updated_at_min={since}"
                if page_info:
                    url += f"&page_info={page_info}"

                response = await http_client.get(url, headers=headers, timeout=60.0)
                if response.status_code != 200:
                    raise RuntimeError(f"Shopify customers.json error {response.status_code}: {response.text}")

                data = response.json()
                customers.extend(data.get("customers", []))

                if limit and len(customers) >= limit:
                    customers = customers[:limit]
                    break

                next_page_info = _extract_next_page_info(response.headers.get("Link", ""))
                if not next_page_info or next_page_info == page_info:
                    break
                page_info = next_page_info
                await asyncio.sleep(0.15)

            clients_import_status["total_customers"] = len(customers)

            for customer in customers:
                try:
                    shopify_customer_id = str(customer.get("id"))
                    default_address = customer.get("default_address") or {}
                    name = " ".join(
                        part for part in [customer.get("first_name"), customer.get("last_name")] if part
                    ).strip()
                    email = customer.get("email") or ""

                    client_doc = {
                        "id": shopify_customer_id,
                        "shopify_customer_id": shopify_customer_id,
                        "name": name,
                        "name_normalized": normalize_text(name),
                        "email": email,
                        "email_normalized": email.strip().lower(),
                        "phone": customer.get("phone") or default_address.get("phone") or "",
                        "address": {
                            "address": default_address.get("address1", "") or "",
                            "address2": default_address.get("address2", "") or "",
                            "city": default_address.get("city", "") or "",
                            "county": default_address.get("province", "") or "",
                            "postal_code": default_address.get("zip", "") or "",
                            "country": default_address.get("country", "") or "",
                        },
                        "orders_count": customer.get("orders_count", 0) or 0,
                        "total_spent": float(customer.get("total_spent") or 0),
                        "created_at": _parse_shopify_datetime(customer.get("created_at")),
                        "shopify_updated_at": _parse_shopify_datetime(customer.get("updated_at")),
                        "source": "shopify",
                        "last_synced_at": datetime.utcnow(),
                    }

                    await db.clients.update_one(
                        {"id": shopify_customer_id},
                        {
                            "$set": client_doc,
                            "$setOnInsert": {"imported_at": datetime.utcnow()},
                        },
                        upsert=True,
                    )

                    orders = await _fetch_shopify_customer_orders(http_client, headers, shopify_customer_id)
                    for order in orders:
                        order_id = str(order.get("id"))
                        line_items = [
                            {
                                "title": item.get("title"),
                                "quantity": item.get("quantity"),
                                "price": float(item.get("price") or 0),
                            }
                            for item in order.get("line_items", [])
                        ]
                        order_doc = {
                            "id": order_id,
                            "client_id": shopify_customer_id,
                            "customer_name": name or None,
                            "customer_email": email or None,
                            "customer_phone": client_doc["phone"] or None,
                            "order_number": order.get("order_number"),
                            "name": order.get("name"),
                            "created_at": _parse_shopify_datetime(order.get("created_at")),
                            "total_price": float(order.get("total_price") or 0),
                            "currency": order.get("currency", "RON"),
                            "financial_status": order.get("financial_status"),
                            "fulfillment_status": order.get("fulfillment_status"),
                            "line_items": line_items,
                        }
                        await db.shopify_order_history.update_one(
                            {"id": order_id},
                            {
                                "$set": order_doc,
                                "$setOnInsert": {"imported_at": datetime.utcnow()},
                            },
                            upsert=True,
                        )
                    clients_import_status["orders_imported"] += len(orders)

                except Exception as e:
                    logger.error(f"Clients import: failed on customer {customer.get('id')}: {e}")
                    clients_import_status["failed_customers"] += 1

                clients_import_status["done_customers"] += 1
                await asyncio.sleep(0.1)

    except Exception as e:
        run_status = "failed"
        run_error = str(e)
        clients_import_status["error"] = run_error
        logger.error(f"Clients import failed: {e}")

    finally:
        finished_at = datetime.utcnow()
        clients_import_status["is_running"] = False
        clients_import_status["last_run"] = finished_at.isoformat()
        # Safe cutoff for the NEXT incremental run: the moment THIS run
        # started, not when it finished - any customer change that happens
        # concurrently with this run is guaranteed to be updated_at >= this
        # value, so it'll be picked up next time even if this run raced it
        # (idempotent upserts mean re-processing it again is harmless).
        clients_import_status["cutoff_recorded"] = run_started_at.isoformat()

        await db.clients_import_runs.insert_one({
            "id": str(uuid.uuid4()),
            "started_at": run_started_at,
            "finished_at": finished_at,
            "since_used": since,
            "cutoff_recorded": run_started_at,
            "limit_used": limit,
            "total_customers": clients_import_status["total_customers"],
            "done_customers": clients_import_status["done_customers"],
            "orders_imported": clients_import_status["orders_imported"],
            "failed_customers": clients_import_status["failed_customers"],
            "status": run_status,
            "error": run_error,
        })


@api_router.post("/admin/clients/import-shopify")
async def admin_import_clients_shopify(
    request: Request,
    background_tasks: BackgroundTasks,
    since: Optional[str] = Body(default=None, embed=True),
    limit: Optional[int] = None,
):
    """Kick off the one-time Shopify customer + order-history import in the
    background. Pass `since` (ISO datetime, e.g. "2026-07-26T00:00:00+00:00")
    in the JSON body to only (re)import customers updated at/after that point
    - use this for the second run once Shopify is deactivated, passing the
    `cutoff_recorded` value from this run's final status. Pass `limit`
    (query param) to cap it to a handful of customers for a quick
    sanity-check run first."""
    admin = await _require_admin(request)
    if clients_import_status["is_running"]:
        raise HTTPException(status_code=409, detail="Importul rulează deja")
    background_tasks.add_task(_run_clients_import, since, limit)
    # As with /admin/migrate-images: this only logs that the import was
    # TRIGGERED with these params - the actual per-customer/per-order writes
    # happen later in the background task, well after this request returns.
    # See db.clients_import_runs / GET .../status for the run's own outcome.
    await _write_audit_log(
        request, admin, action="clients.import_shopify_trigger", resource_type="client",
        after={"since": since, "limit": limit},
    )
    return {"message": "Import pornit", "since": since, "limit": limit}


@api_router.get("/admin/clients/import-shopify/status")
async def admin_import_clients_shopify_status(request: Request):
    """Poll this while the import runs. If this process hasn't run an import
    since it last started (e.g. right after a restart, which can easily
    happen between the first run and the second run weeks/months later),
    fall back to the last persisted run in db.clients_import_runs so the
    cutoff/history isn't lost."""
    await _require_admin(request)
    if not clients_import_status["is_running"] and clients_import_status["last_run"] is None:
        last = await db.clients_import_runs.find_one({}, sort=[("finished_at", -1)])
        if last:
            last.pop("_id", None)
            return last
    return clients_import_status


# ==================== SHOPIFY FULL ORDER HISTORY IMPORT ====================
# Direct /orders.json import (every order in the store, status=any) - unlike
# _run_clients_import above (which only reaches orders tied to a saved
# Shopify customer, via customers/{id}/orders.json), this also captures
# guest checkouts. Upserts into the SAME db.shopify_order_history collection
# by Shopify order id, so it naturally merges with/enriches whatever the
# customer-scoped import already put there - no duplicates, no separate
# collection.
#
# Deliberately independent of agb-crm: CRM already has its own Shopify
# orders webhook pipeline (routes_shopify.py there, a large chunk of these
# orders already exist there) - this function must NEVER call CRM_API_URL
# or anything under /integrations/*, on purpose, per explicit instruction.

shopify_orders_import_status = {
    "is_running": False,
    "total_orders": 0,
    "done_orders": 0,
    "failed_orders": 0,
    "last_run": None,
    "error": None,
}


async def _run_shopify_full_orders_import():
    global shopify_orders_import_status
    if shopify_orders_import_status["is_running"]:
        return

    run_started_at = datetime.utcnow()
    shopify_orders_import_status.update({
        "is_running": True,
        "total_orders": 0,
        "done_orders": 0,
        "failed_orders": 0,
        "error": None,
    })

    run_status = "completed"
    run_error = None

    try:
        admin_token = os.environ.get('SHOPIFY_ADMIN_TOKEN', '') or SHOPIFY_ADMIN_TOKEN
        if not admin_token:
            raise RuntimeError("SHOPIFY_ADMIN_TOKEN not configured")

        headers = {
            "X-Shopify-Access-Token": admin_token,
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient() as http_client:
            page_info = None
            while True:
                url = (
                    f"https://{SHOPIFY_STORE}/admin/api/{SHOPIFY_ADMIN_API_VERSION}/orders.json"
                    f"?status=any&limit=250"
                )
                if page_info:
                    url += f"&page_info={page_info}"

                response = await http_client.get(url, headers=headers, timeout=60.0)
                if response.status_code != 200:
                    raise RuntimeError(f"Shopify orders.json error {response.status_code}: {response.text}")

                data = response.json()
                orders = data.get("orders", [])
                shopify_orders_import_status["total_orders"] += len(orders)

                for order in orders:
                    try:
                        order_id = str(order.get("id"))
                        customer = order.get("customer") or {}
                        shopify_customer_id = str(customer.get("id")) if customer.get("id") else None
                        customer_name = " ".join(
                            part for part in [customer.get("first_name"), customer.get("last_name")] if part
                        ).strip()
                        shipping = order.get("shipping_address") or {}
                        customer_email = (order.get("email") or order.get("contact_email") or "").strip()
                        customer_phone = order.get("phone") or shipping.get("phone") or customer.get("phone") or ""

                        line_items = [
                            {
                                "title": item.get("title"),
                                "quantity": item.get("quantity"),
                                "price": float(item.get("price") or 0),
                            }
                            for item in order.get("line_items", [])
                        ]
                        order_doc = {
                            "id": order_id,
                            "client_id": shopify_customer_id,
                            "customer_name": customer_name or None,
                            "customer_email": customer_email or None,
                            "customer_phone": customer_phone or None,
                            "order_number": order.get("order_number"),
                            "name": order.get("name"),
                            "created_at": _parse_shopify_datetime(order.get("created_at")),
                            "total_price": float(order.get("total_price") or 0),
                            "currency": order.get("currency", "RON"),
                            "financial_status": order.get("financial_status"),
                            "fulfillment_status": order.get("fulfillment_status"),
                            "line_items": line_items,
                        }
                        await db.shopify_order_history.update_one(
                            {"id": order_id},
                            {
                                "$set": order_doc,
                                "$setOnInsert": {"imported_at": datetime.utcnow()},
                            },
                            upsert=True,
                        )
                        shopify_orders_import_status["done_orders"] += 1
                    except Exception as e:
                        logger.error(f"Shopify orders import: failed on order {order.get('id')}: {e}")
                        shopify_orders_import_status["failed_orders"] += 1

                next_page_info = _extract_next_page_info(response.headers.get("Link", ""))
                if not next_page_info or next_page_info == page_info:
                    break
                page_info = next_page_info
                await asyncio.sleep(0.15)

    except Exception as e:
        run_status = "failed"
        run_error = str(e)
        shopify_orders_import_status["error"] = run_error
        logger.error(f"Shopify full orders import failed: {e}")

    finally:
        finished_at = datetime.utcnow()
        shopify_orders_import_status["is_running"] = False
        shopify_orders_import_status["last_run"] = finished_at.isoformat()

        await db.shopify_orders_import_runs.insert_one({
            "id": str(uuid.uuid4()),
            "started_at": run_started_at,
            "finished_at": finished_at,
            "total_orders": shopify_orders_import_status["total_orders"],
            "done_orders": shopify_orders_import_status["done_orders"],
            "failed_orders": shopify_orders_import_status["failed_orders"],
            "status": run_status,
            "error": run_error,
        })


@api_router.post("/admin/shopify-orders-import/start")
async def admin_import_shopify_orders(request: Request, background_tasks: BackgroundTasks):
    """Kick off a full, direct import of every order in the Shopify store
    (status=any - open/closed/cancelled alike), including guest checkouts
    that _run_clients_import's customer-scoped approach can never reach.
    Idempotent (upserts by Shopify order id) - safe to re-run any time to
    pick up new orders."""
    admin = await _require_admin(request)
    if shopify_orders_import_status["is_running"]:
        raise HTTPException(status_code=409, detail="Importul rulează deja")
    background_tasks.add_task(_run_shopify_full_orders_import)
    # Trigger-only log, same reasoning as /admin/clients/import-shopify above.
    await _write_audit_log(
        request, admin, action="shopify_orders.import_trigger", resource_type="order",
    )
    return {"message": "Import pornit"}


@api_router.get("/admin/shopify-orders-import/status")
async def admin_import_shopify_orders_status(request: Request):
    await _require_admin(request)
    if not shopify_orders_import_status["is_running"] and shopify_orders_import_status["last_run"] is None:
        last = await db.shopify_orders_import_runs.find_one({}, sort=[("finished_at", -1)])
        if last:
            last.pop("_id", None)
            return last
    return shopify_orders_import_status


@api_router.get("/admin/clients")
async def admin_list_clients(request: Request, search: Optional[str] = None, limit: int = 50, skip: int = 0):
    """Paginated list of imported Shopify clients, for the admin 'Clienti' list view."""
    await _require_admin(request)

    query = {}
    if search:
        term = normalize_text(search)
        query["$or"] = [
            {"name_normalized": {"$regex": term, "$options": "i"}},
            {"email_normalized": {"$regex": term, "$options": "i"}},
        ]

    total = await db.clients.count_documents(query)
    # Newest-created client first, so admin sees recently-registered/-imported
    # clients up top instead of alphabetically buried among 278+ others.
    # Verified all db.clients docs already carry created_at (see import flow
    # above) before switching off name_normalized ascending.
    cursor = db.clients.find(query).sort("created_at", -1).skip(skip).limit(limit)
    clients = await cursor.to_list(limit)
    for c in clients:
        c.pop("_id", None)
    return {"total": total, "clients": clients}


def _shopify_order_to_merged(o: dict, clients_by_id: Optional[dict] = None) -> dict:
    """Normalizes a db.shopify_order_history doc into the shared merged-order
    shape used by the admin client-detail view, the admin order-history list,
    and the customer's own order history.

    `customer_name`/`customer_email` are only populated directly on the
    order doc by the newer full-store import - older customer-scoped-import
    records may only carry `client_id`, so `clients_by_id` (a batch-fetched
    {client_id: client_doc} map) is an optional fallback to resolve them."""
    client = (clients_by_id or {}).get(o.get("client_id")) if o.get("client_id") else None
    return {
        "source": "shopify",
        "order_id": o.get("id"),
        "order_number": o.get("name") or o.get("order_number"),
        "date": o.get("created_at"),
        "customer_name": o.get("customer_name") or (client.get("name") if client else None),
        "customer_email": o.get("customer_email") or (client.get("email") if client else None),
        "total": o.get("total_price"),
        "currency": o.get("currency", "RON"),
        "financial_status": o.get("financial_status"),
        "fulfillment_status": o.get("fulfillment_status"),
        "payment_method": None,
        "line_items": o.get("line_items", []),
    }


def _native_order_to_merged(o: dict) -> dict:
    """Normalizes a db.orders doc (native webshop checkout) into the shared
    merged-order shape - see _shopify_order_to_merged."""
    customer = o.get("customer") or {}
    line_items = [
        {
            "title": item.get("product_name"),
            "quantity": item.get("quantity"),
            "price": item.get("price"),
        }
        for item in o.get("items", [])
    ]
    return {
        "source": "native",
        "order_id": o.get("id"),
        "order_number": None,
        "date": o.get("created_at"),
        "customer_name": customer.get("name"),
        "customer_email": customer.get("email"),
        "total": o.get("total"),
        "currency": "RON",
        "financial_status": o.get("status"),
        "fulfillment_status": None,
        "payment_method": o.get("payment_method"),
        "line_items": line_items,
    }


def _merged_order_sort_key(entry: dict):
    value = entry.get("date")
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return _parse_shopify_datetime(value) or datetime.min
    return datetime.min


# ==================== ANALYTICS (sales + traffic/conversion) ====================
# Part 1 (sales) is derived entirely from EXISTING order data (db.orders +
# db.shopify_order_history, via the same native/shopify merge helpers
# above) - no new tracking needed. Part 2 (traffic/conversion) is brand new:
# a public pageview beacon fired by agb-webshop
# (localStorage `agb_analytics_session_id`, NOT a cookie - matches this
# app's cookie-less, localStorage-only session architecture) feeding
# db.analytics_pageviews, plus a best-effort conversion link recorded in
# create_order (see db.analytics_conversions and OrderCreate.analytics_
# session_id above). Both admin-aggregation endpoints below are gated the
# same way as every other /admin/* route - see _require_admin.


def _analytics_order_datetime(entry: dict) -> Optional[datetime]:
    """Normalizes a merged order entry's `date` field (see
    _native_order_to_merged/_shopify_order_to_merged) to a naive UTC
    datetime for analytics date-range filtering/bucketing. Native orders
    store a naive UTC datetime (Order.created_at's datetime.utcnow()
    default); imported Shopify orders may carry a timezone-aware datetime
    (see _parse_shopify_datetime) or, in older records, a raw ISO string -
    without normalizing both to the same (naive UTC) shape, comparing them
    against the naive `start`/`end_exclusive` bounds from
    _parse_analytics_date_range would raise. Returns None when the value
    can't be parsed at all, so the caller can simply exclude that entry from
    date-bounded aggregates rather than risk a crash or a silently-wrong
    bucket."""
    value = entry.get("date")
    if isinstance(value, str):
        value = _parse_shopify_datetime(value)
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _analytics_bucket_key(value: datetime, granularity: str) -> str:
    """Buckets a naive-UTC datetime into a 'YYYY-MM-DD' or 'YYYY-MM' string
    key, shared by revenue_over_time (sales) and pageviews_over_time
    (traffic)."""
    return value.strftime("%Y-%m") if granularity == "month" else value.strftime("%Y-%m-%d")


def _parse_analytics_date_range(date_from: str, date_to: str, granularity: str):
    """Shared by GET /admin/analytics/sales and GET /admin/analytics/traffic:
    parses/validates the from=/to=/granularity= query params (from/to as
    plain YYYY-MM-DD calendar dates, both inclusive) into a
    [start, end_exclusive) naive-UTC datetime range plus the validated
    granularity - or raises a clear 400, rather than FastAPI's generic 422
    validation-error page, since these are simple enough to explain in
    Romanian directly."""
    if granularity not in ("day", "month"):
        raise HTTPException(status_code=400, detail="granularity trebuie să fie 'day' sau 'month'")
    try:
        start = datetime.strptime(date_from, "%Y-%m-%d")
        end_day = datetime.strptime(date_to, "%Y-%m-%d")
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Format dată invalid, folosiți YYYY-MM-DD pentru from/to")
    end_exclusive = end_day + timedelta(days=1)
    if end_exclusive <= start:
        raise HTTPException(status_code=400, detail="Intervalul 'to' trebuie să fie după 'from'")
    return start, end_exclusive, granularity


@api_router.get("/admin/analytics/sales")
async def admin_analytics_sales(
    request: Request,
    date_from: str = Query(..., alias="from"),
    date_to: str = Query(..., alias="to"),
    granularity: str = Query("day"),
):
    """Sales analytics over [from, to] (inclusive calendar dates), pulled
    from BOTH native webshop/mobile orders (db.orders) and imported
    historical Shopify orders (db.shopify_order_history) - reuses the exact
    same fetch (_fetch_native_and_shopify_orders_raw) and per-source
    normalization (_native_order_to_merged/_shopify_order_to_merged) as
    GET /admin/orders/history rather than reimplementing the merge.

    - revenue_over_time: total `total` and order count, bucketed by day or
      month per `granularity`.
    - top_products: order line items aggregated by product (by product_id
      when available - only native db.orders items carry one; older
      Shopify line items only have a title, so those are grouped by title
      instead), top 10 by revenue.
    - aov: average order value (total revenue / order count) over the
      period.
    - orders_by_status: count grouped by the merged entry's unified
      `financial_status` (native orders' own `status`; Shopify orders'
      `financial_status`), same field GET /admin/orders/history already
      exposes.
    - new_vs_returning: for each in-period order matched by customer email
      (case-insensitive, same matching already used by
      GET /admin/clients/{client_id}), whether it's that customer's very
      first order ever (across ALL orders, not just this period) or a
      repeat. Orders without a usable email are excluded from this one
      breakdown (can't be matched to a customer at all).
    """
    await _require_admin(request)
    start, end_exclusive, granularity = _parse_analytics_date_range(date_from, date_to, granularity)

    native_orders, shopify_orders, clients_by_id = await _fetch_native_and_shopify_orders_raw()

    all_entries = [
        (_native_order_to_merged(o), o) for o in native_orders
    ] + [
        (_shopify_order_to_merged(o, clients_by_id), o) for o in shopify_orders
    ]

    # Global (all-time, not period-bounded) earliest order per customer
    # email - needed to classify an in-period order as new-vs-returning
    # below regardless of whether that customer's actual first order falls
    # inside or outside the requested period.
    earliest_by_email: Dict[str, tuple] = {}
    for merged, _raw in all_entries:
        dt = _analytics_order_datetime(merged)
        email = (merged.get("customer_email") or "").strip().lower()
        if not dt or not email:
            continue
        current = earliest_by_email.get(email)
        if current is None or dt < current[0]:
            earliest_by_email[email] = (dt, merged["source"], merged["order_id"])

    in_range = [
        (merged, raw) for merged, raw in all_entries
        if (dt := _analytics_order_datetime(merged)) is not None and start <= dt < end_exclusive
    ]

    # revenue_over_time
    buckets: Dict[str, dict] = {}
    for merged, _raw in in_range:
        key = _analytics_bucket_key(_analytics_order_datetime(merged), granularity)
        bucket = buckets.setdefault(key, {"date": key, "revenue": 0.0, "order_count": 0})
        bucket["revenue"] += float(merged.get("total") or 0)
        bucket["order_count"] += 1
    revenue_over_time = [buckets[key] for key in sorted(buckets.keys())]
    for bucket in revenue_over_time:
        bucket["revenue"] = round(bucket["revenue"], 2)

    # orders_by_status
    orders_by_status: Dict[str, int] = {}
    for merged, _raw in in_range:
        status = merged.get("financial_status") or "unknown"
        orders_by_status[status] = orders_by_status.get(status, 0) + 1

    # aov
    order_count = len(in_range)
    total_revenue = sum(float(merged.get("total") or 0) for merged, _raw in in_range)
    aov = round(total_revenue / order_count, 2) if order_count else 0.0

    # top_products
    product_agg: Dict[str, dict] = {}
    for merged, raw in in_range:
        if merged["source"] == "native":
            for item in raw.get("items", []) or []:
                product_id = item.get("product_id")
                title = item.get("product_name") or "Produs necunoscut"
                quantity = item.get("quantity") or 0
                price = float(item.get("price") or 0)
                key = product_id or f"title::{title}"
                agg = product_agg.setdefault(
                    key, {"product_id": product_id, "title": title, "quantity": 0, "revenue": 0.0}
                )
                agg["quantity"] += quantity
                agg["revenue"] += price * quantity
        else:
            for item in raw.get("line_items", []) or []:
                title = item.get("title") or "Produs necunoscut"
                quantity = item.get("quantity") or 0
                price = float(item.get("price") or 0)
                key = f"title::{title}"
                agg = product_agg.setdefault(
                    key, {"product_id": None, "title": title, "quantity": 0, "revenue": 0.0}
                )
                agg["quantity"] += quantity
                agg["revenue"] += price * quantity
    top_products = sorted(product_agg.values(), key=lambda a: a["revenue"], reverse=True)[:10]
    for product in top_products:
        product["revenue"] = round(product["revenue"], 2)

    # new_vs_returning
    new_count = 0
    returning_count = 0
    for merged, _raw in in_range:
        email = (merged.get("customer_email") or "").strip().lower()
        if not email:
            continue
        earliest = earliest_by_email.get(email)
        if earliest and earliest[1] == merged["source"] and earliest[2] == merged["order_id"]:
            new_count += 1
        else:
            returning_count += 1

    return {
        "revenue_over_time": revenue_over_time,
        "top_products": top_products,
        "aov": aov,
        "orders_by_status": orders_by_status,
        "new_vs_returning": {"new": new_count, "returning": returning_count},
    }


_ANALYTICS_FIELD_MAX_LEN = 500


def _sanitize_analytics_field(value: Optional[str]) -> Optional[str]:
    """Trims to _ANALYTICS_FIELD_MAX_LEN chars (abuse/storage-bloat guard on
    an unauthenticated, publicly-writable endpoint) and normalizes a blank/
    whitespace-only string to None - shared by every optional string field
    on POST /analytics/pageview."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    return value[:_ANALYTICS_FIELD_MAX_LEN]


class PageviewCreate(BaseModel):
    """POST /analytics/pageview body - public/no-auth beacon fired by
    agb-webshop on every route change. session_id/path are typed Optional
    (not required) so a request missing either gets our own clear, fast 400
    in the handler below instead of FastAPI's generic 422 validation-error
    page; every other field is optional metadata that may legitimately be
    absent (no referrer on a direct visit, no utm_* outside a campaign
    link)."""
    session_id: Optional[str] = None
    path: Optional[str] = None
    referrer: Optional[str] = None
    utm_source: Optional[str] = None
    utm_medium: Optional[str] = None
    utm_campaign: Optional[str] = None


@api_router.post("/analytics/pageview", status_code=204)
async def track_pageview(payload: PageviewCreate):
    """Public, unauthenticated pageview beacon. Always intended to respond
    fast and never block/break page rendering on the caller's side:
    session_id/path are the only two required fields (fast 400 if either is
    missing/blank after sanitizing, not FastAPI's default 422 page) -
    everything else is best-effort sanitized (length-capped, blank
    normalized to None) rather than strictly validated, and a DB failure is
    swallowed (logged, not raised) rather than ever surfacing as a 500."""
    session_id = _sanitize_analytics_field(payload.session_id)
    path = _sanitize_analytics_field(payload.path)
    if not session_id or not path:
        raise HTTPException(status_code=400, detail="session_id și path sunt obligatorii")

    doc = {
        "_id": str(uuid.uuid4()),
        "session_id": session_id,
        "path": path,
        "referrer": _sanitize_analytics_field(payload.referrer),
        "utm_source": _sanitize_analytics_field(payload.utm_source),
        "utm_medium": _sanitize_analytics_field(payload.utm_medium),
        "utm_campaign": _sanitize_analytics_field(payload.utm_campaign),
        "created_at": datetime.utcnow(),
    }
    try:
        await db.analytics_pageviews.insert_one(doc)
    except Exception:
        logger.exception("Failed to record analytics pageview")
    return Response(status_code=204)


@api_router.get("/admin/analytics/traffic")
async def admin_analytics_traffic(
    request: Request,
    date_from: str = Query(..., alias="from"),
    date_to: str = Query(..., alias="to"),
    granularity: str = Query("day"),
):
    """Traffic/conversion analytics over [from, to] (inclusive calendar
    dates), from db.analytics_pageviews + db.analytics_conversions (see
    POST /analytics/pageview and create_order's analytics_session_id
    handling). Uses real Mongo aggregation pipelines/distinct (not an
    in-process merge like GET /admin/analytics/sales) since pageview volume
    can grow far larger than this store's order count - see the
    (session_id, created_at) indexes added in startup_event.

    - pageviews_over_time: count bucketed by day or month per
      `granularity`.
    - top_pages: top 10 `path` values by pageview count.
    - top_referrers: top 10 non-empty `referrer` values by pageview count.
    - unique_sessions: distinct session_id with >=1 pageview in range.
    - conversions: distinct session_id with >=1 analytics_conversions entry
      in range.
    - conversion_rate: conversions / unique_sessions, 0 if unique_sessions
      is 0 (never divides by zero).
    """
    await _require_admin(request)
    start, end_exclusive, granularity = _parse_analytics_date_range(date_from, date_to, granularity)
    date_format = "%Y-%m" if granularity == "month" else "%Y-%m-%d"
    match_range = {"created_at": {"$gte": start, "$lt": end_exclusive}}

    pageviews_pipeline = [
        {"$match": match_range},
        {"$group": {
            "_id": {"$dateToString": {"format": date_format, "date": "$created_at"}},
            "count": {"$sum": 1},
        }},
        {"$sort": {"_id": 1}},
    ]
    pageviews_rows = await db.analytics_pageviews.aggregate(pageviews_pipeline).to_list(length=None)
    pageviews_over_time = [{"date": r["_id"], "count": r["count"]} for r in pageviews_rows]

    top_pages_pipeline = [
        {"$match": match_range},
        {"$group": {"_id": "$path", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": 10},
    ]
    top_pages_rows = await db.analytics_pageviews.aggregate(top_pages_pipeline).to_list(length=None)
    top_pages = [{"path": r["_id"], "count": r["count"]} for r in top_pages_rows]

    top_referrers_pipeline = [
        {"$match": {**match_range, "referrer": {"$nin": [None, ""]}}},
        {"$group": {"_id": "$referrer", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": 10},
    ]
    top_referrers_rows = await db.analytics_pageviews.aggregate(top_referrers_pipeline).to_list(length=None)
    top_referrers = [{"referrer": r["_id"], "count": r["count"]} for r in top_referrers_rows]

    unique_session_ids = await db.analytics_pageviews.distinct("session_id", match_range)
    unique_sessions = len(unique_session_ids)

    conversion_session_ids = await db.analytics_conversions.distinct("session_id", match_range)
    conversions = len(conversion_session_ids)

    conversion_rate = round(conversions / unique_sessions, 4) if unique_sessions else 0

    return {
        "pageviews_over_time": pageviews_over_time,
        "top_pages": top_pages,
        "top_referrers": top_referrers,
        "unique_sessions": unique_sessions,
        "conversions": conversions,
        "conversion_rate": conversion_rate,
    }


@api_router.get("/admin/clients/{client_id}")
async def admin_get_client_detail(client_id: str, request: Request):
    """Full client detail: profile + the COMPLETE order history ('totalitatea
    comenzilor'), merging (a) the imported historical Shopify orders and (b)
    any native webshop orders (db.orders) matched by customer email
    (case-insensitive - native orders have no client_id link), each entry
    tagged with its `source` ("shopify"/"native"), sorted newest-first."""
    await _require_admin(request)

    client = await db.clients.find_one({"id": client_id})
    if not client:
        raise HTTPException(status_code=404, detail="Client inexistent")
    client.pop("_id", None)

    merged_orders = []

    shopify_orders = await db.shopify_order_history.find({"client_id": client_id}).to_list(1000)
    merged_orders.extend(_shopify_order_to_merged(o) for o in shopify_orders)

    email = client.get("email_normalized") or (client.get("email") or "").strip().lower()
    native_orders = []
    if email:
        native_orders = await db.orders.find(
            {"customer.email": {"$regex": f"^{re.escape(email)}$", "$options": "i"}}
        ).to_list(1000)
    merged_orders.extend(_native_order_to_merged(o) for o in native_orders)

    merged_orders.sort(key=_merged_order_sort_key, reverse=True)

    return {
        "client": client,
        "orders": merged_orders,
        "orders_count_total": len(merged_orders),
        "shopify_orders_count": len(shopify_orders),
        "native_orders_count": len(native_orders),
    }


@api_router.get("/admin/customer-account")
async def admin_get_customer_account(email: str, request: Request):
    """Look up a webshop ACCOUNT (db.users - NOT db.clients, see
    admin_get_client_detail above for that separate collection) by exact
    email. Backs CRM's "Conectează cont web" feature: staff previews a
    webshop account before linking it to a CRM client. "Linking" itself
    happens entirely on the CRM side (setting that client's email to this
    account's email) - agb-crm's _resolve_or_create_client already matches
    an existing client by email on every webshop/mobile order or equipment
    sync, so once linked, future syncs from this account land on the right
    client with no extra mechanism needed here.

    Exact match only, same normalization as login/register - deliberately
    no fuzzy name/company search across accounts (that was considered and
    rejected: a wrong auto-match here would misroute a customer's real
    orders to someone else's CRM record)."""
    admin = await _require_admin(request)
    _enforce_rate_limit(
        f"admin:customer-account-lookup:{admin['id']}", ADMIN_ACTION_LIMIT, ADMIN_ACTION_WINDOW_SECONDS,
        "Prea multe căutări de cont recent. Încearcă din nou mai târziu.",
    )
    normalized_email = email.lower().strip()
    user = await db.users.find_one({"email": normalized_email})
    if not user:
        raise HTTPException(status_code=404, detail="Niciun cont web găsit cu acest email.")
    return _serialize_user(user)


@api_router.patch("/admin/customer-account/{email}")
async def admin_update_customer_account(
    email: str,
    update_data: UserUpdate,
    request: Request,
    background_tasks: BackgroundTasks,
):
    """Corrects a customer's STANDING ACCOUNT profile (db.users) - e.g. staff
    fixing a typo'd phone/address so it's right on every FUTURE order and on
    the customer's own account page from now on, not just retroactively on
    one already-placed order (see PATCH /admin/orders/{order_id}/customer
    for that order-scoped fix).

    Reuses the exact same partial-update + address/company_address
    recombination logic as the customer's own PUT /auth/me, via the shared
    _build_user_profile_update_dict helper, so the two call sites can't
    silently drift apart.

    Looked up by exact email (path param, same normalization as login/
    register and the neighboring GET /admin/customer-account above) - 404
    if no account exists with that email."""
    admin = await _require_admin(request)

    normalized_email = email.lower().strip()
    user = await db.users.find_one({"email": normalized_email})
    if not user:
        raise HTTPException(status_code=404, detail="Niciun cont web găsit cu acest email.")

    update_dict = _build_user_profile_update_dict(user, update_data)

    if update_dict:
        await db.users.update_one({"id": user["id"]}, {"$set": update_dict})

    updated_user = await db.users.find_one({"id": user["id"]})

    await _write_audit_log(
        request, admin, action="customer_account.update", resource_type="user",
        resource_id=user["id"],
        before={k: user.get(k) for k in update_dict},
        after=update_dict,
    )

    # Sync to CRM (fire-and-forget, never blocks/fails the response above) -
    # same call as PUT /auth/me makes on a self-service update, so a staff
    # correction reaches CRM exactly the same way a customer's own edit does.
    background_tasks.add_task(sync_account_to_crm, updated_user)

    return _serialize_user(updated_user)

# ==================== EQUIPMENT/UTILAJE ENDPOINTS ====================

def _equipment_match_key(model: str, chassis_serial: str) -> tuple:
    return (model.strip().lower(), (chassis_serial or "").strip().lower())

async def parse_equipment_from_shopify_notes(notes: str, existing_equipment: list = None) -> list:
    """Parse equipment from Shopify customer notes format.

    NOTE: no longer used by GET /auth/equipment (that resync-from-Shopify
    path was removed - see get_user_equipment below for why). Still used by
    the admin-only GET /debug/customer-notes/{email} diagnostic endpoint,
    kept as-is for that.

    Re-parses on every read (so admin edits made directly in Shopify notes
    show up), but reuses the id/created_at of any existing local entry that
    matches by (model, chassis_serial) instead of always minting a fresh
    uuid4 - otherwise ids churn on every GET and break links/routes that
    reference a specific equipment id (e.g. the edit page).
    """
    equipment_list = []

    if not notes or "UTILAJELE CLIENTULUI:" not in notes:
        return equipment_list

    existing_by_key = {}
    for eq in existing_equipment or []:
        key = _equipment_match_key(eq.get("model", ""), eq.get("chassis_serial", ""))
        existing_by_key[key] = eq

    try:
        # Split by equipment entries (numbered lines like "1. 6820")
        lines = notes.split('\n')
        current_equipment = None

        for line in lines:
            line = line.strip()

            # Check for new equipment entry (starts with number followed by .)
            if line and line[0].isdigit() and '. ' in line:
                # Save previous equipment if exists
                if current_equipment:
                    equipment_list.append(current_equipment)

                # Extract model name
                parts = line.split('. ', 1)
                model = parts[1] if len(parts) > 1 else line

                current_equipment = {
                    "id": str(uuid.uuid4()),
                    "brand": "",
                    "model": model,
                    "chassis_serial": "",
                    "engine_serial": "",
                    "engine_type": "",
                    "transmission_type": "",
                    "front_axle_model": "",
                    "features": [],
                    "created_at": datetime.utcnow().isoformat(),
                    "synced_from_shopify": True
                }
            
            # Parse equipment details (lines starting with • or spaces + •)
            elif current_equipment and '•' in line:
                detail = line.replace('•', '').strip()
                
                if 'Marca:' in detail:
                    current_equipment["brand"] = detail.split('Marca:')[1].strip()
                elif 'Serie șasiu:' in detail:
                    current_equipment["chassis_serial"] = detail.split('Serie șasiu:')[1].strip()
                elif 'Serie motor:' in detail:
                    current_equipment["engine_serial"] = detail.split('Serie motor:')[1].strip()
                elif 'Model motor:' in detail:
                    current_equipment["engine_type"] = detail.split('Model motor:')[1].strip()
                elif 'Model cutie:' in detail:
                    current_equipment["transmission_type"] = detail.split('Model cutie:')[1].strip()
                elif 'Model punte față:' in detail:
                    current_equipment["front_axle_model"] = detail.split('Model punte față:')[1].strip()
                elif 'Echipare:' in detail:
                    features_str = detail.split('Echipare:')[1].strip()
                    current_equipment["features"] = [f.strip() for f in features_str.split(',') if f.strip()]
        
        # Don't forget the last equipment
        if current_equipment:
            equipment_list.append(current_equipment)

        # Reuse ids/created_at from matching existing entries so links to a
        # specific equipment id keep working across repeated GETs.
        for eq in equipment_list:
            key = _equipment_match_key(eq.get("model", ""), eq.get("chassis_serial", ""))
            match = existing_by_key.get(key)
            if match:
                eq["id"] = match.get("id", eq["id"])
                eq["created_at"] = match.get("created_at", eq["created_at"])

        return equipment_list
    except Exception as e:
        logger.error(f"Error parsing equipment from Shopify notes: {e}")
        return []

async def get_shopify_customer_notes(user_email: str) -> str:
    """Get customer notes from Shopify using Admin API"""
    try:
        # Get token directly from environment
        admin_token = os.environ.get('SHOPIFY_ADMIN_TOKEN', '') or SHOPIFY_ADMIN_TOKEN
        
        if not admin_token:
            logger.warning("SHOPIFY_ADMIN_TOKEN not set")
            return ""
        
        store = os.environ.get('SHOPIFY_STORE', '43ca3c-3.myshopify.com')
        logger.info(f"Fetching Shopify notes for {user_email} using store: {store}")
        
        headers = {
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": admin_token
        }
        
        # Use REST API for customer search - more reliable
        search_url = f"https://{store}/admin/api/2024-01/customers/search.json?query=email:{user_email}"
        logger.info(f"Shopify search URL: {search_url}")
        
        async with httpx.AsyncClient() as client:
            response = await client.get(
                search_url,
                headers=headers,
                timeout=30.0
            )
            
            logger.info(f"Shopify response status: {response.status_code}")
            data = response.json()
            
            customers = data.get("customers", [])
            logger.info(f"Found {len(customers)} customers")
            
            if customers:
                note = customers[0].get("note", "") or ""
                logger.info(f"Shopify notes for {user_email}: {note[:100] if note else 'empty'}...")
                return note
            else:
                logger.info(f"No Shopify customer found for {user_email}")
                if "errors" in data:
                    logger.error(f"Shopify API errors: {data['errors']}")
        
        return ""
    except Exception as e:
        logger.error(f"Error getting Shopify customer notes: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return ""

async def sync_equipment_to_shopify_notes(user_email: str, equipment_list: list):
    """Sync user's equipment to Shopify customer notes"""
    try:
        if not SHOPIFY_ADMIN_TOKEN:
            logger.warning("SHOPIFY_ADMIN_TOKEN not configured - skipping Shopify sync")
            return False
        
        # Build notes text - ALWAYS include all fields as template
        if not equipment_list:
            notes_text = "🚜 UTILAJELE CLIENTULUI:\n(Niciun utilaj adăugat)"
        else:
            notes_lines = ["🚜 UTILAJELE CLIENTULUI:", ""]
            for i, eq in enumerate(equipment_list, 1):
                notes_lines.append(f"{i}. {eq.get('model', 'N/A')}")
                # Always include all fields, even if empty (as template for admin to fill)
                notes_lines.append(f"   • Marca: {eq.get('brand', '')}")
                notes_lines.append(f"   • Serie șasiu: {eq.get('chassis_serial', '')}")
                notes_lines.append(f"   • Serie motor: {eq.get('engine_serial', '')}")
                notes_lines.append(f"   • Model motor: {eq.get('engine_type', '')}")
                notes_lines.append(f"   • Model cutie: {eq.get('transmission_type', '')}")
                notes_lines.append(f"   • Model punte față: {eq.get('front_axle_model', '')}")
                features = eq.get('features', [])
                features_str = ', '.join(features) if features else ''
                notes_lines.append(f"   • Echipare: {features_str}")
                notes_lines.append("")
            notes_text = "\n".join(notes_lines)
        
        # Find Shopify customer by email
        headers = {
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": SHOPIFY_ADMIN_TOKEN
        }
        
        # Search for customer
        search_query = """
        query findCustomer($email: String!) {
            customers(first: 1, query: $email) {
                edges {
                    node {
                        id
                        email
                        note
                    }
                }
            }
        }
        """
        
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"https://{SHOPIFY_STORE}/admin/api/2024-01/graphql.json",
                json={"query": search_query, "variables": {"email": f"email:{user_email}"}},
                headers=headers,
                timeout=30.0
            )
            
            data = response.json()
            customers = data.get("data", {}).get("customers", {}).get("edges", [])
            
            if not customers:
                logger.info(f"Customer {user_email} not found in Shopify - cannot sync equipment")
                return False
            
            customer_id = customers[0]["node"]["id"]
            
            # Update customer notes
            update_mutation = """
            mutation updateCustomer($input: CustomerInput!) {
                customerUpdate(input: $input) {
                    customer {
                        id
                        note
                    }
                    userErrors {
                        field
                        message
                    }
                }
            }
            """
            
            response = await client.post(
                f"https://{SHOPIFY_STORE}/admin/api/2024-01/graphql.json",
                json={
                    "query": update_mutation,
                    "variables": {
                        "input": {
                            "id": customer_id,
                            "note": notes_text
                        }
                    }
                },
                headers=headers,
                timeout=30.0
            )
            
            result = response.json()
            errors = result.get("data", {}).get("customerUpdate", {}).get("userErrors", [])
            
            if errors:
                logger.error(f"Error updating Shopify customer notes: {errors}")
                return False
            
            logger.info(f"Successfully synced equipment to Shopify for {user_email}")
            return True
            
    except Exception as e:
        logger.error(f"Error syncing equipment to Shopify: {e}")
        return False

def _clean_equipment_list(local_equipment: list) -> list:
    """Convert None values to empty strings for frontend consumption -
    shared by GET /auth/equipment and GET /auth/me/export so both surfaces
    of the same data can't silently drift apart (same idiom as
    _serialize_user)."""
    cleaned_equipment = []
    for eq in local_equipment:
        cleaned_equipment.append({
            "id": eq.get("id", ""),
            "brand": eq.get("brand") or "",
            "model": eq.get("model", ""),
            "chassis_serial": eq.get("chassis_serial") or "",
            "engine_serial": eq.get("engine_serial") or "",
            "engine_type": eq.get("engine_type") or "",
            "transmission_type": eq.get("transmission_type") or "",
            "front_axle_model": eq.get("front_axle_model") or "",
            "features": eq.get("features") or [],
            "created_at": eq.get("created_at", ""),
        })
    return cleaned_equipment


@api_router.get("/auth/equipment")
async def get_user_equipment(request: Request):
    """Get all equipment for authenticated user, straight from Mongo.

    Used to also re-derive the list from Shopify customer notes on every
    call ("ALWAYS prioritize Shopify data") and overwrite equipment[] with
    that - removed. That path predates this app having its own equipment
    CRUD + CRM sync; Shopify customers have no way to add/edit equipment
    themselves through Shopify, so it was never a real second source of
    truth, only a latent bug source: the notes text format can't represent
    CRM-only fields like crm_tractor_id, so every resync silently dropped
    it (and could even swap an entry's id if the notes round-trip failed to
    match it back to the existing local entry) - breaking the CRM-side
    create-vs-update idempotency check the moment it happened. The other
    direction (local equipment -> Shopify notes, sync_equipment_to_shopify_notes,
    still called from POST/PUT /auth/equipment) is unaffected and stays -
    that one only ever writes outward, so it can't stomp on local data.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Token lipsă sau invalid")

    token = auth_header.replace("Bearer ", "")
    # Search by both credential types (our own multi-device tokens/legacy
    # single token, and Shopify access token)
    user = await _find_user_by_token(token, allow_shopify_access_token=True)

    if not user:
        raise HTTPException(status_code=401, detail="Token invalid sau expirat")

    cleaned_equipment = _clean_equipment_list(user.get("equipment", []))

    return {"equipment": cleaned_equipment, "count": len(cleaned_equipment), "max_allowed": 10}

async def sync_equipment_to_crm(equipment: dict, user: dict, source: str = "webshop") -> None:
    """Fire-and-forget: push a newly added/updated equipment entry into
    agb-crm as a tractor record tied to the owning client.

    Must never raise - any failure (missing config, timeout, connection
    error, 4xx/5xx) is logged and swallowed so it can't affect the write
    that was already saved/returned to the client. Mirrors
    sync_order_to_crm / sync_interest_to_crm / sync_account_to_crm.

    On success, writes CRM's returned `tractor_id` back onto the local
    equipment sub-document as `crm_tractor_id` - without this, equipment
    added through the normal web/mobile flow (as opposed to migrated in
    from CRM via receive_equipment_from_crm, which already sets it) would
    never get a crm_tractor_id at all, silently breaking
    delete_user_equipment's "only notify CRM if this equipment has a
    crm_tractor_id" check for every normally-added tractor. Confirmed as a
    real bug by the coordinator, fixed here.

    NOTE on `source`: /auth/equipment is the exact same endpoint for both
    webshop and mobile - there is currently no header/user-agent convention
    in this codebase that reliably distinguishes the two callers, so this
    defaults to "webshop" for all callers until such a signal is added.
    """
    if not CRM_API_URL or not CRM_INTEGRATION_KEY:
        logger.error("CRM sync skipped for equipment %s: CRM_API_URL/CRM_INTEGRATION_KEY not configured", equipment.get("id"))
        return

    payload = {
        "source": source,
        "source_equipment_id": equipment.get("id"),
        "customer": {
            "nume": user.get("name"),
            "email": user.get("email"),
            "telefon": user.get("phone"),
        },
        "brand": equipment.get("brand"),
        "model": equipment.get("model"),
        "chassis_serial": equipment.get("chassis_serial"),
        "engine_serial": equipment.get("engine_serial"),
        "engine_type": equipment.get("engine_type"),
        "transmission_type": equipment.get("transmission_type"),
        "front_axle_model": equipment.get("front_axle_model"),
        "features": equipment.get("features") or [],
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as http_client:
            response = await http_client.post(
                f"{CRM_API_URL}/integrations/equipment",
                json=payload,
                headers={"X-Integration-Key": CRM_INTEGRATION_KEY},
            )
            if response.status_code >= 400:
                logger.error(
                    "CRM equipment sync failed for equipment %s: HTTP %s - %s",
                    equipment.get("id"), response.status_code, response.text,
                )
                return

            tractor_id = response.json().get("tractor_id")
            if tractor_id:
                await db.users.update_one(
                    {"id": user["id"], "equipment.id": equipment.get("id")},
                    {"$set": {"equipment.$.crm_tractor_id": tractor_id}},
                )
            else:
                logger.warning(
                    "CRM equipment sync for equipment %s succeeded but response had no tractor_id: %s",
                    equipment.get("id"), response.text,
                )
    except Exception as e:
        logger.error("CRM equipment sync failed for equipment %s: %s", equipment.get("id"), e)


async def sync_equipment_delete_to_crm(crm_tractor_id: str) -> None:
    """Fire-and-forget: tell agb-crm a web/mobile account deleted a piece of
    equipment that was linked to a CRM tractor. Same never-raise contract as
    sync_equipment_to_crm - only called when the equipment being deleted has
    a crm_tractor_id at all (nothing to tell CRM about otherwise).

    CRM decides on its own whether to actually delete the tractor or just
    unlink it (e.g. if it has associated orders) - we just fire the request
    and log/ignore the outcome, no branching needed on this side.
    """
    if not CRM_API_URL or not CRM_INTEGRATION_KEY:
        logger.error("CRM delete sync skipped for tractor %s: CRM_API_URL/CRM_INTEGRATION_KEY not configured", crm_tractor_id)
        return

    try:
        async with httpx.AsyncClient(timeout=10.0) as http_client:
            response = await http_client.delete(
                f"{CRM_API_URL}/integrations/equipment/{crm_tractor_id}",
                headers={"X-Integration-Key": CRM_INTEGRATION_KEY},
            )
            if response.status_code >= 400:
                logger.error(
                    "CRM equipment delete sync failed for tractor %s: HTTP %s - %s",
                    crm_tractor_id, response.status_code, response.text,
                )
    except Exception as e:
        logger.error("CRM equipment delete sync failed for tractor %s: %s", crm_tractor_id, e)


@api_router.post("/auth/equipment")
async def add_user_equipment(request: Request, equipment_data: EquipmentCreate, background_tasks: BackgroundTasks):
    """Add new equipment for authenticated user (max 10)"""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Token lipsă sau invalid")
    
    token = auth_header.replace("Bearer ", "")
    # Search by both credential types (our own multi-device tokens/legacy
    # single token, and Shopify access token)
    user = await _find_user_by_token(token, allow_shopify_access_token=True)
    
    if not user:
        raise HTTPException(status_code=401, detail="Token invalid sau expirat")
    
    # Check current equipment count
    current_equipment = user.get("equipment", [])
    if len(current_equipment) >= 10:
        raise HTTPException(status_code=400, detail="Ați atins limita maximă de 10 utilaje")
    
    # Create new equipment entry
    new_equipment = {
        "id": str(uuid.uuid4()),
        "brand": equipment_data.brand,
        "model": equipment_data.model,
        "chassis_serial": equipment_data.chassis_serial,
        "engine_serial": equipment_data.engine_serial,
        "engine_type": equipment_data.engine_type,
        "transmission_type": equipment_data.transmission_type,
        "front_axle_model": equipment_data.front_axle_model,
        "features": equipment_data.features,
        "created_at": datetime.utcnow().isoformat()
    }
    
    # Add to user's equipment array
    await db.users.update_one(
        {"_id": user["_id"]},
        {"$push": {"equipment": new_equipment}}
    )
    
    # Sync to Shopify
    updated_equipment = current_equipment + [new_equipment]
    await sync_equipment_to_shopify_notes(user["email"], updated_equipment)

    # Sync to CRM (fire-and-forget, never blocks/fails the response above)
    background_tasks.add_task(sync_equipment_to_crm, new_equipment, user, "webshop")

    return {"message": "Utilaj adăugat cu succes", "equipment": new_equipment}

@api_router.put("/auth/equipment/{equipment_id}")
async def update_user_equipment(request: Request, equipment_id: str, equipment_data: EquipmentUpdate, background_tasks: BackgroundTasks):
    """Update existing equipment"""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Token lipsă sau invalid")
    
    token = auth_header.replace("Bearer ", "")
    # Search by both credential types (our own multi-device tokens/legacy
    # single token, and Shopify access token)
    user = await _find_user_by_token(token, allow_shopify_access_token=True)
    
    if not user:
        raise HTTPException(status_code=401, detail="Token invalid sau expirat")
    
    # Find and update equipment
    equipment_list = user.get("equipment", [])
    equipment_found = False
    
    for eq in equipment_list:
        if eq.get("id") == equipment_id:
            if equipment_data.brand is not None:
                eq["brand"] = equipment_data.brand
            if equipment_data.model is not None:
                eq["model"] = equipment_data.model
            if equipment_data.chassis_serial is not None:
                eq["chassis_serial"] = equipment_data.chassis_serial
            if equipment_data.engine_serial is not None:
                eq["engine_serial"] = equipment_data.engine_serial
            if equipment_data.engine_type is not None:
                eq["engine_type"] = equipment_data.engine_type
            if equipment_data.transmission_type is not None:
                eq["transmission_type"] = equipment_data.transmission_type
            if equipment_data.front_axle_model is not None:
                eq["front_axle_model"] = equipment_data.front_axle_model
            if equipment_data.features is not None:
                eq["features"] = equipment_data.features
            equipment_found = True
            break
    
    if not equipment_found:
        raise HTTPException(status_code=404, detail="Utilajul nu a fost găsit")
    
    # Save updated equipment list
    await db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {"equipment": equipment_list}}
    )
    
    # Sync to Shopify
    await sync_equipment_to_shopify_notes(user["email"], equipment_list)

    # Sync to CRM (fire-and-forget, never blocks/fails the response above)
    background_tasks.add_task(sync_equipment_to_crm, eq, user, "webshop")

    return {"message": "Utilaj actualizat cu succes", "equipment": equipment_list}

@api_router.delete("/auth/equipment/{equipment_id}")
async def delete_user_equipment(request: Request, equipment_id: str, background_tasks: BackgroundTasks):
    """Delete equipment"""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Token lipsă sau invalid")

    token = auth_header.replace("Bearer ", "")
    # Search by both credential types (our own multi-device tokens/legacy
    # single token, and Shopify access token)
    user = await _find_user_by_token(token, allow_shopify_access_token=True)

    if not user:
        raise HTTPException(status_code=401, detail="Token invalid sau expirat")

    # Remove equipment from list
    equipment_list = user.get("equipment", [])
    deleted_equipment = next((eq for eq in equipment_list if eq.get("id") == equipment_id), None)
    new_equipment_list = [eq for eq in equipment_list if eq.get("id") != equipment_id]

    if len(new_equipment_list) == len(equipment_list):
        raise HTTPException(status_code=404, detail="Utilajul nu a fost găsit")

    # Save updated equipment list
    await db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {"equipment": new_equipment_list}}
    )

    # Sync to Shopify
    await sync_equipment_to_shopify_notes(user["email"], new_equipment_list)

    # Sync to CRM (fire-and-forget, never blocks/fails the response above) -
    # only if this piece of equipment was ever linked to a CRM tractor in
    # the first place.
    crm_tractor_id = (deleted_equipment or {}).get("crm_tractor_id")
    if crm_tractor_id:
        background_tasks.add_task(sync_equipment_delete_to_crm, crm_tractor_id)

    return {"message": "Utilaj șters cu succes", "remaining_count": len(new_equipment_list)}

# ==================== INBOUND CRM -> WEB/MOBILE EQUIPMENT SYNC ====================
# Reverse direction of sync_equipment_to_crm above: CRM staff add/edit a
# tractor directly on a client's CRM record, and it should land on that
# client's web/mobile account equipment list automatically, if they have
# one. Authenticated the same way agb-crm authenticates our outbound calls
# to it (X-Integration-Key), except here *we* are the ones checking it -
# confirmed with the coordinator that CRM_INTEGRATION_KEY (this repo's
# .env) and CRM's own INTEGRATION_API_KEY are the same value, so no new
# secret was introduced for this.

def _require_crm_integration_key(request: Request) -> None:
    incoming = request.headers.get("X-Integration-Key", "")
    if not CRM_INTEGRATION_KEY or not secrets.compare_digest(incoming, CRM_INTEGRATION_KEY):
        raise HTTPException(status_code=401, detail="Invalid or missing X-Integration-Key")


# ==================== INBOUND CRM -> BFF ADMIN SESSION REVOCATION ====================
# Lets CRM force-invalidate an already-issued BFF admin JWT immediately
# (staff logout, account disabled, suspected token leak, etc.) instead of
# waiting out its own short (5-15 min) expiry - see _is_bff_session_revoked
# and _verify_bff_jwt above. Deliberately a DIFFERENT shared secret
# (CRM_BFF_SERVICE_KEY) from CRM_INTEGRATION_KEY just above, which is for
# the unrelated /integrations/* channel - rotating/compromising one must
# never affect the other.

class RevokeBffAdminRequest(BaseModel):
    staff_user_id: str


def _require_crm_bff_service_key(request: Request) -> None:
    """Same compare_digest pattern as _require_crm_integration_key just
    above, with one deliberate difference: if CRM_BFF_SERVICE_KEY isn't
    configured in this environment at all, this fails CLOSED with 503
    (service unavailable) rather than 401. An unconfigured secret must
    never be reachable by "just don't send the header" the way a wrong
    value would be blocked - 503 makes the "this channel isn't provisioned
    here" case unambiguous, both to CRM and in logs/monitoring, without
    ever falling back to accepting all requests (fail-open)."""
    if not CRM_BFF_SERVICE_KEY:
        raise HTTPException(status_code=503, detail="BFF revocation channel not configured")
    incoming = request.headers.get("X-CRM-BFF-Service-Key", "")
    if not secrets.compare_digest(incoming, CRM_BFF_SERVICE_KEY):
        raise HTTPException(status_code=401, detail="Invalid or missing X-CRM-BFF-Service-Key")


@api_router.post("/internal/revoke-bff-admin")
async def revoke_bff_admin(request: Request, payload: RevokeBffAdminRequest):
    """Record an immediate revocation for every BFF admin JWT already issued
    to `staff_user_id` at or before this moment. Idempotent to call
    repeatedly (each call just inserts another row - _is_bff_session_revoked
    only cares whether *any* matching row exists). Self-cleans via the TTL
    index on bff_revoked_sessions.expires_at created in startup_event - no
    separate purge job needed. The 24h expires_at window is intentionally
    generous relative to any plausible BFF JWT TTL (5-15 min); it only
    exists so this collection doesn't grow forever, not as a meaningful
    revocation duration."""
    _require_crm_bff_service_key(request)

    now = datetime.utcnow()
    await db.bff_revoked_sessions.insert_one({
        "staff_user_id": payload.staff_user_id,
        "revoked_at": now,
        "expires_at": now + timedelta(hours=24),
    })
    return {"status": "revoked", "staff_user_id": payload.staff_user_id}


@api_router.post("/integrations/equipment-from-crm")
async def receive_equipment_from_crm(request: Request, payload: EquipmentFromCrm):
    """Create/update a piece of equipment on a client's web/mobile account
    from a CRM-side add/edit, matching the client by email when present,
    falling back to phone ONLY when the payload has no email at all - same
    priority rule as CRM's own _resolve_or_create_client (updated on their
    side to prioritize email over phone for source in ("webshop","mobile")).

    Critically, phone is NOT a fallback for "email didn't match" - only for
    "email wasn't provided". A failed email lookup must not silently fall
    through to a phone match, since a phone number can be legitimately
    shared (e.g. a company phone reused on a separate personal test
    account) and matching on it in that case would attach the wrong
    person's equipment to the wrong account. Confirmed with the coordinator
    after a real mismatch caused by this exact scenario. Matching itself is
    still an exact string comparison against stored users.phone/email - no
    phone-format normalization on this side.

    Idempotency key is `crm_tractor_id`, stored on the matched equipment
    sub-document (new field, mirrors `web_equipment_id` on CRM's side of
    this same loop) so repeated calls for the same tractor update in place
    instead of duplicating.

    Deliberately does NOT call sync_equipment_to_crm for the write made
    here - this is the one path in the whole equipment sync loop that must
    not echo back to CRM, or every inbound sync would immediately trigger
    an outbound one back at CRM for the same tractor.
    """
    _require_crm_integration_key(request)

    user = None
    if payload.client_email:
        user = await db.users.find_one({"email": payload.client_email.lower().strip()})
    elif payload.client_phone:
        user = await db.users.find_one({"phone": payload.client_phone})

    if not user:
        return {"status": "no_matching_account", "equipment_id": None}

    equipment_list = user.get("equipment", [])
    existing = next((eq for eq in equipment_list if eq.get("crm_tractor_id") == payload.crm_tractor_id), None)

    equipment_fields = {
        "brand": payload.brand,
        "model": payload.model,
        "chassis_serial": payload.chassis_serial,
        "engine_serial": payload.engine_serial,
        "engine_type": payload.engine_type,
        "transmission_type": payload.transmission_type,
        "front_axle_model": payload.front_axle_model,
        "features": payload.features or [],
    }

    if existing:
        existing.update(equipment_fields)
        await db.users.update_one(
            {"_id": user["_id"]},
            {"$set": {"equipment": equipment_list}}
        )
        await sync_equipment_to_shopify_notes(user["email"], equipment_list)
        return {"status": "updated", "equipment_id": existing["id"]}

    if len(equipment_list) >= 10:
        logger.warning(
            "CRM equipment sync: user %s already has 10 equipment entries, "
            "cannot add crm_tractor_id %s", user.get("id"), payload.crm_tractor_id,
        )
        return {"status": "equipment_limit_reached", "equipment_id": None}

    new_equipment = {
        "id": str(uuid.uuid4()),
        "crm_tractor_id": payload.crm_tractor_id,
        **equipment_fields,
        "created_at": datetime.utcnow().isoformat(),
    }
    await db.users.update_one(
        {"_id": user["_id"]},
        {"$push": {"equipment": new_equipment}}
    )
    await sync_equipment_to_shopify_notes(user["email"], equipment_list + [new_equipment])
    return {"status": "created", "equipment_id": new_equipment["id"]}


@api_router.delete("/integrations/equipment/{equipment_id}")
async def receive_equipment_delete_from_crm(request: Request, equipment_id: str):
    """CRM staff deleted a tractor linked to a web/mobile account - remove
    the matching entry from that account's equipment[]. CRM only calls this
    once it's already confirmed the tractor is safe to delete (blocked on
    their side if it has associated orders), so no branching needed here.

    `equipment_id` here is OUR equipment sub-document id (what
    receive_equipment_from_crm/sync_equipment_to_crm returned/sent as
    equipment_id/source_equipment_id - stored by CRM as web_equipment_id),
    not crm_tractor_id - it's already globally unique, so no
    phone/email lookup is needed, unlike the create/update endpoint above.
    """
    _require_crm_integration_key(request)

    user = await db.users.find_one({"equipment.id": equipment_id})
    if not user:
        return {"status": "not_found"}

    equipment_list = user.get("equipment", [])
    new_equipment_list = [eq for eq in equipment_list if eq.get("id") != equipment_id]

    await db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {"equipment": new_equipment_list}}
    )
    await sync_equipment_to_shopify_notes(user["email"], new_equipment_list)

    return {"status": "deleted"}

# ==================== EQUIPMENT OPTIONS (admin-managed dropdown/checkbox lists) ====================
# Powers the transmission-type / front-axle-model / features dropdown and
# checkbox lists on the equipment form used by both web and mobile. Public
# GET (no auth) - same tier as /products / /collections, this is
# catalog-style reference data, not sensitive. Admin can add new option
# values from the admin UI without a code change; there is deliberately no
# edit/delete endpoint yet - not requested, can be added later if needed.

EQUIPMENT_OPTION_CATEGORIES = ("transmission_type", "front_axle_model", "features")

class EquipmentOptionCreate(BaseModel):
    """Add-option request body for POST /admin/equipment-options."""
    category: str
    value: str

@api_router.get("/equipment-options")
async def get_equipment_options():
    """Public: return all three equipment-option lists in one response, each
    sorted by created_at ascending (insertion order)."""
    result = {category: [] for category in EQUIPMENT_OPTION_CATEGORIES}
    cursor = db.equipment_options.find({}).sort("created_at", 1)
    async for opt in cursor:
        category = opt.get("category")
        if category in result:
            result[category].append(opt.get("value"))
    return result

@api_router.post("/admin/equipment-options")
async def admin_add_equipment_option(request: Request, option_data: EquipmentOptionCreate):
    """Admin-only: add a new value to one of the three equipment-option
    categories. Idempotent - a case-insensitive duplicate already present in
    that category is a silent no-op rather than an error, same style as the
    other "add" endpoints in this file (see customer-interests)."""
    admin = await _require_admin(request)

    if option_data.category not in EQUIPMENT_OPTION_CATEGORIES:
        raise HTTPException(status_code=400, detail="Categorie invalidă")

    value = (option_data.value or "").strip()
    if not value:
        raise HTTPException(status_code=400, detail="Valoare invalidă")

    existing = await db.equipment_options.find_one({
        "category": option_data.category,
        "value": {"$regex": f"^{re.escape(value)}$", "$options": "i"},
    })
    option_id = None
    if not existing:
        option_id = str(uuid.uuid4())
        await db.equipment_options.insert_one({
            "id": option_id,
            "category": option_data.category,
            "value": value,
            "created_at": datetime.utcnow(),
        })

    await _write_audit_log(
        request, admin, action="equipment_option.create", resource_type="equipment_option",
        resource_id=option_id or existing.get("id"),
        after={"category": option_data.category, "value": value, "was_duplicate": existing is not None},
    )

    return {"message": "ok", "category": option_data.category, "value": value}

# ==================== CUSTOMER INTERESTS (Favorite / Price Alert / Stock Alert) ====================
# Pure capture-and-display feature: a customer toggles interest in a product
# from the product page (favorite/wishlist, price-drop alert, back-in-stock
# alert) and admin staff see the resulting list in
# GET /admin/customer-interests to follow up manually. There is deliberately
# no automated notification-sending here (no email/push when a price
# actually changes or stock returns) - that's out of scope for this feature.
# Each new interest is also fire-and-forget synced into agb-crm (see
# sync_interest_to_crm below), so CRM staff see it alongside interests they
# log manually there for out-of-stock requests - same retry/reconciliation
# pattern as sync_order_to_crm.

INTEREST_TYPES = ("favorite", "price_alert", "stock_alert")

async def sync_interest_to_crm(interest_id: str, interest_type: str, user: dict, product: Optional[dict]) -> None:
    """Fire-and-forget: push a newly recorded customer interest into agb-crm.

    Must never raise - any failure (missing config, timeout, connection
    error, 4xx/5xx) is logged and swallowed so it can't affect the interest
    that was already saved/returned to the client. Mirrors sync_order_to_crm.
    """
    if not CRM_API_URL or not CRM_INTEGRATION_KEY:
        logger.error("CRM sync skipped for interest %s: CRM_API_URL/CRM_INTEGRATION_KEY not configured", interest_id)
        return

    payload = {
        "source": "webshop",
        "source_interest_id": interest_id,
        "type": interest_type,
        "customer": {
            "nume": user.get("name"),
            "email": user.get("email"),
            "telefon": user.get("phone"),
        },
        "product": {
            "denumire": product.get("title") if product else None,
            "cod_prod": product.get("sku") if product else None,
            "pret": product.get("price") if product else None,
            "moneda": product.get("currency") if product else None,
            "imagine_url": product.get("image_url") if product else None,
            "stoc_status": product.get("stock_status") if product else None,
        },
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as http_client:
            response = await http_client.post(
                f"{CRM_API_URL}/integrations/interests",
                json=payload,
                headers={"X-Integration-Key": CRM_INTEGRATION_KEY},
            )
            if response.status_code >= 400:
                error_message = f"HTTP {response.status_code} - {response.text}"
                logger.error("CRM sync failed for interest %s: %s", interest_id, error_message)
                await db.customer_interests.update_one(
                    {"id": interest_id},
                    {
                        "$set": {"crm_synced": False, "crm_sync_error": error_message},
                        "$inc": {"crm_sync_attempts": 1},
                    },
                )
            else:
                await db.customer_interests.update_one(
                    {"id": interest_id},
                    {"$set": {"crm_synced": True, "crm_sync_error": None}},
                )
    except Exception as e:
        logger.error("CRM sync failed for interest %s: %s", interest_id, e)
        await db.customer_interests.update_one(
            {"id": interest_id},
            {
                "$set": {"crm_synced": False, "crm_sync_error": str(e)},
                "$inc": {"crm_sync_attempts": 1},
            },
        )

@api_router.get("/auth/interests")
async def get_user_interest_state(request: Request, product_id: str):
    """Return which of the three interest toggles the current user has set
    for a single product, so the product page can render correct initial
    toggle state on load without fetching the user's whole interest history."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Token lipsă sau invalid")

    token = auth_header.replace("Bearer ", "")
    # Search by both credential types (our own multi-device tokens/legacy
    # single token, and Shopify access token) - same idiom as /auth/equipment.
    user = await _find_user_by_token(token, allow_shopify_access_token=True)

    if not user:
        raise HTTPException(status_code=401, detail="Token invalid sau expirat")

    existing = await db.customer_interests.find(
        {"user_id": user["id"], "product_id": product_id}
    ).to_list(len(INTEREST_TYPES))
    existing_types = {i["type"] for i in existing}

    return {t: (t in existing_types) for t in INTEREST_TYPES}

@api_router.post("/auth/interests")
async def add_user_interest(request: Request, interest_data: CustomerInterestCreate, background_tasks: BackgroundTasks):
    """Idempotently record that the current user is interested in a product
    (favorite / price alert / stock alert). No-op (not an error) if that
    exact user+product+type combo is already recorded."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Token lipsă sau invalid")

    token = auth_header.replace("Bearer ", "")
    user = await _find_user_by_token(token, allow_shopify_access_token=True)

    if not user:
        raise HTTPException(status_code=401, detail="Token invalid sau expirat")

    existing = await db.customer_interests.find_one({
        "user_id": user["id"],
        "product_id": interest_data.product_id,
        "type": interest_data.type,
    })
    if not existing:
        interest_id = str(uuid.uuid4())
        await db.customer_interests.insert_one({
            "id": interest_id,
            "user_id": user["id"],
            "product_id": interest_data.product_id,
            "type": interest_data.type,
            "created_at": datetime.utcnow(),
            "crm_synced": False,
            "crm_sync_error": None,
            "crm_sync_attempts": 0,
        })
        product = await db.shopify_products.find_one({"id": interest_data.product_id})
        background_tasks.add_task(sync_interest_to_crm, interest_id, interest_data.type, user, product)

    return {"message": "ok"}

@api_router.delete("/auth/interests")
async def remove_user_interest(
    request: Request,
    product_id: str,
    type: Literal["favorite", "price_alert", "stock_alert"],
):
    """Idempotently remove a previously recorded interest. No-op (not an
    error) if that user+product+type combo doesn't exist."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Token lipsă sau invalid")

    token = auth_header.replace("Bearer ", "")
    user = await _find_user_by_token(token, allow_shopify_access_token=True)

    if not user:
        raise HTTPException(status_code=401, detail="Token invalid sau expirat")

    await db.customer_interests.delete_one({
        "user_id": user["id"],
        "product_id": product_id,
        "type": type,
    })

    return {"message": "ok"}

@api_router.get("/auth/favorites", response_model=List[Product])
async def get_user_favorites(request: Request):
    """Return the current user's own favorited products as full Product
    objects (most recently favorited first), so the frontend can render
    them with the same product-card/grid components used by GET /products."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Token lipsă sau invalid")

    token = auth_header.replace("Bearer ", "")
    user = await _find_user_by_token(token, allow_shopify_access_token=True)

    if not user:
        raise HTTPException(status_code=401, detail="Token invalid sau expirat")

    cursor = db.customer_interests.find(
        {"user_id": user["id"], "type": "favorite"}
    ).sort("created_at", -1)
    interests = await cursor.to_list(None)
    product_ids = [i["product_id"] for i in interests]

    products_by_id = {}
    if product_ids:
        async for p in db.shopify_products.find({"id": {"$in": product_ids}}):
            products_by_id[p["id"]] = p

    # Preserve favorited-order (most recent first) rather than $in's
    # arbitrary order; silently skip products deleted since being favorited.
    favorites = []
    for pid in product_ids:
        product = products_by_id.get(pid)
        if product:
            favorites.append(Product(**product))

    return favorites

# ==================== WEBHOOK ENDPOINTS ====================

async def verify_shopify_webhook(request: Request) -> bool:
    """Verify that webhook request is from Shopify"""
    if not SHOPIFY_WEBHOOK_SECRET:
        # If no secret configured, accept all webhooks (development mode)
        logger.warning("SHOPIFY_WEBHOOK_SECRET not configured - accepting webhook without verification")
        return True
    
    hmac_header = request.headers.get("X-Shopify-Hmac-SHA256", "")
    body = await request.body()
    
    computed_hmac = hmac.new(
        SHOPIFY_WEBHOOK_SECRET.encode('utf-8'),
        body,
        hashlib.sha256
    ).digest()
    
    import base64
    computed_hmac_b64 = base64.b64encode(computed_hmac).decode('utf-8')
    
    return hmac.compare_digest(computed_hmac_b64, hmac_header)

async def update_single_product(shopify_product_id: str):
    """Fetch and update a single product from Shopify"""
    try:
        graphql_query = """
        query getProduct($id: ID!) {
            product(id: $id) {
                id
                title
                handle
                description
                tags
                productType
                vendor
                priceRange {
                    minVariantPrice {
                        amount
                        currencyCode
                    }
                }
                images(first: 1) {
                    edges {
                        node {
                            url
                        }
                    }
                }
                variants(first: 1) {
                    edges {
                        node {
                            id
                            sku
                            quantityAvailable
                        }
                    }
                }
            }
        }
        """
        
        full_id = f"gid://shopify/Product/{shopify_product_id}"
        
        url = f"https://{SHOPIFY_STORE}/api/{SHOPIFY_API_VERSION}/graphql.json"
        headers = {
            "X-Shopify-Storefront-Access-Token": SHOPIFY_STOREFRONT_TOKEN,
            "Content-Type": "application/json"
        }
        
        async with httpx.AsyncClient() as http_client:
            response = await http_client.post(
                url,
                json={"query": graphql_query, "variables": {"id": full_id}},
                headers=headers,
                timeout=30.0
            )
            
            data = response.json()
            
            if data.get("data", {}).get("product"):
                product = parse_shopify_node(data["data"]["product"])
                
                # Update or insert in database
                await db.shopify_products.update_one(
                    {"id": shopify_product_id},
                    {"$set": product},
                    upsert=True
                )
                
                logger.info(f"Product updated via webhook: {product['title'][:50]}...")
                return True
            
            return False
            
    except Exception as e:
        logger.error(f"Error updating product {shopify_product_id}: {e}")
        return False

@api_router.post("/webhooks/shopify")
async def shopify_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    Handle Shopify webhooks for real-time updates.
    Supports: products/create, products/update, products/delete, inventory_levels/update
    """
    # Verify webhook signature
    if not await verify_shopify_webhook(request):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")
    
    # Get webhook topic
    topic = request.headers.get("X-Shopify-Topic", "")
    body = await request.json()
    
    logger.info(f"Received Shopify webhook: {topic}")
    
    try:
        if topic in ["products/create", "products/update"]:
            product_id = str(body.get("id", ""))
            if product_id:
                background_tasks.add_task(update_single_product, product_id)
                return {"status": "accepted", "action": "update_product", "product_id": product_id}
        
        elif topic == "products/delete":
            product_id = str(body.get("id", ""))
            if product_id:
                result = await db.shopify_products.delete_one({"id": product_id})
                logger.info(f"Product deleted: {product_id} (deleted: {result.deleted_count})")
                return {"status": "accepted", "action": "delete_product", "product_id": product_id}
        
        elif topic == "inventory_levels/update":
            inventory_item_id = body.get("inventory_item_id")
            available = body.get("available", 0)
            logger.info(f"Inventory update: item={inventory_item_id}, available={available}")
            return {"status": "accepted", "action": "inventory_update", "available": available}
        
        return {"status": "accepted", "topic": topic}
        
    except Exception as e:
        logger.error(f"Webhook processing error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@api_router.get("/webhooks/status")
async def get_webhook_status():
    """Get webhook configuration status"""
    count = await db.shopify_products.count_documents({})
    return {
        "webhook_secret_configured": bool(SHOPIFY_WEBHOOK_SECRET),
        "auto_sync_enabled": False,
        "auto_sync_interval_minutes": AUTO_SYNC_INTERVAL_MINUTES,
        "webhook_url": "/api/webhooks/shopify",
        "supported_topics": [
            "products/create",
            "products/update", 
            "products/delete",
            "inventory_levels/update"
        ],
        "last_sync": sync_status.get("last_sync"),
        "products_in_db": count
    }

# ==================== SHOPIFY OAUTH2 FOR ADMIN API ====================

# OAuth scopes needed for order creation
SHOPIFY_OAUTH_SCOPES = "write_orders,read_orders,read_products,write_products"

class ShopifyOAuthConfig(BaseModel):
    admin_token: Optional[str] = None
    installed_at: Optional[datetime] = None
    scopes: Optional[str] = None

async def get_shopify_admin_token() -> Optional[str]:
    """Get the Shopify Admin API token from database or env"""
    # First check environment variable
    if SHOPIFY_ADMIN_TOKEN:
        return SHOPIFY_ADMIN_TOKEN
    
    # Then check database
    config = await db.shopify_config.find_one({"type": "admin_oauth"})
    if config and config.get("admin_token"):
        return config["admin_token"]
    
    return None

async def save_shopify_admin_token(token: str, scopes: str):
    """Save the Shopify Admin API token to database"""
    await db.shopify_config.update_one(
        {"type": "admin_oauth"},
        {
            "$set": {
                "type": "admin_oauth",
                "admin_token": token,
                "scopes": scopes,
                "installed_at": datetime.utcnow()
            }
        },
        upsert=True
    )
    logger.info(f"Shopify Admin token saved to database")

@api_router.get("/shopify/auth")
async def shopify_oauth_start(request: Request):
    """Start Shopify OAuth flow - redirect to Shopify authorization page"""
    if not SHOPIFY_CLIENT_ID:
        raise HTTPException(status_code=500, detail="SHOPIFY_CLIENT_ID nu este configurat")
    
    # Get the base URL for redirect
    # In production, this should be your actual domain
    host = request.headers.get("host", "localhost:8001")
    protocol = "https" if "localhost" not in host else "http"
    redirect_uri = f"{protocol}://{host}/api/shopify/callback"
    
    # Build Shopify authorization URL
    shop_domain = SHOPIFY_STORE.replace('.myshopify.com', '')
    auth_url = (
        f"https://{shop_domain}.myshopify.com/admin/oauth/authorize"
        f"?client_id={SHOPIFY_CLIENT_ID}"
        f"&scope={SHOPIFY_OAUTH_SCOPES}"
        f"&redirect_uri={redirect_uri}"
    )
    
    logger.info(f"Redirecting to Shopify OAuth: {auth_url}")
    
    # Return HTML page that redirects
    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Conectare Shopify - AGB Agroparts</title>
        <meta http-equiv="refresh" content="2;url={auth_url}">
        <style>
            body {{
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                display: flex;
                justify-content: center;
                align-items: center;
                min-height: 100vh;
                background: #f5f5f5;
                margin: 0;
            }}
            .container {{
                text-align: center;
                background: white;
                padding: 40px;
                border-radius: 10px;
                box-shadow: 0 2px 10px rgba(0,0,0,0.1);
            }}
            h1 {{ color: #367c2b; }}
            .spinner {{
                border: 4px solid #f3f3f3;
                border-top: 4px solid #367c2b;
                border-radius: 50%;
                width: 40px;
                height: 40px;
                animation: spin 1s linear infinite;
                margin: 20px auto;
            }}
            @keyframes spin {{
                0% {{ transform: rotate(0deg); }}
                100% {{ transform: rotate(360deg); }}
            }}
            a {{
                color: #367c2b;
                text-decoration: none;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>🚜 AGB Agroparts</h1>
            <div class="spinner"></div>
            <p>Se redirectionează către Shopify pentru autorizare...</p>
            <p><a href="{auth_url}">Click aici dacă nu ești redirecționat automat</a></p>
        </div>
    </body>
    </html>
    """
    
    return HTMLResponse(content=html_content)

# DISABLED - Using the callback at the end of the file instead
# @api_router.get("/shopify/callback")
async def shopify_oauth_callback_OLD(
    code: Optional[str] = None,
    shop: Optional[str] = None,
    error: Optional[str] = None,
    error_description: Optional[str] = None
):
    """DISABLED - Handle Shopify OAuth callback - exchange code for access token"""
    
    if error:
        logger.error(f"Shopify OAuth error: {error} - {error_description}")
        return HTMLResponse(content=f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Eroare - AGB Agroparts</title>
            <style>
                body {{
                    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                    display: flex;
                    justify-content: center;
                    align-items: center;
                    min-height: 100vh;
                    background: #f5f5f5;
                }}
                .container {{
                    text-align: center;
                    background: white;
                    padding: 40px;
                    border-radius: 10px;
                    box-shadow: 0 2px 10px rgba(0,0,0,0.1);
                    max-width: 500px;
                }}
                h1 {{ color: #d32f2f; }}
                .error {{ color: #666; }}
            </style>
        </head>
        <body>
            <div class="container">
                <h1>❌ Eroare OAuth</h1>
                <p class="error">{error}: {error_description}</p>
                <p><a href="/api/shopify/auth">Încearcă din nou</a></p>
            </div>
        </body>
        </html>
        """, status_code=400)
    
    if not code:
        logger.error("No authorization code received")
        return HTMLResponse(content="""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Eroare - AGB Agroparts</title>
            <style>
                body {
                    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                    display: flex;
                    justify-content: center;
                    align-items: center;
                    min-height: 100vh;
                    background: #f5f5f5;
                }
                .container {
                    text-align: center;
                    background: white;
                    padding: 40px;
                    border-radius: 10px;
                    box-shadow: 0 2px 10px rgba(0,0,0,0.1);
                }
                h1 { color: #d32f2f; }
            </style>
        </head>
        <body>
            <div class="container">
                <h1>❌ Eroare</h1>
                <p>Nu s-a primit codul de autorizare de la Shopify.</p>
                <p><a href="/api/shopify/auth">Încearcă din nou</a></p>
            </div>
        </body>
        </html>
        """, status_code=400)
    
    # Exchange code for access token
    shop_domain = shop or SHOPIFY_STORE.replace('.myshopify.com', '')
    if '.myshopify.com' in shop_domain:
        shop_domain = shop_domain.replace('.myshopify.com', '')
    
    token_url = f"https://{shop_domain}.myshopify.com/admin/oauth/access_token"
    
    payload = {
        "client_id": SHOPIFY_CLIENT_ID,
        "client_secret": SHOPIFY_CLIENT_SECRET,
        "code": code
    }
    
    try:
        async with httpx.AsyncClient() as client:
            # IMPORTANT: Shopify requires form-urlencoded data, NOT JSON!
            response = await client.post(token_url, data=payload, timeout=30.0)
            
            logger.info(f"Token exchange response status: {response.status_code}")
            logger.info(f"Token exchange response: {response.text}")
            
            if response.status_code != 200:
                return HTMLResponse(content=f"""
                <!DOCTYPE html>
                <html>
                <head>
                    <title>Eroare - AGB Agroparts</title>
                    <style>
                        body {{
                            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                            display: flex;
                            justify-content: center;
                            align-items: center;
                            min-height: 100vh;
                            background: #f5f5f5;
                        }}
                        .container {{
                            text-align: center;
                            background: white;
                            padding: 40px;
                            border-radius: 10px;
                            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
                            max-width: 600px;
                        }}
                        h1 {{ color: #d32f2f; }}
                        pre {{ background: #f5f5f5; padding: 10px; border-radius: 5px; overflow-x: auto; }}
                    </style>
                </head>
                <body>
                    <div class="container">
                        <h1>❌ Eroare la obținerea tokenului</h1>
                        <p>Status: {response.status_code}</p>
                        <pre>{response.text}</pre>
                        <p><a href="/api/shopify/auth">Încearcă din nou</a></p>
                    </div>
                </body>
                </html>
                """, status_code=400)
            
            data = response.json()
            access_token = data.get("access_token")
            scopes = data.get("scope", "")
            
            if not access_token:
                return HTMLResponse(content="""
                <!DOCTYPE html>
                <html>
                <head>
                    <title>Eroare - AGB Agroparts</title>
                </head>
                <body>
                    <h1>❌ Nu s-a primit token-ul de acces</h1>
                    <p><a href="/api/shopify/auth">Încearcă din nou</a></p>
                </body>
                </html>
                """, status_code=400)
            
            # Save token to database
            await save_shopify_admin_token(access_token, scopes)
            
            # Show success page
            return HTMLResponse(content=f"""
            <!DOCTYPE html>
            <html>
            <head>
                <title>Succes! - AGB Agroparts</title>
                <style>
                    body {{
                        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                        display: flex;
                        justify-content: center;
                        align-items: center;
                        min-height: 100vh;
                        background: linear-gradient(135deg, #e8f5e9 0%, #c8e6c9 100%);
                    }}
                    .container {{
                        text-align: center;
                        background: white;
                        padding: 40px 60px;
                        border-radius: 15px;
                        box-shadow: 0 4px 20px rgba(0,0,0,0.15);
                        max-width: 500px;
                    }}
                    h1 {{ color: #367c2b; }}
                    .success-icon {{ font-size: 60px; margin-bottom: 20px; }}
                    .scopes {{ 
                        background: #f5f5f5; 
                        padding: 15px; 
                        border-radius: 8px; 
                        margin: 20px 0;
                        text-align: left;
                    }}
                    .scopes strong {{ color: #367c2b; }}
                    p {{ color: #555; line-height: 1.6; }}
                </style>
            </head>
            <body>
                <div class="container">
                    <div class="success-icon">✅</div>
                    <h1>Conectare reușită!</h1>
                    <p>Aplicația AGB Agroparts a fost conectată cu succes la magazinul tău Shopify.</p>
                    <div class="scopes">
                        <strong>Permisiuni acordate:</strong><br>
                        {scopes.replace(',', ', ')}
                    </div>
                    <p>Acum comenzile plasate în aplicație vor apărea în panoul tău Shopify!</p>
                    <p style="margin-top: 30px; font-size: 14px; color: #888;">
                        Poți închide această fereastră.
                    </p>
                </div>
            </body>
            </html>
            """)
            
    except Exception as e:
        logger.error(f"OAuth callback error: {e}")
        return HTMLResponse(content=f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Eroare - AGB Agroparts</title>
            <style>
                body {{
                    font-family: Arial, sans-serif;
                    display: flex;
                    justify-content: center;
                    align-items: center;
                    min-height: 100vh;
                    background: #f5f5f5;
                }}
                .container {{
                    text-align: center;
                    background: white;
                    padding: 40px;
                    border-radius: 10px;
                }}
                h1 {{ color: #d32f2f; }}
            </style>
        </head>
        <body>
            <div class="container">
                <h1>❌ Eroare</h1>
                <p>A apărut o eroare: {str(e)}</p>
                <p><a href="/api/shopify/auth">Încearcă din nou</a></p>
            </div>
        </body>
        </html>
        """, status_code=500)

@api_router.get("/shopify/status")
async def get_shopify_oauth_status():
    """Check if Shopify Admin API is connected"""
    admin_token = await get_shopify_admin_token()
    config = await db.shopify_config.find_one({"type": "admin_oauth"})
    
    return {
        "connected": bool(admin_token),
        "has_admin_token": bool(admin_token),
        "scopes": config.get("scopes") if config else None,
        "installed_at": config.get("installed_at") if config else None,
        "oauth_url": "/api/shopify/auth"
    }

# ==================== SHOPIFY ADMIN API - CREATE ORDER ====================

class ShopifyOrderItem(BaseModel):
    product_id: str
    variant_id: Optional[str] = None
    title: str
    quantity: int
    price: float

class ShopifyOrderCreate(BaseModel):
    items: List[ShopifyOrderItem]
    customer: CustomerInfo
    payment_method: str = "ramburs"  # "ramburs" or "online"
    note: Optional[str] = None

@api_router.post("/shopify/orders/create")
async def create_shopify_order(order_data: ShopifyOrderCreate):
    """Create an order directly in Shopify using Admin API"""
    admin_token = await get_shopify_admin_token()
    
    if not admin_token:
        logger.error("No Shopify Admin token available")
        raise HTTPException(
            status_code=400, 
            detail="Conexiunea cu Shopify nu este configurată. Administratorul trebuie să autorizeze aplicația la /api/shopify/auth"
        )
    
    try:
        # Build line items for the order
        line_items = []
        for item in order_data.items:
            item.price = await _get_authoritative_price(item.product_id)
            line_item = {
                "title": item.title,
                "quantity": item.quantity,
                "price": str(item.price),
            }
            
            # Add variant_id if available
            if item.variant_id:
                line_item["variant_id"] = int(item.variant_id)
            
            line_items.append(line_item)
        
        # Determine financial status based on payment method
        financial_status = "pending" if order_data.payment_method == "ramburs" else "paid"
        
        # Build order payload
        order_payload = {
            "order": {
                "line_items": line_items,
                "customer": {
                    "first_name": order_data.customer.name.split()[0] if order_data.customer.name else "",
                    "last_name": " ".join(order_data.customer.name.split()[1:]) if len(order_data.customer.name.split()) > 1 else "",
                    "email": order_data.customer.email,
                    "phone": order_data.customer.phone
                },
                "billing_address": {
                    "first_name": order_data.customer.name.split()[0] if order_data.customer.name else "",
                    "last_name": " ".join(order_data.customer.name.split()[1:]) if len(order_data.customer.name.split()) > 1 else "",
                    "address1": order_data.customer.address,
                    "city": order_data.customer.city,
                    "province": order_data.customer.county,
                    "zip": order_data.customer.postal_code,
                    "country": "Romania",
                    "phone": order_data.customer.phone
                },
                "shipping_address": {
                    "first_name": order_data.customer.name.split()[0] if order_data.customer.name else "",
                    "last_name": " ".join(order_data.customer.name.split()[1:]) if len(order_data.customer.name.split()) > 1 else "",
                    "address1": order_data.customer.address,
                    "city": order_data.customer.city,
                    "province": order_data.customer.county,
                    "zip": order_data.customer.postal_code,
                    "country": "Romania",
                    "phone": order_data.customer.phone
                },
                "financial_status": financial_status,
                "tags": f"app-mobile,{order_data.payment_method}",
                "note": order_data.note or f"Comandă din aplicația mobilă AGB - {order_data.payment_method.upper()}",
                "source_name": "AGB Mobile App"
            }
        }
        
        # Add shipping line
        order_payload["order"]["shipping_lines"] = [{
            "title": "Livrare standard",
            "price": "25.00",
            "code": "STANDARD"
        }]
        
        # Make API call to Shopify Admin API
        shop_domain = SHOPIFY_STORE.replace('.myshopify.com', '')
        url = f"https://{shop_domain}.myshopify.com/admin/api/{SHOPIFY_API_VERSION}/orders.json"
        
        headers = {
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": admin_token
        }
        
        logger.info(f"Creating Shopify order with payload: {order_payload}")
        
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=order_payload, headers=headers, timeout=30.0)
            
            logger.info(f"Shopify order response status: {response.status_code}")
            logger.info(f"Shopify order response: {response.text}")
            
            if response.status_code == 201:
                order_result = response.json()
                shopify_order = order_result.get("order", {})
                
                # Save order to local database too
                local_order_id = str(uuid.uuid4())
                local_order = {
                    "id": local_order_id,
                    "shopify_order_id": str(shopify_order.get("id")),
                    "shopify_order_number": shopify_order.get("order_number"),
                    "items": [item.dict() for item in order_data.items],
                    "customer": order_data.customer.dict(),
                    "subtotal": sum(item.price * item.quantity for item in order_data.items),
                    "shipping": 25.0,
                    "total": sum(item.price * item.quantity for item in order_data.items) + 25.0,
                    "status": "confirmed",
                    "payment_method": order_data.payment_method,
                    "created_at": datetime.utcnow()
                }
                await db.orders.insert_one(local_order)
                
                return {
                    "success": True,
                    "order_id": local_order_id,
                    "shopify_order_id": str(shopify_order.get("id")),
                    "shopify_order_number": shopify_order.get("order_number"),
                    "total": shopify_order.get("total_price"),
                    "status": shopify_order.get("financial_status"),
                    "message": f"Comanda #{shopify_order.get('order_number')} a fost creată cu succes!"
                }
            else:
                error_data = response.json()
                error_msg = error_data.get("errors", response.text)
                logger.error(f"Shopify order creation failed: {error_msg}")
                raise HTTPException(status_code=response.status_code, detail=f"Eroare Shopify: {error_msg}")
                
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating Shopify order: {e}")
        raise HTTPException(status_code=500, detail=f"Eroare la crearea comenzii: {str(e)}")

def _get_cors_allowed_origins() -> List[str]:
    """Explicit CORS allowlist - allow_origins=["*"] together with
    allow_credentials=True effectively let ANY origin make credentialed
    requests against this API, which is what browsers' CORS spec is
    supposed to prevent. CORS_ALLOWED_ORIGINS is a new, comma-separated
    env var (e.g. "https://agb-agroparts.ro,https://admin.agb-agroparts.ro").
    WEBSHOP_PUBLIC_URL (already used elsewhere, e.g. password-reset email
    links) is always folded in too when set, as a reasonable default for
    the storefront's own origin so this doesn't need to be configured
    twice. If neither is set, the allowlist is empty (fail restrictive,
    not "*") rather than silently falling back to allow-all."""
    origins = set()
    for origin in os.environ.get("CORS_ALLOWED_ORIGINS", "").split(","):
        origin = origin.strip()
        if origin:
            origins.add(origin.rstrip("/"))

    webshop_public_url = os.environ.get("WEBSHOP_PUBLIC_URL", "").strip()
    if webshop_public_url:
        origins.add(webshop_public_url.rstrip("/"))

    return sorted(origins)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=_get_cors_allowed_origins(),
    allow_methods=["*"],
    allow_headers=["*"],
    # Without this, cross-origin JS (webshop/mobile) can't read
    # Content-Disposition off a fetch() response even though the header is
    # sent - it's not on the browser's default-exposed safelist. Needed for
    # GET /auth/me/export's suggested-filename download to actually work.
    expose_headers=["Content-Disposition"],
)

# ==================== AUTO-SYNC BACKGROUND TASK ====================

auto_sync_task = None

async def auto_sync_loop():
    """Background task that syncs products periodically"""
    while True:
        try:
            await asyncio.sleep(AUTO_SYNC_INTERVAL_MINUTES * 60)  # Convert minutes to seconds
            
            if not sync_status["is_syncing"]:
                logger.info(f"Auto-sync starting (every {AUTO_SYNC_INTERVAL_MINUTES} minutes)...")
                await sync_all_products()
                logger.info("Auto-sync completed")
        except asyncio.CancelledError:
            logger.info("Auto-sync task cancelled")
            break
        except Exception as e:
            logger.error(f"Auto-sync error: {e}")
            await asyncio.sleep(300)

# ==================== CRM RECONCILIATION BACKGROUND TASK ====================

crm_reconciliation_task = None

async def crm_reconciliation_loop():
    """Background task that retries CRM sync for orders and customer
    interests that previously failed (or predate crm_sync_attempts existing
    on the document at all - $lt excludes missing fields, so the "OR missing"
    branch is what catches those older records)."""
    while True:
        try:
            await asyncio.sleep(600)  # 10 minutes

            pending_orders = await db.orders.find({
                "crm_synced": {"$ne": True},
                "crm_sync_attempts": {"$lt": 10},
            }).to_list(1000)

            for doc in pending_orders:
                doc.pop("_id", None)
                order = Order(**doc)
                logger.info(f"CRM reconciliation: retry order {order.id}, attempt {order.crm_sync_attempts + 1}")
                await sync_order_to_crm(order)

            pending_interests = await db.customer_interests.find({
                "crm_synced": {"$ne": True},
                "$or": [
                    {"crm_sync_attempts": {"$exists": False}},
                    {"crm_sync_attempts": {"$lt": 10}},
                ],
            }).to_list(1000)

            for doc in pending_interests:
                attempts = doc.get("crm_sync_attempts", 0)
                logger.info(f"CRM reconciliation: retry interest {doc['id']}, attempt {attempts + 1}")
                user = await db.users.find_one({"id": doc["user_id"]})
                product = await db.shopify_products.find_one({"id": doc["product_id"]})
                if not user:
                    logger.error(f"CRM reconciliation: skipping interest {doc['id']}, user {doc['user_id']} no longer exists")
                    continue
                await sync_interest_to_crm(doc["id"], doc["type"], user, product)

            pending_item_syncs = await db.orders.find({
                "crm_synced": True,
                "crm_items_dirty": True,
                "$or": [
                    {"crm_items_sync_attempts": {"$exists": False}},
                    {"crm_items_sync_attempts": {"$lt": 10}},
                ],
            }).to_list(1000)

            for doc in pending_item_syncs:
                doc.pop("_id", None)
                order = Order(**doc)
                logger.info(f"CRM reconciliation: retry item sync for order {order.id}, attempt {order.crm_items_sync_attempts + 1}")
                await sync_order_update_to_crm(order)
        except asyncio.CancelledError:
            logger.info("CRM reconciliation task cancelled")
            break
        except Exception as e:
            logger.error(f"CRM reconciliation error: {e}")
            await asyncio.sleep(300)

async def _migrate_legacy_single_token_users():
    """One-time compatibility migration for the multi-device login cap
    (max MAX_DEVICE_TOKENS concurrent sessions, see _issue_session_token):
    fold each account's legacy single `token` field (from before this
    feature existed, when every login overwrote one shared field) into the
    new `tokens` array, so already-logged-in users - including the admin
    account - keep working after this deploy instead of being silently
    logged out by a schema mismatch.

    Matches only docs that still have the old `token` field and haven't
    been converted yet (`tokens` missing), so this is a cheap no-op on every
    subsequent startup once a given database has been migrated once.
    _find_user_by_token also has a legacy `token` fallback as a second
    safety net in case any document is somehow missed here.
    """
    try:
        result = await db.users.update_many(
            {"token": {"$exists": True}, "tokens": {"$exists": False}},
            [
                {"$set": {"tokens": ["$token"]}},
                {"$unset": "token"},
            ],
        )
        if result.modified_count:
            logger.info(
                f"Migrated {result.modified_count} user(s) from legacy single "
                f"`token` field to the new `tokens` array (multi-device login cap)"
            )
    except Exception:
        logger.exception("Failed to migrate legacy user tokens to tokens[] array")


@app.on_event("startup")
async def startup_event():
    """Start background tasks on app startup"""
    global auto_sync_task, crm_reconciliation_task

    try:
        await db.users.create_index("email", unique=True)
    except Exception:
        logger.exception("Failed to create unique index on users.email")

    # _find_user_by_token() runs on every authenticated request and matches
    # on either field (see its docstring) - without these, that's a full
    # collection scan on db.users every single time.
    try:
        await db.users.create_index("tokens")
    except Exception:
        logger.exception("Failed to create index on users.tokens")
    try:
        await db.users.create_index("token")
    except Exception:
        logger.exception("Failed to create index on users.token")
    # New-format session entries are embedded documents inside tokens[]
    # (see _new_session_token_doc) - the plain "tokens" index above indexes
    # each array element as a whole (works for the legacy bare-string
    # match), but the $elemMatch-on-subfield lookup for these needs its own
    # index on the "tokens.token" dotted path to avoid a collection scan.
    try:
        await db.users.create_index("tokens.token")
    except Exception:
        logger.exception("Failed to create index on users.tokens.token")

    # Every cart read/write filters by session_id (get_cart, add_to_cart's
    # existing-item lookup, create_order's cleanup) - without this, each one
    # scans the whole (unboundedly-growing, never-expired) cart collection.
    try:
        await db.cart.create_index("session_id")
    except Exception:
        logger.exception("Failed to create index on cart.session_id")

    try:
        # Belt-and-braces defense against duplicate interest rows on top of
        # the check-then-insert idempotency in add_user_interest() (e.g. two
        # concurrent taps of the same toggle) - see customer_interests.
        await db.customer_interests.create_index(
            [("user_id", 1), ("product_id", 1), ("type", 1)], unique=True
        )
    except Exception:
        logger.exception("Failed to create unique index on customer_interests")

    # Perf fix (250-concurrent-user staging latency investigation,
    # 2026-08-02): `id` is looked up via find_one({"id": ...}) all over the
    # product endpoints (get_product, admin update/delete, cart pricing,
    # equivalent/complementary lookups, etc.) - without an index each lookup
    # is a full collection scan. Not marked unique: manual product creation
    # (_apply "manual" create) generates the id via uuid4() but never checks
    # for a pre-existing duplicate before inserting, so uniqueness isn't
    # actually guaranteed by the data/app layer today.
    try:
        await db.shopify_products.create_index("id")
    except Exception:
        logger.exception("Failed to create index on shopify_products.id")

    # Home/featured product queries filter on is_featured (see the curated
    # picks logic) - without this, each one scans the whole catalog.
    try:
        await db.shopify_products.create_index("is_featured")
    except Exception:
        logger.exception("Failed to create index on shopify_products.is_featured")

    # Stock-based filtering/sorting (in-stock vs out-of-stock curated picks,
    # stock > 0 / == 0 queries) - without this, each one scans the whole
    # catalog.
    try:
        await db.shopify_products.create_index("stock")
    except Exception:
        logger.exception("Failed to create index on shopify_products.stock")

    # Admin product list "sort by newest/recently updated"
    # (?sort=created_at_desc / ?sort=updated_at_desc, see SORT_FIELDS) -
    # without these, each such sort scans the whole catalog.
    try:
        await db.shopify_products.create_index("created_at")
    except Exception:
        logger.exception("Failed to create index on shopify_products.created_at")
    try:
        await db.shopify_products.create_index("updated_at")
    except Exception:
        logger.exception("Failed to create index on shopify_products.updated_at")

    # Account order history (GET /auth/orders) filters by customer.email -
    # without this, each request scans the whole orders collection.
    try:
        await db.orders.create_index("customer.email")
    except Exception:
        logger.exception("Failed to create index on orders.customer.email")

    # GET /orders/{session_id} filters by session_id - without this, each
    # request scans the whole orders collection.
    try:
        await db.orders.create_index("session_id")
    except Exception:
        logger.exception("Failed to create index on orders.session_id")

    # GET /admin/audit-log lists newest-first, and per-admin activity is a
    # common follow-up lookup - without this, each request scans the whole
    # (append-only, ever-growing) audit log collection.
    try:
        await db.admin_audit_log.create_index([("timestamp", -1), ("admin_id", 1)])
    except Exception:
        logger.exception("Failed to create index on admin_audit_log.(timestamp, admin_id)")
    # Supports "show every audit entry for this specific action + resource"
    # lookups (e.g. the full edit history of one product/order).
    try:
        await db.admin_audit_log.create_index([("action", 1), ("resource_id", 1)])
    except Exception:
        logger.exception("Failed to create index on admin_audit_log.(action, resource_id)")

    # BFF admin-session revocation records (see POST
    # /api/internal/revoke-bff-admin and _is_bff_session_revoked) should
    # self-expire instead of accumulating forever - expireAfterSeconds=0
    # means "expire exactly at the datetime stored in expires_at" (each
    # document sets its own expires_at = revoked_at + 24h, see the
    # endpoint), the standard Mongo TTL-index idiom for a per-document
    # expiry instant rather than a fixed age.
    try:
        await db.bff_revoked_sessions.create_index("expires_at", expireAfterSeconds=0)
    except Exception:
        logger.exception("Failed to create TTL index on bff_revoked_sessions.expires_at")

    # POST /analytics/pageview (unauthenticated, high write volume) and its
    # admin aggregation counterpart GET /admin/analytics/traffic both filter
    # by created_at (date-range) and/or group by session_id - without these,
    # each admin query scans the whole (unboundedly-growing) collection.
    try:
        await db.analytics_pageviews.create_index([("session_id", 1), ("created_at", 1)])
    except Exception:
        logger.exception("Failed to create index on analytics_pageviews.(session_id, created_at)")
    try:
        await db.analytics_pageviews.create_index("created_at")
    except Exception:
        logger.exception("Failed to create index on analytics_pageviews.created_at")

    # Conversion links written by create_order's analytics_session_id
    # handling and read by GET /admin/analytics/traffic's conversions/
    # conversion_rate - same reasoning as analytics_pageviews above.
    try:
        await db.analytics_conversions.create_index([("session_id", 1), ("created_at", 1)])
    except Exception:
        logger.exception("Failed to create index on analytics_conversions.(session_id, created_at)")
    try:
        await db.analytics_conversions.create_index("created_at")
    except Exception:
        logger.exception("Failed to create index on analytics_conversions.created_at")

    # Must run before the app starts accepting traffic under the new
    # tokens[] auth scheme - see docstring.
    await _migrate_legacy_single_token_users()

    # Auto-sync from Shopify is permanently disabled as of 2026-07-26: the
    # webshop/admin panel is now the source of truth for the product catalog
    # (independence from Shopify is the whole point of this project). A full
    # catalog resync can still be triggered manually via POST /sync/start if
    # ever needed, but nothing runs it on a recurring schedule anymore.
    logger.info("Auto-sync from Shopify is disabled - product catalog is now locally owned")

    crm_reconciliation_task = asyncio.create_task(crm_reconciliation_loop())

    logger.info("=== WEBHOOK SETUP ===")
    logger.info("Add webhooks in Shopify Admin -> Settings -> Notifications -> Webhooks")
    logger.info(f"  URL: https://YOUR_DOMAIN/api/webhooks/shopify")
    logger.info("  Topics: products/create, products/update, products/delete, inventory_levels/update")
    
    # Schedule blog checker to start after a short delay (to ensure all functions are loaded)
    asyncio.get_event_loop().call_later(5, lambda: asyncio.create_task(start_blog_checker()))

async def start_blog_checker():
    """Start the blog checker task"""
    try:
        # Define the checker inline to avoid ordering issues
        logger.info("Blog checker started - checking every 600 seconds (10 minutes)")
        while True:
            await asyncio.sleep(600)  # 10 minutes
            await check_for_new_blog_posts()
    except Exception as e:
        logger.error(f"Blog checker error: {e}")

@app.on_event("shutdown")
async def shutdown_db_client():
    global auto_sync_task
    
    if auto_sync_task:
        auto_sync_task.cancel()
        try:
            await auto_sync_task
        except asyncio.CancelledError:
            pass
    
    client.close()

# Privacy Policy Page
@app.get("/privacy-policy", response_class=HTMLResponse)
@app.get("/api/privacy-policy", response_class=HTMLResponse)
async def privacy_policy():
    html_content = """
    <!DOCTYPE html>
    <html lang="ro">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Politica de Confidențialitate - AGB Agroparts</title>
        <style>
            body {
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif;
                max-width: 800px;
                margin: 0 auto;
                padding: 20px;
                line-height: 1.6;
                color: #333;
                background-color: #f9f9f9;
            }
            h1 {
                color: #367c2b;
                border-bottom: 3px solid #367c2b;
                padding-bottom: 10px;
            }
            h2 {
                color: #367c2b;
                margin-top: 30px;
            }
            .logo {
                text-align: center;
                margin-bottom: 20px;
            }
            .logo img {
                max-width: 150px;
            }
            .container {
                background: white;
                padding: 30px;
                border-radius: 10px;
                box-shadow: 0 2px 10px rgba(0,0,0,0.1);
            }
            .updated {
                color: #666;
                font-style: italic;
            }
            ul {
                margin: 10px 0;
            }
            li {
                margin: 8px 0;
            }
            .contact {
                background: #e8f5e9;
                padding: 15px;
                border-radius: 5px;
                margin-top: 20px;
            }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="logo">
                <h1>🚜 AGB Agroparts</h1>
            </div>
            
            <h1>Politica de Confidențialitate</h1>
            <p class="updated">Ultima actualizare: Martie 2026</p>
            
            <h2>1. Introducere</h2>
            <p>AGB Agroparts ("noi", "al nostru") operează aplicația mobilă AGB (denumită în continuare "Aplicația"). Această pagină vă informează despre politicile noastre privind colectarea, utilizarea și divulgarea datelor cu caracter personal atunci când utilizați Aplicația noastră.</p>
            
            <h2>2. Date Colectate</h2>
            <p>Colectăm următoarele tipuri de informații:</p>
            <ul>
                <li><strong>Informații de contact:</strong> nume, adresă de email, număr de telefon, adresă de livrare (doar când plasați o comandă)</li>
                <li><strong>Informații despre comenzi:</strong> produsele comandate, istoric comenzi</li>
                <li><strong>Date tehnice:</strong> tip dispozitiv, sistem de operare, pentru a îmbunătăți funcționalitatea aplicației</li>
            </ul>
            
            <h2>3. Utilizarea Datelor</h2>
            <p>Utilizăm datele colectate pentru:</p>
            <ul>
                <li>Procesarea și livrarea comenzilor dumneavoastră</li>
                <li>Comunicarea privind comenzile (confirmare, expediere, livrare)</li>
                <li>Îmbunătățirea serviciilor și a experienței utilizatorului</li>
                <li>Răspunsuri la întrebările dumneavoastră</li>
            </ul>
            
            <h2>4. Protecția Datelor</h2>
            <p>Implementăm măsuri de securitate pentru a proteja datele dumneavoastră personale împotriva accesului neautorizat, modificării, divulgării sau distrugerii.</p>
            
            <h2>5. Partajarea Datelor</h2>
            <p>Nu vindem și nu închiriem datele dumneavoastră personale terților. Putem partaja informații doar cu:</p>
            <ul>
                <li>Servicii de curierat pentru livrarea comenzilor</li>
                <li>Procesatori de plăți pentru finalizarea tranzacțiilor</li>
                <li>Autorități, când legea o impune</li>
            </ul>
            
            <h2>6. Drepturile Dumneavoastră</h2>
            <p>Conform GDPR, aveți dreptul să:</p>
            <ul>
                <li>Accesați datele personale pe care le deținem despre dumneavoastră</li>
                <li>Solicitați corectarea datelor inexacte</li>
                <li>Solicitați ștergerea datelor</li>
                <li>Vă opuneți prelucrării datelor</li>
                <li>Solicitați portabilitatea datelor</li>
            </ul>
            
            <h2>7. Cookies și Tehnologii Similare</h2>
            <p>Aplicația poate utiliza tehnologii locale de stocare pentru a îmbunătăți experiența utilizatorului (de exemplu, pentru păstrarea coșului de cumpărături).</p>
            
            <h2>8. Modificări ale Politicii</h2>
            <p>Ne rezervăm dreptul de a actualiza această politică de confidențialitate. Vă vom notifica despre orice modificări prin publicarea noii politici în Aplicație.</p>
            
            <h2>9. Contact</h2>
            <div class="contact">
                <p>Pentru întrebări despre această politică de confidențialitate sau despre datele dumneavoastră, ne puteți contacta:</p>
                <ul>
                    <li><strong>Email:</strong> contact@agb-agroparts.ro</li>
                    <li><strong>Website:</strong> <a href="https://agb-agroparts.ro">https://agb-agroparts.ro</a></li>
                    <li><strong>Telefon:</strong> Disponibil pe website</li>
                </ul>
            </div>
            
            <p style="margin-top: 30px; text-align: center; color: #666;">
                © 2026 AGB Agroparts. Toate drepturile rezervate.
            </p>
        </div>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)



# Feature Graphic Page for Google Play
@app.get("/feature-graphic", response_class=HTMLResponse)
@app.get("/api/feature-graphic", response_class=HTMLResponse)
async def feature_graphic():
    html_path = ROOT_DIR / "feature_graphic.html"
    with open(html_path, "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


# ==================== SHOPIFY OAUTH & ADMIN API ====================

async def get_admin_access_token():
    """Get the stored admin access token from database or environment"""
    # First check environment variable directly
    admin_token = os.environ.get('SHOPIFY_ADMIN_TOKEN', '')
    if admin_token:
        logger.info(f"Using SHOPIFY_ADMIN_TOKEN from env: {admin_token[:10]}...")
        return admin_token
    
    # Fallback to global variable
    if SHOPIFY_ADMIN_TOKEN:
        logger.info(f"Using SHOPIFY_ADMIN_TOKEN from global: {SHOPIFY_ADMIN_TOKEN[:10]}...")
        return SHOPIFY_ADMIN_TOKEN
    
    # Then check database
    token_doc = await db.shopify_tokens.find_one({"store": SHOPIFY_STORE})
    if token_doc:
        logger.info("Using token from database")
        return token_doc.get("access_token")
    
    logger.warning("No Shopify Admin Token found!")
    return None

@api_router.get("/shopify/install")
async def shopify_install():
    """Start the Shopify OAuth flow - redirect to Shopify authorization"""
    shop = SHOPIFY_STORE
    scopes = "read_customers,write_customers,read_orders,write_orders,read_products"
    redirect_uri = f"https://agb-backend.onrender.com/api/shopify/callback"
    nonce = str(uuid.uuid4())
    
    # Store nonce for validation
    await db.shopify_nonces.insert_one({
        "nonce": nonce,
        "created_at": datetime.utcnow(),
        "expires_at": datetime.utcnow() + timedelta(minutes=10)
    })
    
    auth_url = (
        f"https://{shop}/admin/oauth/authorize?"
        f"client_id={SHOPIFY_CLIENT_ID}&"
        f"scope={scopes}&"
        f"redirect_uri={redirect_uri}&"
        f"state={nonce}"
    )
    
    return {"auth_url": auth_url, "message": "Redirect user to auth_url to authorize the app"}

@api_router.get("/shopify/callback")
async def shopify_oauth_callback(code: str = None, state: str = None, shop: str = None, hmac: str = None):
    """Handle OAuth callback from Shopify and exchange code for access token"""
    try:
        if not code:
            raise HTTPException(status_code=400, detail="No authorization code provided")

        # CSRF/nonce validation: `state` must match a nonce we generated and
        # stored in /shopify/install, so this callback can only be completed
        # as part of an OAuth flow we actually started - not replayed or
        # forged by a third party. Single-use: find_one_and_delete both
        # validates it and consumes it atomically, so the same nonce can
        # never be redeemed twice.
        if not state:
            raise HTTPException(status_code=400, detail="Parametrul state lipsește")

        nonce_doc = await db.shopify_nonces.find_one_and_delete({"nonce": state})
        if not nonce_doc:
            raise HTTPException(status_code=400, detail="Parametrul state este invalid sau a fost deja folosit")

        expires_at = nonce_doc.get("expires_at")
        if not expires_at or datetime.utcnow() > expires_at:
            raise HTTPException(status_code=400, detail="Parametrul state a expirat")

        # `shop` is client-controlled input - only ever proceed with the
        # single store this backend is actually configured for, never
        # whatever domain the query string happens to contain.
        if shop and shop != SHOPIFY_STORE:
            raise HTTPException(status_code=400, detail="Magazin Shopify nerecunoscut")

        shop_domain = SHOPIFY_STORE
        token_url = f"https://{shop_domain}/admin/oauth/access_token"
        
        logger.info(f"Exchanging code at: {token_url}")
        logger.info(f"Client ID: {SHOPIFY_CLIENT_ID[:10]}...")
        logger.info(f"Client Secret configured: {bool(SHOPIFY_CLIENT_SECRET)}")
        
        async with httpx.AsyncClient() as client:
            # IMPORTANT: Shopify requires form-urlencoded data with explicit Content-Type
            response = await client.post(
                token_url, 
                data={
                    "client_id": SHOPIFY_CLIENT_ID,
                    "client_secret": SHOPIFY_CLIENT_SECRET,
                    "code": code
                },
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json"
                }
            )
            
            if response.status_code != 200:
                logger.error(f"OAuth token exchange failed: {response.text}")
                raise HTTPException(status_code=400, detail=f"Failed to exchange code: {response.text}")
            
            token_data = response.json()
            access_token = token_data.get("access_token")
            
            if not access_token:
                raise HTTPException(status_code=400, detail="No access token in response")
            
            # Store the token in database
            await db.shopify_tokens.update_one(
                {"store": SHOPIFY_STORE},
                {
                    "$set": {
                        "access_token": access_token,
                        "scope": token_data.get("scope"),
                        "updated_at": datetime.utcnow()
                    }
                },
                upsert=True
            )
            
            logger.info("Successfully obtained and stored Shopify Admin API token")

            # Return a generic success page - the token itself is never
            # rendered/logged anywhere, not even partially. It's already
            # persisted in db.shopify_tokens (see the update_one() above);
            # anyone needing it for the SHOPIFY_ADMIN_TOKEN env var should
            # read it from there directly, not from this response.
            html = """
            <!DOCTYPE html>
            <html>
            <head>
                <title>AGB Mobile API - Succes!</title>
                <style>
                    body { font-family: Arial, sans-serif; text-align: center; padding: 50px; background: #1a1a1a; color: #fff; }
                    .success { color: #367c2b; font-size: 24px; margin-bottom: 20px; }
                    .note { color: #f5a623; margin-top: 20px; }
                </style>
            </head>
            <body>
                <h1 class="success">✅ Autorizare Reușită!</h1>
                <p>Aplicația AGB Mobile API a fost autorizată cu succes.</p>
                <p class="note">Token-ul a fost salvat direct în baza de date. Puteți închide această pagină.</p>
            </body>
            </html>
            """
            return HTMLResponse(content=html)
            
    except Exception as e:
        logger.error(f"OAuth callback error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@api_router.get("/shopify/token-status")
async def shopify_token_status():
    """Check if we have a valid Shopify Admin API token"""
    token = await get_admin_access_token()
    
    if not token:
        return {
            "has_token": False,
            "message": "No Admin API token found. Visit /api/shopify/install to authorize.",
            "install_url": "https://agb-backend.onrender.com/api/shopify/install"
        }
    
    # Test the token
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"https://{SHOPIFY_STORE}/admin/api/{SHOPIFY_API_VERSION}/shop.json",
                headers={"X-Shopify-Access-Token": token}
            )
            
            if response.status_code == 200:
                shop_data = response.json().get("shop", {})
                return {
                    "has_token": True,
                    "valid": True,
                    "shop_name": shop_data.get("name"),
                    "shop_email": shop_data.get("email")
                }
            else:
                return {
                    "has_token": True,
                    "valid": False,
                    "error": f"Token invalid: {response.status_code}"
                }
    except Exception as e:
        return {
            "has_token": True,
            "valid": False,
            "error": str(e)
        }

# ==================== SHOPIFY ORDER CREATION ====================

class ShopifyOrderItem(BaseModel):
    variant_id: Optional[str] = None
    product_id: str
    title: str
    quantity: int
    price: float

class ShopifyOrderCustomer(BaseModel):
    email: str
    first_name: str
    last_name: str
    phone: Optional[str] = None

class ShopifyOrderAddress(BaseModel):
    first_name: str
    last_name: str
    address1: str
    city: str
    province: str  # County/State
    zip: str
    country: str = "RO"
    phone: Optional[str] = None

class CreateShopifyOrderRequest(BaseModel):
    items: List[ShopifyOrderItem]
    customer: ShopifyOrderCustomer
    shipping_address: ShopifyOrderAddress
    billing_address: Optional[ShopifyOrderAddress] = None
    note: Optional[str] = None
    payment_method: str = "bank_transfer"  # or "cash_on_delivery"

@api_router.post("/orders/shopify")
async def create_shopify_order(request: CreateShopifyOrderRequest):
    """
    Create an order directly in Shopify Admin using the Admin API.
    This will make the order appear in Shopify's Orders dashboard.
    """
    token = await get_admin_access_token()
    
    if not token:
        raise HTTPException(
            status_code=503, 
            detail="Shopify Admin API not configured. Please authorize the app first."
        )
    
    try:
        # Build line items - we need to find variant IDs for products
        line_items = []
        for item in request.items:
            # Try to get variant ID from product
            variant_id = item.variant_id
            
            if not variant_id:
                # Look up the product to get variant ID
                product_doc = await db.shopify_products.find_one({"id": item.product_id})
                if product_doc and product_doc.get("variant_id"):
                    variant_id = product_doc.get("variant_id")
            
            if variant_id:
                # Use variant_id if available - Shopify prices the line item
                # from the variant itself, so item.price is never used here.
                line_items.append({
                    "variant_id": int(variant_id.split("/")[-1]) if "/" in str(variant_id) else int(variant_id),
                    "quantity": item.quantity
                })
            else:
                # Fallback: create custom line item. product_doc was already
                # looked up above (that's the only way to reach this branch)
                # - price must come from it, never from the client, or a
                # fabricated product_id could inject an arbitrarily-priced
                # fake line item into a real Shopify order.
                if not product_doc or product_doc.get("price") is None:
                    raise HTTPException(status_code=400, detail=f"Produs inexistent: {item.product_id}")
                line_items.append({
                    "title": item.title,
                    "quantity": item.quantity,
                    "price": str(product_doc["price"]),
                    "requires_shipping": True
                })
        
        # Build order payload
        order_payload = {
            "order": {
                "line_items": line_items,
                "customer": {
                    "first_name": request.customer.first_name,
                    "last_name": request.customer.last_name,
                    "email": request.customer.email,
                    "phone": request.customer.phone
                },
                "shipping_address": {
                    "first_name": request.shipping_address.first_name,
                    "last_name": request.shipping_address.last_name,
                    "address1": request.shipping_address.address1,
                    "city": request.shipping_address.city,
                    "province": request.shipping_address.province,
                    "zip": request.shipping_address.zip,
                    "country": request.shipping_address.country,
                    "phone": request.shipping_address.phone
                },
                "billing_address": {
                    "first_name": (request.billing_address or request.shipping_address).first_name,
                    "last_name": (request.billing_address or request.shipping_address).last_name,
                    "address1": (request.billing_address or request.shipping_address).address1,
                    "city": (request.billing_address or request.shipping_address).city,
                    "province": (request.billing_address or request.shipping_address).province,
                    "zip": (request.billing_address or request.shipping_address).zip,
                    "country": (request.billing_address or request.shipping_address).country,
                    "phone": (request.billing_address or request.shipping_address).phone
                },
                "financial_status": "pending",  # Payment not yet received
                "note": request.note or f"Comandă din aplicația mobilă AGB. Metoda de plată: {request.payment_method}",
                "tags": ["mobile-app", f"payment-{request.payment_method}"],
                "source_name": "AGB Mobile App",
                "send_receipt": True,  # Send order confirmation email to customer
                "send_fulfillment_receipt": True  # Send shipping notification when fulfilled
            }
        }
        
        # Create order via Admin API
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"https://{SHOPIFY_STORE}/admin/api/{SHOPIFY_API_VERSION}/orders.json",
                headers={
                    "X-Shopify-Access-Token": token,
                    "Content-Type": "application/json"
                },
                json=order_payload
            )
            
            if response.status_code not in [200, 201]:
                logger.error(f"Shopify order creation failed: {response.text}")
                raise HTTPException(
                    status_code=response.status_code,
                    detail=f"Failed to create Shopify order: {response.text}"
                )
            
            shopify_order = response.json().get("order", {})
            
            # Store order reference in our database
            order_record = {
                "shopify_order_id": shopify_order.get("id"),
                "shopify_order_number": shopify_order.get("order_number"),
                "shopify_order_name": shopify_order.get("name"),
                "customer_email": request.customer.email,
                "total_price": shopify_order.get("total_price"),
                "currency": shopify_order.get("currency"),
                "items_count": len(request.items),
                "payment_method": request.payment_method,
                "created_at": datetime.utcnow(),
                "source": "mobile_app"
            }
            await db.mobile_orders.insert_one(order_record)
            
            logger.info(f"Successfully created Shopify order #{shopify_order.get('order_number')}")
            
            return {
                "success": True,
                "order_id": shopify_order.get("id"),
                "order_number": shopify_order.get("order_number"),
                "order_name": shopify_order.get("name"),
                "total_price": shopify_order.get("total_price"),
                "currency": shopify_order.get("currency"),
                "status_url": shopify_order.get("order_status_url"),
                "message": f"Comanda #{shopify_order.get('order_number')} a fost creată cu succes în Shopify!"
            }
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating Shopify order: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# Privacy Policy endpoint
@api_router.get("/privacy-policy", response_class=HTMLResponse)
async def privacy_policy():
    """Privacy policy page for AGB Agroparts app"""
    html_content = """
    <!DOCTYPE html>
    <html lang="ro">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Politica de Confidențialitate - AGB Agroparts</title>
        <style>
            body { font-family: Arial, sans-serif; max-width: 800px; margin: 0 auto; padding: 20px; line-height: 1.6; }
            h1 { color: #367c2b; }
            h2 { color: #333; margin-top: 30px; }
            p { color: #555; }
            .last-updated { color: #888; font-size: 14px; }
        </style>
    </head>
    <body>
        <h1>Politica de Confidențialitate</h1>
        <p class="last-updated">Ultima actualizare: Martie 2026</p>
        
        <h2>1. Introducere</h2>
        <p>AGB Agroparts ("noi", "al nostru") respectă confidențialitatea utilizatorilor săi. Această Politică de Confidențialitate explică modul în care colectăm, folosim și protejăm informațiile dumneavoastră personale când utilizați aplicația noastră mobilă.</p>
        
        <h2>2. Informații pe care le colectăm</h2>
        <p>Colectăm următoarele tipuri de informații:</p>
        <ul>
            <li><strong>Informații de cont:</strong> nume, adresă de email, număr de telefon</li>
            <li><strong>Informații de livrare:</strong> adresa de livrare pentru comenzi</li>
            <li><strong>Informații despre comenzi:</strong> produsele comandate, istoricul achizițiilor</li>
            <li><strong>Informații despre dispozitiv:</strong> tipul dispozitivului, sistemul de operare</li>
        </ul>
        
        <h2>3. Cum folosim informațiile</h2>
        <p>Utilizăm informațiile colectate pentru:</p>
        <ul>
            <li>Procesarea și livrarea comenzilor</li>
            <li>Comunicarea cu dumneavoastră despre comenzi</li>
            <li>Îmbunătățirea serviciilor noastre</li>
            <li>Trimiterea de oferte și noutăți (doar cu acordul dumneavoastră)</li>
        </ul>
        
        <h2>4. Partajarea informațiilor</h2>
        <p>Nu vindem și nu închiriem informațiile dumneavoastră personale terților. Putem partaja informațiile doar cu:</p>
        <ul>
            <li>Furnizori de servicii de livrare pentru procesarea comenzilor</li>
            <li>Procesatori de plăți pentru tranzacții sigure</li>
            <li>Autorități legale, când suntem obligați prin lege</li>
        </ul>
        
        <h2>5. Securitatea datelor</h2>
        <p>Implementăm măsuri tehnice și organizatorice adecvate pentru a proteja datele dumneavoastră împotriva accesului neautorizat, modificării, divulgării sau distrugerii.</p>
        
        <h2>6. Drepturile dumneavoastră</h2>
        <p>Aveți dreptul să:</p>
        <ul>
            <li>Accesați datele personale pe care le deținem despre dumneavoastră</li>
            <li>Solicitați corectarea datelor incorecte</li>
            <li>Solicitați ștergerea datelor dumneavoastră</li>
            <li>Vă retrageți consimțământul în orice moment</li>
        </ul>
        
        <h2>7. Contact</h2>
        <p>Pentru întrebări despre această politică de confidențialitate sau despre datele dumneavoastră, ne puteți contacta la:</p>
        <ul>
            <li>Email: contact@agbagroparts.ro</li>
            <li>WhatsApp: +40 725 088 655</li>
        </ul>
        
        <h2>8. Modificări ale politicii</h2>
        <p>Ne rezervăm dreptul de a actualiza această politică periodic. Vă vom notifica despre orice modificări semnificative prin aplicație sau email.</p>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)

# ==================== BLOG/NEWS NOTIFICATIONS ====================

@api_router.get("/news")
async def get_news():
    """Fetch blog posts from Shopify for news/notifications"""
    try:
        if not SHOPIFY_STOREFRONT_TOKEN:
            return {"articles": [], "count": 0}
        
        query = """
        {
            blogs(first: 5) {
                edges {
                    node {
                        title
                        handle
                        articles(first: 10, sortKey: PUBLISHED_AT, reverse: true) {
                            edges {
                                node {
                                    id
                                    title
                                    handle
                                    publishedAt
                                    excerpt
                                    excerptHtml
                                    content
                                    contentHtml
                                    tags
                                    image {
                                        url
                                    }
                                    blog {
                                        title
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
        """
        
        headers = {
            "Content-Type": "application/json",
            "X-Shopify-Storefront-Access-Token": SHOPIFY_STOREFRONT_TOKEN
        }
        
        async with httpx.AsyncClient() as http_client:
            response = await http_client.post(
                f"https://{SHOPIFY_STORE}/api/{SHOPIFY_API_VERSION}/graphql.json",
                json={"query": query},
                headers=headers,
                timeout=30.0
            )
            
            data = response.json()
            
            articles = []
            blogs = data.get("data", {}).get("blogs", {}).get("edges", [])
            
            for blog in blogs:
                blog_articles = blog.get("node", {}).get("articles", {}).get("edges", [])
                for article in blog_articles:
                    node = article.get("node", {})
                    # Get tags and normalize them (lowercase, trimmed)
                    tags = node.get("tags", []) or []
                    normalized_tags = [tag.strip().lower() for tag in tags]
                    
                    # Convert HTML to formatted plain text for old app versions
                    content_html = node.get("contentHtml", "")
                    content_formatted = content_html
                    if content_html:
                        import html
                        # Convert HTML to plain text with proper line breaks
                        content_formatted = content_html
                        content_formatted = re.sub(r'<br\s*/?>', '\n', content_formatted)
                        content_formatted = re.sub(r'</p>', '\n\n', content_formatted)
                        content_formatted = re.sub(r'</div>', '\n', content_formatted)
                        content_formatted = re.sub(r'<li[^>]*>', '\n• ', content_formatted)
                        content_formatted = re.sub(r'<h[1-6][^>]*>', '\n\n', content_formatted)
                        content_formatted = re.sub(r'</h[1-6]>', '\n', content_formatted)
                        content_formatted = re.sub(r'<[^>]+>', '', content_formatted)  # Remove all HTML tags
                        content_formatted = html.unescape(content_formatted)  # Decode HTML entities
                        content_formatted = re.sub(r'\n{3,}', '\n\n', content_formatted)  # Clean multiple newlines
                        content_formatted = content_formatted.strip()
                    
                    articles.append({
                        "id": node.get("id", ""),
                        "title": node.get("title", ""),
                        "handle": node.get("handle", ""),
                        "published_at": node.get("publishedAt", ""),
                        "excerpt": node.get("excerpt", ""),
                        "excerpt_html": node.get("excerptHtml", ""),
                        "content": content_formatted,  # Formatted plain text for old app versions
                        "content_html": content_html,  # HTML for new app versions
                        "image_url": node.get("image", {}).get("url") if node.get("image") else None,
                        "blog_title": node.get("blog", {}).get("title", "News"),
                        "tags": tags,  # Original tags
                        "model_tags": normalized_tags  # Normalized for matching
                    })
            
            # Sort by published date
            articles.sort(key=lambda x: x.get("published_at", ""), reverse=True)
            
            return {"articles": articles[:20], "count": len(articles)}
    
    except Exception as e:
        logger.error(f"Error fetching news: {e}")
        return {"articles": [], "count": 0, "error": str(e)}

# ==================== BLOG POSTS (webshop, from db.blog_posts) ====================
# Public read-only endpoints over the `blog_posts` collection populated by
# scripts/migrate_blog_from_shopify.py (one-off migration of Shopify blog
# articles into Mongo, keyed by the article's Shopify handle - see that
# script's docstring for the field mapping and idempotency model).
#
# Deliberately independent of GET /news above and of
# check_for_new_blog_posts (the push-notification blog checker): both of
# those keep reading LIVE from Shopify, unchanged. This is a separate,
# additive read path for the webshop's blog section, over the migrated
# Mongo copy - repointing /news or the blog checker at db.blog_posts is a
# distinct, deliberate future decision and out of scope here.

@api_router.get("/blog/posts")
async def get_blog_posts(limit: int = 20, offset: int = 0):
    """List blog posts (paginated), sorted by published_at descending.
    Omits content_html/excerpt_html/blog_title to keep the list payload
    small - use GET /blog/posts/{handle} for the full article."""
    limit = max(1, min(limit, 50))
    offset = max(0, offset)

    total = await db.blog_posts.count_documents({})
    cursor = db.blog_posts.find({}).sort("published_at", -1).skip(offset).limit(limit)
    docs = await cursor.to_list(limit)

    posts = [
        {
            "id": doc.get("id"),
            "handle": doc.get("handle"),
            "title": doc.get("title"),
            "excerpt": doc.get("excerpt"),
            "image_url": doc.get("image_url"),
            "published_at": doc.get("published_at"),
            "tags": doc.get("tags", []),
        }
        for doc in docs
    ]

    return {"posts": posts, "total": total}


@api_router.get("/blog/posts/{handle}")
async def get_blog_post_by_handle(handle: str):
    """Full article body for a single blog post, by its Shopify handle."""
    post = await db.blog_posts.find_one({"handle": handle})
    if not post:
        raise HTTPException(status_code=404, detail="Articolul nu a fost găsit")
    post.pop("_id", None)
    return post

# ==================== PUSH NOTIFICATIONS ====================

class PushTokenRequest(BaseModel):
    push_token: str
    user_email: Optional[str] = None
    platform: str = "android"

class PushTokenUnregisterRequest(BaseModel):
    push_token: str

# Store for tracking last seen blog post
last_seen_blog_id = None
blog_check_interval = 600  # 10 minutes

@api_router.post("/push/register")
async def register_push_token(request: PushTokenRequest):
    """Register a device for push notifications"""
    try:
        # Store the push token in database
        existing = await db.push_tokens.find_one({"push_token": request.push_token})
        
        if existing:
            # Update existing token
            await db.push_tokens.update_one(
                {"push_token": request.push_token},
                {"$set": {
                    "user_email": request.user_email,
                    "platform": request.platform,
                    "updated_at": datetime.utcnow().isoformat()
                }}
            )
        else:
            # Insert new token
            await db.push_tokens.insert_one({
                "push_token": request.push_token,
                "user_email": request.user_email,
                "platform": request.platform,
                "created_at": datetime.utcnow().isoformat(),
                "updated_at": datetime.utcnow().isoformat()
            })
        
        logger.info(f"Push token registered: {request.push_token[:20]}...")
        return {"success": True, "message": "Push token registered"}
    
    except Exception as e:
        logger.error(f"Error registering push token: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@api_router.post("/push/unregister")
async def unregister_push_token(request: PushTokenUnregisterRequest):
    """Unregister a device from push notifications"""
    try:
        result = await db.push_tokens.delete_one({"push_token": request.push_token})
        logger.info(f"Push token unregistered: {request.push_token[:20]}...")
        return {"success": True, "deleted": result.deleted_count}
    
    except Exception as e:
        logger.error(f"Error unregistering push token: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@api_router.get("/push/tokens/count")
async def get_push_tokens_count():
    """Get count of registered push tokens"""
    count = await db.push_tokens.count_documents({})
    return {"count": count}

async def send_push_notification(title: str, body: str, data: dict = None):
    """Send push notification to all registered devices using Firebase Cloud Messaging"""
    try:
        import firebase_admin
        from firebase_admin import credentials, messaging
        
        # Initialize Firebase if not already done
        if not firebase_admin._apps:
            try:
                # First try environment variable (for Render)
                firebase_creds_json = os.environ.get('FIREBASE_SERVICE_ACCOUNT_JSON')
                if firebase_creds_json:
                    import json
                    cred_dict = json.loads(firebase_creds_json)
                    cred = credentials.Certificate(cred_dict)
                    firebase_admin.initialize_app(cred)
                    logger.info("Firebase Admin SDK initialized from environment variable")
                else:
                    # Try local file (for development)
                    cred = credentials.Certificate('/app/backend/firebase-service-account.json')
                    firebase_admin.initialize_app(cred)
                    logger.info("Firebase Admin SDK initialized from local file")
            except Exception as init_error:
                logger.error(f"Firebase init error: {init_error}")
                return 0
        
        # Get all push tokens
        tokens = await db.push_tokens.find({}).to_list(1000)
        
        if not tokens:
            logger.info("No push tokens registered")
            return 0
        
        # Extract FCM tokens (Expo tokens contain the FCM token)
        fcm_tokens = []
        for token_doc in tokens:
            push_token = token_doc.get("push_token", "")
            # Expo push tokens look like: ExponentPushToken[xxxxxx]
            # We need to extract the actual FCM token or use the whole thing
            if push_token:
                fcm_tokens.append(push_token)
        
        if not fcm_tokens:
            logger.info("No valid FCM tokens found")
            return 0
        
        # Send using Firebase Admin SDK
        sent_count = 0
        for token in fcm_tokens:
            try:
                message = messaging.Message(
                    notification=messaging.Notification(
                        title=title,
                        body=body,
                    ),
                    data={str(k): str(v) for k, v in (data or {}).items()},
                    token=token,
                    android=messaging.AndroidConfig(
                        priority='high',
                        notification=messaging.AndroidNotification(
                            sound='default',
                            channel_id='blog',
                        ),
                    ),
                )
                
                response = messaging.send(message)
                logger.info(f"FCM message sent: {response}")
                sent_count += 1
                
            except Exception as send_error:
                error_str = str(send_error)
                if "not a valid FCM registration token" in error_str or "Requested entity was not found" in error_str:
                    # Token is invalid, try Expo Push API as fallback
                    logger.info(f"FCM token invalid, trying Expo Push API for: {token[:30]}...")
                    try:
                        async with httpx.AsyncClient() as http_client:
                            response = await http_client.post(
                                "https://exp.host/--/api/v2/push/send",
                                json=[{
                                    "to": token,
                                    "sound": "default",
                                    "title": title,
                                    "body": body,
                                    "data": data or {},
                                    "channelId": "blog",
                                    "priority": "high",
                                }],
                                headers={
                                    "Accept": "application/json",
                                    "Content-Type": "application/json",
                                },
                                timeout=30.0
                            )
                            if response.status_code == 200:
                                result = response.json()
                                logger.info(f"Expo Push API response: {result}")
                                if result.get("data") and result["data"][0].get("status") == "ok":
                                    sent_count += 1
                    except Exception as expo_error:
                        logger.error(f"Expo Push API error: {expo_error}")
                else:
                    logger.error(f"FCM send error for {token[:30]}...: {send_error}")
        
        logger.info(f"Total notifications sent: {sent_count}")
        return sent_count
        
    except Exception as e:
        logger.error(f"Error sending push notifications: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return 0
        
        return sent_count
    
    except Exception as e:
        logger.error(f"Error sending push notifications: {e}")
        return 0

async def check_for_new_blog_posts():
    """Check for new blog posts and send push notifications"""
    global last_seen_blog_id
    
    try:
        if not SHOPIFY_STOREFRONT_TOKEN:
            return
        
        # Fetch latest blog post
        query = """
        {
            blogs(first: 1) {
                edges {
                    node {
                        articles(first: 1, sortKey: PUBLISHED_AT, reverse: true) {
                            edges {
                                node {
                                    id
                                    title
                                    excerpt
                                    publishedAt
                                }
                            }
                        }
                    }
                }
            }
        }
        """
        
        headers = {
            "Content-Type": "application/json",
            "X-Shopify-Storefront-Access-Token": SHOPIFY_STOREFRONT_TOKEN
        }
        
        async with httpx.AsyncClient() as http_client:
            response = await http_client.post(
                f"https://{SHOPIFY_STORE}/api/{SHOPIFY_API_VERSION}/graphql.json",
                json={"query": query},
                headers=headers,
                timeout=30.0
            )
            
            data = response.json()
            blogs = data.get("data", {}).get("blogs", {}).get("edges", [])
            
            if not blogs:
                return
            
            articles = blogs[0].get("node", {}).get("articles", {}).get("edges", [])
            if not articles:
                return
            
            latest_article = articles[0].get("node", {})
            article_id = latest_article.get("id")
            article_title = latest_article.get("title", "Articol nou")
            article_excerpt = latest_article.get("excerpt", "")[:100]
            
            # Check if this is a new article
            if last_seen_blog_id is None:
                # First run - just store the ID
                last_seen_blog_id = article_id
                # Check database for stored last ID
                stored = await db.app_state.find_one({"key": "last_seen_blog_id"})
                if stored:
                    last_seen_blog_id = stored.get("value")
                else:
                    await db.app_state.insert_one({
                        "key": "last_seen_blog_id",
                        "value": article_id
                    })
                logger.info(f"Blog checker initialized with ID: {article_id}")
                return
            
            if article_id != last_seen_blog_id:
                # New article detected!
                logger.info(f"New blog post detected: {article_title}")
                
                # Update stored ID
                last_seen_blog_id = article_id
                await db.app_state.update_one(
                    {"key": "last_seen_blog_id"},
                    {"$set": {"value": article_id}},
                    upsert=True
                )
                
                # Send push notification
                sent = await send_push_notification(
                    title="📰 Articol Nou!",
                    body=article_title,
                    data={
                        "type": "blog",
                        "articleId": article_id,
                        "title": article_title
                    }
                )
                
                logger.info(f"Push notifications sent to {sent} devices")
    
    except Exception as e:
        logger.error(f"Error checking for new blog posts: {e}")

async def blog_checker_task():
    """Background task to periodically check for new blog posts"""
    logger.info(f"Blog checker started - checking every {blog_check_interval} seconds")
    while True:
        await asyncio.sleep(blog_check_interval)
        await check_for_new_blog_posts()

@api_router.post("/push/test")
async def test_push_notification(request: Request):
    """Test endpoint to send a push notification to all devices"""
    await _require_admin(request)
    sent = await send_push_notification(
        title="🔔 Test Notificare",
        body="Aceasta este o notificare de test!",
        data={"type": "test"}
    )
    return {"success": True, "sent_to": sent}

@api_router.get("/push/debug")
async def debug_push_tokens(request: Request):
    """Debug endpoint to see push tokens and test sending"""
    await _require_admin(request)
    try:
        tokens = await db.push_tokens.find({}).to_list(100)
        token_info = []
        for t in tokens:
            token_info.append({
                "token_preview": t.get("push_token", "")[:30] + "..." if t.get("push_token") else None,
                "platform": t.get("platform"),
                "user_email": t.get("user_email"),
                "created_at": t.get("created_at")
            })
        
        # Test sending to first token
        test_result = None
        if tokens and tokens[0].get("push_token"):
            push_token = tokens[0]["push_token"]
            async with httpx.AsyncClient() as http_client:
                response = await http_client.post(
                    "https://exp.host/--/api/v2/push/send",
                    json=[{
                        "to": push_token,
                        "sound": "default",
                        "title": "🔔 Debug Test",
                        "body": "Test direct din debug endpoint",
                        "priority": "high",
                    }],
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                    },
                    timeout=30.0
                )
                test_result = {
                    "status_code": response.status_code,
                    "response": response.json() if response.status_code == 200 else response.text
                }
        
        return {
            "token_count": len(tokens),
            "tokens": token_info,
            "test_send_result": test_result
        }
    except Exception as e:
        return {"error": str(e)}

@api_router.post("/push/check-blogs")
async def trigger_blog_check(request: Request):
    """Manually trigger a blog check"""
    admin = await _require_admin(request)
    _enforce_rate_limit(
        f"admin:check-blogs:{admin['id']}", ADMIN_ACTION_LIMIT, ADMIN_ACTION_WINDOW_SECONDS,
        "Prea multe verificări de blog pornite recent. Încearcă din nou mai târziu.",
    )
    await check_for_new_blog_posts()
    return {"success": True, "message": "Blog check triggered"}

@api_router.get("/debug/shopify-token")
async def debug_shopify_token(request: Request):
    """Debug endpoint to check if Shopify Admin Token is set"""
    await _require_admin(request)
    has_token = bool(SHOPIFY_ADMIN_TOKEN and len(SHOPIFY_ADMIN_TOKEN) > 10)
    return {
        "has_shopify_admin_token": has_token,
        "token_length": len(SHOPIFY_ADMIN_TOKEN) if SHOPIFY_ADMIN_TOKEN else 0,
        "token_prefix": SHOPIFY_ADMIN_TOKEN[:10] + "..." if has_token else "NOT SET"
    }

@api_router.get("/debug/customer-notes/{email}")
async def debug_customer_notes(email: str, request: Request):
    """Debug endpoint to fetch customer notes"""
    await _require_admin(request)
    if not SHOPIFY_ADMIN_TOKEN:
        return {"error": "SHOPIFY_ADMIN_TOKEN not set", "notes": None}
    
    notes = await get_shopify_customer_notes(email)
    
    # Also test parsing
    parsed_equipment = []
    if notes:
        parsed_equipment = await parse_equipment_from_shopify_notes(notes)
    
    return {
        "email": email,
        "has_notes": bool(notes),
        "notes_preview": notes[:500] if notes else None,
        "notes_full_length": len(notes) if notes else 0,
        "contains_equipment": "UTILAJELE CLIENTULUI:" in notes if notes else False,
        "parsed_equipment_count": len(parsed_equipment),
        "parsed_equipment": parsed_equipment
    }

# ==================== EMAIL NOTIFICATIONS (BREVO) ====================

async def send_blog_notification_email(recipient_email: str, recipient_name: str, blog_title: str, blog_excerpt: str, blog_url: str):
    """Send email notification about new blog post using Brevo API"""
    try:
        if not BREVO_API_KEY:
            logger.warning("BREVO_API_KEY not set - skipping email")
            return False
        
        import sib_api_v3_sdk
        from sib_api_v3_sdk.rest import ApiException
        
        configuration = sib_api_v3_sdk.Configuration()
        configuration.api_key['api-key'] = BREVO_API_KEY
        
        api_instance = sib_api_v3_sdk.TransactionalEmailsApi(sib_api_v3_sdk.ApiClient(configuration))
        
        # Clean excerpt
        clean_excerpt = blog_excerpt.replace('<[^>]*>', '')[:200] if blog_excerpt else ''
        
        html_content = f"""
        <html>
        <body style="font-family: Arial, sans-serif; background-color: #f5f5f5; padding: 20px;">
            <div style="max-width: 600px; margin: 0 auto; background-color: #ffffff; border-radius: 10px; overflow: hidden;">
                <div style="background-color: #367c2b; padding: 20px; text-align: center;">
                    <h1 style="color: #ffffff; margin: 0;">🚜 AGB Agroparts</h1>
                </div>
                <div style="padding: 30px;">
                    <h2 style="color: #333;">Noutate pentru utilajul tău!</h2>
                    <h3 style="color: #367c2b;">{blog_title}</h3>
                    <p style="color: #666; line-height: 1.6;">{clean_excerpt}...</p>
                    <div style="text-align: center; margin-top: 30px;">
                        <a href="{blog_url}" style="background-color: #367c2b; color: #ffffff; padding: 15px 30px; text-decoration: none; border-radius: 5px; font-weight: bold;">
                            Citește articolul
                        </a>
                    </div>
                </div>
                <div style="background-color: #f0f0f0; padding: 15px; text-align: center; font-size: 12px; color: #999;">
                    <p>Primești acest email pentru că ai un utilaj înregistrat în aplicația AGB Agroparts.</p>
                    <p>AGB Agroparts Solution S.R.L.</p>
                </div>
            </div>
        </body>
        </html>
        """
        
        send_smtp_email = sib_api_v3_sdk.SendSmtpEmail(
            to=[{"email": recipient_email, "name": recipient_name}],
            sender={"email": "noreply@agb-agroparts.ro", "name": "AGB Agroparts"},
            subject=f"🚜 Noutate: {blog_title}",
            html_content=html_content
        )
        
        api_instance.send_transac_email(send_smtp_email)
        logger.info(f"Email sent to {recipient_email} for blog: {blog_title}")
        return True
        
    except Exception as e:
        logger.error(f"Error sending email to {recipient_email}: {e}")
        return False

@api_router.post("/notifications/send-blog-emails")
async def send_blog_notification_to_matching_users(
    request: Request,
    blog_title: str,
    blog_excerpt: str = "",
    blog_url: str = "",
    model_tags: list = []
):
    """Send email notifications to users whose equipment matches the blog tags"""
    admin = await _require_admin(request)
    _enforce_rate_limit(
        f"admin:send-blog-emails:{admin['id']}", ADMIN_ACTION_LIMIT, ADMIN_ACTION_WINDOW_SECONDS,
        "Prea multe trimiteri de notificări pornite recent. Încearcă din nou mai târziu.",
    )
    try:
        if not BREVO_API_KEY:
            raise HTTPException(status_code=500, detail="BREVO_API_KEY not configured")
        
        # Get all users with equipment
        users_with_equipment = await db.users.find(
            {"equipment": {"$exists": True, "$ne": []}},
            {"email": 1, "name": 1, "equipment": 1, "notify_news_email": 1}
        ).to_list(1000)

        sent_count = 0
        matched_users = []

        for user in users_with_equipment:
            if user.get("notify_news_email", True) is False:
                continue
            user_email = user.get("email", "")
            user_name = user.get("name", "Client")
            user_equipment = user.get("equipment", [])
            
            # Get user's equipment models
            user_models = [eq.get("model", "").lower().strip() for eq in user_equipment if eq.get("model")]
            
            # If no tags specified, send to all users with equipment
            if not model_tags:
                should_send = True
            else:
                # Check if any user model matches any blog tag
                normalized_tags = [tag.lower().strip() for tag in model_tags]
                should_send = any(
                    any(tag in model or model in tag for tag in normalized_tags)
                    for model in user_models
                )
            
            if should_send and user_email:
                success = await send_blog_notification_email(
                    recipient_email=user_email,
                    recipient_name=user_name,
                    blog_title=blog_title,
                    blog_excerpt=blog_excerpt,
                    blog_url=blog_url
                )
                if success:
                    sent_count += 1
                    matched_users.append(user_email)
        
        return {
            "success": True,
            "emails_sent": sent_count,
            "matched_users_count": len(matched_users),
            "model_tags_used": model_tags
        }
        
    except Exception as e:
        logger.error(f"Error sending blog notifications: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# Include the router in the main app - MUST be after all route definitions
app.include_router(api_router)
