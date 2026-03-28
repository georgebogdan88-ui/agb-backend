from fastapi import FastAPI, APIRouter, HTTPException, Query, BackgroundTasks, Request
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import logging
from pathlib import Path
from pydantic import BaseModel, Field
from typing import List, Optional
import uuid
from datetime import datetime, timedelta
import httpx
import re
import unicodedata
import asyncio
import hashlib
import hmac

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

# MongoDB connection
mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
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

# Auto-sync configuration
AUTO_SYNC_INTERVAL_MINUTES = int(os.environ.get('AUTO_SYNC_INTERVAL_MINUTES', '5'))  # Default 5 minutes

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
    city: Optional[str] = None
    county: Optional[str] = None
    postal_code: Optional[str] = None
    # Company fields
    is_company: Optional[bool] = None
    company_name: Optional[str] = None
    cui: Optional[str] = None
    reg_com: Optional[str] = None
    company_address: Optional[str] = None

# ==================== EQUIPMENT MODELS ====================

class Equipment(BaseModel):
    """Model for customer's equipment/tractors"""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
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
    model: str
    chassis_serial: Optional[str] = None
    engine_serial: Optional[str] = None
    engine_type: Optional[str] = None
    transmission_type: Optional[str] = None
    front_axle_model: Optional[str] = None
    features: Optional[List[str]] = None

class EquipmentUpdate(BaseModel):
    """Update equipment request"""
    model: Optional[str] = None
    chassis_serial: Optional[str] = None
    engine_serial: Optional[str] = None
    engine_type: Optional[str] = None
    transmission_type: Optional[str] = None
    front_axle_model: Optional[str] = None
    features: Optional[List[str]] = None

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
def hash_password(password: str) -> str:
    """Hash password using SHA256"""
    return hashlib.sha256(password.encode()).hexdigest()

def verify_password(password: str, hashed: str) -> bool:
    """Verify password against hash"""
    return hash_password(password) == hashed

def generate_token() -> str:
    """Generate a simple auth token"""
    return str(uuid.uuid4()) + "-" + str(uuid.uuid4())

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

async def sync_all_products():
    """Sync ALL products from Shopify to MongoDB"""
    global sync_status
    
    if sync_status["is_syncing"]:
        return
    
    sync_status["is_syncing"] = True
    sync_status["total_synced"] = 0
    sync_status["error"] = None
    
    try:
        # Clear existing products
        await db.shopify_products.delete_many({})
        
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
        
        sync_status["total_synced"] = total_products
        sync_status["last_sync"] = datetime.utcnow().isoformat()
        logger.info(f"Sync complete! Total products: {total_products}")
        
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

@api_router.get("/products", response_model=List[Product])
async def get_products(
    search: Optional[str] = None,
    product_type: Optional[str] = None,
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
        
        # Build query
        query = {}
        
        if product_type:
            query["product_type"] = product_type
        
        if search:
            # Normalize search terms and handle "Premium" variations
            # Convert "6930 Premium" to search for both "6930Premium" and "6930PR"
            search_normalized = search
            
            # Handle "Premium" -> also search for "PR" variant
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
                        # For regular terms (non-model numbers)
                        # Short terms (<=4 chars) should use word boundaries to avoid false matches
                        # e.g., "usa" should not match inside other words like "caUzA"
                        if len(term) <= 4:
                            # Short terms - search with word boundary, prioritize title
                            regex_conditions.append({
                                "$or": [
                                    {"title_normalized": {"$regex": f"\\b{term}\\b", "$options": "i"}},
                                    {"description_normalized": {"$regex": f"\\b{term}\\b", "$options": "i"}},
                                    {"sku": {"$regex": f"\\b{term}\\b", "$options": "i"}}
                                ]
                            })
                        else:
                            # Longer terms - search normally
                            regex_conditions.append({
                                "$or": [
                                    {"title_normalized": {"$regex": term, "$options": "i"}},
                                    {"description_normalized": {"$regex": term, "$options": "i"}},
                                    {"compatible_models": {"$regex": term, "$options": "i"}},
                                    {"sku": {"$regex": term, "$options": "i"}}
                                ]
                            })
                
                if regex_conditions:
                    query["$and"] = regex_conditions
        
        # Execute query
        cursor = db.shopify_products.find(query).skip(skip).limit(limit)
        products = await cursor.to_list(limit)
        
        # Sort by relevance if searching
        if search:
            search_normalized = normalize_text(search)
            products.sort(
                key=lambda p: (
                    -10 if search_normalized in p.get("title_normalized", "") else 0,
                    -5 if search_normalized in p.get("description_normalized", "") else 0,
                    -1 if p.get("stock", 0) > 0 else 0
                )
            )
        
        return [Product(**p) for p in products]
        
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
    """Get featured products - prioritize in-stock items"""
    try:
        product_count = await db.shopify_products.count_documents({})
        
        if product_count > 0:
            # Get from local DB
            products = await db.shopify_products.find(
                {"stock": {"$gt": 0}}
            ).limit(limit).to_list(limit)
            
            if len(products) < limit:
                # Add out-of-stock if needed
                more = await db.shopify_products.find(
                    {"stock": 0}
                ).limit(limit - len(products)).to_list(limit - len(products))
                products.extend(more)
            
            return [Product(**p) for p in products]
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

@api_router.get("/products/{product_id}/complementary")
async def get_complementary_products(product_id: str):
    """Get complementary and related products from Shopify metafields"""
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
            return Product(**product)
        
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

@api_router.get("/cart/{session_id}", response_model=List[CartItem])
async def get_cart(session_id: str):
    """Get cart items for a session"""
    items = await db.cart.find({"session_id": session_id}).to_list(100)
    return [CartItem(**item) for item in items]

@api_router.post("/cart", response_model=CartItem)
async def add_to_cart(item: CartItemCreate):
    """Add item to cart"""
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

@api_router.post("/orders", response_model=Order)
async def create_order(order_data: OrderCreate):
    """Create a new order"""
    order = Order(**order_data.dict())
    await db.orders.insert_one(order.dict())
    await db.cart.delete_many({"session_id": order_data.session_id})
    return order

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

@api_router.post("/auth/register")
async def register_user(user_data: UserRegister):
    """Register a new user - Creates customer in Shopify"""
    
    # Step 1: Create customer in Shopify
    mutation = """
    mutation customerCreate($input: CustomerCreateInput!) {
        customerCreate(input: $input) {
            customer {
                id
                email
                firstName
                lastName
                phone
            }
            customerUserErrors {
                code
                field
                message
            }
        }
    }
    """
    
    # Split name into first and last name
    name_parts = user_data.name.strip().split(' ', 1)
    first_name = name_parts[0]
    last_name = name_parts[1] if len(name_parts) > 1 else ""
    
    variables = {
        "input": {
            "email": user_data.email.lower(),
            "password": user_data.password,
            "firstName": first_name,
            "lastName": last_name,
            "phone": user_data.phone if user_data.phone else None,
            "acceptsMarketing": True
        }
    }
    
    headers = {
        "Content-Type": "application/json",
        "X-Shopify-Storefront-Access-Token": SHOPIFY_STOREFRONT_TOKEN
    }
    
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"https://{SHOPIFY_STORE}/api/{SHOPIFY_API_VERSION}/graphql.json",
            json={"query": mutation, "variables": variables},
            headers=headers
        )
        
        data = response.json()
        logger.info(f"Shopify customerCreate response: {data}")
        
        result = data.get("data", {}).get("customerCreate", {})
        errors = result.get("customerUserErrors", [])
        
        if errors:
            error_msg = errors[0].get("message", "Eroare la înregistrare")
            error_code = errors[0].get("code", "")
            error_field = errors[0].get("field", [""])[0] if errors[0].get("field") else ""
            
            logger.info(f"Registration error - code: {error_code}, field: {error_field}, msg: {error_msg}")
            
            if error_code == "TAKEN":
                if error_field == "phone" or "phone" in error_msg.lower():
                    raise HTTPException(status_code=400, detail="Numărul de telefon este deja înregistrat pe alt cont")
                elif error_field == "email" or "email" in error_msg.lower():
                    # Check if user exists in local DB but was deleted from Shopify
                    existing_user = await db.users.find_one({"email": user_data.email.lower()})
                    if existing_user:
                        # Delete from local DB to allow re-registration
                        await db.users.delete_one({"email": user_data.email.lower()})
                        logger.info(f"Deleted orphaned local user: {user_data.email}")
                    raise HTTPException(status_code=400, detail="Adresa de email este deja înregistrată în Shopify")
                else:
                    raise HTTPException(status_code=400, detail="Email-ul sau telefonul este deja înregistrat")
            if error_code == "TOO_SHORT":
                raise HTTPException(status_code=400, detail="Parola trebuie să aibă minim 5 caractere")
            if error_code == "INVALID":
                if "phone" in error_field.lower() or "phone" in error_msg.lower():
                    raise HTTPException(status_code=400, detail="Numărul de telefon este invalid. Folosiți formatul +40XXXXXXXXX")
                elif "email" in error_field.lower() or "email" in error_msg.lower():
                    raise HTTPException(status_code=400, detail="Adresa de email este invalidă")
            raise HTTPException(status_code=400, detail=error_msg)
        
        customer = result.get("customer")
        if not customer:
            raise HTTPException(status_code=400, detail="Eroare la crearea contului")
    
    # Step 2: Login to get access token
    login_mutation = """
    mutation customerAccessTokenCreate($input: CustomerAccessTokenCreateInput!) {
        customerAccessTokenCreate(input: $input) {
            customerAccessToken {
                accessToken
                expiresAt
            }
            customerUserErrors {
                code
                message
            }
        }
    }
    """
    
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"https://{SHOPIFY_STORE}/api/{SHOPIFY_API_VERSION}/graphql.json",
            json={
                "query": login_mutation, 
                "variables": {
                    "input": {
                        "email": user_data.email.lower(),
                        "password": user_data.password
                    }
                }
            },
            headers=headers
        )
        
        login_data = response.json()
        token_result = login_data.get("data", {}).get("customerAccessTokenCreate", {})
        shopify_token = token_result.get("customerAccessToken", {}).get("accessToken")
    
    # Step 3: Create local user record linked to Shopify
    user_id = str(uuid.uuid4())
    local_token = generate_token()
    shopify_customer_id = customer.get("id", "").replace("gid://shopify/Customer/", "")
    
    user = {
        "id": user_id,
        "email": user_data.email.lower(),
        "password_hash": "",
        "name": user_data.name,
        "phone": user_data.phone,
        "address": None,
        "city": None,
        "county": None,
        "postal_code": None,
        "is_company": False,
        "company_name": None,
        "cui": None,
        "reg_com": None,
        "company_address": None,
        "token": local_token,
        "is_shopify_customer": True,
        "shopify_customer_id": shopify_customer_id,
        "shopify_access_token": shopify_token,
        "created_at": datetime.utcnow()
    }
    
    await db.users.insert_one(user)
    
    return {
        "token": local_token,
        "user": {
            "id": user_id,
            "email": user_data.email.lower(),
            "name": user_data.name,
            "phone": user_data.phone,
            "is_company": False,
            "is_shopify_customer": True,
            "created_at": user["created_at"]
        }
    }

class ForgotPasswordRequest(BaseModel):
    email: str

@api_router.post("/auth/forgot-password")
async def forgot_password(request: ForgotPasswordRequest):
    """Send password reset email via Shopify"""
    
    mutation = """
    mutation customerRecover($email: String!) {
        customerRecover(email: $email) {
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
            json={"query": mutation, "variables": {"email": request.email.lower()}},
            headers=headers
        )
        
        data = response.json()
        logger.info(f"Shopify password recovery response: {data}")
        
        errors = data.get("data", {}).get("customerRecover", {}).get("customerUserErrors", [])
        
        if errors:
            error_msg = errors[0].get("message", "Eroare la trimiterea emailului")
            raise HTTPException(status_code=400, detail=error_msg)
        
        return {"message": "Email de resetare trimis cu succes"}

@api_router.post("/auth/login")
async def login_user(credentials: UserLogin):
    """Login a user - Uses Shopify authentication"""
    
    # Authenticate with Shopify
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
    
    variables = {
        "input": {
            "email": credentials.email,
            "password": credentials.password
        }
    }
    
    headers = {
        "Content-Type": "application/json",
        "X-Shopify-Storefront-Access-Token": SHOPIFY_STOREFRONT_TOKEN
    }
    
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"https://{SHOPIFY_STORE}/api/{SHOPIFY_API_VERSION}/graphql.json",
            json={"query": mutation, "variables": variables},
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
    local_token = generate_token()
    shopify_customer_id = customer.get("id", "").replace("gid://shopify/Customer/", "")
    
    existing_user = await db.users.find_one({"email": credentials.email.lower()})
    
    default_address = customer.get("defaultAddress") or {}
    
    user_update_data = {
        "email": credentials.email.lower(),
        "name": f"{customer.get('firstName', '')} {customer.get('lastName', '')}".strip() or credentials.email.split('@')[0],
        "phone": customer.get("phone") or "",
        "address": default_address.get("address1") or "",
        "city": default_address.get("city") or "",
        "county": default_address.get("province") or "",
        "postal_code": default_address.get("zip") or "",
        "is_company": bool(default_address.get("company")),
        "company_name": default_address.get("company") or None,
        "token": local_token,
        "is_shopify_customer": True,
        "shopify_customer_id": shopify_customer_id,
        "shopify_access_token": shopify_access_token,
        "updated_at": datetime.utcnow()
    }
    
    if existing_user:
        # Preserve locally-saved data that Shopify doesn't have
        # Only update fields from Shopify if they have values, otherwise keep existing
        preserved_fields = ['cui', 'reg_com', 'company_address', 'is_company', 'company_name']
        for field in preserved_fields:
            if not user_update_data.get(field) and existing_user.get(field):
                user_update_data[field] = existing_user[field]
        
        # Also preserve phone, address, city, county, postal_code if Shopify doesn't have them
        local_fields = ['phone', 'address', 'city', 'county', 'postal_code']
        for field in local_fields:
            if not user_update_data.get(field) and existing_user.get(field):
                user_update_data[field] = existing_user[field]
        
        await db.users.update_one(
            {"email": credentials.email.lower()},
            {"$set": user_update_data}
        )
        user_id = existing_user["id"]
        created_at = existing_user["created_at"]
    else:
        user_id = str(uuid.uuid4())
        user_update_data["id"] = user_id
        user_update_data["password_hash"] = ""
        user_update_data["created_at"] = datetime.utcnow()
        created_at = user_update_data["created_at"]
        await db.users.insert_one(user_update_data)
    
    # Extract Shopify orders
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
    
    return {
        "token": local_token,
        "user": {
            "id": user_id,
            "email": credentials.email.lower(),
            "name": user_update_data["name"],
            "phone": user_update_data["phone"],
            "address": user_update_data["address"],
            "city": user_update_data["city"],
            "county": user_update_data["county"],
            "postal_code": user_update_data["postal_code"],
            "is_company": user_update_data.get("is_company", False),
            "company_name": user_update_data.get("company_name"),
            "cui": user_update_data.get("cui"),
            "reg_com": user_update_data.get("reg_com"),
            "company_address": user_update_data.get("company_address"),
            "is_shopify_customer": True,
            "created_at": created_at
        },
        "shopify_orders": shopify_orders
    }

@api_router.get("/auth/me")
async def get_current_user(request: Request):
    """Get current user by token"""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Token lipsă sau invalid")
    
    token = auth_header.replace("Bearer ", "")
    user = await db.users.find_one({"token": token})
    
    if not user:
        raise HTTPException(status_code=401, detail="Token invalid sau expirat")
    
    return {
        "id": user["id"],
        "email": user["email"],
        "name": user["name"],
        "phone": user["phone"],
        "address": user.get("address"),
        "city": user.get("city"),
        "county": user.get("county"),
        "postal_code": user.get("postal_code"),
        "is_company": user.get("is_company", False),
        "company_name": user.get("company_name"),
        "cui": user.get("cui"),
        "reg_com": user.get("reg_com"),
        "company_address": user.get("company_address"),
        "created_at": user["created_at"],
        "is_shopify_customer": user.get("is_shopify_customer", False)
    }

# ==================== SHOPIFY CUSTOMER AUTH ====================

@api_router.post("/auth/shopify-login")
async def shopify_customer_login(credentials: ShopifyCustomerLogin):
    """Login with existing Shopify customer account"""
    # Step 1: Get customer access token from Shopify
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
    
    variables = {
        "input": {
            "email": credentials.email,
            "password": credentials.password
        }
    }
    
    headers = {
        "Content-Type": "application/json",
        "X-Shopify-Storefront-Access-Token": SHOPIFY_STOREFRONT_TOKEN
    }
    
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"https://{SHOPIFY_STORE}/api/{SHOPIFY_API_VERSION}/graphql.json",
            json={"query": mutation, "variables": variables},
            headers=headers
        )
        
        data = response.json()
        logger.info(f"Shopify customer login response: {data}")
        
        result = data.get("data", {}).get("customerAccessTokenCreate", {})
        errors = result.get("customerUserErrors", [])
        
        if errors:
            error_msg = errors[0].get("message", "Autentificare eșuată")
            raise HTTPException(status_code=401, detail=error_msg)
        
        customer_token = result.get("customerAccessToken", {})
        if not customer_token or not customer_token.get("accessToken"):
            raise HTTPException(status_code=401, detail="Email sau parolă incorectă")
        
        shopify_access_token = customer_token.get("accessToken")
        
    # Step 2: Get customer details from Shopify
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
        logger.info(f"Shopify customer data: {customer_data}")
        
        customer = customer_data.get("data", {}).get("customer")
        
        if not customer:
            raise HTTPException(status_code=401, detail="Nu s-au putut obține datele clientului")
    
    # Step 3: Create or update local user linked to Shopify
    local_token = generate_token()
    shopify_customer_id = customer.get("id", "").replace("gid://shopify/Customer/", "")
    
    existing_user = await db.users.find_one({"email": credentials.email.lower()})
    
    default_address = customer.get("defaultAddress") or {}
    
    user_data = {
        "email": credentials.email.lower(),
        "name": f"{customer.get('firstName', '')} {customer.get('lastName', '')}".strip() or credentials.email.split('@')[0],
        "phone": customer.get("phone") or "",
        "address": default_address.get("address1") or "",
        "city": default_address.get("city") or "",
        "county": default_address.get("province") or "",
        "postal_code": default_address.get("zip") or "",
        "is_company": bool(default_address.get("company")),
        "company_name": default_address.get("company") or None,
        "token": local_token,
        "is_shopify_customer": True,
        "shopify_customer_id": shopify_customer_id,
        "shopify_access_token": shopify_access_token,
        "updated_at": datetime.utcnow()
    }
    
    if existing_user:
        # Update existing user
        await db.users.update_one(
            {"email": credentials.email.lower()},
            {"$set": user_data}
        )
        user_id = existing_user["id"]
    else:
        # Create new user linked to Shopify
        user_id = str(uuid.uuid4())
        user_data["id"] = user_id
        user_data["password_hash"] = ""  # No local password
        user_data["created_at"] = datetime.utcnow()
        await db.users.insert_one(user_data)
    
    # Extract Shopify orders
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
    
    return {
        "token": local_token,
        "user": {
            "id": user_id,
            "email": credentials.email.lower(),
            "name": user_data["name"],
            "phone": user_data["phone"],
            "address": user_data["address"],
            "city": user_data["city"],
            "county": user_data["county"],
            "postal_code": user_data["postal_code"],
            "is_company": user_data["is_company"],
            "company_name": user_data["company_name"],
            "is_shopify_customer": True,
            "created_at": existing_user["created_at"] if existing_user else user_data["created_at"]
        },
        "shopify_orders": shopify_orders
    }

@api_router.put("/auth/me")
async def update_current_user(request: Request, update_data: UserUpdate):
    """Update current user profile"""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Token lipsă sau invalid")
    
    token = auth_header.replace("Bearer ", "")
    user = await db.users.find_one({"token": token})
    
    if not user:
        raise HTTPException(status_code=401, detail="Token invalid sau expirat")
    
    # Build update dict with only non-None values
    update_dict = {}
    for field, value in update_data.dict().items():
        if value is not None:
            update_dict[field] = value
    
    if update_dict:
        await db.users.update_one(
            {"id": user["id"]},
            {"$set": update_dict}
        )
    
    # Fetch updated user
    updated_user = await db.users.find_one({"id": user["id"]})
    
    return {
        "id": updated_user["id"],
        "email": updated_user["email"],
        "name": updated_user["name"],
        "phone": updated_user["phone"],
        "address": updated_user.get("address"),
        "city": updated_user.get("city"),
        "county": updated_user.get("county"),
        "postal_code": updated_user.get("postal_code"),
        "is_company": updated_user.get("is_company", False),
        "company_name": updated_user.get("company_name"),
        "cui": updated_user.get("cui"),
        "reg_com": updated_user.get("reg_com"),
        "company_address": updated_user.get("company_address"),
        "created_at": updated_user["created_at"]
    }

@api_router.post("/auth/logout")
async def logout_user(request: Request):
    """Logout user (invalidate token)"""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header.replace("Bearer ", "")
        # Generate new token to invalidate current one
        await db.users.update_one(
            {"token": token},
            {"$set": {"token": generate_token()}}
        )
    return {"message": "Deconectat cu succes"}

@api_router.get("/auth/orders")
async def get_user_orders(request: Request):
    """Get orders for authenticated user"""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Token lipsă sau invalid")
    
    token = auth_header.replace("Bearer ", "")
    user = await db.users.find_one({"token": token})
    
    if not user:
        raise HTTPException(status_code=401, detail="Token invalid sau expirat")
    
    # Get orders by user email
    orders = await db.orders.find({"customer.email": user["email"]}).sort("created_at", -1).to_list(100)
    return [Order(**order) for order in orders]

@api_router.get("/auth/shopify-orders")
async def get_user_shopify_orders(request: Request):
    """Get orders from Shopify for authenticated user - includes fulfillment status"""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Token lipsă sau invalid")
    
    token = auth_header.replace("Bearer ", "")
    user = await db.users.find_one({"token": token})
    
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

# ==================== EQUIPMENT/UTILAJE ENDPOINTS ====================

async def parse_equipment_from_shopify_notes(notes: str) -> list:
    """Parse equipment from Shopify customer notes format"""
    equipment_list = []
    
    if not notes or "UTILAJELE CLIENTULUI:" not in notes:
        return equipment_list
    
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
                
                if 'Serie șasiu:' in detail:
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
        
        return equipment_list
    except Exception as e:
        logger.error(f"Error parsing equipment from Shopify notes: {e}")
        return []

async def get_shopify_customer_notes(user_email: str) -> str:
    """Get customer notes from Shopify using Admin API"""
    try:
        if not SHOPIFY_ADMIN_TOKEN:
            logger.warning("SHOPIFY_ADMIN_TOKEN not set")
            return ""
        
        logger.info(f"Fetching Shopify notes for {user_email} using store: {SHOPIFY_STORE}")
        
        headers = {
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": SHOPIFY_ADMIN_TOKEN
        }
        
        # Use REST API for customer search - more reliable
        search_url = f"https://{SHOPIFY_STORE}/admin/api/2024-01/customers/search.json?query=email:{user_email}"
        logger.info(f"Shopify search URL: {search_url}")
        
        async with httpx.AsyncClient() as client:
            response = await client.get(
                search_url,
                headers=headers,
                timeout=30.0
            )
            
            logger.info(f"Shopify response status: {response.status_code}")
            data = response.json()
            logger.info(f"Shopify response data keys: {data.keys()}")
            
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
    """Get all equipment for authenticated user - syncs from Shopify if notes changed"""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Token lipsă sau invalid")
    
    token = auth_header.replace("Bearer ", "")
    # Search by both token types (our token and Shopify access token)
    user = await db.users.find_one({
        "$or": [
            {"token": token},
            {"shopify_access_token": token}
        ]
    })
    
    if not user:
        raise HTTPException(status_code=401, detail="Token invalid sau expirat")
    
    # Get equipment from user's equipment array
    local_equipment = user.get("equipment", [])
    
    # Try to sync from Shopify notes - ALWAYS prioritize Shopify data
    try:
        shopify_notes = await get_shopify_customer_notes(user.get("email", ""))
        logger.info(f"Shopify notes for {user.get('email')}: {shopify_notes[:200] if shopify_notes else 'empty'}...")
        
        if shopify_notes and "UTILAJELE CLIENTULUI:" in shopify_notes:
            shopify_equipment = await parse_equipment_from_shopify_notes(shopify_notes)
            logger.info(f"Parsed {len(shopify_equipment)} equipment from Shopify notes")
            
            if shopify_equipment:
                # Always update from Shopify if notes contain equipment data
                # This ensures edits made in Shopify are reflected in the app
                await db.users.update_one(
                    {"_id": user["_id"]},
                    {"$set": {"equipment": shopify_equipment}}
                )
                local_equipment = shopify_equipment
                logger.info(f"Synced equipment from Shopify for {user.get('email')}")
    except Exception as e:
        logger.error(f"Error syncing from Shopify: {e}")
    
    # Convert None values to empty strings for frontend
    cleaned_equipment = []
    for eq in local_equipment:
        cleaned_eq = {
            "id": eq.get("id", ""),
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

@api_router.post("/auth/equipment")
async def add_user_equipment(request: Request, equipment_data: EquipmentCreate):
    """Add new equipment for authenticated user (max 10)"""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Token lipsă sau invalid")
    
    token = auth_header.replace("Bearer ", "")
    # Search by both token types (our token and Shopify access token)
    user = await db.users.find_one({
        "$or": [
            {"token": token},
            {"shopify_access_token": token}
        ]
    })
    
    if not user:
        raise HTTPException(status_code=401, detail="Token invalid sau expirat")
    
    # Check current equipment count
    current_equipment = user.get("equipment", [])
    if len(current_equipment) >= 10:
        raise HTTPException(status_code=400, detail="Ați atins limita maximă de 10 utilaje")
    
    # Create new equipment entry
    new_equipment = {
        "id": str(uuid.uuid4()),
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
    
    return {"message": "Utilaj adăugat cu succes", "equipment": new_equipment}

@api_router.put("/auth/equipment/{equipment_id}")
async def update_user_equipment(request: Request, equipment_id: str, equipment_data: EquipmentUpdate):
    """Update existing equipment"""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Token lipsă sau invalid")
    
    token = auth_header.replace("Bearer ", "")
    # Search by both token types (our token and Shopify access token)
    user = await db.users.find_one({
        "$or": [
            {"token": token},
            {"shopify_access_token": token}
        ]
    })
    
    if not user:
        raise HTTPException(status_code=401, detail="Token invalid sau expirat")
    
    # Find and update equipment
    equipment_list = user.get("equipment", [])
    equipment_found = False
    
    for eq in equipment_list:
        if eq.get("id") == equipment_id:
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
    
    return {"message": "Utilaj actualizat cu succes", "equipment": equipment_list}

@api_router.delete("/auth/equipment/{equipment_id}")
async def delete_user_equipment(request: Request, equipment_id: str):
    """Delete equipment"""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Token lipsă sau invalid")
    
    token = auth_header.replace("Bearer ", "")
    # Search by both token types (our token and Shopify access token)
    user = await db.users.find_one({
        "$or": [
            {"token": token},
            {"shopify_access_token": token}
        ]
    })
    
    if not user:
        raise HTTPException(status_code=401, detail="Token invalid sau expirat")
    
    # Remove equipment from list
    equipment_list = user.get("equipment", [])
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
    
    return {"message": "Utilaj șters cu succes", "remaining_count": len(new_equipment_list)}

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
        "auto_sync_enabled": True,
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

# Load OAuth credentials
SHOPIFY_CLIENT_ID = os.environ.get('SHOPIFY_CLIENT_ID', '')
SHOPIFY_CLIENT_SECRET = os.environ.get('SHOPIFY_CLIENT_SECRET', '')
SHOPIFY_ADMIN_TOKEN = os.environ.get('SHOPIFY_ADMIN_TOKEN', '')

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

@app.on_event("startup")
async def startup_event():
    """Start background tasks on app startup"""
    global auto_sync_task
    
    auto_sync_task = asyncio.create_task(auto_sync_loop())
    logger.info(f"Auto-sync enabled: every {AUTO_SYNC_INTERVAL_MINUTES} minutes")
    logger.info("=== WEBHOOK SETUP ===")
    logger.info("Add webhooks in Shopify Admin -> Settings -> Notifications -> Webhooks")
    logger.info(f"  URL: https://YOUR_DOMAIN/api/webhooks/shopify")
    logger.info("  Topics: products/create, products/update, products/delete, inventory_levels/update")

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
    # First check environment variable
    if SHOPIFY_ADMIN_TOKEN:
        return SHOPIFY_ADMIN_TOKEN
    
    # Then check database
    token_doc = await db.shopify_tokens.find_one({"store": SHOPIFY_STORE})
    if token_doc:
        return token_doc.get("access_token")
    
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
                # Use variant_id if available
                line_items.append({
                    "variant_id": int(variant_id.split("/")[-1]) if "/" in str(variant_id) else int(variant_id),
                    "quantity": item.quantity
                })
            else:
                # Fallback: create custom line item
                line_items.append({
                    "title": item.title,
                    "quantity": item.quantity,
                    "price": str(item.price),
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

@api_router.get("/orders/mobile")
async def get_mobile_orders(limit: int = 50):
    """Get orders created from the mobile app"""
    orders = await db.mobile_orders.find().sort("created_at", -1).limit(limit).to_list(limit)
    for order in orders:
        order["_id"] = str(order["_id"])
    return orders

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
                                    content
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
                    
                    articles.append({
                        "id": node.get("id", ""),
                        "title": node.get("title", ""),
                        "handle": node.get("handle", ""),
                        "published_at": node.get("publishedAt", ""),
                        "excerpt": node.get("excerpt", ""),
                        "content": node.get("content", ""),
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

@api_router.get("/debug/shopify-token")
async def debug_shopify_token():
    """Debug endpoint to check if Shopify Admin Token is set"""
    has_token = bool(SHOPIFY_ADMIN_TOKEN and len(SHOPIFY_ADMIN_TOKEN) > 10)
    return {
        "has_shopify_admin_token": has_token,
        "token_length": len(SHOPIFY_ADMIN_TOKEN) if SHOPIFY_ADMIN_TOKEN else 0,
        "token_prefix": SHOPIFY_ADMIN_TOKEN[:10] + "..." if has_token else "NOT SET"
    }

@api_router.get("/debug/customer-notes/{email}")
async def debug_customer_notes(email: str):
    """Debug endpoint to fetch customer notes"""
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
