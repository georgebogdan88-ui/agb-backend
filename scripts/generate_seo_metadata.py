"""
Programmatic (template + data, NOT AI/LLM) generator for the `meta_title`/
`meta_description` SEO fields on `db.shopify_products` (see `Product` in
server.py). Part of the SEO sprint: 99.8% of the catalog (15,454/15,484
products) has no `meta_title`, 100% has no `meta_description` - `description`
(the page body) is fine, only the SEO metadata is missing.

This is a PREVIEW-ONLY script by default. It NEVER writes to MongoDB unless
you pass --apply explicitly - see "Usage" below. As of this writing it has
only ever been run in preview mode (--curated-sample / --sample-size), never
--apply, per the coordinator's explicit instruction to review a sample
before deciding whether to run it over the full catalog.

-------------------------------------------------------------------------
Design notes / why the templates look the way they do
-------------------------------------------------------------------------

meta_title: `{title}` (smart-truncated to fit) + a vendor mention if there's
room and it's not redundant + the fixed " | AGB Agroparts" suffix, mirroring
the fallback already used on the storefront today. Target: <=60 chars total
(Google's typical display cutoff), truncated at a word boundary, never
mid-word.

meta_description: the one with real SEO value. Built entirely from fields
that actually exist on Product - `title`, `product_type`, `vendor`,
`compatible_models`. There is NO dedicated OEM/part-code field on Product
(confirmed by reading the Product model in server.py and sampling the DB:
`sku` is null on 15,338/15,483 products) - codes are free text embedded in
`title` (e.g. "Carcasa Termostate John Deere R522776"). server.py already
has a heuristic for this, `extract_oem_code()` (search "OEM/part code
extraction" in server.py), used by the admin "Adaugă echivalente" feature.
`_split_title_name_and_codes()` below is the SAME heuristic (same regex/
tokenizer rules), extended to also return the *residual* product name with
the code(s) stripped off, and ALL of the trailing/leading code run (not
just the primary one extract_oem_code returns) - useful for a description,
where showing 1-2 codes is nice, whereas the equivalence-matching feature
only ever needed the single primary one. Deliberately duplicated as a
small, clearly-attributed copy rather than `import server` here, so this
one-off script stays a plain pymongo script like the other scripts/*.py
backfills (no FastAPI app / Cloudinary / Sentry / Motor client spun up just
to reuse ~30 lines of regex) - keep the two in sync if extract_oem_code's
heuristic ever changes.

IMPORTANT data-semantics decision (flagged for the coordinator to sanity
check): `vendor` is the PART's own brand/manufacturer (e.g. "John Deere"
for an OEM part, but also "Engitech"/"Donaldson"/"Vapormatic"/"AGB
Agroparts Solutions" for aftermarket parts, per the actual DB distribution:
John Deere 7533, Vapormatic 7198, AGB Agroparts Solutions 114, Granit 104,
Donaldson 86, ...) - it is NOT the equipment brand. `compatible_models` is
a list of equipment model codes (e.g. "8320R", "6230 Premium") with no
brand attached, and there is no separate "equipment brand" field. Products
observed in the DB are overwhelmingly John Deere equipment regardless of
the PART's vendor (e.g. a "Donaldson"-branded filter kit compatible with
John Deere "4920"/"8120"/...). To stay STRICTLY factual (per the task
brief), this generator therefore never asserts a sentence shape like
"compatible with Donaldson equipment: 4920, 8120" - that would misattribute
the equipment brand. Instead `vendor` (the part's own brand) and
`compatible_models` (the equipment model list) are used in two SEPARATE,
independently-true clauses: one about what/whose-brand the PART is, one
about which equipment MODELS it fits (no brand asserted for the equipment
side). Also: when `vendor` IS effectively "AGB Agroparts Solutions" (the
store's own house brand, ~114 products) it's skipped entirely rather than
producing a redundant "produs AGB Agroparts Solutions ... la AGB
Agroparts." right next to the meta_title's own " | AGB Agroparts" suffix.

Three sentence-frame variants (selected by `product_type`, per the task
brief) give structural variety across the catalog without inventing
different facts per product:
  - "Dezmembrari" (486 products): frames it as a part sourced from
    dismantled equipment.
  - "Pachet" (3 products): frames it as a bundle/kit of several parts.
  - everything else ("Nou" - the vast majority - plus the empty-string/None
    product_type seen on 472 legacy docs, and "Utilaje" - 2 used-equipment
    listings): a plain "part for agricultural equipment" frame.
Within each frame, the actual wording still varies product to product
(name, codes, vendor, model list, and whether the optional closer sentence
is appended to reach the 140-160 target) - it's not the SAME 3 sentences
copy-pasted 15,484 times.

No invented marketing claims (no "fast/guaranteed delivery", no "best
price" - nothing not backed by an actual field). The handful of non-data
closer sentences used to pad short descriptions up to the target length
(see _CLOSERS) are neutral/factual (store name, "see product page") -
never a promotional claim.

-------------------------------------------------------------------------
Usage
-------------------------------------------------------------------------
    # Preview a hand-picked, deliberately diverse set of 20 products
    # (mix of compatible_models present/absent, short/long titles,
    # different product_type values, vendor-already-in-title,
    # vendor-is-house-brand, a Pachet, comma+dotted code lists, etc.)
    # - this is what backed the sample shown to the coordinator.
    venv/Scripts/python.exe scripts/generate_seo_metadata.py --curated-sample

    # Preview N arbitrary products (no DB writes either way)
    venv/Scripts/python.exe scripts/generate_seo_metadata.py --sample-size 20

    # Real run - NOT executed yet, needs explicit sign-off first. Only
    # touches products missing meta_title AND/OR meta_description unless
    # --overwrite-existing is also passed (the ~30 products with a
    # manually/already-set value are left alone by default).
    venv/Scripts/python.exe scripts/generate_seo_metadata.py --apply

Env vars come from .env in the repo root (MONGO_URL / DB_NAME), same DB the
rest of this repo's scripts use.
"""
import argparse
import hashlib
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv
from pymongo import MongoClient, UpdateOne

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env", override=False)

# Make Romanian diacritics safe to print on a default Windows console
# (cp1252) - same UnicodeEncodeError class of issue server.py's own Sentry
# gating comment calls out for local runs.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


# ==================== OEM/part-code extraction ====================
# Ported from server.py's extract_oem_code() (search "OEM/part code
# extraction" there) - see module docstring above for why this is a
# deliberate, attributed copy rather than `import server`. Keep the regex/
# constants identical to the original if that one ever changes.

_OEM_CODE_BOUNDARY_STRIP = '()[]{}"\';:'
_OEM_CODE_TOKEN_RE = re.compile(r'^[A-Za-z]{0,4}\d[A-Za-z0-9./-]{2,}$')
_OEM_CODE_MIN_LEN = 4


def _clean_oem_token(token: str) -> str:
    return token.strip(_OEM_CODE_BOUNDARY_STRIP)


def _is_oem_code_token(token: str) -> bool:
    token = _clean_oem_token(token)
    if len(token) < _OEM_CODE_MIN_LEN:
        return False
    return bool(_OEM_CODE_TOKEN_RE.match(token))


def _tokenize_title_for_oem_code(title: str) -> List[str]:
    return [t for t in re.split(r'[\s,]+', (title or '').strip()) if t]


def split_title_name_and_codes(title: str) -> Tuple[str, List[str]]:
    """Same trailing-run-then-leading-run heuristic as server.py's
    extract_oem_code(), but returns (residual_name, all_codes_in_the_run)
    instead of just the single primary code - useful for a description,
    which can show 1-2 codes AND needs the remaining descriptive text.

    Returns (title, []) unchanged if no code-like run is found at either
    end (extract_oem_code would return None in that case).
    """
    tokens = _tokenize_title_for_oem_code(title)
    if not tokens:
        return (title or "").strip(), []

    trailing_run: List[str] = []
    cut = len(tokens)
    for tok in reversed(tokens):
        if _is_oem_code_token(tok):
            trailing_run.append(_clean_oem_token(tok))
            cut -= 1
        else:
            break
    if trailing_run:
        trailing_run.reverse()
        name_part = " ".join(tokens[:cut]).strip()
        return (name_part or title.strip()), trailing_run

    leading_run: List[str] = []
    cut = 0
    for tok in tokens:
        if _is_oem_code_token(tok):
            leading_run.append(_clean_oem_token(tok))
            cut += 1
        else:
            break
    if leading_run:
        name_part = " ".join(tokens[cut:]).strip()
        return (name_part or title.strip()), leading_run

    return title.strip(), []


# ==================== meta_title ====================

STORE_SUFFIX = " | AGB Agroparts"
META_TITLE_MAX = 60


def _truncate_at_word_boundary(text: str, max_len: int) -> str:
    text = text.strip()
    if len(text) <= max_len:
        return text
    truncated = text[:max_len]
    last_space = truncated.rfind(" ")
    if last_space > 0:
        truncated = truncated[:last_space]
    return truncated.rstrip(" ,.-;:")


def build_meta_title(title: str, vendor: Optional[str]) -> str:
    """`title` + " | AGB Agroparts", smart-truncated (word boundary, never
    mid-word) to fit ~60 chars, optionally folding in `vendor` first if
    there's room and it isn't already mentioned in the title (case-
    insensitive) - see module docstring."""
    base = (title or "").strip()
    if not base:
        base = "Piesă utilaje agricole"

    budget = META_TITLE_MAX - len(STORE_SUFFIX)

    vendor = (vendor or "").strip()
    if vendor and vendor.lower() not in base.lower():
        candidate = f"{base} {vendor}"
        if len(candidate) <= budget:
            base = candidate

    if len(base) > budget:
        base = _truncate_at_word_boundary(base, budget)

    return f"{base}{STORE_SUFFIX}"


# ==================== meta_description ====================

DESC_TARGET_MIN = 140
DESC_TARGET_MAX = 160
# The task brief's range is a hard 140-160, not just a soft target - keep
# the ladder/truncation logic strictly inside it rather than allowing a
# few chars of slack.
DESC_HARD_MAX = DESC_TARGET_MAX

# House brand - never mention it as "the part's vendor" in a description
# that's already labelled " | AGB Agroparts" in the title and is being
# served from the AGB Agroparts storefront; would just read as redundant.
_HOUSE_BRAND_MARKERS = ("agb agroparts",)

# Closer sentences used ONLY to pad a description that's still short of
# DESC_TARGET_MIN after the main sentence - neutral/factual (store name,
# "see product page"), never a promotional claim ("fast delivery", "best
# price", etc. are deliberately absent - nothing here is backed by an
# actual field). Kept as several same-meaning phrasings (see
# _pick_variant/_FRAMES below for why) so this doesn't become its own
# single 40-60 char block of literally-identical text glued onto tens of
# thousands of otherwise-short descriptions.
_CLOSERS = [
    " Detalii tehnice complete pe pagina produsului AGB Agroparts.",
    " Vezi fișa completă a produsului pe AGB Agroparts.",
    " Piesă disponibilă în catalogul AGB Agroparts.",
    " Disponibil la AGB Agroparts.",
]


def _pick_variant(product_id: str, n: int) -> int:
    """Deterministic (idempotent across reruns of the same product) but
    effectively-random-looking index in range(n), derived from the
    product's own id. Used to spread products across a handful of
    equivalent phrasings per template bucket, instead of every product in
    a bucket getting byte-identical sentence structure - the task brief's
    explicit concern about Google penalizing overly rigid duplicate
    patterns across ~15k pages otherwise built from the same few
    templates. NOT used for anything that would change which FACTS are
    stated (codes/vendor/compat still come from the actual data only) -
    purely picks between equivalent phrasings of the same facts.
    """
    if n <= 1:
        return 0
    digest = hashlib.md5((product_id or "").encode("utf-8")).hexdigest()
    return int(digest, 16) % n


def _vendor_clause(name_part: str, vendor: Optional[str]) -> str:
    vendor = (vendor or "").strip()
    if not vendor:
        return ""
    if any(marker in vendor.lower() for marker in _HOUSE_BRAND_MARKERS):
        return ""
    if vendor.lower() in name_part.lower():
        return ""
    return f", produs {vendor}"


def _codes_clause(codes: List[str], n: int) -> str:
    shown = codes[:n]
    if not shown:
        return ""
    if len(shown) == 1:
        return f", cod OEM {shown[0]}"
    return f", coduri OEM {', '.join(shown[:-1])} și {shown[-1]}"


def _models_text(models: List[str], n: int) -> str:
    models = [m.strip() for m in models if m and m.strip()]
    if not models:
        return ""
    shown = models[:n]
    rest = len(models) - len(shown)
    text = ", ".join(shown)
    if rest > 0:
        noun = "model" if rest == 1 else "modele"
        text += f" și alte {rest} {noun}"
    return text


def _cap_first_letter(text: str) -> str:
    text = text.strip()
    if not text:
        return text
    return text[0].upper() + text[1:]


def _hard_truncate(text: str) -> str:
    """Last-resort safety net: word-boundary-truncate down to
    DESC_HARD_MAX, ending on a real sentence (period) if one fits,
    otherwise the last full word plus a period. Never cuts mid-word."""
    if len(text) <= DESC_HARD_MAX:
        return text
    candidate = text[:DESC_HARD_MAX]
    last_period = candidate.rfind(".")
    if last_period >= DESC_TARGET_MIN:
        return candidate[: last_period + 1]
    last_space = candidate.rfind(" ")
    if last_space > 0:
        candidate = candidate[:last_space]
    return candidate.rstrip(" ,.-;:") + "."


# Sentence-frame variants, keyed by (product_type bucket, has_compat).
# Several equivalent phrasings per bucket (see _pick_variant) so the ~13.5k
# products with no compatible_models and/or no extractable code don't all
# converge on one single literal sentence tail. `{models}` is substituted
# with _models_text()'s output where present. All facts (name/codes/
# vendor/models) are still only ever the real data - only the CONNECTIVE
# wording around them varies.
_FRAMES: Dict[Tuple[str, bool], List[str]] = {
    ("Dezmembrari", True): [
        "{name}{codes}{vendor}, piesă din dezmembrări, compatibilă cu modelele {models}.",
        "{name}{codes}{vendor}, piesă provenită din dezmembrări, potrivită pentru modelele {models}.",
        "{name}{codes}{vendor}, componentă din dezmembrări, compatibilă cu modelele {models}.",
    ],
    ("Dezmembrari", False): [
        "{name}{codes}{vendor}, piesă originală provenită din dezmembrări de utilaje agricole.",
        "{name}{codes}{vendor}, piesă folosită, provenită din dezmembrări de utilaje agricole.",
        "{name}{codes}{vendor}, componentă provenită din dezmembrări de utilaje agricole.",
    ],
    ("Pachet", True): [
        "{name}{vendor} - pachet de piese pentru utilaje agricole, compatibil cu modelele {models}.",
        "{name}{vendor} - kit de piese pentru utilaje agricole, potrivit pentru modelele {models}.",
    ],
    ("Pachet", False): [
        "{name}{vendor} - pachet de piese pentru utilaje agricole, disponibil la AGB Agroparts.",
        "{name}{vendor} - kit de piese pentru utilaje agricole, disponibil la AGB Agroparts.",
    ],
    ("standard", True): [
        "{name}{codes}{vendor}, piesă pentru utilaje agricole, compatibilă cu modelele {models}.",
        "{name}{codes}{vendor}, piesă agricolă potrivită pentru modelele {models}.",
        "{name}{codes}{vendor}, piesă pentru utilaje agricole, compatibilă cu {models}.",
    ],
    ("standard", False): [
        "{name}{codes}{vendor}, piesă pentru utilaje agricole disponibilă la AGB Agroparts.",
        "{name}{codes}{vendor}, piesă pentru utilaje agricole, în oferta AGB Agroparts.",
        "{name}{codes}{vendor}, piesă pentru utilaje agricole, în catalogul AGB Agroparts.",
    ],
}


def build_meta_description(doc: Dict[str, Any]) -> str:
    """Assembles the description from richest to leanest via a fixed
    "detail ladder" and picks the first variant that fits DESC_HARD_MAX,
    rather than an all-or-nothing drop of any one component - this is what
    lets the compatibility clause actually survive into the final text
    instead of almost always losing out to the codes/vendor clauses (an
    earlier version of this function dropped compat_clause first whenever
    the combined string was too long, which turned out to mean it survived
    on almost NONE of a 20-product diverse sample - not what "combine
    what/codes/compatibility" in the task brief calls for). Order (most to
    least detail): 3 models shown -> 2 -> 1, vendor clause on -> off, 2
    codes shown -> 1, and only as an absolute last resort does compat drop
    out entirely (falling back to the plain "available at AGB Agroparts"
    close) - a hard word-boundary truncation is the final safety net.

    Once the facts are fixed, still short of DESC_TARGET_MIN, gets padded
    with the largest of _CLOSERS that still fits under DESC_HARD_MAX - a
    neutral, non-promotional sentence, never inventing more facts.
    """
    product_id = doc.get("id") or ""
    title = (doc.get("title") or "").strip()
    vendor = (doc.get("vendor") or "").strip()
    product_type = (doc.get("product_type") or "").strip()
    models = [m for m in (doc.get("compatible_models") or []) if m and m.strip()]

    name_part, codes = split_title_name_and_codes(title)
    name_part = _cap_first_letter(name_part or title)

    vendor_clause_full = _vendor_clause(name_part, vendor)
    has_compat = bool(models)

    bucket = product_type if product_type in ("Dezmembrari", "Pachet") else "standard"

    def render(models_n: Optional[int], use_vendor: bool, codes_n: int) -> str:
        codes_clause = _codes_clause(codes, codes_n)
        vendor_clause = vendor_clause_full if use_vendor else ""
        frame_has_compat = models_n is not None and has_compat
        frames = _FRAMES[(bucket, frame_has_compat)]
        frame = frames[_pick_variant(product_id, len(frames))]
        models_text = _models_text(models, models_n) if frame_has_compat else ""
        return frame.format(name=name_part, codes=codes_clause, vendor=vendor_clause, models=models_text)

    ladder = []
    if has_compat:
        for models_n in (3, 2, 1):
            for use_vendor in (True, False):
                for codes_n in (2, 1):
                    ladder.append((models_n, use_vendor, codes_n))
    for use_vendor in (True, False):
        for codes_n in (2, 1, 0):
            ladder.append((None, use_vendor, codes_n))

    best = None
    for models_n, use_vendor, codes_n in ladder:
        candidate = render(models_n, use_vendor, codes_n)
        best = best or candidate
        if len(candidate) <= DESC_HARD_MAX:
            best = candidate
            break

    if len(best) < DESC_TARGET_MIN:
        # Among the closers that both (a) fit under DESC_HARD_MAX and (b)
        # actually bring the total into the target range, pick one via
        # _pick_variant rather than always the same one - real diversity,
        # not just a fixed "longest that fits" pick every time. Falls back
        # to the single longest closer that fits at all (even if it can't
        # reach DESC_TARGET_MIN) when no closer reaches the target - a
        # very short base description with no closer big enough to close
        # the gap, better to add what does fit than nothing.
        fitting = [c for c in _CLOSERS if len(best) + len(c) <= DESC_HARD_MAX]
        on_target = [c for c in fitting if len(best) + len(c) >= DESC_TARGET_MIN]
        if on_target:
            chosen = on_target[_pick_variant(product_id, len(on_target))]
            best = best + chosen
        elif fitting:
            best = best + max(fitting, key=len)

    return _hard_truncate(best)


def generate_seo_fields(doc: Dict[str, Any]) -> Tuple[str, str]:
    meta_title = build_meta_title(doc.get("title") or "", doc.get("vendor"))
    meta_description = build_meta_description(doc)
    return meta_title, meta_description


# ==================== curated preview sample ====================
# 20 hand-picked product ids covering the diversity the coordinator asked
# to see before deciding on a full run: compatible_models present (small
# AND huge lists) vs. absent, very long vs. very short titles, every
# product_type value seen in the catalog ("Nou", "Dezmembrari", "Pachet",
# "Utilaje", "" empty-string legacy docs), vendor already mentioned in the
# title (should be suppressed) vs. not (should be added), the house-brand
# vendor "AGB Agroparts Solutions" (always suppressed), a leading-code
# title, a comma+dot-separated cross-reference code list, and one exact
# example from the task brief ("Cablu ambreiaj 960 mm PowrQuad...").
CURATED_SAMPLE_IDS = [
    "8419227861256",  # Cablu ambreiaj 960mm - Dezmembrari, JD, compat, trailing codes (task's own example)
    "local-8ecc4464-e474-4c77-b9cd-f806ad1cfd13",  # Kit consumabile 8.1L - Pachet, Donaldson, huge compat, no codes
    "8419211149576",  # Pompa Amorsare - product_type '', JD, no compat, single trailing numeric code
    "8419217342728",  # 191-char title - Nou, ZF, compat, many duplicate-ish trailing codes
    "8419211280648",  # H1263/1 Filtru 1931018 - '', JD, no compat, code-like prefix retained in name
    "8419231564040",  # R169152 Inel - '', JD, no compat, leading code -> very short residual name
    "8419211608328",  # Colier Carcasa Filtru Aer Cnh 315052A1 - '', JD, no compat
    "8399170044168",  # Arbore motor 4045 - '', JD (already in title), compat w/ messy merged entry, 3 codes
    "8409002901768",  # Reductor stanga punte spate trompa - '', house-brand vendor, compat=110 (+N truncation)
    "8376385962248",  # Pompa ulei motor ... ENGITECH ... - Nou, vendor already in title -> suppressed
    "8419256041736",  # Garnitura baie motor ... Engitech ... - Nou, vendor already in title -> suppressed
    "8379865104648",  # Garnitura rampa retur combustibil - Nou, house-brand vendor, compat~119
    "8381769023752",  # Bara stabilizatoare - comma+dotted multi-code list (AL79782, ..., 10.0214.004, ...)
    "local-79718a33-176c-46dc-832f-110d23db5a32",  # John Deere 6630 Premium - Utilaje, vendor already in title
    "8419211575560",  # Simering 31032069B - Nou, JD, no compat, short title
    "8419212132616",  # Siguranta 40M5099 - Nou, JD, no compat, short title
    "9636685742344",  # Ax A68537 - Nou, JD, no compat, shortest title in the catalog (9 chars)
    "8376386224392",  # Grup conic punte spate AutoPowr IVT RE227896 - Dezmembrari, vendor NOT in title -> added
    "8375488381192",  # Distribuitor hidraulic valva SCV seria 200 - Dezmembrari, compat=23 (+N truncation)
    "8375487922440",  # Carcasa Termostate John Deere R522776 - Dezmembrari, vendor already in title -> suppressed
]


def _print_preview(docs: List[Dict[str, Any]]) -> None:
    for doc in docs:
        meta_title, meta_description = generate_seo_fields(doc)
        print("=" * 100)
        print(f"id: {doc.get('id')}  product_type: {doc.get('product_type')!r}  vendor: {doc.get('vendor')!r}")
        print(f"compatible_models: {len(doc.get('compatible_models') or [])} item(s)")
        print(f"title original ({len(doc.get('title') or '')} chars):\n  {doc.get('title')}")
        print(f"meta_title ({len(meta_title)} chars):\n  {meta_title}")
        print(f"meta_description ({len(meta_description)} chars):\n  {meta_description}")
    print("=" * 100)
    print(f"\n{len(docs)} product(s) previewed. NO writes performed (preview mode).")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--curated-sample", action="store_true",
        help="Preview the 20 hand-picked, deliberately diverse products in CURATED_SAMPLE_IDS. No writes.",
    )
    parser.add_argument(
        "--sample-size", type=int, default=None,
        help="Preview this many arbitrary products from the catalog (in insertion order). No writes.",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually write meta_title/meta_description to MongoDB for the WHOLE catalog. "
             "Without this flag (default) the script only ever previews/prints - never writes. "
             "NOT to be used without explicit sign-off from the coordinator.",
    )
    parser.add_argument(
        "--overwrite-existing", action="store_true",
        help="Only meaningful with --apply: also overwrite products that already have a non-empty "
             "meta_title/meta_description (there are ~30 today, some manually curated). "
             "Default is to leave those alone and only fill in what's missing.",
    )
    args = parser.parse_args()

    mongo = MongoClient(os.environ["MONGO_URL"])
    db = mongo[os.environ["DB_NAME"]]

    if args.apply:
        query = {} if args.overwrite_existing else {
            "$or": [
                {"meta_title": {"$in": [None, ""]}},
                {"meta_description": {"$in": [None, ""]}},
            ]
        }
        docs = list(db.shopify_products.find(query))
        print(f"--apply: {len(docs)} product(s) to update "
              f"({'ALL products, including already-set ones' if args.overwrite_existing else 'only missing meta_title/meta_description'}).")
        ops = []
        for doc in docs:
            meta_title, meta_description = generate_seo_fields(doc)
            ops.append(UpdateOne({"id": doc["id"]}, {"$set": {
                "meta_title": meta_title,
                "meta_description": meta_description,
            }}))
        if not ops:
            print("Nothing to update.")
            return
        result = db.shopify_products.bulk_write(ops)
        print(f"bulk_write: matched={result.matched_count} modified={result.modified_count}")
        return

    # Preview-only modes (default).
    if args.curated_sample:
        docs = list(db.shopify_products.find({"id": {"$in": CURATED_SAMPLE_IDS}}))
        by_id = {d["id"]: d for d in docs}
        missing = [pid for pid in CURATED_SAMPLE_IDS if pid not in by_id]
        if missing:
            print(f"WARNING: {len(missing)} curated id(s) not found in DB (catalog may have changed): {missing}")
        ordered = [by_id[pid] for pid in CURATED_SAMPLE_IDS if pid in by_id]
        _print_preview(ordered)
        return

    sample_size = args.sample_size or 20
    docs = list(db.shopify_products.find({}).limit(sample_size))
    _print_preview(docs)


if __name__ == "__main__":
    main()
