"""
macbook_finder.py
==================
Standalone, personal-use tool for finding underpriced M-series MacBooks
on Facebook Marketplace + Craigslist. Deliberately SEPARATE from main.py's
scan loop — it does not touch SEARCH_QUERIES or run on the 15-minute
schedule, so it can't slow down or interfere with the live PC-flipping bot.

Run it on demand whenever you want to check:
    python macbook_finder.py

Reuses the same scrapers and shared Playwright browser as the main bot
(facebook_scraper.py, craigslist_scraper.py) so there's no duplicate
browser-automation code, just a different query list and a MacBook-specific
valuation table (macbook_baseline_values.py) instead of the PC parts-based
scoring engine.

Simpler than the main bot on purpose: a MacBook is one fixed SKU (chip +
RAM + storage soldered in at purchase), so there's no chassis/PSU/cooler
component-summing needed the way scoring.py does for PCs.
"""

import re
import math
import sys
from pathlib import Path

# Standalone Windows Python defaults stdout/stderr to the legacy cp1252
# console codepage, which crashes on the emoji in this script's print()
# calls (see main.py, which has the same guard for the same reason).
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

from config import ZIP_CODE, SEARCH_RADIUS_MILES, CRAIGSLIST_SUBDOMAIN, DISCORD_WEBHOOKS

_APPLE_WEBHOOK = DISCORD_WEBHOOKS.get("Apple Devices", "")
import facebook_scraper
from facebook_scraper import fetch_facebook_listings, close_browser_session
from craigslist_scraper import fetch_craigslist_listings

# Use a SEPARATE persistent browser profile from the main bot's. Playwright
# refuses to open a second launch_persistent_context() against a user_data_dir
# that's already locked by another running process — and main.py is designed
# to run continuously, so its profile is normally locked whenever you'd want
# to run this on-demand tool. A dedicated profile lets both run at once,
# which is the entire reason this script exists as a standalone process
# rather than a mode flag on main.py. One-time cost: the first run against
# this new profile pops a visible Chromium window for a manual Facebook
# login, exactly like the main bot's first-ever run did.
facebook_scraper.PLAYWRIGHT_PROFILE_DIR = str(
    Path.home() / ".pc_flip_scanner" / "browser_profile_macbook"
)
from macbook_baseline_values import (
    MACBOOK_AIR_BASE_VALUES, MACBOOK_AIR_15IN_PREMIUM,
    MACBOOK_PRO_BASE_VALUES, MACBOOK_PRO_16IN_PREMIUM,
    RAM_STEP_VALUE, RAM_STEP_SIZE_GB, STORAGE_DOUBLING_VALUE,
    _parse_baseline,
)

# ---------------------------------------------------------------------------
# Search config — separate from the PC bot's SEARCH_QUERIES entirely
# ---------------------------------------------------------------------------
MACBOOK_SEARCH_QUERIES = [
    "macbook air",
    "macbook pro",
    "macbook m1",
    "macbook m2",
    "macbook m3",
    "macbook m4",
    "macbook m5",
]

MACBOOK_MAX_PRICE_USD = 2800   # separate, higher cap than the PC bot — MacBooks cost more
MIN_MARGIN_RATIO       = 0.25  # flag anything with 25%+ estimated margin
MIN_NET_PROFIT_USD     = 150   # ...and at least $150 in real dollar terms

# ---------------------------------------------------------------------------
# Spec detection
# ---------------------------------------------------------------------------

# Normalizes "m3pro"/"m3-pro" -> "m3 pro" the same way the PC scanner learned
# to do for "4070ti" -> "4070 ti" — sellers write chip names inconsistently.
_CHIP_SUFFIX_FIX_RE = re.compile(r'\bm([1-5])[\s\-]?(pro|max)\b')

# Longest-match-first so "m3 max" is checked before bare "m3"
_CHIP_ORDER = [
    "m5 max", "m5 pro", "m5",
    "m4 max", "m4 pro", "m4",
    "m3 max", "m3 pro", "m3",
    "m2 max", "m2 pro", "m2",
    "m1 max", "m1 pro", "m1",
]

# RAM: only matches values that ONLY ever mean RAM on a MacBook (no overlap
# with storage tiers), unless explicit "ram"/"memory" context is nearby —
# avoids the same kind of silent misread that bit the PC scanner's GPU
# detection (see scoring.py's RAM-vs-GPU-model-number collision guard).
_RAM_UNAMBIGUOUS_RE = re.compile(r'\b(96|64|48|36|24|18|16|8)\s*gb\b')
_RAM_CONTEXT_RE     = re.compile(r'(\d+)\s*gb\s*(?:ram|memory|unified memory)|(?:ram|memory)\s*:?\s*(\d+)\s*gb')

# Storage: only matches values that are almost always storage (512GB+),
# or explicit "ssd"/"storage" context for the ambiguous 128/256 tier.
_STORAGE_UNAMBIGUOUS_RE = re.compile(r'\b(256|512)\s*gb\b|\b([1248])\s*tb\b')
_STORAGE_CONTEXT_RE     = re.compile(r'(\d+)\s*gb\s*(?:ssd|storage)|(?:ssd|storage)\s*:?\s*(\d+)\s*gb')

_SCREEN_15_RE = re.compile(r'\b15[\.\-]?(?:inch|in|")?\b')
_SCREEN_16_RE = re.compile(r'\b16[\.\-]?(?:inch|in|")?\b')

# ---------------------------------------------------------------------------
# Risk detection — hard disqualifiers, checked before valuation. Mirrors the
# hard-disqualify pattern scoring.py uses for damaged/parts-only PC hardware
# (see _is_damaged_item there), plus one risk that's unique to MacBooks and
# has no PC equivalent: iCloud Activation Lock. A locked Mac can't be wiped
# or reactivated without the original owner's Apple ID — it's not "a good
# deal at $400", it's a $400 paperweight. That risk alone makes this guard
# worth having even though the rest of this tool stays deliberately simple.
#
# "as is" / "untested" / "sold as is" deliberately excluded — scoring.py's
# RED_FLAG_PHRASES comment notes these are common private-seller boilerplate,
# not real risk signals, and hard-disqualifying on them would kill too many
# legitimate listings.
# ---------------------------------------------------------------------------
_ICLOUD_LOCK_SIGNALS = [
    "icloud locked", "icloud lock", "activation locked", "activation lock",
    "find my mac", "still logged in", "forgot apple id", "forgot the apple id",
    "don't know apple id", "don't have the apple id", "unknown apple id",
    "locked to owner", "locked to previous owner", "mdm locked", "mdm lock",
    "management lock", "remote management lock", "supervised device",
]

_DAMAGED_SIGNALS = [
    "cracked screen", "screen cracked", "screen is cracked",
    "cracked display", "display cracked", "display is cracked",
    "shattered screen", "screen shattered", "screen damage",
    "cracked lcd", "lcd cracked", "crack in the screen", "crack in screen",
    "liquid damage", "water damage", "spill damage", "corrosion",
    "no display", "no power", "won't turn on", "will not turn on",
    "doesn't turn on", "does not turn on", "won't boot", "will not boot",
    "not working", "no longer working", "stopped working",
    "swollen battery", "battery swollen", "for parts", "parts only",
    "not functional", "needs repair", "repair needed", "keyboard not working",
]

_SCAM_SIGNALS = [
    "box only", "box-only", "empty box", "accessories only", "charger only",
    "case only",
]


def _detect_risk(text_lower: str) -> str:
    """Returns a short disqualifying reason, or "" if the listing looks clean."""
    if any(sig in text_lower for sig in _ICLOUD_LOCK_SIGNALS):
        return "iCloud/Activation Locked"
    if any(sig in text_lower for sig in _DAMAGED_SIGNALS):
        return "Damaged / Not Working"
    if any(sig in text_lower for sig in _SCAM_SIGNALS):
        return "Empty Box / Accessories Only"
    return ""


def _detect_chip(text_lower: str) -> str:
    normalized = _CHIP_SUFFIX_FIX_RE.sub(r'm\1 \2', text_lower)
    for chip in _CHIP_ORDER:
        if chip in normalized:
            return chip
    return ""


def _detect_ram_gb(text_lower: str) -> int:
    m = _RAM_CONTEXT_RE.search(text_lower)
    if m:
        return int(m.group(1) or m.group(2))
    m = _RAM_UNAMBIGUOUS_RE.search(text_lower)
    if m:
        return int(m.group(1))
    return 0  # unknown — falls back to baseline RAM, no adjustment applied


def _detect_storage_gb(text_lower: str) -> int:
    m = _STORAGE_CONTEXT_RE.search(text_lower)
    if m:
        return int(m.group(1) or m.group(2))
    m = _STORAGE_UNAMBIGUOUS_RE.search(text_lower)
    if m:
        if m.group(1):
            return int(m.group(1))       # 512gb
        return int(m.group(2)) * 1024    # 1/2/4/8 tb -> GB
    return 0  # unknown — falls back to baseline storage, no adjustment applied


def estimate_macbook_value(title: str, description: str) -> dict:
    """
    Returns a dict with detected spec + estimated used-market value, or
    {"detected": False} if this doesn't look like a MacBook listing at all.
    """
    text = f"{title} {description}"
    text_lower = text.lower()

    if "macbook" not in text_lower:
        return {"detected": False}

    risk = _detect_risk(text_lower)
    if risk:
        # Still "detected" (it IS a MacBook listing) but disqualified from
        # valuation/alerting — the caller decides how to surface this.
        return {"detected": True, "disqualified": risk}

    chip = _detect_chip(text_lower)
    if not chip:
        return {"detected": False}  # "macbook" mentioned but no chip generation found — too vague to price

    chip_parts = chip.split()
    is_pro_chip = len(chip_parts) > 1 and chip_parts[1] in ("pro", "max")
    # Pro/Max chips ONLY exist in the MacBook Pro line — that alone tells us
    # the model even if the seller never wrote "Pro" in the model name.
    if is_pro_chip or "macbook pro" in text_lower:
        model = "pro"
    elif "macbook air" in text_lower:
        model = "air"
    else:
        return {"detected": False}  # bare "M3 MacBook" with no model — too ambiguous to price safely

    ram_gb     = _detect_ram_gb(text_lower)
    storage_gb = _detect_storage_gb(text_lower)

    if model == "air":
        entry = MACBOOK_AIR_BASE_VALUES.get(chip)
        if not entry:
            return {"detected": False}  # e.g. "M2 Pro MacBook Air" doesn't exist — bad data, skip
        base_ram, base_storage = _parse_baseline(entry["baseline"])
        value = entry["value"]
        if _SCREEN_15_RE.search(text_lower):
            value += MACBOOK_AIR_15IN_PREMIUM
    else:
        entry = MACBOOK_PRO_BASE_VALUES.get(chip)
        if not entry:
            return {"detected": False}
        base_ram, base_storage = _parse_baseline(entry["baseline"])
        value = entry["value"]
        if _SCREEN_16_RE.search(text_lower):
            value += MACBOOK_PRO_16IN_PREMIUM

    if ram_gb:
        ram_steps = (ram_gb - base_ram) / RAM_STEP_SIZE_GB
        value += ram_steps * RAM_STEP_VALUE

    if storage_gb:
        # each doubling relative to baseline storage is one "step"
        if storage_gb > 0 and base_storage > 0:
            doublings = math.log2(storage_gb / base_storage)
            value += doublings * STORAGE_DOUBLING_VALUE

    return {
        "detected":    True,
        "model":       model,
        "chip":        chip,
        "ram_gb":      ram_gb or base_ram,
        "storage_gb":  storage_gb or base_storage,
        "est_value":   round(value),
    }


# ---------------------------------------------------------------------------
# Discord alert (optional — reuses the Apple webhook that's already set up)
# ---------------------------------------------------------------------------

def _send_discord_alert(listing: dict, spec: dict, profit: float) -> None:
    if not _APPLE_WEBHOOK:
        return
    import requests
    embed = {
        "title": listing.get("title", "")[:256],
        "url": listing.get("url", ""),
        "color": 0x2ECC71,
        "fields": [
            {"name": "Price", "value": f"${listing['price']:.0f}", "inline": True},
            {"name": "Est. Value", "value": f"${spec['est_value']:.0f}", "inline": True},
            {"name": "Predicted Profit", "value": f"${profit:.0f}", "inline": True},
            {"name": "Spec", "value": f"{spec['model'].title()} {spec['chip'].upper()}, {spec['ram_gb']}GB/{spec['storage_gb']}GB", "inline": False},
            {"name": "Platform", "value": listing.get("platform", ""), "inline": True},
        ],
    }
    if listing.get("image_url"):
        embed["thumbnail"] = {"url": listing["image_url"]}
    try:
        requests.post(_APPLE_WEBHOOK, json={"embeds": [embed]}, timeout=8)
    except Exception as exc:
        print(f"   ⚠️  Discord post failed: {exc}")


# ---------------------------------------------------------------------------
# Main scan
# ---------------------------------------------------------------------------

def run_macbook_scan():
    print("\n" + "=" * 60)
    print("MacBook deal scan — standalone, personal use")
    print("=" * 60 + "\n")

    all_listings = []
    try:
        for query in MACBOOK_SEARCH_QUERIES:
            print(f"🔍 [FB] '{query}'...")
            try:
                listings = fetch_facebook_listings(
                    query, zip_code=ZIP_CODE, radius_miles=SEARCH_RADIUS_MILES,
                )
                all_listings.extend(listings)
            except Exception as e:
                print(f"❌ FB error: {e}")

            print(f"🔍 [CL] '{query}'...")
            try:
                listings = fetch_craigslist_listings(query, subdomain=CRAIGSLIST_SUBDOMAIN)
                all_listings.extend(listings)
            except Exception as e:
                print(f"❌ CL error: {e}")
    finally:
        close_browser_session()

    seen_urls = set()
    unique_listings = []
    for l in all_listings:
        key = l.get("url") or l.get("title", "")
        if key not in seen_urls and 0 < l.get("price", 0) <= MACBOOK_MAX_PRICE_USD:
            seen_urls.add(key)
            unique_listings.append(l)

    print(f"\n📦 {len(unique_listings)} unique MacBook listings within price cap (${MACBOOK_MAX_PRICE_USD}).\n")

    skipped_risky = 0
    results = []
    for listing in unique_listings:
        spec = estimate_macbook_value(listing.get("title", ""), listing.get("description", ""))
        if not spec["detected"]:
            continue

        if spec.get("disqualified"):
            skipped_risky += 1
            print(
                f"  🚫 {listing.get('platform','?'):<10} | {listing.get('title','')[:45]:<45} | "
                f"SKIPPED — {spec['disqualified']}"
            )
            continue

        price  = listing["price"]
        profit = spec["est_value"] - price
        margin = profit / spec["est_value"] if spec["est_value"] else 0
        is_hit = margin >= MIN_MARGIN_RATIO and profit >= MIN_NET_PROFIT_USD
        results.append((listing, spec, price, profit, is_hit))

    # Best deals first — profit is the whole point of running this scan, so
    # don't bury it under whatever order the scrapers happened to return.
    results.sort(key=lambda r: r[3], reverse=True)

    hits = 0
    for listing, spec, price, profit, is_hit in results:
        tag = ""
        if is_hit:
            tag = "  🔥 UNDERPRICED"
            hits += 1

        print(
            f"  {listing.get('platform','?'):<10} | {listing.get('title','')[:45]:<45} | "
            f"${price:<6.0f} | est ${spec['est_value']:<6.0f} | profit ${profit:<6.0f}{tag}"
        )
        print(f"    🔗 {listing.get('url','')}")

        if is_hit:
            _send_discord_alert(listing, spec, profit)

    if skipped_risky:
        print(f"\n⚠️  {skipped_risky} listing(s) skipped — iCloud lock / damage / scam risk detected.\n")
    print(f"\n✅ Scan complete. {hits} underpriced MacBook(s) found.\n")


if __name__ == "__main__":
    run_macbook_scan()
