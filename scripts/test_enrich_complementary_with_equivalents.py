"""
Focused, standalone check for
scripts/enrich_complementary_with_equivalents.py's core logic:
_compute_enriched_complementary_ids(product, complementary_docs).

Per George's exact requirement (2026-08-22): for every product with a
non-empty complementary_product_ids, each complementary item's own
equivalent_product_ids ("same part, different brand") get folded DIRECTLY
into the main product's complementary_product_ids, as real stored data -
unconditionally, regardless of whether the complementary item itself is
currently in stock or not. Once stored, get_complementary_products' existing
_stock_sort_key ordering (server.py) handles display order with no further
read-time logic needed - so this test only needs to check the stored-data
shape the enrichment produces, not any endpoint response.

_compute_enriched_complementary_ids has no I/O (pure dict/list logic), so
this tests it directly with plain dicts - no mongomock / real Mongo
connection needed at all, per George's instruction to test the extracted
function directly rather than running the whole script against a live DB.

Not a pytest suite - mirrors the existing scripts/test_*.py convention
(PASS/FAIL harness, no pytest in requirements.txt).

Run (from repo root): venv/Scripts/python.exe scripts/test_enrich_complementary_with_equivalents.py
"""
import os
import sys
from pathlib import Path

# enrich_complementary_with_equivalents.py reads MONGO_URL/DB_NAME from the
# environment at import time (same os.environ["..."] pattern as server.py) -
# these dummy values are never actually connected to; _compute_enriched_
# complementary_ids itself does no I/O at all.
os.environ.setdefault("MONGO_URL", "mongodb://fake-for-import-only/")
os.environ.setdefault("DB_NAME", "fake_db_for_import_only")

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

import enrich_complementary_with_equivalents as enrich  # noqa: E402

PASS = []
FAIL = []


def check(name, condition, detail=""):
    if condition:
        PASS.append(name)
        print(f"PASS: {name}")
    else:
        FAIL.append(name)
        print(f"FAIL: {name}  {detail}")


def product(product_id, complementary_ids=None):
    return {"id": product_id, "title": f"Produs {product_id}", "complementary_product_ids": complementary_ids or []}


def complementary(product_id, equivalent_ids=None, stock_status="in_stock"):
    """A complementary product's own doc - only the fields
    _compute_enriched_complementary_ids reads matter here."""
    return {"id": product_id, "equivalent_product_ids": equivalent_ids or [], "stock_status": stock_status}


def scenario_a_stock_status_of_complementary_is_irrelevant():
    """George: apply equivalents unconditionally, regardless of whether the
    complementary item itself is in stock or not - no stock-based gating in
    this script (unlike the separate, unrelated dead-stock-fallback
    read-time feature)."""
    main = product("main", complementary_ids=["comp-in-stock", "comp-out-of-stock"])
    docs = {
        "comp-in-stock": complementary("comp-in-stock", equivalent_ids=["eq-of-instock"], stock_status="in_stock"),
        "comp-out-of-stock": complementary("comp-out-of-stock", equivalent_ids=["eq-of-oos"], stock_status="out_of_stock"),
    }
    updated_ids, new_ids = enrich._compute_enriched_complementary_ids(main, docs)
    check(
        "a) equivalents pulled in from BOTH an in-stock and an out-of-stock complementary item",
        set(new_ids) == {"eq-of-instock", "eq-of-oos"},
        new_ids,
    )
    check(
        "a) updated_complementary_ids contains both original complementary ids and both new equivalents",
        set(updated_ids) == {"comp-in-stock", "comp-out-of-stock", "eq-of-instock", "eq-of-oos"},
        updated_ids,
    )


def scenario_b_equivalents_of_complementary_are_added_to_main_list():
    main = product("main", complementary_ids=["comp-a"])
    docs = {"comp-a": complementary("comp-a", equivalent_ids=["eq-1", "eq-2"])}
    updated_ids, new_ids = enrich._compute_enriched_complementary_ids(main, docs)
    check("b) both equivalents of the single complementary item are added", new_ids == ["eq-1", "eq-2"], new_ids)
    check(
        "b) final complementary_product_ids = original + new equivalents, in that order",
        updated_ids == ["comp-a", "eq-1", "eq-2"],
        updated_ids,
    )


def scenario_c_no_duplicates_existing_or_self_reference():
    # "dup-existing" is both already a complementary id AND (independently)
    # one complementary item's equivalent - must not appear twice.
    # "main" appearing as one complementary item's "equivalent" must never
    # be added back onto its own complementary_product_ids (no self-ref).
    # "eq-shared" is an equivalent of BOTH comp-a and comp-b - must only be
    # added once even though it's discovered twice.
    main = product("main", complementary_ids=["comp-a", "comp-b", "dup-existing"])
    docs = {
        "comp-a": complementary("comp-a", equivalent_ids=["dup-existing", "eq-shared", "main"]),
        "comp-b": complementary("comp-b", equivalent_ids=["eq-shared", "eq-only-b"]),
        "dup-existing": complementary("dup-existing", equivalent_ids=[]),
    }
    updated_ids, new_ids = enrich._compute_enriched_complementary_ids(main, docs)
    check("c) no duplicate of an already-existing complementary id", new_ids.count("dup-existing") == 0, new_ids)
    check("c) no self-reference to the main product's own id", "main" not in new_ids, new_ids)
    check("c) an equivalent shared by two complementary items is added only once", new_ids.count("eq-shared") == 1, new_ids)
    check("c) a genuinely new equivalent unique to one complementary item is still added", "eq-only-b" in new_ids, new_ids)
    check("c) updated list itself has no duplicate entries at all", len(updated_ids) == len(set(updated_ids)), updated_ids)


def scenario_d_second_run_is_idempotent():
    main = product("main", complementary_ids=["comp-a"])
    docs = {"comp-a": complementary("comp-a", equivalent_ids=["eq-1", "eq-2"])}
    updated_ids_first, new_ids_first = enrich._compute_enriched_complementary_ids(main, docs)
    check("d) first pass adds the expected new ids", new_ids_first == ["eq-1", "eq-2"], new_ids_first)

    # Second pass: simulate the DB now holding the already-enriched list
    # (what the real script would re-read on a re-run).
    main_after_first_run = product("main", complementary_ids=updated_ids_first)
    updated_ids_second, new_ids_second = enrich._compute_enriched_complementary_ids(main_after_first_run, docs)
    check("d) second pass over already-enriched data adds nothing new (idempotent)", new_ids_second == [], new_ids_second)
    check("d) second pass leaves complementary_product_ids unchanged", updated_ids_second == updated_ids_first, updated_ids_second)


def scenario_e_product_without_complementary_ids_is_untouched():
    main = product("main", complementary_ids=[])
    updated_ids, new_ids = enrich._compute_enriched_complementary_ids(main, {})
    check("e) product with empty complementary_product_ids: no new ids", new_ids == [], new_ids)
    check("e) product with empty complementary_product_ids: list stays empty", updated_ids == [], updated_ids)

    # Also cover the field being entirely absent (not just an empty list) -
    # matches how the real script's Mongo query would still hand this doc
    # to the function if it somehow matched (defensive, shouldn't normally
    # happen given the script's own $ne: [] query filter).
    main_no_field = {"id": "main2"}
    updated_ids2, new_ids2 = enrich._compute_enriched_complementary_ids(main_no_field, {})
    check("e) product with missing complementary_product_ids field: no new ids", new_ids2 == [], new_ids2)
    check("e) product with missing complementary_product_ids field: list stays empty", updated_ids2 == [], updated_ids2)


def scenario_f_missing_complementary_doc_is_skipped_gracefully():
    """A complementary id that couldn't be resolved to a doc (deleted
    product, or simply not passed in complementary_docs) contributes
    nothing, same as get_complementary_products' own find_one loop silently
    skips missing referenced products."""
    main = product("main", complementary_ids=["comp-a", "comp-missing"])
    docs = {"comp-a": complementary("comp-a", equivalent_ids=["eq-1"])}
    updated_ids, new_ids = enrich._compute_enriched_complementary_ids(main, docs)
    check("f) missing complementary doc contributes no equivalents, no crash", new_ids == ["eq-1"], new_ids)
    check("f) missing complementary id itself is preserved untouched in the list", "comp-missing" in updated_ids, updated_ids)


def main():
    scenario_a_stock_status_of_complementary_is_irrelevant()
    scenario_b_equivalents_of_complementary_are_added_to_main_list()
    scenario_c_no_duplicates_existing_or_self_reference()
    scenario_d_second_run_is_idempotent()
    scenario_e_product_without_complementary_ids_is_untouched()
    scenario_f_missing_complementary_doc_is_skipped_gracefully()

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
