"""
One-off seed: populate the new db.equipment_options collection with today's
current hardcoded transmission-type / front-axle-model / features values
(previously duplicated as hardcoded lists in both agb-webshop and the mobile
app's source code), so the equipment form's dropdown/checkbox lists become
admin-manageable data (see GET /equipment-options and
POST /admin/equipment-options in server.py) instead of requiring a code
change in two separate repos to add a new option.

front_axle_model also carries the manufacturer-name fix George asked for:
the model codes are actually associated with specific axle manufacturers
(ZF / Dana Spicer), so the manufacturer name is now prefixed on each value.

Idempotent / safe to re-run: skips inserting any (category, value) pair that
already exists as a document (case-sensitive exact match, matching the
category+value shape POST /admin/equipment-options inserts - re-running
this script won't create duplicates of its own seed values, though it does
not attempt to de-dupe against values a human already added via the admin
endpoint with different casing).

This is a fresh, brand-new collection - no migration of *existing customer
equipment records* is done or needed here. Existing equipment already has
its transmission_type/front_axle_model/features values stored as plain
strings on each equipment record regardless of this options list; this
options list only powers the dropdown/checkbox UI going forward, it doesn't
validate or migrate historical data.

Usage:
    venv/Scripts/python.exe scripts/seed_equipment_options.py

Env vars come from .env in the repo root (MONGO_URL / DB_NAME), same as
server.py and the rest of this repo's scripts.
"""
import asyncio
import os
import uuid
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env", override=False)

MONGO_URL = os.environ["MONGO_URL"]
DB_NAME = os.environ["DB_NAME"]

TRANSMISSION_TYPE_SEED = [
    "PowrQuad", "PowrQuad Plus", "AutoQuad", "AutoQuad Plus", "AutoQuad EcoShift",
    "CommandQuad", "AutoPowr / IVT", "PowerShift", "PowrReverser SyncroPlus",
]
FRONT_AXLE_MODEL_SEED = [
    "ZF APL2025", "ZF APL2045", "ZF APL2060", "ZF AS2060", "ZF APL315", "ZF APL325", "ZF APL735",
    "Dana Spicer 702", "Dana Spicer 730", "Dana Spicer 740", "Dana Spicer 745", "Dana Spicer 750", "Dana Spicer 755",
]
FEATURES_SEED = [
    "Compresor AC", "Compresor aer comprimat", "Punte fata TLS", "Punte fata ILS",
    "E-SCV", "Suspensie cabina", "AutoTrac Ready", "Power Beyond",
]

SEED_BY_CATEGORY = {
    "transmission_type": TRANSMISSION_TYPE_SEED,
    "front_axle_model": FRONT_AXLE_MODEL_SEED,
    "features": FEATURES_SEED,
}


async def main():
    mongo = AsyncIOMotorClient(MONGO_URL)
    db = mongo[DB_NAME]
    print(f"Connected to DB '{DB_NAME}'")

    inserted = 0
    skipped = 0

    for category, values in SEED_BY_CATEGORY.items():
        for value in values:
            existing = await db.equipment_options.find_one({"category": category, "value": value})
            if existing:
                skipped += 1
                continue
            await db.equipment_options.insert_one({
                "id": str(uuid.uuid4()),
                "category": category,
                "value": value,
                "created_at": datetime.utcnow(),
            })
            inserted += 1

    print(f"\nInserted {inserted} new option(s), skipped {skipped} already-present option(s).")

    print("\nFinal counts per category:")
    for category in SEED_BY_CATEGORY:
        count = await db.equipment_options.count_documents({"category": category})
        print(f"  {category}: {count}")


if __name__ == "__main__":
    asyncio.run(main())
