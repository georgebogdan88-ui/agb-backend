"""Seed a STAGING MongoDB with synthetic products and test user accounts,
for k6 load testing (see tests/load/k6/). Never touches real data - all
generated records are clearly marked as synthetic (title/description say
so, emails end in @loadtest.invalid, product codes use a SEED- prefix
that will never collide with a real AGB part code).

SAFETY: requires --mongo-url explicitly (does NOT read MONGO_URL from
.env - this repo's local .env points at the real shared MongoDB cluster
used by both local dev and production, so defaulting to it here would be
exactly the accident this script exists to prevent). Also refuses to run
against any URL that looks like the production Atlas cluster.

Usage:
    python scripts/seed_staging_data.py \
        --mongo-url "mongodb+srv://.../staging_cluster" \
        --db-name agb_agroparts \
        --products 15000 \
        --users 1000

Add --dry-run to see counts/samples without writing anything.
"""
import argparse
import random
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Windows consoles often default stdout/stderr to cp1252, which can't
# encode the diacritics in this script's Romanian output - force UTF-8
# explicitly rather than avoiding diacritics everywhere.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).parent.parent))

import bcrypt
from pymongo import MongoClient, InsertOne

# Same denylist approach as tests/load/k6/config.js - these are the real
# production hosts, hardcoded here on purpose so this script can never
# silently target them even if someone passes the wrong --mongo-url.
PRODUCTION_HOST_FRAGMENTS = [
    "cluster0.ifsa0yw.mongodb.net",  # the actual shared Atlas cluster
    "agb-backend.onrender.com",
    "agb-webshop.onrender.com",
    "agb-crm-api.onrender.com",
]

PART_TYPES = [
    "Filtru ulei", "Filtru combustibil", "Filtru aer", "Filtru hidraulic",
    "Pompa hidraulica", "Pompa apa", "Pompa combustibil", "Rulment roata",
    "Rulment ambreiaj", "Cuzineti biela", "Cuzineti arbore", "Simering pinion",
    "Simering butuc", "Bucsa", "Arc supapa", "Piston", "Segment piston",
    "Garnitura chiulasa", "Electrovalva", "Senzor presiune", "Injector",
    "Alternator", "Starter", "Radiator", "Termostat", "Curea transmisie",
]
BRANDS = ["SeedParts", "TestAgro", "MockJD", "SynthTractor", "LoadTestCo"]
MODELS = ["6120M", "6420S", "7930", "6155M", "5090E", "6210J", "7530", "6820"]


def make_products(count: int):
    products = []
    for i in range(count):
        part = random.choice(PART_TYPES)
        brand = random.choice(BRANDS)
        code = f"SEED{100000 + i:06d}"
        title = f"{part} {brand} John Deere {code}"
        product_type = random.choices(["Nou", "Dezmembrari"], weights=[70, 30])[0]
        collection = "Piese noi" if product_type == "Nou" else "Piese din dezmembrare"
        stock = random.randint(0, 40)
        stock_status = "in_stock" if stock > 0 else random.choice(["out_of_stock", "supplier_stock"])
        price = round(random.uniform(20, 3000), 2)
        title_normalized = title.lower()
        description = (
            f"{title} - PRODUS SINTETIC DE TEST, generat pentru încărcare (k6). "
            f"Nu este un produs real, nu apare în catalogul de producție."
        )
        products.append({
            "id": str(uuid.uuid4()),
            "title": title,
            "handle": f"seed-{code.lower()}",
            "description": description,
            "description_normalized": description.lower(),
            "technical_specs": None,
            "title_normalized": title_normalized,
            "price": price,
            "currency": "RON",
            # Public, no-auth-required placeholder image (Lorem Picsum) -
            # not a real Cloudinary asset, so staging never depends on a
            # Cloudinary account/credential either.
            "image_url": f"https://picsum.photos/seed/{code}/800/800.jpg",
            "images": [f"https://picsum.photos/seed/{code}/800/800.jpg"],
            "tags": ["seed-data", "load-test"],
            "product_type": product_type,
            "vendor": brand,
            "stock": stock,
            "stock_status": stock_status,
            "sku": code,
            "compatible_models": random.sample(MODELS, k=random.randint(1, 3)),
            "collections": [collection],
            "collections_normalized": [collection.lower()],
            "complementary_product_ids": [],
            "equivalent_product_ids": [],
            "is_featured": False,
        })
    return products


def make_users(count: int, email_prefix: str, password: str):
    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    now = datetime.now(timezone.utc)
    users = []
    for i in range(count):
        users.append({
            "id": str(uuid.uuid4()),
            "email": f"{email_prefix}{i}@loadtest.invalid",
            "password_hash": hashed,
            "name": f"Load Test User {i}",
            "phone": "0700000000",
            "tokens": [],
            "role": "customer",
            "is_company": False,
            "is_shopify_customer": False,
            "created_at": now,
        })
    return users


def bulk_insert(collection, docs, label, batch_size=1000):
    for start in range(0, len(docs), batch_size):
        batch = docs[start:start + batch_size]
        collection.bulk_write([InsertOne(d) for d in batch], ordered=False)
        print(f"  {label}: {min(start + batch_size, len(docs))}/{len(docs)}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mongo-url", required=True, help="Staging MongoDB connection string - never omit, never reuse production's")
    parser.add_argument("--db-name", required=True, help="Staging database name")
    parser.add_argument("--products", type=int, default=15000)
    parser.add_argument("--users", type=int, default=1000)
    parser.add_argument("--user-email-prefix", default="loadtest-user-")
    parser.add_argument("--user-password", default="LoadTest123!")
    parser.add_argument("--dry-run", action="store_true", help="Generate and print samples/counts, write nothing")
    args = parser.parse_args()

    for frag in PRODUCTION_HOST_FRAGMENTS:
        if frag in args.mongo_url:
            sys.exit(
                f"REFUZAT: --mongo-url conține '{frag}', care arată ca infrastructura de producție.\n"
                "Acest script scrie ~15.000 documente sintetice - trebuie să ruleze DOAR împotriva\n"
                "unui cluster de staging separat. Verifică --mongo-url și încearcă din nou."
            )

    print(f"Target: {args.db_name} @ {args.mongo_url.split('@')[-1] if '@' in args.mongo_url else args.mongo_url}")
    print(f"Generating {args.products} synthetic products and {args.users} synthetic users...")

    products = make_products(args.products)
    users = make_users(args.users, args.user_email_prefix, args.user_password)

    print(f"\nSample product: {products[0]['title']} ({products[0]['sku']}, {products[0]['price']} RON, stock={products[0]['stock']})")
    print(f"Sample user: {users[0]['email']}")

    if args.dry_run:
        print("\n--dry-run: nimic scris. Rulează fără acest flag ca să scrii efectiv în baza de date.")
        return

    client = MongoClient(args.mongo_url)
    db = client[args.db_name]

    print(f"\nInserting into {args.db_name}.shopify_products ...")
    bulk_insert(db.shopify_products, products, "products")

    print(f"\nInserting into {args.db_name}.users ...")
    bulk_insert(db.users, users, "users")

    # Indexes mirror what server.py's startup_event already creates - safe
    # to also create here so the seeded data is immediately query-efficient
    # even before the staging backend has been started once.
    print("\nCreating indexes...")
    db.shopify_products.create_index("title_normalized")
    db.shopify_products.create_index("description_normalized")
    db.shopify_products.create_index("product_type")
    db.shopify_products.create_index("compatible_models")
    db.shopify_products.create_index("collections")
    db.users.create_index("email", unique=True)
    db.users.create_index("tokens")
    db.users.create_index("token")
    db.cart.create_index("session_id")

    print("\nDone.")
    print(f"Products: {db.shopify_products.count_documents({'tags': 'seed-data'})}")
    print(f"Test users: {db.users.count_documents({'email': {'$regex': '@loadtest.invalid$'}})}")


if __name__ == "__main__":
    main()
