"""Focused check for PATCH /admin/equipment/{equipment_id}/bundles and the
new assigned_bundle_ids field on Equipment/_clean_equipment_list.

Same style as this directory's other standalone scripts - direct-call
against the real configured MONGO_URL, throwaway data, full cleanup either
way. Does NOT touch db.shopify_products or trigger any CRM sync - purely a
db.users.equipment[] read/write, so no risk of production-CRM side effects
(unlike scripts that call create_order()).

Run (from repo root): python scripts/test_equipment_bundle_assignment.py
"""
import asyncio
import sys
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402
from fastapi import Request  # noqa: E402

PASS = []
FAIL = []


def check(name, condition, detail=""):
    if condition:
        PASS.append(name)
        print(f"PASS: {name}")
    else:
        FAIL.append(name)
        print(f"FAIL: {name}  {detail}")


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
    suffix = uuid.uuid4().hex[:8]
    now = datetime.utcnow()
    user_id = f"test-eq-bundle-user-{suffix}"
    equipment_id = f"test-eq-{suffix}"
    email = f"eqbundle-{suffix}@example.com"

    await db.users.insert_one({
        "_id": user_id,
        "email": email,
        "name": "Test EqBundle",
        "password_hash": "x",
        "is_shopify_customer": False,
        "created_at": now,
        "equipment": [{
            "id": equipment_id,
            "brand": "John Deere",
            "model": "6150R",
            "chassis_serial": None,
            "engine_serial": None,
            "engine_type": "6068H",
            "transmission_type": "AutoPowr",
            "front_axle_model": None,
            "features": [],
            "created_at": now.isoformat(),
        }],
    })

    original = patch_require_admin()
    try:
        # a) set two bundle ids
        result = await server.admin_set_equipment_bundles(
            make_request(), equipment_id,
            server.EquipmentBundlesUpdate(bundle_ids=["bundle-a", "bundle-b"]),
        )
        check("a) response echoes assigned_bundle_ids", result["assigned_bundle_ids"] == ["bundle-a", "bundle-b"], result)

        stored = await db.users.find_one({"_id": user_id})
        stored_eq = stored["equipment"][0]
        check("a2) persisted on the equipment subdocument", stored_eq.get("assigned_bundle_ids") == ["bundle-a", "bundle-b"], stored_eq)

        # b) GET /auth/equipment (_clean_equipment_list) actually surfaces it -
        # this is the exact allowlist-projection gotcha this session already
        # hit once for CRM's _project_for_list; verifying it isn't repeated.
        cleaned = server._clean_equipment_list(stored["equipment"])
        check("b) _clean_equipment_list includes assigned_bundle_ids", cleaned[0].get("assigned_bundle_ids") == ["bundle-a", "bundle-b"], cleaned[0])

        # c) replace with a shorter list (simulates an "unassign" from CRM,
        # which sends the full remaining list, not a delta)
        result2 = await server.admin_set_equipment_bundles(
            make_request(), equipment_id,
            server.EquipmentBundlesUpdate(bundle_ids=["bundle-a"]),
        )
        check("c) unassign via full-list replace", result2["assigned_bundle_ids"] == ["bundle-a"], result2)

        # d) unknown equipment id -> 404
        try:
            await server.admin_set_equipment_bundles(
                make_request(), "does-not-exist",
                server.EquipmentBundlesUpdate(bundle_ids=["x"]),
            )
            check("d) unknown equipment rejected", False, "no exception raised")
        except server.HTTPException as e:
            check("d) unknown equipment rejected -> 404", e.status_code == 404, e.status_code)

    finally:
        server._require_admin = original
        await db.users.delete_one({"_id": user_id})

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
