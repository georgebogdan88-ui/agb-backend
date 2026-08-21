"""
Focused, standalone check for the `meta_title`/`meta_description` SEO
fields on products (Product/ProductCreate/ProductUpdate in server.py) -
confirms they round-trip through POST /admin/products, PUT
/admin/products/{id}, PUT /admin/products/bulk-save, and are actually
returned by the public GET /products/{id} fetch used by the storefront.
Also confirms POST /admin/products auto-fills meta_title/meta_description
via scripts.generate_seo_metadata.generate_seo_fields whenever staff leaves
either blank (each field independently, never overwriting one that's
already filled in) - see admin_create_product in server.py.

The equivalent auto-fill behavior on the Shopify products/create and
products/update webhook path (update_single_product in server.py) is NOT
covered here (it needs an httpx-mocked Shopify GraphQL response, out of
scope for this in-memory-Mongo-only harness) - verified separately.

Not a pytest suite - mirrors scripts/test_stock_checkout.py's and
scripts/test_admin_customer_account_lookup.py's approach (this repo still
has no test framework, tests/ dir, or pytest in requirements.txt). Uses
mongomock + mongomock-motor to swap in an in-memory Mongo double for
server.client/db, and monkeypatches server._require_admin to a fixed-admin
stub (already covered end-to-end by test_require_admin_bff_only.py) so
these scenarios isolate the SEO-field plumbing, not the auth layer.

Run (from repo root): python scripts/test_product_seo_fields.py
"""
import asyncio
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


def make_request():
    """None of the endpoints under test read anything from `request` beyond
    passing it through to _require_admin (stubbed below) and _write_audit_log
    (which only needs request.headers/request.client, both fine on an empty
    scope) - same helper as test_admin_customer_account_lookup.py."""
    return Request({"type": "http", "headers": [], "method": "PUT", "path": "/admin/products"})


def patch_require_admin(admin_id="admin-1", admin_email="staff@example.com"):
    original = server._require_admin

    async def _stub(request):
        return {"id": admin_id, "email": admin_email, "role": "admin"}

    server._require_admin = _stub
    return original


async def scenario_a_create_round_trip():
    """(a) POST /admin/products accepts meta_title/meta_description and
    persists them on the created product doc."""
    await fresh_db()
    original = patch_require_admin()
    try:
        payload = server.ProductCreate(
            title="Filtru ulei",
            price=99.9,
            meta_title="Filtru ulei original - AGB Agroparts",
            meta_description="Filtru ulei original de calitate pentru utilaje agricole.",
        )
        created = await server.admin_create_product(request=make_request(), product_data=payload)
        check("a) create: response has meta_title", created.meta_title == "Filtru ulei original - AGB Agroparts", created.meta_title)
        check("a) create: response has meta_description", created.meta_description == "Filtru ulei original de calitate pentru utilaje agricole.", created.meta_description)

        doc = await server.db.shopify_products.find_one({"id": created.id})
        check("a) create: persisted doc has meta_title", doc.get("meta_title") == "Filtru ulei original - AGB Agroparts", doc)
        check("a) create: persisted doc has meta_description", doc.get("meta_description") == "Filtru ulei original de calitate pentru utilaje agricole.", doc)
        return created.id
    finally:
        server._require_admin = original


async def scenario_a2_create_without_seo_fields_autogenerates():
    """(a2) creating a product without meta_title/meta_description no
    longer leaves them None - admin_create_product now auto-fills both via
    scripts.generate_seo_metadata.generate_seo_fields (see server.py, right
    before the insert_one) so newly-created products don't join the same
    empty-SEO backlog scripts/generate_seo_metadata.py --apply backfilled
    across the pre-existing catalog. Matches generate_seo_fields' own output
    for the same input exactly, rather than just asserting non-None."""
    await fresh_db()
    original = patch_require_admin()
    try:
        payload = server.ProductCreate(title="Curea transmisie", price=49.5)
        created = await server.admin_create_product(request=make_request(), product_data=payload)
        # generate_seo_fields' meta_description picks between a few
        # equivalent closer phrasings deterministically keyed off the
        # product's own `id` (see _pick_variant) - so the expected value
        # must be computed using the SAME id admin_create_product actually
        # generated (created.id), not a hand-rolled id-less dict, or this
        # assertion would flake/fail depending on which id uuid4() picked.
        expected_title, expected_description = server.generate_seo_fields(
            {"id": created.id, "title": "Curea transmisie", "vendor": None, "product_type": None, "compatible_models": None}
        )
        check("a2) create without SEO fields: meta_title auto-generated", created.meta_title == expected_title, created.meta_title)
        check("a2) create without SEO fields: meta_description auto-generated", created.meta_description == expected_description, created.meta_description)
    finally:
        server._require_admin = original


async def scenario_a3_create_partial_seo_fields_only_fills_the_blank_one():
    """(a3) if staff fills in ONLY one of meta_title/meta_description, the
    other is auto-generated but the one they typed is left exactly as-is -
    each field is filled independently, never overwriting a value that's
    already present (same "don't touch what's already set" rule
    scripts/generate_seo_metadata.py --apply uses by default)."""
    await fresh_db()
    original = patch_require_admin()
    try:
        payload = server.ProductCreate(
            title="Furtun hidraulic",
            price=15.0,
            meta_title="Titlu SEO scris manual de staff",
        )
        created = await server.admin_create_product(request=make_request(), product_data=payload)
        check(
            "a3) create with only meta_title set: meta_title NOT overwritten",
            created.meta_title == "Titlu SEO scris manual de staff", created.meta_title,
        )
        check(
            "a3) create with only meta_title set: meta_description auto-generated (not None)",
            bool(created.meta_description), created.meta_description,
        )

        payload2 = server.ProductCreate(
            title="Cuplaj rapid hidraulic",
            price=25.0,
            meta_description="Descriere SEO scrisa manual de staff.",
        )
        created2 = await server.admin_create_product(request=make_request(), product_data=payload2)
        check(
            "a3) create with only meta_description set: meta_description NOT overwritten",
            created2.meta_description == "Descriere SEO scrisa manual de staff.", created2.meta_description,
        )
        check(
            "a3) create with only meta_description set: meta_title auto-generated (not None)",
            bool(created2.meta_title), created2.meta_title,
        )
    finally:
        server._require_admin = original


async def scenario_b_update_round_trip():
    """(b) PUT /admin/products/{id} accepts and persists meta_title/
    meta_description on an existing product, without disturbing other
    fields."""
    db = await fresh_db()
    original = patch_require_admin()
    try:
        await db.shopify_products.insert_one({
            "id": "p-update-1",
            "title": "Rulment",
            "handle": "rulment",
            "description": "desc",
            "price": 10.0,
            "currency": "RON",
            "images": [],
            "tags": [],
            "stock": 5,
            "compatible_models": [],
            "collections": [],
            "complementary_product_ids": [],
            "equivalent_product_ids": [],
            "is_featured": False,
        })
        patch = server.ProductUpdate(meta_title="Rulment - piesă originală", meta_description="Rulment de calitate.")
        updated = await server.admin_update_product(
            "p-update-1", request=make_request(), product_data=patch, background_tasks=BackgroundTasks(),
        )
        check("b) update: response has meta_title", updated.meta_title == "Rulment - piesă originală", updated.meta_title)
        check("b) update: response has meta_description", updated.meta_description == "Rulment de calitate.", updated.meta_description)
        check("b) update: unrelated field (price) untouched", updated.price == 10.0, updated.price)
        check("b) update: unrelated field (title) untouched", updated.title == "Rulment", updated.title)
    finally:
        server._require_admin = original


async def scenario_c_bulk_save_round_trip():
    """(c) PUT /admin/products/bulk-save accepts a per-product
    meta_title/meta_description patch (Shopify-grid-style bulk editor)."""
    db = await fresh_db()
    original = patch_require_admin()
    try:
        await db.shopify_products.insert_one({
            "id": "p-bulk-1", "title": "Piesa A", "handle": "piesa-a", "description": "d",
            "price": 20.0, "currency": "RON", "images": [], "tags": [], "stock": 1,
            "compatible_models": [], "collections": [], "complementary_product_ids": [],
            "equivalent_product_ids": [], "is_featured": False,
        })
        await db.shopify_products.insert_one({
            "id": "p-bulk-2", "title": "Piesa B", "handle": "piesa-b", "description": "d",
            "price": 30.0, "currency": "RON", "images": [], "tags": [], "stock": 1,
            "compatible_models": [], "collections": [], "complementary_product_ids": [],
            "equivalent_product_ids": [], "is_featured": False,
        })
        bulk_data = server.ProductBulkSave(updates=[
            server.ProductBulkSaveItem(id="p-bulk-1", patch=server.ProductUpdate(meta_title="Piesa A SEO title")),
            server.ProductBulkSaveItem(id="p-bulk-2", patch=server.ProductUpdate(meta_description="Piesa B SEO description")),
        ])
        result = await server.admin_bulk_save_products(request=make_request(), bulk_data=bulk_data, background_tasks=BackgroundTasks())
        check("c) bulk-save: both updated", result["updated"] == 2, result)
        check("c) bulk-save: none not_found", result["not_found"] == [], result)

        doc1 = await db.shopify_products.find_one({"id": "p-bulk-1"})
        doc2 = await db.shopify_products.find_one({"id": "p-bulk-2"})
        check("c) bulk-save: p1 meta_title persisted", doc1.get("meta_title") == "Piesa A SEO title", doc1)
        check("c) bulk-save: p2 meta_description persisted", doc2.get("meta_description") == "Piesa B SEO description", doc2)
    finally:
        server._require_admin = original


async def scenario_d_public_get_returns_seo_fields():
    """(d) GET /products/{id} (public storefront single-product fetch)
    actually returns meta_title/meta_description in its response, not just
    stores them - guards against a separate response-shaping/whitelist step
    silently dropping the new fields."""
    db = await fresh_db()
    await db.shopify_products.insert_one({
        "id": "p-public-1",
        "title": "Anvelopă tractor",
        "handle": "anvelopa-tractor",
        "description": "desc",
        "price": 500.0,
        "currency": "RON",
        "images": [],
        "tags": [],
        "stock": 2,
        "compatible_models": [],
        "collections": [],
        "complementary_product_ids": [],
        "equivalent_product_ids": [],
        "is_featured": False,
        "meta_title": "Anvelopă tractor - AGB Agroparts",
        "meta_description": "Anvelope pentru tractor, stoc disponibil.",
    })
    fetched = await server.get_product("p-public-1")
    check("d) public GET /products/{id}: meta_title returned", fetched.meta_title == "Anvelopă tractor - AGB Agroparts", fetched.meta_title)
    check("d) public GET /products/{id}: meta_description returned", fetched.meta_description == "Anvelope pentru tractor, stoc disponibil.", fetched.meta_description)


async def scenario_d2_public_get_without_seo_fields_defaults_none():
    """(d2) a pre-existing product doc with no meta_title/meta_description
    at all (the common case for every product created before this change)
    still fetches fine, with both defaulting to None - proves this is
    backward-compatible, not a breaking change for existing products."""
    db = await fresh_db()
    await db.shopify_products.insert_one({
        "id": "p-public-2",
        "title": "Filtru aer",
        "handle": "filtru-aer",
        "description": "desc",
        "price": 80.0,
        "currency": "RON",
        "images": [],
        "tags": [],
        "stock": 3,
        "compatible_models": [],
        "collections": [],
        "complementary_product_ids": [],
        "equivalent_product_ids": [],
        "is_featured": False,
        # deliberately no meta_title/meta_description keys at all
    })
    fetched = await server.get_product("p-public-2")
    check("d2) pre-existing product without SEO fields: meta_title is None", fetched.meta_title is None, fetched.meta_title)
    check("d2) pre-existing product without SEO fields: meta_description is None", fetched.meta_description is None, fetched.meta_description)


async def main():
    await scenario_a_create_round_trip()
    await scenario_a2_create_without_seo_fields_autogenerates()
    await scenario_a3_create_partial_seo_fields_only_fills_the_blank_one()
    await scenario_b_update_round_trip()
    await scenario_c_bulk_save_round_trip()
    await scenario_d_public_get_returns_seo_fields()
    await scenario_d2_public_get_without_seo_fields_defaults_none()

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
