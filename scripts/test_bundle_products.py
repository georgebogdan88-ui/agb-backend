"""Focused, standalone check for the "Pachet" (bundle product) backend
pieces: get_bundle_items, get_compatible_bundles,
admin_bundle_price_suggestion (incl. the 5% consumable-description
discount), and that a Pachet product prices/checks out through the exact
same add_to_cart/create_order path as any other product (no special-casing
needed - see the Product model's own note on bundle_items).

Same style as this directory's other standalone scripts - direct-call
against the real configured MONGO_URL, throwaway data, full cleanup either
way.

Run (from repo root): python scripts/test_bundle_products.py
"""
import asyncio
import sys
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402
from fastapi import Request, HTTPException  # noqa: E402

PASS = []
FAIL = []


def check(name, condition, detail=""):
    if condition:
        PASS.append(name)
        print(f"PASS: {name}")
    else:
        FAIL.append(name)
        print(f"FAIL: {name} - {detail}")


def make_request():
    return Request({"type": "http", "headers": [], "method": "POST", "path": "/admin/test"})


def patch_require_admin():
    original = server._require_admin

    async def _stub(request):
        return {"id": "admin-1", "email": "staff@example.com", "role": "admin"}

    server._require_admin = _stub
    return original


async def main():
    db = server.db
    suffix = uuid.uuid4().hex[:8]
    now = datetime.utcnow()

    # Two component products - one a plain part, one a "consumable" (word in
    # its description, matching the real-catalog pattern found via search).
    part_id = f"bundle-test-part-{suffix}"
    consumable_id = f"bundle-test-consumable-{suffix}"
    await db.shopify_products.insert_one({
        "id": part_id, "title": "Piesă test", "handle": part_id, "description": "O piesă normală.",
        "price": 200.0, "currency": "RON", "stock": 5, "stock_status": "in_stock",
        "created_at": now, "updated_at": now,
    })
    await db.shopify_products.insert_one({
        "id": consumable_id, "title": "Filtru test", "handle": consumable_id,
        "description": "Acesta este un produs consumabil, se schimbă periodic.",
        "price": 100.0, "currency": "RON", "stock": 20, "stock_status": "in_stock",
        "created_at": now, "updated_at": now,
    })

    bundle_id = f"bundle-test-pachet-{suffix}"
    original = patch_require_admin()
    try:
        # a) price suggestion: 200 (part, full) + 100*0.95=95 (consumable) = 295
        suggestion = await server.admin_bundle_price_suggestion(
            server.BundlePriceSuggestionRequest(items=[
                {"product_id": part_id, "quantity": 1},
                {"product_id": consumable_id, "quantity": 1},
            ]),
            make_request(),
        )
        check("a) suggested price = part + 5%-off consumable", suggestion["suggested_price"] == 295.0, suggestion)
        consumable_line = next(b for b in suggestion["breakdown"] if b["product_id"] == consumable_id)
        check("a2) consumable line flagged + discounted", consumable_line["is_consumable"] is True and consumable_line["unit_price"] == 95.0, consumable_line)
        part_line = next(b for b in suggestion["breakdown"] if b["product_id"] == part_id)
        check("a3) non-consumable line unchanged", part_line["is_consumable"] is False and part_line["unit_price"] == 200.0, part_line)

        # b) quantity multiplies correctly
        suggestion2 = await server.admin_bundle_price_suggestion(
            server.BundlePriceSuggestionRequest(items=[{"product_id": consumable_id, "quantity": 3}]),
            make_request(),
        )
        check("b) quantity multiplies line total", suggestion2["suggested_price"] == 285.0, suggestion2)  # 95*3

        # Create the actual Pachet product - staff overrides the suggestion
        # down to a round 250 RON fixed price.
        await db.shopify_products.insert_one({
            "id": bundle_id, "title": "Pachet test", "handle": bundle_id, "description": "Pachet de test.",
            "price": 250.0, "currency": "RON", "stock": 10, "stock_status": "in_stock",
            "product_type": "Pachet",
            "bundle_items": [{"product_id": part_id, "quantity": 1}, {"product_id": consumable_id, "quantity": 1}],
            "compatible_models": ["6150R"], "compatible_engines": ["6068H"], "compatible_transmissions": ["AutoPowr"],
            "created_at": now, "updated_at": now,
        })

        # c) get_bundle_items returns full component details + quantity
        items_result = await server.get_bundle_items(bundle_id)
        item_ids = [i["id"] for i in items_result["items"]]
        check("c) bundle-items returns both components", set(item_ids) == {part_id, consumable_id}, items_result)
        part_item = next(i for i in items_result["items"] if i["id"] == part_id)
        check("c2) bundle-items includes quantity + real price", part_item["quantity"] == 1 and part_item["price"] == 200.0, part_item)

        # d) compatible-bundles matching by model
        by_model = await server.get_compatible_bundles(model="6150R")
        check("d) compatible bundle found by model", bundle_id in [p.id for p in by_model], [p.id for p in by_model])

        # e) matching by engine alone (different axis, same bundle)
        by_engine = await server.get_compatible_bundles(engine="6068H")
        check("e) compatible bundle found by engine", bundle_id in [p.id for p in by_engine], [p.id for p in by_engine])

        # f) no match for an unrelated model
        by_wrong_model = await server.get_compatible_bundles(model="totally-different-model-xyz")
        check("f) no false-positive match for unrelated model", bundle_id not in [p.id for p in by_wrong_model], [p.id for p in by_wrong_model])

        # g) no criteria at all -> 400 (would otherwise match every bundle)
        try:
            await server.get_compatible_bundles()
            check("g) no criteria rejected", False, "no exception raised")
        except HTTPException as e:
            check("g) no criteria rejected -> 400", e.status_code == 400, e.status_code)

        # h) THE key claim: a Pachet is a completely normal product for
        # pricing/checkout purposes - no special-casing needed anywhere in
        # the cart/order path. _get_authoritative_price returns its own
        # fixed price untouched.
        authoritative = await server._get_authoritative_price(bundle_id)
        check("h) bundle prices through the normal authoritative-price path", authoritative == 250.0, authoritative)

        # i) admin_create_product (the REAL creation endpoint the CRM form
        # actually calls, as opposed to the direct db.insert_one() above)
        # must itself persist bundle_items/compatible_engines/
        # compatible_transmissions - it builds the Mongo doc manually
        # field-by-field rather than from ProductCreate.dict(), so these 3
        # fields were silently dropped on every real Pachet creation until
        # this check was added (caught 2026-08-16, from a real report of an
        # empty Pachet after save).
        created_id = None
        try:
            created = await server.admin_create_product(
                make_request(),
                server.ProductCreate(
                    title="Pachet creat via endpoint",
                    price=300.0,
                    product_type="Pachet",
                    category="motor",
                    bundle_items=[{"product_id": part_id, "quantity": 2}],
                    compatible_engines=["6090H"],
                    compatible_transmissions=["PowrQuad"],
                ),
            )
            created_id = created.id
            check("i1) created id is a real string", isinstance(created.id, str) and bool(created.id))
            stored = await db.shopify_products.find_one({"id": created.id})
            check("i2) bundle_items persisted", stored.get("bundle_items") == [{"product_id": part_id, "quantity": 2}], stored.get("bundle_items"))
            check("i3) compatible_engines persisted", stored.get("compatible_engines") == ["6090H"], stored.get("compatible_engines"))
            check("i4) compatible_transmissions persisted", stored.get("compatible_transmissions") == ["PowrQuad"], stored.get("compatible_transmissions"))
            check("i5) Pachet category round-trips through collections", "Pachete motor" in (stored.get("collections") or []), stored.get("collections"))
        finally:
            await db.shopify_products.delete_one({"id": created_id})

    finally:
        server._require_admin = original
        await db.shopify_products.delete_many({"id": {"$in": [part_id, consumable_id, bundle_id]}})

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
