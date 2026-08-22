"""
Focused, standalone check for the "stock hits 0 -> stock_status auto-set to
supplier_stock" policy (George, 2026-08-22).

Background: a live product was found with stock=0 but stock_status=
"in_stock" (a data inconsistency, most likely left over from the last unit
selling, or a manual edit that changed stock without touching
stock_status). That produced a visibly broken product page ("Toate cele 0
bucăți sunt în coș" - already patched defensively in the frontend, but the
root cause is here on the backend). The fix: every code path that can make
`stock` land at/below 0 must automatically force stock_status to
"supplier_stock" - never silently leave "in_stock", never automatically
pick "out_of_stock" either (that value is retired as an automatic outcome,
though staff can still choose it explicitly) - UNLESS the very same
request/operation explicitly sets stock_status itself, in which case that
explicit choice always wins.

The shared rule lives in a single helper, _auto_derived_stock_status()
(server.py, right before parse_shopify_node), used by every stock-writing
path:
  1. _decrement_stock_once() - the real checkout/order decrement (via
     _reserve_stock_for_order(), called from create_order()). This is very
     likely how most products actually reach stock=0 in production (the
     last unit selling).
  2. _apply_product_update() - shared by all three admin edit endpoints
     (single PUT /admin/products/{id}, shared-patch PUT
     /admin/products/bulk, per-item PUT /admin/products/bulk-save).
  3. parse_shopify_node() - shared by the Shopify webhook handler
     (update_single_product, products/create + products/update) and the
     admin-triggered full resync (sync_all_products).
  4. admin_create_product() - product creation, for the same reason (a
     newly created product with stock=0 and no explicit stock_status must
     not be left stock_status=None either).

Not a pytest suite - same mongomock/mongomock-motor + direct-function-call
approach as every other scripts/test_*.py file here (see
scripts/test_stock_checkout.py's own docstring for the fuller rationale:
no tests/ dir, no pytest in requirements.txt, no FastAPI TestClient either).

Run (from repo root): python scripts/test_auto_supplier_stock_on_zero.py
"""
import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

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


def make_admin_request():
    return Request({"type": "http", "headers": [], "method": "PUT", "path": "/admin/products/p1"})


def patch_require_admin(admin_id="admin-1", admin_email="staff@example.com"):
    original = server._require_admin

    async def _stub(request):
        return {"id": admin_id, "email": admin_email, "role": "admin"}

    server._require_admin = _stub
    return original


def base_product(**overrides):
    doc = {
        "handle": "placeholder-handle", "description": "", "title": "Produs test",
        "title_normalized": "produs test", "price": 100.0, "currency": "RON",
        "images": [], "tags": [], "compatible_models": [], "collections": [],
        "complementary_product_ids": [], "equivalent_product_ids": [],
        "is_featured": False, "sku": "SKU-1", "image_url": "https://example.com/img.jpg",
    }
    doc.update(overrides)
    return doc


# --- (0) pure unit tests of the shared helper itself -----------------------

def scenario_0_helper_unit_tests():
    """Direct checks of _auto_derived_stock_status()'s own contract, in
    isolation from any of the higher-level paths that call it."""
    f = server._auto_derived_stock_status
    check("0a) stock=0, no current status -> supplier_stock", f(0, None) == "supplier_stock")
    check("0b) stock=0, current in_stock -> supplier_stock", f(0, "in_stock") == "supplier_stock")
    check("0c) stock=0, current already supplier_stock -> None (no-op)", f(0, "supplier_stock") is None)
    check("0d) negative stock -> supplier_stock too", f(-1, "in_stock") == "supplier_stock")
    check("0e) positive stock -> None (never auto-touched)", f(5, "in_stock") is None)
    check("0f) stock=None -> None (nothing to derive)", f(None, "in_stock") is None)


# --- (1) checkout: last unit sold -> supplier_stock -------------------------

async def scenario_1_checkout_last_unit_sets_supplier_stock():
    """Checkout (_reserve_stock_for_order, the real order-decrement path
    used by create_order) driving stock to exactly 0 must auto-set
    stock_status to supplier_stock, not leave/derive out_of_stock."""
    db = await fresh_db()
    await db.shopify_products.insert_one(
        base_product(id="p1", stock=1, stock_status="in_stock", title="Ultima bucata")
    )
    items = [{"product_id": "p1", "quantity": 1, "price": 100.0}]
    await server._reserve_stock_for_order(items)
    doc = await db.shopify_products.find_one({"id": "p1"})
    check("1) stock reaches exactly 0", doc["stock"] == 0, f"got {doc['stock']}")
    check("1) stock_status auto-set to supplier_stock", doc["stock_status"] == "supplier_stock", f"got {doc['stock_status']}")


# --- (2) admin edit, no explicit stock_status -> supplier_stock ------------

async def scenario_2_admin_edit_to_zero_without_explicit_status():
    """Admin edits only `stock` down to 0 via PUT /admin/products/{id}
    (ProductUpdate.stock_status left unset/None) -> auto-set to
    supplier_stock. Exercised through the real admin_update_product
    endpoint function (not just _apply_product_update directly) so the
    audit-log diff logic gets covered by the same call too."""
    db = await fresh_db()
    await db.shopify_products.insert_one(
        base_product(id="p2", stock=3, stock_status="in_stock", title="Filtru aer")
    )
    original_admin = patch_require_admin()
    try:
        bt = BackgroundTasks()
        updated = await server.admin_update_product(
            "p2", request=make_admin_request(),
            product_data=server.ProductUpdate(stock=0),
            background_tasks=bt,
        )
        check("2) stock updated to 0", updated.stock == 0, updated.stock)
        check("2) stock_status auto-set to supplier_stock", updated.stock_status == "supplier_stock", updated.stock_status)

        persisted = await db.shopify_products.find_one({"id": "p2"})
        check("2) persisted doc also shows supplier_stock", persisted["stock_status"] == "supplier_stock", persisted["stock_status"])
    finally:
        server._require_admin = original_admin


# --- (3) admin edit, EXPLICIT different stock_status -> respected ----------

async def scenario_3_admin_edit_explicit_status_is_respected():
    """Admin edits stock down to 0 AND explicitly sets stock_status to
    something else ("in_stock") in the very same request - that explicit
    choice must win, the automatic rule must NOT override it."""
    db = await fresh_db()
    await db.shopify_products.insert_one(
        base_product(id="p3", stock=2, stock_status="in_stock", title="Curea ventilator")
    )
    original_admin = patch_require_admin()
    try:
        updated = await server.admin_update_product(
            "p3", request=make_admin_request(),
            product_data=server.ProductUpdate(stock=0, stock_status="in_stock"),
            background_tasks=BackgroundTasks(),
        )
        check("3) stock updated to 0", updated.stock == 0, updated.stock)
        check("3) explicit stock_status='in_stock' is respected, not overridden",
              updated.stock_status == "in_stock", updated.stock_status)
    finally:
        server._require_admin = original_admin


async def scenario_3b_admin_edit_explicit_out_of_stock_is_respected():
    """Same as (3), but staff explicitly choosing out_of_stock (still a
    valid MANUAL choice, just no longer an automatic one) must also be
    respected, not silently rewritten to supplier_stock."""
    db = await fresh_db()
    await db.shopify_products.insert_one(
        base_product(id="p3b", stock=2, stock_status="in_stock", title="Piesa discontinuata")
    )
    original_admin = patch_require_admin()
    try:
        updated = await server.admin_update_product(
            "p3b", request=make_admin_request(),
            product_data=server.ProductUpdate(stock=0, stock_status="out_of_stock"),
            background_tasks=BackgroundTasks(),
        )
        check("3b) stock updated to 0", updated.stock == 0, updated.stock)
        check("3b) explicit stock_status='out_of_stock' is respected",
              updated.stock_status == "out_of_stock", updated.stock_status)
    finally:
        server._require_admin = original_admin


# --- (4) Shopify webhook driving stock to 0 -> supplier_stock --------------

async def scenario_4_webhook_to_zero_sets_supplier_stock():
    """update_single_product (products/create + products/update webhook
    handler) parses a Shopify node with quantityAvailable=0 -> persisted
    stock_status must be supplier_stock, never out_of_stock/in_stock. This
    path bypasses _apply_product_update entirely (its own separate
    update_one(upsert=True) call, see update_single_product's own inline
    comments) so it needs its own direct coverage of the same rule, via
    parse_shopify_node()."""
    db = await fresh_db()
    await db.shopify_products.insert_one(
        base_product(id="999", stock=4, stock_status="in_stock", title="Piesa Shopify")
    )

    fake_graphql_node = {
        "id": "gid://shopify/Product/999",
        "title": "Piesa Shopify",
        "handle": "piesa-shopify",
        "description": "",
        "tags": [],
        "productType": "Nou",
        "vendor": "AGB",
        "priceRange": {"minVariantPrice": {"amount": "150.0", "currencyCode": "RON"}},
        "images": {"edges": [{"node": {"url": "https://example.com/img2.jpg"}}]},
        "variants": {"edges": [{"node": {"id": "v1", "sku": "SKU-999", "quantityAvailable": 0}}]},
    }

    class _FakeResponse:
        def json(self):
            return {"data": {"product": fake_graphql_node}}

    with patch.object(server.httpx.AsyncClient, "post", new=AsyncMock(return_value=_FakeResponse())):
        result = await server.update_single_product("999")
    check("4) webhook update reports success", result is True, result)

    persisted = await db.shopify_products.find_one({"id": "999"})
    check("4) stock persisted as 0", persisted.get("stock") == 0, persisted.get("stock"))
    check("4) stock_status auto-set to supplier_stock via webhook", persisted.get("stock_status") == "supplier_stock", persisted.get("stock_status"))


# --- (5) product remaining positive is never touched ------------------------

async def scenario_5_positive_stock_untouched():
    """(a) An admin edit that changes `stock` but keeps it positive must
    leave stock_status alone (still in_stock, not forced to anything).
    (b) An edit that doesn't touch `stock` at all (e.g. price-only) must
    never touch stock_status either, even if the product already sits at
    stock=0 (nothing about THIS request concerns stock, so nothing should
    fire)."""
    db = await fresh_db()
    await db.shopify_products.insert_one(
        base_product(id="p5", stock=3, stock_status="in_stock", title="Produs cu stoc")
    )
    await db.shopify_products.insert_one(
        base_product(id="p6", stock=0, stock_status="supplier_stock", title="Produs fara stoc, deja corect")
    )
    original_admin = patch_require_admin()
    try:
        updated5 = await server.admin_update_product(
            "p5", request=make_admin_request(),
            product_data=server.ProductUpdate(stock=8),
            background_tasks=BackgroundTasks(),
        )
        check("5a) stock updated to 8", updated5.stock == 8, updated5.stock)
        check("5a) stock_status untouched (still in_stock)", updated5.stock_status == "in_stock", updated5.stock_status)

        updated6 = await server.admin_update_product(
            "p6", request=make_admin_request(),
            product_data=server.ProductUpdate(price=249.99),
            background_tasks=BackgroundTasks(),
        )
        check("5b) price-only edit doesn't touch stock", updated6.stock == 0, updated6.stock)
        check("5b) price-only edit doesn't touch stock_status", updated6.stock_status == "supplier_stock", updated6.stock_status)
    finally:
        server._require_admin = original_admin


# --- (6) product creation ---------------------------------------------------

async def scenario_6_creation_zero_stock_defaults_to_supplier_stock():
    """admin_create_product with stock=0 and no explicit stock_status must
    default to supplier_stock, not be left stock_status=None."""
    db = await fresh_db()
    original_admin = patch_require_admin()
    try:
        created = await server.admin_create_product(
            request=make_admin_request(),
            product_data=server.ProductCreate(title="Produs nou fara stoc", price=99.9, stock=0),
        )
        check("6a) created with stock 0", created.stock == 0, created.stock)
        check("6a) stock_status defaults to supplier_stock", created.stock_status == "supplier_stock", created.stock_status)

        created2 = await server.admin_create_product(
            request=make_admin_request(),
            product_data=server.ProductCreate(
                title="Produs nou fara stoc, status explicit", price=99.9, stock=0,
                stock_status="out_of_stock",
            ),
        )
        check("6b) explicit stock_status on creation is respected", created2.stock_status == "out_of_stock", created2.stock_status)

        created3 = await server.admin_create_product(
            request=make_admin_request(),
            product_data=server.ProductCreate(title="Produs nou cu stoc", price=99.9, stock=10),
        )
        check("6c) positive stock on creation defaults to in_stock", created3.stock_status == "in_stock", created3.stock_status)
    finally:
        server._require_admin = original_admin


async def main():
    scenario_0_helper_unit_tests()
    await scenario_1_checkout_last_unit_sets_supplier_stock()
    await scenario_2_admin_edit_to_zero_without_explicit_status()
    await scenario_3_admin_edit_explicit_status_is_respected()
    await scenario_3b_admin_edit_explicit_out_of_stock_is_respected()
    await scenario_4_webhook_to_zero_sets_supplier_stock()
    await scenario_5_positive_stock_untouched()
    await scenario_6_creation_zero_stock_defaults_to_supplier_stock()

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
