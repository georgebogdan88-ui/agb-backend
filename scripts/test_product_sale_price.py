"""Focused, standalone check for the discount/liquidation sale price
(admin_set_product_sale_price, _get_authoritative_price's sale_price
priority, get_liquidation_products). Same style/pattern as this
directory's other standalone scripts (see test_stock_checkout.py's own
docstring) - direct-call against the real configured MONGO_URL, throwaway
data, full cleanup either way.

Run (from repo root): python scripts/test_product_sale_price.py
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
    return Request({"type": "http", "headers": [], "method": "PATCH", "path": "/admin/test"})


def patch_require_admin():
    original = server._require_admin

    async def _stub(request):
        return {"id": "admin-1", "email": "staff@example.com", "role": "admin"}

    server._require_admin = _stub
    return original


async def main():
    db = server.db
    pid = f"sale-price-test-{uuid.uuid4().hex[:8]}"
    await db.shopify_products.insert_one({
        "id": pid, "title": "Test Product", "handle": pid, "description": "",
        "price": 100.0, "currency": "RON", "stock": 10, "stock_status": "in_stock",
        "created_at": datetime.utcnow(), "updated_at": datetime.utcnow(),
    })

    original = patch_require_admin()
    try:
        # a) sale_price >= current price rejected
        try:
            await server.admin_set_product_sale_price(
                pid, server.ProductSalePriceUpdate(sale_type="discount", sale_price=100.0), make_request()
            )
            check("a) sale_price >= current price rejected", False, "no exception raised")
        except HTTPException as e:
            check("a) sale_price >= current price rejected -> 400", e.status_code == 400, e.status_code)

        # b) sale_type without sale_price rejected
        try:
            await server.admin_set_product_sale_price(
                pid, server.ProductSalePriceUpdate(sale_type="discount", sale_price=None), make_request()
            )
            check("b) missing sale_price rejected", False, "no exception raised")
        except HTTPException as e:
            check("b) missing sale_price rejected -> 400", e.status_code == 400, e.status_code)

        # c) valid discount set
        result = await server.admin_set_product_sale_price(
            pid, server.ProductSalePriceUpdate(sale_type="discount", sale_price=80.0), make_request()
        )
        check("c) discount set correctly", result.sale_type == "discount" and result.sale_price == 80.0, result)

        # d) _get_authoritative_price returns sale_price, ignoring any B2B discount passed
        price = await server._get_authoritative_price(pid, discount_percent=50)
        check("d) sale_price wins over B2B discount", price == 80.0, price)

        # e) discount product NOT included in liquidation listing
        liquidation = await server.get_liquidation_products(limit=1000)
        check("e) discount-type product excluded from liquidation list", pid not in [p.id for p in liquidation], [p.id for p in liquidation])

        # f) switch to liquidation
        result2 = await server.admin_set_product_sale_price(
            pid, server.ProductSalePriceUpdate(sale_type="liquidation", sale_price=60.0), make_request()
        )
        check("f) liquidation set correctly", result2.sale_type == "liquidation" and result2.sale_price == 60.0, result2)

        # g) liquidation product IS included in liquidation listing
        liquidation2 = await server.get_liquidation_products(limit=1000)
        check("g) liquidation-type product included in liquidation list", pid in [p.id for p in liquidation2], [p.id for p in liquidation2])

        # h) clearing (sale_type=None) removes it from liquidation list and restores normal price
        await server.admin_set_product_sale_price(pid, server.ProductSalePriceUpdate(sale_type=None), make_request())
        price_after_clear = await server._get_authoritative_price(pid)
        check("h) price restored to catalog price after clearing", price_after_clear == 100.0, price_after_clear)
        liquidation3 = await server.get_liquidation_products(limit=1000)
        check("h2) removed from liquidation list after clearing", pid not in [p.id for p in liquidation3], [p.id for p in liquidation3])

        # i) unknown product -> 404
        try:
            await server.admin_set_product_sale_price(
                "no-such-product-xyz", server.ProductSalePriceUpdate(sale_type="discount", sale_price=10.0), make_request()
            )
            check("i) unknown product rejected", False, "no exception raised")
        except HTTPException as e:
            check("i) unknown product rejected -> 404", e.status_code == 404, e.status_code)

    finally:
        server._require_admin = original
        await db.shopify_products.delete_one({"id": pid})

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
