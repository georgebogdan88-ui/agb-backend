"""
Focused, standalone check for the "Adaugă echivalente" feature added in
server.py:
  - extract_oem_code() - the title-based OEM/part-code extraction heuristic
  - POST /admin/products/equivalents/preview (admin_preview_product_equivalents)
  - POST /admin/products/equivalents/apply (admin_apply_product_equivalents)

Not a pytest suite - mirrors scripts/test_admin_customer_account_lookup.py's
approach (this repo still has no test framework, tests/ dir, or pytest in
requirements.txt). Uses mongomock + mongomock-motor to swap in an in-memory
Mongo double for server.client/db.

_require_admin itself is already covered end-to-end by
scripts/test_require_admin_bff_only.py, so it's monkeypatched here to a
fixed-admin stub - this script isolates the NEW logic (extraction, matching,
preview being read-only, apply's bidirectional $addToSet writes), not the
auth layer.

Run (from repo root): venv/Scripts/python.exe scripts/test_product_equivalent_matching.py
"""
import asyncio
import os
import sys
from pathlib import Path

os.environ.setdefault("MONGO_URL", "mongodb://fake-for-import-only/")
os.environ.setdefault("DB_NAME", "fake_db_for_import_only")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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
    mock_client = mongomock_motor.AsyncMongoMockClient()
    mock_db = mock_client["test_db"]
    server.client = mock_client
    server.db = mock_db
    return mock_db


def make_request():
    """Both endpoints under test only ever pass `request` through to
    _require_admin/_write_audit_log (both stubbed/tolerant below), never
    read it directly themselves, so an empty scope is enough."""
    return Request({"type": "http", "headers": [], "method": "POST", "path": "/admin/products/equivalents"})


def patch_require_admin(admin_id="admin-1", admin_email="staff@example.com"):
    original = server._require_admin

    async def _stub(request):
        return {"id": admin_id, "email": admin_email, "role": "admin"}

    server._require_admin = _stub
    return original


def base_product(**overrides):
    doc = {
        "id": "p-default",
        "title": "Default title",
        "handle": "default-title",
        "description": "",
        "price": 100.0,
        "currency": "RON",
        "vendor": "John Deere",
        "sku": "SKU-1",
        "image_url": None,
        "stock": 5,
        "equivalent_product_ids": [],
        "complementary_product_ids": [],
        "compatible_models": [],
        "tags": [],
        "collections": [],
    }
    doc.update(overrides)
    return doc


# ==================== 1) extract_oem_code() heuristic ====================

def scenario_1_extract_oem_code():
    cases = [
        # (title, expected_code, description)
        ("Carcasa Termostate John Deere R522776", "R522776", "trailing single code"),
        ("Filtru combustibil compatibil AL79782, AL175855, AL175835, AL201127",
         "AL79782", "trailing comma-separated code list -> first is primary"),
        ("Filtru combustibil compatibil AL79782 AL175855 AL175835 AL201127",
         "AL79782", "trailing space-separated code list -> first is primary"),
        ("R169152 Inel etanșare transmisie", "R169152", "leading code (gap a)"),
        ("L111005 Garnitura", "L111005", "leading code, short title (gap a)"),
        ("Bucsa ax cotit 644724.1", "644724.1", "trailing code with period punctuation (gap c)"),
        ("Racord hidraulic 72/384-21", "72/384-21", "trailing code with slash+hyphen punctuation (gap c)"),
        ("Piesa fara niciun cod identificabil aici", None, "no code-like token anywhere -> None"),
    ]
    for title, expected, desc in cases:
        got = server.extract_oem_code(title)
        check(f"1) extract_oem_code: {desc!r} -> {expected!r}", got == expected,
              f"title={title!r} got={got!r}")

    # Known false-positive-prone unit/spec-like tokens - not required to be
    # filtered perfectly, but extraction must not crash / raise on them.
    for title in ["Motor 25Cm3 Diesel", "Siguranta 10A", "25Cm3", "10A"]:
        try:
            result = server.extract_oem_code(title)
            check(f"1) extract_oem_code doesn't crash on {title!r}", True, f"-> {result!r}")
        except Exception as e:
            check(f"1) extract_oem_code doesn't crash on {title!r}", False, f"raised {e!r}")

    # Empty / None-ish input shouldn't crash either.
    for title in ["", "   ", None]:
        try:
            result = server.extract_oem_code(title)
            check(f"1) extract_oem_code doesn't crash on {title!r}", result is None, f"-> {result!r}")
        except Exception as e:
            check(f"1) extract_oem_code doesn't crash on {title!r}", False, f"raised {e!r}")


# ==================== 2) preview endpoint ====================

async def scenario_2_preview_finds_cross_vendor_matches_and_writes_nothing():
    db = await fresh_db()
    original = patch_require_admin()
    try:
        # Same part (R522776), 3 different vendors + 1 same-vendor decoy +
        # 1 unrelated product with a different code entirely.
        await db.shopify_products.insert_many([
            base_product(id="jd-1", title="Carcasa Termostate John Deere R522776", vendor="John Deere"),
            base_product(id="vp-1", title="Carcasa Termostate Vapormatic R522776", vendor="Vapormatic"),
            base_product(id="fp-1", title="Carcasa Termostate FP Diesel R522776", vendor="FP Diesel"),
            base_product(id="jd-2", title="Carcasa Termostate John Deere R522776 varianta 2", vendor="John Deere"),
            base_product(id="other-1", title="Alt produs complet diferit L999999", vendor="Mahle"),
        ])
        before_docs = await db.shopify_products.find({}).to_list(None)

        payload = server.ProductEquivalentsPreviewRequest(product_ids=["jd-1"])
        result = await server.admin_preview_product_equivalents(request=make_request(), payload=payload)

        after_docs = await db.shopify_products.find({}).to_list(None)
        check("2) preview makes zero writes", before_docs == after_docs, "docs changed after preview")

        entry = result["results"][0]
        check("2) preview: extracted_code correct", entry["extracted_code"] == "R522776", entry)
        match_ids = {m["id"] for m in entry["matches"]}
        check("2) preview: finds cross-vendor match (vp-1)", "vp-1" in match_ids, match_ids)
        check("2) preview: finds cross-vendor match (fp-1)", "fp-1" in match_ids, match_ids)
        check("2) preview: excludes same-vendor match (jd-2)", "jd-2" not in match_ids, match_ids)
        check("2) preview: excludes self (jd-1)", "jd-1" not in match_ids, match_ids)
        check("2) preview: excludes unrelated product (other-1)", "other-1" not in match_ids, match_ids)
        check("2) preview: match count is exactly 2", len(entry["matches"]) == 2, entry["matches"])
        # Sanity on the returned shape - useful for a staff member to
        # visually confirm the match.
        vp_match = next(m for m in entry["matches"] if m["id"] == "vp-1")
        check("2) preview: match carries vendor", vp_match.get("vendor") == "Vapormatic", vp_match)
        check("2) preview: match carries title", "Vapormatic" in (vp_match.get("title") or ""), vp_match)
        check("2) preview: match carries price", vp_match.get("price") == 100.0, vp_match)
    finally:
        server._require_admin = original


async def scenario_3_preview_excludes_already_linked_equivalents():
    db = await fresh_db()
    original = patch_require_admin()
    try:
        await db.shopify_products.insert_many([
            base_product(id="jd-1", title="Piesa X R555555", vendor="John Deere",
                         equivalent_product_ids=["vp-1"]),
            base_product(id="vp-1", title="Piesa X R555555", vendor="Vapormatic"),
            base_product(id="fp-1", title="Piesa X R555555", vendor="FP Diesel"),
        ])
        payload = server.ProductEquivalentsPreviewRequest(product_ids=["jd-1"])
        result = await server.admin_preview_product_equivalents(request=make_request(), payload=payload)
        match_ids = {m["id"] for m in result["results"][0]["matches"]}
        check("3) preview excludes already-linked equivalent (vp-1)", "vp-1" not in match_ids, match_ids)
        check("3) preview still proposes not-yet-linked match (fp-1)", "fp-1" in match_ids, match_ids)
    finally:
        server._require_admin = original


async def scenario_4_preview_no_code_and_not_found_ids():
    db = await fresh_db()
    original = patch_require_admin()
    try:
        await db.shopify_products.insert_one(
            base_product(id="no-code-1", title="Piesa fara cod identificabil", vendor="John Deere")
        )
        payload = server.ProductEquivalentsPreviewRequest(product_ids=["no-code-1", "does-not-exist"])
        result = await server.admin_preview_product_equivalents(request=make_request(), payload=payload)
        by_id = {r["product_id"]: r for r in result["results"]}
        check("4) preview: no-code product -> extracted_code None", by_id["no-code-1"]["extracted_code"] is None, by_id["no-code-1"])
        check("4) preview: no-code product -> empty matches", by_id["no-code-1"]["matches"] == [], by_id["no-code-1"])
        check("4) preview: unknown id -> not_found True", by_id["does-not-exist"]["not_found"] is True, by_id["does-not-exist"])
    finally:
        server._require_admin = original


async def scenario_5_preview_empty_product_ids_rejected():
    await fresh_db()
    original = patch_require_admin()
    try:
        try:
            await server.admin_preview_product_equivalents(
                request=make_request(), payload=server.ProductEquivalentsPreviewRequest(product_ids=[]))
            check("5) preview rejects empty product_ids", False, "no exception raised")
        except server.HTTPException as e:
            check("5) preview rejects empty product_ids -> 400", e.status_code == 400, f"got {e.status_code}")
    finally:
        server._require_admin = original


# ==================== 3) apply endpoint ====================

async def scenario_6_apply_bidirectional_linking():
    db = await fresh_db()
    original = patch_require_admin()
    try:
        await db.shopify_products.insert_many([
            base_product(id="jd-1", title="Piesa Y R777777", vendor="John Deere"),
            base_product(id="vp-1", title="Piesa Y R777777", vendor="Vapormatic"),
            base_product(id="fp-1", title="Piesa Y R777777", vendor="FP Diesel"),
        ])

        payload = server.ProductEquivalentsApplyRequest(links=[
            server.ProductEquivalentLinkItem(product_id="jd-1", equivalent_ids=["vp-1", "fp-1"]),
        ])
        result = await server.admin_apply_product_equivalents(request=make_request(), payload=payload)
        check("6) apply: updated_pairs == 2", result["updated_pairs"] == 2, result)
        check("6) apply: not_found empty", result["not_found"] == [], result)

        jd1 = await db.shopify_products.find_one({"id": "jd-1"})
        vp1 = await db.shopify_products.find_one({"id": "vp-1"})
        fp1 = await db.shopify_products.find_one({"id": "fp-1"})

        check("6) apply: forward link jd-1 -> vp-1", "vp-1" in jd1["equivalent_product_ids"], jd1)
        check("6) apply: forward link jd-1 -> fp-1", "fp-1" in jd1["equivalent_product_ids"], jd1)
        check("6) apply: reverse link vp-1 -> jd-1 (bidirectional)", "jd-1" in vp1["equivalent_product_ids"], vp1)
        check("6) apply: reverse link fp-1 -> jd-1 (bidirectional)", "jd-1" in fp1["equivalent_product_ids"], fp1)
        # vp-1 and fp-1 were never linked to EACH OTHER - apply only wrote
        # the exact pairs given (jd-1<->vp-1, jd-1<->fp-1), not a full mesh.
        check("6) apply: vp-1 NOT linked to fp-1 (only given pairs written)",
              "fp-1" not in vp1["equivalent_product_ids"], vp1)
        check("6) apply: fp-1 NOT linked to vp-1 (only given pairs written)",
              "vp-1" not in fp1["equivalent_product_ids"], fp1)
    finally:
        server._require_admin = original


async def scenario_7_apply_addToSet_idempotent_no_duplicates():
    db = await fresh_db()
    original = patch_require_admin()
    try:
        await db.shopify_products.insert_many([
            base_product(id="jd-1", title="Piesa Z R888888", vendor="John Deere"),
            base_product(id="vp-1", title="Piesa Z R888888", vendor="Vapormatic"),
        ])
        payload = server.ProductEquivalentsApplyRequest(links=[
            server.ProductEquivalentLinkItem(product_id="jd-1", equivalent_ids=["vp-1"]),
        ])
        await server.admin_apply_product_equivalents(request=make_request(), payload=payload)
        await server.admin_apply_product_equivalents(request=make_request(), payload=payload)  # re-apply

        jd1 = await db.shopify_products.find_one({"id": "jd-1"})
        vp1 = await db.shopify_products.find_one({"id": "vp-1"})
        check("7) apply twice: jd-1.equivalent_product_ids has no duplicates",
              jd1["equivalent_product_ids"].count("vp-1") == 1, jd1["equivalent_product_ids"])
        check("7) apply twice: vp-1.equivalent_product_ids has no duplicates",
              vp1["equivalent_product_ids"].count("jd-1") == 1, vp1["equivalent_product_ids"])
    finally:
        server._require_admin = original


async def scenario_8_apply_additive_preserves_existing_links():
    db = await fresh_db()
    original = patch_require_admin()
    try:
        await db.shopify_products.insert_many([
            base_product(id="jd-1", title="Piesa W R999999", vendor="John Deere",
                         equivalent_product_ids=["pre-existing-1"]),
            base_product(id="pre-existing-1", title="Unrelated pre-existing link", vendor="Mahle"),
            base_product(id="vp-1", title="Piesa W R999999", vendor="Vapormatic"),
        ])
        payload = server.ProductEquivalentsApplyRequest(links=[
            server.ProductEquivalentLinkItem(product_id="jd-1", equivalent_ids=["vp-1"]),
        ])
        await server.admin_apply_product_equivalents(request=make_request(), payload=payload)
        jd1 = await db.shopify_products.find_one({"id": "jd-1"})
        check("8) apply is additive: pre-existing link preserved", "pre-existing-1" in jd1["equivalent_product_ids"], jd1)
        check("8) apply is additive: new link added too", "vp-1" in jd1["equivalent_product_ids"], jd1)
    finally:
        server._require_admin = original


async def scenario_9_apply_only_touches_given_ids_not_redo_matching():
    """apply must not redo the code-based matching itself - a product
    elsewhere in the catalog that WOULD match by code/vendor but isn't
    named in `links` must stay completely untouched."""
    db = await fresh_db()
    original = patch_require_admin()
    try:
        await db.shopify_products.insert_many([
            base_product(id="jd-1", title="Piesa V R121212", vendor="John Deere"),
            base_product(id="vp-1", title="Piesa V R121212", vendor="Vapormatic"),
            # Would also match jd-1 by code+vendor if matching were re-run,
            # but is deliberately NOT included in `links` below.
            base_product(id="fp-1", title="Piesa V R121212", vendor="FP Diesel"),
        ])
        payload = server.ProductEquivalentsApplyRequest(links=[
            server.ProductEquivalentLinkItem(product_id="jd-1", equivalent_ids=["vp-1"]),
        ])
        await server.admin_apply_product_equivalents(request=make_request(), payload=payload)
        fp1 = await db.shopify_products.find_one({"id": "fp-1"})
        jd1 = await db.shopify_products.find_one({"id": "jd-1"})
        check("9) apply doesn't redo matching: fp-1 untouched", fp1["equivalent_product_ids"] == [], fp1)
        check("9) apply doesn't redo matching: fp-1 not linked into jd-1 either",
              "fp-1" not in jd1["equivalent_product_ids"], jd1)
    finally:
        server._require_admin = original


async def scenario_10_apply_not_found_reported_both_sides():
    db = await fresh_db()
    original = patch_require_admin()
    try:
        await db.shopify_products.insert_one(base_product(id="jd-1", title="Piesa U R343434", vendor="John Deere"))
        payload = server.ProductEquivalentsApplyRequest(links=[
            server.ProductEquivalentLinkItem(product_id="jd-1", equivalent_ids=["does-not-exist"]),
            server.ProductEquivalentLinkItem(product_id="also-missing", equivalent_ids=["jd-1"]),
        ])
        result = await server.admin_apply_product_equivalents(request=make_request(), payload=payload)
        check("10) apply: unknown equivalent id reported in not_found", "does-not-exist" in result["not_found"], result)
        check("10) apply: unknown product_id reported in not_found", "also-missing" in result["not_found"], result)
        check("10) apply: updated_pairs == 0 (nothing valid to write)", result["updated_pairs"] == 0, result)
        jd1 = await db.shopify_products.find_one({"id": "jd-1"})
        check("10) apply: jd-1 untouched by the invalid pairs", jd1["equivalent_product_ids"] == [], jd1)
    finally:
        server._require_admin = original


async def scenario_11_apply_empty_links_rejected():
    await fresh_db()
    original = patch_require_admin()
    try:
        try:
            await server.admin_apply_product_equivalents(
                request=make_request(), payload=server.ProductEquivalentsApplyRequest(links=[]))
            check("11) apply rejects empty links", False, "no exception raised")
        except server.HTTPException as e:
            check("11) apply rejects empty links -> 400", e.status_code == 400, f"got {e.status_code}")
    finally:
        server._require_admin = original


async def scenario_12_apply_writes_audit_log_entry():
    db = await fresh_db()
    original = patch_require_admin()
    try:
        await db.shopify_products.insert_many([
            base_product(id="jd-1", title="Piesa T R565656", vendor="John Deere"),
            base_product(id="vp-1", title="Piesa T R565656", vendor="Vapormatic"),
        ])
        payload = server.ProductEquivalentsApplyRequest(links=[
            server.ProductEquivalentLinkItem(product_id="jd-1", equivalent_ids=["vp-1"]),
        ])
        await server.admin_apply_product_equivalents(request=make_request(), payload=payload)
        logs = await db.admin_audit_log.find({"action": "product.equivalents_apply"}).to_list(None)
        check("12) apply writes exactly one audit log entry for the batch", len(logs) == 1, logs)
    finally:
        server._require_admin = original


async def main():
    scenario_1_extract_oem_code()
    await scenario_2_preview_finds_cross_vendor_matches_and_writes_nothing()
    await scenario_3_preview_excludes_already_linked_equivalents()
    await scenario_4_preview_no_code_and_not_found_ids()
    await scenario_5_preview_empty_product_ids_rejected()
    await scenario_6_apply_bidirectional_linking()
    await scenario_7_apply_addToSet_idempotent_no_duplicates()
    await scenario_8_apply_additive_preserves_existing_links()
    await scenario_9_apply_only_touches_given_ids_not_redo_matching()
    await scenario_10_apply_not_found_reported_both_sides()
    await scenario_11_apply_empty_links_rejected()
    await scenario_12_apply_writes_audit_log_entry()

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
