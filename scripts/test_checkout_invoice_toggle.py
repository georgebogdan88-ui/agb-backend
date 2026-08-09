"""
Focused, standalone check for the checkout "issue on company" (invoice
toggle) fields added to CustomerInfo and to sync_order_to_crm's payload in
server.py.

Not a pytest suite - mirrors scripts/test_stock_checkout.py's and
scripts/test_admin_customer_account_lookup.py's approach (this repo still
has no test framework, tests/ dir, or pytest in requirements.txt). Uses
mongomock + mongomock-motor to swap in an in-memory Mongo double for
server.client/db, and a fake httpx.AsyncClient to capture the outbound CRM
sync payload without making a real network call.

Covers:
  (a) a company order (CustomerInfo.is_company=True + the new company_*
      fields) round-trips correctly through CustomerInfo, through
      Order(**order_data.dict()) (the exact construction create_order uses),
      and into the stored Order doc in db.orders.
  (b) an old-style customer payload with NONE of the new keys present at all
      (mimics a pre-change webshop/mobile request body) still validates,
      with is_company defaulting to False and the rest to None - proving
      the change is backward-compatible, not just additive on paper.
  (c) sync_order_to_crm's payload includes denumire_societate/cui/reg_com/
      administrator/company_address (nested strada/numar/bloc/scara/ap/
      oras/judet/cod_postal) ONLY when order.customer.is_company is True,
      with the exact values from the order.
  (d) sync_order_to_crm's payload for a personal (is_company=False) order is
      byte-identical (same keys, same values, same JSON serialization) to
      the shape it produced before this change - none of the new keys leak
      in as null/empty.

Run (from repo root): python scripts/test_checkout_invoice_toggle.py
"""
import asyncio
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("MONGO_URL", "mongodb://fake-for-import-only/")
os.environ.setdefault("DB_NAME", "fake_db_for_import_only")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mongomock_motor  # noqa: E402
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


class _FakeResponse:
    def __init__(self, status_code=200, text="OK"):
        self.status_code = status_code
        self.text = text


class _FakeAsyncClient:
    """Captures every POST payload instead of hitting the network. Class-
    level `captured` list so the test can inspect calls made inside
    sync_order_to_crm without needing to thread a client instance through."""

    captured = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, json=None, headers=None):
        _FakeAsyncClient.captured.append({"url": url, "json": json, "headers": headers})
        return _FakeResponse(status_code=200, text="OK")


def patch_crm_config():
    """Point server's module-level CRM_API_URL/CRM_INTEGRATION_KEY (read
    directly, not via os.environ, inside sync_order_to_crm) at fake test
    values, and swap httpx.AsyncClient for the capturing fake. Returns the
    originals so callers can restore them."""
    original_url = server.CRM_API_URL
    original_key = server.CRM_INTEGRATION_KEY
    original_client_cls = server.httpx.AsyncClient
    server.CRM_API_URL = "https://fake-crm.example/api"
    server.CRM_INTEGRATION_KEY = "fake-integration-key"
    server.httpx.AsyncClient = _FakeAsyncClient
    _FakeAsyncClient.captured = []
    return original_url, original_key, original_client_cls


def restore_crm_config(originals):
    original_url, original_key, original_client_cls = originals
    server.CRM_API_URL = original_url
    server.CRM_INTEGRATION_KEY = original_key
    server.httpx.AsyncClient = original_client_cls


COMPANY_CUSTOMER = {
    "name": "Ion Popescu",
    "email": "ion@example.com",
    "phone": "0700000000",
    "address": "Str. Muncii 1",
    "city": "Cluj-Napoca",
    "county": "Cluj",
    "postal_code": "400001",
    "notes": "Livrare dupa ora 14",
    "is_company": True,
    "company_name": "Ion SRL",
    "cui": "RO12345678",
    "reg_com": "J12/345/2020",
    "administrator": "Ion Popescu",
    "company_address_strada": "Str. Fabricii",
    "company_address_numar": "10",
    "company_address_bloc": "B2",
    "company_address_scara": "A",
    "company_address_ap": "5",
    "company_address_oras": "Cluj-Napoca",
    "company_address_judet": "Cluj",
    "company_address_cod_postal": "400002",
}

# Deliberately has NONE of the new keys - mimics a request body sent by a
# webshop/mobile client build that predates this change.
PERSONAL_CUSTOMER_OLD_STYLE = {
    "name": "Maria Ionescu",
    "email": "maria@example.com",
    "phone": "0711111111",
    "address": "Str. Lalelelor 2",
    "city": "Bucuresti",
    "county": "Bucuresti",
    "postal_code": "010101",
}


def make_order_create(customer_dict, session_id="sess-1"):
    return server.OrderCreate(
        session_id=session_id,
        items=[{"product_id": "p1", "product_name": "Piesa", "quantity": 1, "price": 100.0}],
        customer=customer_dict,
        subtotal=100.0,
        shipping=25.0,
        total=125.0,
        payment_method="ramburs",
    )


async def scenario_a_company_order_roundtrips_through_stored_doc():
    """(a) a company order's new fields survive CustomerInfo validation,
    Order(**order_data.dict()) construction (the exact line create_order
    uses), and a real insert_one/find_one round-trip through db.orders."""
    db = await fresh_db()
    order_data = make_order_create(COMPANY_CUSTOMER)
    order = server.Order(**order_data.dict())

    check("a) in-memory Order.customer.is_company True", order.customer.is_company is True)
    check("a) in-memory Order.customer.company_name", order.customer.company_name == "Ion SRL")
    check("a) in-memory Order.customer.cui", order.customer.cui == "RO12345678")
    check("a) in-memory Order.customer.reg_com", order.customer.reg_com == "J12/345/2020")
    check("a) in-memory Order.customer.administrator", order.customer.administrator == "Ion Popescu")
    check("a) in-memory Order.customer.company_address_strada", order.customer.company_address_strada == "Str. Fabricii")
    check("a) in-memory Order.customer.company_address_cod_postal", order.customer.company_address_cod_postal == "400002")

    await db.orders.insert_one(order.dict())
    raw_doc = await db.orders.find_one({"id": order.id})
    check("a) raw stored doc has customer.is_company True", raw_doc["customer"]["is_company"] is True, raw_doc["customer"])
    check("a) raw stored doc has customer.company_name", raw_doc["customer"]["company_name"] == "Ion SRL")
    check("a) raw stored doc has customer.cui", raw_doc["customer"]["cui"] == "RO12345678")
    check("a) raw stored doc has customer.company_address_judet", raw_doc["customer"]["company_address_judet"] == "Cluj")

    reloaded = server.Order(**raw_doc)
    check("a) reloaded Order.customer.is_company True", reloaded.customer.is_company is True)
    check("a) reloaded Order.customer.company_address_bloc", reloaded.customer.company_address_bloc == "B2")
    check("a) reloaded Order.customer.company_address_scara", reloaded.customer.company_address_scara == "A")
    check("a) reloaded Order.customer.company_address_ap", reloaded.customer.company_address_ap == "5")
    check("a) reloaded Order.customer.company_address_oras", reloaded.customer.company_address_oras == "Cluj-Napoca")
    check("a) non-company fields still intact (name)", reloaded.customer.name == "Ion Popescu")
    check("a) non-company fields still intact (notes)", reloaded.customer.notes == "Livrare dupa ora 14")


async def scenario_b_old_style_payload_still_validates_backward_compat():
    """(b) a customer payload with none of the new keys at all (pre-change
    request shape) still validates - is_company defaults to False, all new
    string fields default to None."""
    ci = server.CustomerInfo(**PERSONAL_CUSTOMER_OLD_STYLE)
    check("b) old-style payload validates without new keys", ci.name == "Maria Ionescu")
    check("b) is_company defaults to False", ci.is_company is False)
    check("b) company_name defaults to None", ci.company_name is None)
    check("b) cui defaults to None", ci.cui is None)
    check("b) reg_com defaults to None", ci.reg_com is None)
    check("b) administrator defaults to None", ci.administrator is None)
    check("b) company_address_strada defaults to None", ci.company_address_strada is None)
    check("b) company_address_cod_postal defaults to None", ci.company_address_cod_postal is None)


async def scenario_c_sync_payload_includes_company_fields_when_is_company_true():
    """(c) sync_order_to_crm's payload includes denumire_societate/cui/
    reg_com/administrator/company_address (nested) with the order's exact
    values, only because is_company is True."""
    db = await fresh_db()
    order_data = make_order_create(COMPANY_CUSTOMER, session_id="sess-company")
    order = server.Order(**order_data.dict())
    await db.orders.insert_one(order.dict())

    originals = patch_crm_config()
    try:
        await server.sync_order_to_crm(order)
    finally:
        restore_crm_config(originals)

    check("c) exactly one CRM POST made", len(_FakeAsyncClient.captured) == 1, _FakeAsyncClient.captured)
    sent = _FakeAsyncClient.captured[-1]["json"]
    customer = sent.get("customer", {})
    check("c) denumire_societate present and correct", customer.get("denumire_societate") == "Ion SRL", customer)
    check("c) cui present and correct", customer.get("cui") == "RO12345678", customer)
    check("c) reg_com present and correct", customer.get("reg_com") == "J12/345/2020", customer)
    check("c) administrator present and correct", customer.get("administrator") == "Ion Popescu", customer)
    company_address = customer.get("company_address")
    check("c) company_address is present as nested object", isinstance(company_address, dict), customer)
    expected_company_address = {
        "strada": "Str. Fabricii",
        "numar": "10",
        "bloc": "B2",
        "scara": "A",
        "ap": "5",
        "oras": "Cluj-Napoca",
        "judet": "Cluj",
        "cod_postal": "400002",
    }
    check("c) company_address exact nested shape/values", company_address == expected_company_address,
          f"got {company_address}")
    # Base (pre-existing) personal fields must still be present and correct.
    check("c) base field nume still present", customer.get("nume") == "Ion Popescu")
    check("c) base field email still present", customer.get("email") == "ion@example.com")


async def scenario_d_sync_payload_byte_identical_for_personal_order():
    """(d) sync_order_to_crm's payload for a personal (is_company=False)
    order is byte-identical to the pre-change shape - no new keys leak in,
    not even as null."""
    db = await fresh_db()
    order_data = make_order_create(PERSONAL_CUSTOMER_OLD_STYLE, session_id="sess-personal")
    order = server.Order(**order_data.dict())
    await db.orders.insert_one(order.dict())

    originals = patch_crm_config()
    try:
        await server.sync_order_to_crm(order)
    finally:
        restore_crm_config(originals)

    check("d) exactly one CRM POST made", len(_FakeAsyncClient.captured) == 1, _FakeAsyncClient.captured)
    sent = _FakeAsyncClient.captured[-1]["json"]

    expected_payload = {
        "source": "webshop",
        "source_order_id": order.id,
        "payment_method": "ramburs",
        "note": "",
        "customer": {
            "nume": "Maria Ionescu",
            "email": "maria@example.com",
            "telefon": "0711111111",
            "adresa_strada": "Str. Lalelelor 2",
            "adresa_oras": "Bucuresti",
            "adresa_judet": "Bucuresti",
            "adresa_cod_postal": "010101",
        },
        "items": [
            {
                "denumire": "Piesa",
                "cod_prod": "p1",
                "cantitate": 1,
                "pret_unitar_cu_tva": 100.0,
            }
        ],
    }
    check("d) payload dict equals pre-change expected shape exactly", sent == expected_payload,
          f"diff: got {sent}")
    check("d) payload JSON-serialization byte-identical to pre-change shape",
          json.dumps(sent, sort_keys=True) == json.dumps(expected_payload, sort_keys=True))
    check("d) customer sub-dict has exactly the 7 original keys (no new keys, not even null)",
          set(sent["customer"].keys()) == {
              "nume", "email", "telefon", "adresa_strada", "adresa_oras",
              "adresa_judet", "adresa_cod_postal",
          },
          sent["customer"].keys())
    for leaked_key in ("denumire_societate", "cui", "reg_com", "administrator", "company_address", "is_company"):
        check(f"d) '{leaked_key}' NOT present in personal-order payload", leaked_key not in sent["customer"])


async def main():
    await scenario_a_company_order_roundtrips_through_stored_doc()
    await scenario_b_old_style_payload_still_validates_backward_compat()
    await scenario_c_sync_payload_includes_company_fields_when_is_company_true()
    await scenario_d_sync_payload_byte_identical_for_personal_order()

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
