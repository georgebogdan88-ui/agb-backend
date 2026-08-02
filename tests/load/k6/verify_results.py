"""Post-test verification against the STAGING database - run this after
any k6 scenario that creates orders, to check things k6 itself can't see
from the outside (duplicate orders, orphaned carts, etc).

This must point at the STAGING MongoDB, never production - pass its
connection string explicitly, there is no default, on purpose.

Usage:
    python verify_results.py "mongodb+srv://.../staging_db"
"""
import sys
from collections import Counter

from pymongo import MongoClient

if len(sys.argv) != 2:
    print(__doc__)
    sys.exit(1)

mongo_url = sys.argv[1]
client = MongoClient(mongo_url)
db = client.get_default_database()

print(f"Connected to: {db.name}  (double-check this is staging, not production)")
print()

# Duplicate orders: same synthetic customer email + same total, created
# within a short window of each other - a real duplicate-submit would look
# like this. Loose on purpose (bucketing by minute) rather than requiring
# identical timestamps.
orders = list(
    db.orders.find(
        {"customer.email": {"$regex": "@loadtest.invalid$"}},
        {"customer.email": 1, "total": 1, "created_at": 1},
    )
)
print(f"Synthetic load-test orders found: {len(orders)}")

buckets = Counter()
for o in orders:
    minute = o["created_at"].strftime("%Y-%m-%d %H:%M") if o.get("created_at") else "unknown"
    key = (o["customer"]["email"], o["total"], minute)
    buckets[key] += 1

duplicates = {k: v for k, v in buckets.items() if v > 1}
if duplicates:
    print(f"POTENTIAL DUPLICATE ORDERS: {len(duplicates)} groups look like double-submits:")
    for (email, total, minute), count in list(duplicates.items())[:20]:
        print(f"  {email}  {total} RON  ~{minute}  x{count}")
else:
    print("No duplicate-looking orders found.")
print()

# Negative stock - only meaningful once the stock-reservation fix (flagged
# as still-pending in the security/scalability audits) is actually
# implemented. Checked anyway so this script is ready for that day.
negative_stock = db.shopify_products.count_documents({"stock": {"$lt": 0}})
print(f"Products with negative stock: {negative_stock}"
      + ("" if negative_stock == 0 else "  <-- investigate immediately"))
print()

# Cart rows left behind by the test (cart.session_id starting with "k6-").
orphaned_carts = db.cart.count_documents({"session_id": {"$regex": "^k6-"}})
print(f"Leftover k6 cart rows: {orphaned_carts}")
if orphaned_carts:
    print('  Clean up with: db.cart.delete_many({"session_id": {"$regex": "^k6-"}})')
