"""
One-off importer: backfill db.shopify_order_history (and db.clients where
needed) from a Shopify admin "export orders" CSV.

Why this exists: the Shopify Admin API token used by the earlier
`/admin/clients/import-shopify` job lacks `read_all_orders`, so
`_fetch_shopify_customer_orders` only ever returns orders from the last ~60
days (20 orders total). To backfill full order history, George exported the
complete order history from the Shopify admin UI as CSV instead of
requesting the extra API scope.

CSV format notes (standard Shopify order export):
  - One row PER LINE ITEM. For a multi-item order, only the FIRST row of the
    group carries the order-level fields (Id, Financial Status, Total, Paid
    at, Billing/Shipping details, Payment Method, Cancelled at, ...) -
    subsequent rows for the same order leave those columns BLANK and only
    fill in Lineitem quantity/name/price/sku plus Email/Name/Created at
    (which happen to repeat on every row).
  - `Id` is therefore the anchor: a new non-blank `Id` starts a new order
    group; blank `Id` rows are additional line items appended to the last
    group. This matches Name 1:1 (220 distinct Name values <-> 220 non-blank
    Id rows in the 2026-07-26 export), so grouping by forward-filled Id is
    equivalent to grouping by Name, but Id is the real Shopify order id and
    matches what the existing `/admin/clients/import-shopify` job already
    stored in db.shopify_order_history.id.

This is NOT wired into server.py as an endpoint - it's a one-off backfill
George will run rarely (maybe once more if he re-exports later), so a plain
script kept it out of the shared API surface. It's idempotent: re-running is
safe, it upserts by the real Shopify order id and by a deterministic id for
CSV-only clients (hash of their normalized email), so nothing is duplicated.

Usage:
    venv/Scripts/python.exe scripts/import_orders_csv.py [path/to/export.csv] [--dry-run]

Env vars come from .env in the repo root (MONGO_URL / DB_NAME), same DB the
rest of the clients/orders feature already uses.
"""
import argparse
import csv
import hashlib
import os
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from pymongo import MongoClient, UpdateOne

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env", override=False)

DEFAULT_CSV_PATH = ROOT_DIR / "orders_export_1.csv"


def normalize_text(text: str) -> str:
    """Same normalization used in server.py for name_normalized/search."""
    if not text:
        return ""
    normalized = unicodedata.normalize("NFD", text)
    ascii_text = "".join(c for c in normalized if unicodedata.category(c) != "Mn")
    return ascii_text.lower()


def parse_shopify_csv_datetime(value: str):
    """CSV format: '2026-07-23 18:05:58 +0300'. Returns a tz-aware datetime
    (UTC-normalized) so it's stored consistently with the API-imported
    orders, which used datetime.fromisoformat on ISO8601 values with the
    same kind of fixed offset."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S %z")
        return dt.astimezone(timezone.utc)
    except ValueError:
        print(f"  WARNING: could not parse datetime {value!r}", file=sys.stderr)
        return None


def deterministic_csv_client_id(email_normalized: str) -> str:
    """Stable id for a client we only know about from the CSV (no real
    Shopify customer id available) - so re-running the importer upserts the
    same client doc instead of creating a new one each time."""
    digest = hashlib.md5(email_normalized.encode("utf-8")).hexdigest()[:16]
    return f"csv-{digest}"


def pick_first(*values):
    for v in values:
        if v:
            return v
    return ""


def group_orders(rows):
    """Forward-fill order-level columns and group consecutive rows into one
    order dict per real Shopify order id."""
    orders = []
    current = None

    for row in rows:
        raw_id = (row.get("Id") or "").strip()
        if raw_id:
            # New order anchor row.
            current = {
                "id": raw_id,
                "name": (row.get("Name") or "").strip(),
                "email": (row.get("Email") or "").strip(),
                "financial_status": (row.get("Financial Status") or "").strip().lower() or None,
                "fulfillment_status_raw": (row.get("Fulfillment Status") or "").strip().lower(),
                "currency": (row.get("Currency") or "RON").strip() or "RON",
                "total_price": (row.get("Total") or "0").strip(),
                "created_at_raw": (row.get("Created at") or "").strip(),
                "cancelled_at_raw": (row.get("Cancelled at") or "").strip(),
                "billing_name": (row.get("Billing Name") or "").strip(),
                "billing_phone": (row.get("Billing Phone") or "").strip(),
                "billing_address1": (row.get("Billing Address1") or "").strip(),
                "billing_address2": (row.get("Billing Address2") or "").strip(),
                "billing_city": (row.get("Billing City") or "").strip(),
                "billing_province": (row.get("Billing Province") or "").strip(),
                "billing_zip": (row.get("Billing Zip") or "").strip(),
                "billing_country": (row.get("Billing Country") or "").strip(),
                "shipping_name": (row.get("Shipping Name") or "").strip(),
                "shipping_phone": (row.get("Shipping Phone") or "").strip(),
                "shipping_address1": (row.get("Shipping Address1") or "").strip(),
                "shipping_address2": (row.get("Shipping Address2") or "").strip(),
                "shipping_city": (row.get("Shipping City") or "").strip(),
                "shipping_province": (row.get("Shipping Province") or "").strip(),
                "shipping_zip": (row.get("Shipping Zip") or "").strip(),
                "shipping_country": (row.get("Shipping Country") or "").strip(),
                "phone_col": (row.get("Phone") or "").strip(),
                "line_items": [],
            }
            orders.append(current)
        if current is None:
            # Shouldn't happen (first row of the file always had an Id in
            # the observed export), but guard against malformed input.
            raise ValueError(f"Line item row with no preceding order anchor: {row}")

        qty_raw = (row.get("Lineitem quantity") or "0").strip()
        price_raw = (row.get("Lineitem price") or "0").strip()
        current["line_items"].append({
            "title": (row.get("Lineitem name") or "").strip(),
            "quantity": int(float(qty_raw)) if qty_raw else 0,
            "price": float(price_raw) if price_raw else 0.0,
            "sku": (row.get("Lineitem sku") or "").strip() or None,
        })

    return orders


def build_order_doc(order: dict, client_id: str) -> dict:
    name = order["name"]
    order_number = None
    stripped = name.lstrip("#")
    if stripped.isdigit():
        order_number = int(stripped)

    fulfillment_status = order["fulfillment_status_raw"] or None
    if fulfillment_status == "unfulfilled":
        fulfillment_status = None  # matches how the API-imported docs store it

    line_items = [
        {"title": li["title"], "quantity": li["quantity"], "price": li["price"]}
        for li in order["line_items"]
    ]

    return {
        "id": order["id"],
        "client_id": client_id,
        "order_number": order_number,
        "name": name,
        "created_at": parse_shopify_csv_datetime(order["created_at_raw"]),
        "total_price": float(order["total_price"]) if order["total_price"] else 0.0,
        "currency": order["currency"],
        "financial_status": order["financial_status"],
        "fulfillment_status": fulfillment_status,
        "line_items": line_items,
    }


def build_minimal_client_doc(email_normalized: str, order: dict, orders_for_email: list) -> dict:
    name = pick_first(order["billing_name"], order["shipping_name"])
    phone = pick_first(order["billing_phone"], order["shipping_phone"], order["phone_col"])
    address1 = pick_first(order["billing_address1"], order["shipping_address1"])
    address2 = pick_first(order["billing_address2"], order["shipping_address2"])
    city = pick_first(order["billing_city"], order["shipping_city"])
    province = pick_first(order["billing_province"], order["shipping_province"])
    zip_code = pick_first(order["billing_zip"], order["shipping_zip"])
    country = pick_first(order["billing_country"], order["shipping_country"])

    total_spent = sum(float(o["total_price"]) for o in orders_for_email if o["total_price"])
    created_at_candidates = [
        parse_shopify_csv_datetime(o["created_at_raw"]) for o in orders_for_email
    ]
    created_at_candidates = [d for d in created_at_candidates if d is not None]
    earliest_created_at = min(created_at_candidates) if created_at_candidates else None

    client_id = deterministic_csv_client_id(email_normalized)

    return {
        "id": client_id,
        "shopify_customer_id": None,
        "name": name,
        "name_normalized": normalize_text(name),
        "email": order["email"],
        "email_normalized": email_normalized,
        "phone": phone,
        "address": {
            "address": address1,
            "address2": address2,
            "city": city,
            "county": province,
            "postal_code": zip_code,
            "country": country,
        },
        "orders_count": len(orders_for_email),
        "total_spent": total_spent,
        "created_at": earliest_created_at,
        "shopify_updated_at": None,
        "source": "csv_import",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_path", nargs="?", default=str(DEFAULT_CSV_PATH))
    parser.add_argument("--dry-run", action="store_true", help="Parse and report, but don't write to the DB")
    args = parser.parse_args()

    csv_path = Path(args.csv_path)
    if not csv_path.exists():
        print(f"CSV not found: {csv_path}", file=sys.stderr)
        sys.exit(1)

    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    print(f"Read {len(rows)} CSV rows from {csv_path}")

    orders = group_orders(rows)
    print(f"Grouped into {len(orders)} orders")

    mongo_url = os.environ["MONGO_URL"]
    db_name = os.environ["DB_NAME"]
    mongo = MongoClient(mongo_url)
    db = mongo[db_name]
    print(f"Connected to DB '{db_name}'")

    # Build email_normalized -> client id map from existing db.clients.
    existing_client_by_email = {}
    for c in db.clients.find({}, {"id": 1, "email_normalized": 1}):
        if c.get("email_normalized"):
            existing_client_by_email[c["email_normalized"]] = c["id"]
    print(f"Loaded {len(existing_client_by_email)} existing client emails")

    # Group CSV orders by email so we can build minimal client docs with
    # correct aggregate orders_count/total_spent for unmatched emails.
    orders_by_email = {}
    for order in orders:
        email_normalized = order["email"].strip().lower()
        orders_by_email.setdefault(email_normalized, []).append(order)

    unmatched_emails = sorted(e for e in orders_by_email if e not in existing_client_by_email)
    print(f"\n{len(unmatched_emails)} email(s) in the CSV have no matching client in db.clients:")
    for e in unmatched_emails:
        sample_order = orders_by_email[e][0]
        print(f"  - {e!r} (name={sample_order['billing_name'] or sample_order['shipping_name']!r}, "
              f"{len(orders_by_email[e])} order(s), first order Name={sample_order['name']})")

    new_client_docs = []
    for email_normalized in unmatched_emails:
        orders_for_email = orders_by_email[email_normalized]
        doc = build_minimal_client_doc(email_normalized, orders_for_email[0], orders_for_email)
        new_client_docs.append(doc)
        existing_client_by_email[email_normalized] = doc["id"]

    # Build the order docs to upsert.
    order_docs = []
    for order in orders:
        email_normalized = order["email"].strip().lower()
        client_id = existing_client_by_email.get(email_normalized)
        order_docs.append(build_order_doc(order, client_id))

    existing_order_ids = set(o["id"] for o in db.shopify_order_history.find({}, {"id": 1}))
    new_order_ids = [o["id"] for o in order_docs if o["id"] not in existing_order_ids]
    already_present_ids = [o["id"] for o in order_docs if o["id"] in existing_order_ids]

    print(f"\nOrders already present in db.shopify_order_history (will be upserted/left consistent): {len(already_present_ids)}")
    print(f"New orders to insert: {len(new_order_ids)}")
    print(f"New minimal client records to create: {len(new_client_docs)}")

    if args.dry_run:
        print("\n--dry-run set: no writes performed.")
        return

    now = datetime.utcnow()

    if new_client_docs:
        client_ops = []
        for doc in new_client_docs:
            set_doc = dict(doc)
            client_ops.append(
                UpdateOne(
                    {"id": doc["id"]},
                    {"$set": set_doc, "$setOnInsert": {"imported_at": now}},
                    upsert=True,
                )
            )
        result = db.clients.bulk_write(client_ops)
        print(f"\nclients bulk_write: matched={result.matched_count} upserted={len(result.upserted_ids)} modified={result.modified_count}")

    order_ops = []
    for doc in order_docs:
        set_doc = dict(doc)
        order_ops.append(
            UpdateOne(
                {"id": doc["id"]},
                {"$set": set_doc, "$setOnInsert": {"imported_at": now}},
                upsert=True,
            )
        )
    result = db.shopify_order_history.bulk_write(order_ops)
    print(f"shopify_order_history bulk_write: matched={result.matched_count} upserted={len(result.upserted_ids)} modified={result.modified_count}")

    final_count = db.shopify_order_history.count_documents({})
    print(f"\ndb.shopify_order_history total documents now: {final_count}")
    print(f"db.clients total documents now: {db.clients.count_documents({})}")


if __name__ == "__main__":
    main()
