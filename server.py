from fastapi import FastAPI, APIRouter, HTTPException, Query, BackgroundTasks, Request, UploadFile, File, Body
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import logging
from pathlib import Path
from pydantic import BaseModel, Field
from typing import List, Optional, Literal
import uuid
from datetime import datetime, timedelta
import httpx
import re
import json
import unicodedata
import asyncio
import hashlib
import hmac
import secrets
import bcrypt
import cloudinary
import cloudinary.uploader

ROOT_DIR = Path(__file__).parent
# Load .env but don't override existing environment variables (important for Render deployment)
load_dotenv(ROOT_DIR / '.env', override=False)

# MongoDB connection
# maxPoolSize explicit (was left at the driver default of 100) - this
# process shares a ~500-connection Atlas M0 budget with agb-crm, and 100
# idle-capable connections from a single-worker process is more than this
# app's actual concurrency needs (each request holds a connection only
# briefly, being I/O-bound async). 20 leaves headroom for agb-crm and for
# any future horizontal scaling of this service.
mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url, maxPoolSize=20)
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

# Auto-sync configuration
AUTO_SYNC_INTERVAL_MINUTES = int(os.environ.get('AUTO_SYNC_INTERVAL_MINUTES', '5'))  # Default 5 minutes

# Cloudinary configuration (admin product image uploads)
cloudinary.config(
    cloud_name=os.environ.get('CLOUDINARY_CLOUD_NAME', ''),
    api_key=os.environ.get('CLOUDINARY_API_KEY', ''),
    api_secret=os.environ.get('CLOUDINARY_API_SECRET', ''),
    secure=True,
)

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

class OrderCreate(BaseModel):
    session_id: str
    items: List[dict]
    customer: CustomerInfo
    subtotal: float
    shipping: float = 25.0
    total: float
    payment_method: str = "ramburs"

# ==================== AUTH MODELS ====================

class UserRegister(BaseModel):
    email: str
    password: str
    name: str
    phone: str

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
    an over-sized array.
    """
    new_token = generate_token()
    await db.users.update_one(
        {"email": email},
        {"$push": {"tokens": {"$each": [new_token], "$slice": -MAX_DEVICE_TOKENS}}},
    )
    return new_token


async def _find_user_by_token(token: str, allow_shopify_access_token: bool = False) -> Optional[dict]:
    """Resolve a bearer token to a user doc.

    Checks the `tokens` array (current, multi-device scheme) first, then
    falls back to a legacy singular `token` field for any account the
    one-time startup migration hasn't converted yet (belt-and-braces safety
    net - in steady state every account should already have `tokens`).

    When `allow_shopify_access_token` is set, also matches on the user's
    stored Shopify customer access token, matching the handful of endpoints
    (equipment CRUD) that have always accepted either credential type as a
    bearer token.
    """
    or_clauses = [{"tokens": token}, {"token": token}]
    if allow_shopify_access_token:
        or_clauses.append({"shopify_access_token": token})
    return await db.users.find_one({"$or": or_clauses})

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
        preserved_images = {
            p["id"]: {"image_url": p.get("image_url"), "images": p.get("images") or []}
            for p in await db.shopify_products.find(
                {
                    "source": {"$ne": "manual"},
                    "$or": [
                        {"image_url": {"$regex": "res.cloudinary.com"}},
                        {"images": {"$regex": "res.cloudinary.com"}},
                    ],
                },
                {"id": 1, "image_url": 1, "images": 1}
            ).to_list(None)
        }

        # Clear existing Shopify-synced products, but keep manually-created
        # products (source="manual") - those aren't part of the Shopify catalog
        # and would be permanently lost if wiped here.
        await db.shopify_products.delete_many({"source": {"$ne": "manual"}})

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
                    product["image_url"] = preserved_images[product["id"]]["image_url"]
                    product["images"] = preserved_images[product["id"]]["images"]
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
async def start_sync(background_tasks: BackgroundTasks):
    """Start syncing all products from Shopify"""
    if sync_status["is_syncing"]:
        return {"message": "Sincronizare deja în curs", "status": sync_status}
    
    background_tasks.add_task(sync_all_products)
    return {"message": "Sincronizare pornită! Verificați /api/sync/status pentru progres"}

@api_router.post("/sync/collections")
async def sync_collections(background_tasks: BackgroundTasks):
    """Sync only collections to existing products"""
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
}


def build_products_query(
    search: Optional[str],
    product_type: Optional[str],
    collection: Optional[str],
) -> dict:
    """Builds the MongoDB filter dict for the storefront catalog/search -
    shared by GET /products (paginated results) and GET /products/count
    (total match count for the "N produse găsite" indicator), so the two
    always agree on what counts as a match."""
    query = {}

    if product_type:
        query["product_type"] = product_type

    if collection:
        # `collections` is a list field per product; querying it with a
        # scalar matches documents where the array contains that value.
        query["collections"] = collection

    if search:
        # Normalize search terms and handle "Premium" variations
        # Convert "6930 Premium" to search for both "6930Premium" and "6930PR"
        premium_pattern = re.compile(r'(\d{4})\s*Premium', re.IGNORECASE)
        premium_matches = premium_pattern.findall(search)

        search_terms = [normalize_text(term) for term in search.split() if term.strip()]

        # Remove "premium" from search terms if it was part of a model number
        if premium_matches:
            search_terms = [t for t in search_terms if t.lower() != 'premium']

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
                    has_digit = bool(re.search(r'\d', term))
                    if has_digit or len(term) < 5:
                        term_regex = f"\\b{term}\\b"
                    else:
                        term_regex = f"\\b{term}"
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
):
    """Total count of products matching the same filters as GET /products -
    powers the "N produse găsite" indicator on the storefront catalog/search
    page (which itself only loads a page at a time via infinite scroll, so
    it has no way to know the true total on its own). Named distinctly from
    the pre-existing unfiltered GET /products/count (used elsewhere for the
    Shopify-sync total) rather than overloading it."""
    query = build_products_query(search, product_type, collection)
    total = await db.shopify_products.count_documents(query)
    return {"total": total}


@api_router.get("/products", response_model=List[Product])
async def get_products(
    search: Optional[str] = None,
    product_type: Optional[str] = None,
    collection: Optional[str] = None,
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

        query = build_products_query(search, product_type, collection)

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
async def get_product_vendors():
    """Get distinct vendor/brand names from the database, for the admin
    product form's Marcă field."""
    vendors = await db.shopify_products.distinct("vendor")
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

    payload = {
        "source": "webshop",
        "source_order_id": order.id,
        "payment_method": order.payment_method,
        "note": order.customer.notes,
        "customer": {
            "nume": order.customer.name,
            "email": order.customer.email,
            "telefon": order.customer.phone,
            "adresa_strada": order.customer.address,
            "adresa_oras": order.customer.city,
            "adresa_judet": order.customer.county,
            "adresa_cod_postal": order.customer.postal_code,
        },
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
    order = Order(**order_data.dict())
    await db.orders.insert_one(order.dict())
    await db.cart.delete_many({"session_id": order_data.session_id})
    background_tasks.add_task(sync_order_to_crm, order)
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
async def register_user(user_data: UserRegister, background_tasks: BackgroundTasks):
    """Register a new user - fully local account, independent of Shopify"""

    email = user_data.email.lower().strip()

    if len(user_data.password) < 6:
        raise HTTPException(status_code=400, detail="Parola trebuie să aibă minim 6 caractere")

    existing_user = await db.users.find_one({"email": email})
    if existing_user:
        raise HTTPException(status_code=400, detail="Adresa de email este deja înregistrată")

    user_id = str(uuid.uuid4())
    local_token = generate_token()
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
        "tokens": [local_token],
        "is_shopify_customer": False,
        "created_at": created_at
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
async def forgot_password(request: ForgotPasswordRequest):
    """Send a local password reset email. Always returns a generic success
    message regardless of whether the address is registered, to avoid
    leaking which emails have accounts."""
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
        local_token = generate_token()
        user_update_data["tokens"] = [local_token]
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
    }


async def _authenticate_user(email: str, password: str) -> dict:
    """Authenticate a customer for login. Checks the local password hash
    first; only falls back to Shopify (and silently migrates) for accounts
    that don't have one yet. See `_legacy_shopify_login_and_migrate`.
    """
    email = email.lower().strip()
    existing_user = await db.users.find_one({"email": email})

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
async def login_user(credentials: UserLogin):
    """Login a user - local password first, Shopify fallback for legacy accounts"""
    return await _authenticate_user(credentials.email, credentials.password)

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

# ==================== SHOPIFY CUSTOMER AUTH ====================

@api_router.post("/auth/shopify-login")
async def shopify_customer_login(credentials: ShopifyCustomerLogin):
    """Kept for backward compatibility with older app builds that call this
    route specifically; behaves identically to /auth/login now."""
    return await _authenticate_user(credentials.email, credentials.password)

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
    
    # Build update dict with only non-None values
    update_dict = {}
    for field, value in update_data.dict().items():
        if value is not None:
            update_dict[field] = value

    # address/company_address stay in sync as a derived combo whenever the
    # split fields are sent - a handful of older call sites (checkout
    # prefill, admin order display, the CRM order sync payload) still read
    # the single combined field, so it can't just go stale the moment
    # someone starts using the new split fields instead.
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


@api_router.post("/auth/logout")
async def logout_user(request: Request):
    """Logout the current device only: free up this token's slot in the
    `tokens` array so the user can log back in elsewhere without hitting the
    device cap, without touching that user's other active sessions."""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header.replace("Bearer ", "")
        await db.users.update_one(
            {"tokens": token},
            {"$pull": {"tokens": token}}
        )
        # Legacy safety net: also clear it if this account still had it
        # stored as a single un-migrated `token` field (see
        # _find_user_by_token / the startup migration).
        await db.users.update_one(
            {"token": token},
            {"$unset": {"token": ""}}
        )
    return {"message": "Deconectat cu succes"}

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

class ProductCreate(BaseModel):
    title: str
    description: str = ""
    technical_specs: Optional[str] = None
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

class ProductBulkSaveItem(BaseModel):
    id: str
    patch: ProductUpdate

class ProductBulkSave(BaseModel):
    updates: List[ProductBulkSaveItem]

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

async def _require_admin(request: Request) -> dict:
    """Resolve the bearer token to a user and confirm they have the admin
    role. There's no self-serve way to become admin - the role is only ever
    set directly in the database for the store owner's own account."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Token lipsă sau invalid")

    token = auth_header.replace("Bearer ", "")
    user = await _find_user_by_token(token)

    if not user:
        raise HTTPException(status_code=401, detail="Token invalid sau expirat")

    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Acces interzis")

    return user

@api_router.get("/admin/products")
async def admin_list_products(request: Request, search: Optional[str] = None, limit: int = 100, skip: int = 0):
    """List/search the full product catalog (originally Shopify-imported
    products and manually-created ones alike - both are now owned by this
    database, see sync_all_products()). Returns a total count alongside the
    page of results so the admin list can render real page-number
    pagination instead of silently truncating at one page."""
    await _require_admin(request)

    query = {}
    if search:
        term = normalize_text(search)
        query["$or"] = [
            {"title_normalized": {"$regex": term, "$options": "i"}},
            {"sku": {"$regex": term, "$options": "i"}},
        ]

    total = await db.shopify_products.count_documents(query)
    cursor = db.shopify_products.find(query).sort("title_normalized", 1).skip(skip).limit(limit)
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

@api_router.post("/admin/products")
async def admin_create_product(request: Request, product_data: ProductCreate):
    await _require_admin(request)

    product_id = f"local-{uuid.uuid4()}"
    now = datetime.utcnow()
    collections = build_collections(product_data.product_type, product_data.category, [])
    product = {
        "id": product_id,
        "title": product_data.title,
        "handle": slugify(product_data.title),
        "description": product_data.description,
        "technical_specs": product_data.technical_specs,
        "description_normalized": normalize_text(product_data.description),
        "title_normalized": normalize_text(product_data.title),
        "price": product_data.price,
        "currency": product_data.currency,
        "image_url": product_data.image_url,
        "images": product_data.images,
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
    }
    await db.shopify_products.insert_one(product)
    return Product(**product)

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
    if "title" in update_dict:
        update_dict["title_normalized"] = normalize_text(update_dict["title"])
        update_dict["handle"] = slugify(update_dict["title"])
    if "description" in update_dict:
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
    await _require_admin(request)

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

    return {"updated": updated_count, "not_found": not_found}

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
    await _require_admin(request)

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
    await _require_admin(request)

    updated = await _apply_product_update(product_id, product_data)
    if updated is None:
        raise HTTPException(status_code=404, detail="Produs inexistent")

    return Product(**updated)

@api_router.delete("/admin/products/{product_id}")
async def admin_delete_product(product_id: str, request: Request):
    await _require_admin(request)

    existing = await db.shopify_products.find_one({"id": product_id})
    if not existing:
        raise HTTPException(status_code=404, detail="Produs inexistent")

    await db.shopify_products.delete_one({"id": product_id})
    return {"message": "Produs șters"}

@api_router.post("/admin/upload-image")
async def admin_upload_image(request: Request, file: UploadFile = File(...)):
    """Upload a product image (e.g. exported from Canva) to Cloudinary and
    return its public URL, for pasting into the image fields above."""
    await _require_admin(request)

    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Fișierul trebuie să fie o imagine")

    contents = await file.read()
    try:
        # Offloaded to a thread, same reasoning as the bulk migration
        # function below - cloudinary.uploader.upload() is a blocking
        # network call, and this process runs a single Uvicorn worker, so
        # calling it directly would stall every other concurrent request
        # for the duration of the upload.
        result = await asyncio.to_thread(
            cloudinary.uploader.upload,
            contents,
            folder="agb-agroparts/products",
            resource_type="image",
        )
    except Exception as e:
        logger.error(f"Cloudinary upload error: {e}")
        raise HTTPException(status_code=502, detail="Încărcarea imaginii a eșuat")

    return {"url": result["secure_url"]}

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
    await _require_admin(request)
    if image_migration_status["is_running"]:
        raise HTTPException(status_code=409, detail="Migrarea rulează deja")
    background_tasks.add_task(_run_image_migration, limit)
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

    native_orders = await db.orders.find({}).to_list(5000)
    shopify_orders = await db.shopify_order_history.find({}).to_list(5000)

    client_ids = list({o["client_id"] for o in shopify_orders if o.get("client_id")})
    clients_by_id = {}
    if client_ids:
        async for c in db.clients.find({"id": {"$in": client_ids}}):
            clients_by_id[c["id"]] = c

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
    await _require_admin(request)

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

    items = [item.dict() for item in payload.items]
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

    if was_crm_synced:
        background_tasks.add_task(sync_order_update_to_crm, Order(**updated))

    return updated

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
    await _require_admin(request)
    if clients_import_status["is_running"]:
        raise HTTPException(status_code=409, detail="Importul rulează deja")
    background_tasks.add_task(_run_clients_import, since, limit)
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
    await _require_admin(request)
    if shopify_orders_import_status["is_running"]:
        raise HTTPException(status_code=409, detail="Importul rulează deja")
    background_tasks.add_task(_run_shopify_full_orders_import)
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
    cursor = db.clients.find(query).sort("name_normalized", 1).skip(skip).limit(limit)
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

    local_equipment = user.get("equipment", [])

    # Convert None values to empty strings for frontend
    cleaned_equipment = []
    for eq in local_equipment:
        cleaned_eq = {
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
        }
        cleaned_equipment.append(cleaned_eq)
    
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
    await _require_admin(request)

    if option_data.category not in EQUIPMENT_OPTION_CATEGORIES:
        raise HTTPException(status_code=400, detail="Categorie invalidă")

    value = (option_data.value or "").strip()
    if not value:
        raise HTTPException(status_code=400, detail="Valoare invalidă")

    existing = await db.equipment_options.find_one({
        "category": option_data.category,
        "value": {"$regex": f"^{re.escape(value)}$", "$options": "i"},
    })
    if not existing:
        await db.equipment_options.insert_one({
            "id": str(uuid.uuid4()),
            "category": option_data.category,
            "value": value,
            "created_at": datetime.utcnow(),
        })

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

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
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
        
        # Exchange code for access token
        # Use shop domain from callback or fallback to configured store
        shop_domain = shop if shop else SHOPIFY_STORE
        if shop_domain and not shop_domain.endswith('.myshopify.com'):
            shop_domain = f"{shop_domain}.myshopify.com"
        
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
            
            logger.info(f"Successfully obtained and stored Shopify Admin API token")
            
            # Return success HTML page
            html = f"""
            <!DOCTYPE html>
            <html>
            <head>
                <title>AGB Mobile API - Succes!</title>
                <style>
                    body {{ font-family: Arial, sans-serif; text-align: center; padding: 50px; background: #1a1a1a; color: #fff; }}
                    .success {{ color: #367c2b; font-size: 24px; margin-bottom: 20px; }}
                    .token {{ background: #2a2a2a; padding: 20px; border-radius: 8px; margin: 20px auto; max-width: 600px; word-break: break-all; }}
                    .note {{ color: #f5a623; margin-top: 20px; }}
                </style>
            </head>
            <body>
                <h1 class="success">✅ Autorizare Reușită!</h1>
                <p>Aplicația AGB Mobile API a fost autorizată cu succes.</p>
                <div class="token">
                    <strong>Access Token (salvat în baza de date):</strong><br><br>
                    <code>{access_token[:20]}...{access_token[-10:]}</code>
                </div>
                <p class="note">⚠️ Pentru siguranță, adăugați acest token ca variabilă de mediu SHOPIFY_ADMIN_TOKEN în Render.</p>
                <p>Token complet: <code>{access_token}</code></p>
                <p>Puteți închide această pagină.</p>
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
async def trigger_blog_check():
    """Manually trigger a blog check"""
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
    blog_title: str,
    blog_excerpt: str = "",
    blog_url: str = "",
    model_tags: list = []
):
    """Send email notifications to users whose equipment matches the blog tags"""
    try:
        if not BREVO_API_KEY:
            raise HTTPException(status_code=500, detail="BREVO_API_KEY not configured")
        
        # Get all users with equipment
        users_with_equipment = await db.users.find(
            {"equipment": {"$exists": True, "$ne": []}},
            {"email": 1, "name": 1, "equipment": 1}
        ).to_list(1000)
        
        sent_count = 0
        matched_users = []
        
        for user in users_with_equipment:
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
            "matched_users": matched_users,
            "model_tags_used": model_tags
        }
        
    except Exception as e:
        logger.error(f"Error sending blog notifications: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# Include the router in the main app - MUST be after all route definitions
app.include_router(api_router)
