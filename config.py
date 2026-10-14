"""
config.py
=========
Single source of truth for every tunable knob in the scanner.
UPDATED: Multi-region support (REGIONS), heartbeat monitoring webhook,
and all existing feature settings preserved.
"""

import os
from dotenv import load_dotenv

load_dotenv(override=True)


# ---------------------------------------------------------------------------
# 1. SEARCH FILTERS
# ---------------------------------------------------------------------------

ZIP_CODE            = os.getenv("ZIP_CODE", "20001")
SEARCH_RADIUS_MILES = int(os.getenv("SEARCH_RADIUS_MILES", "30"))
MAX_PRICE_USD       = float(os.getenv("MAX_PRICE_USD", "1400"))
# Below this, a "price" is almost certainly a scraper mis-extraction (e.g. the
# DOM fallback grabbing a stray "$1" financing teaser instead of the real
# price), not a real listing — no genuine GPU/PC listing is priced this low.
MIN_PRICE_USD       = float(os.getenv("MIN_PRICE_USD", "20"))

SEARCH_QUERIES = [
    # ── Full systems — broad terms ────────────────────────────────────────────
    "gaming pc",
    "gaming desktop",
    "gaming computer",
    "gaming tower",
    "prebuilt gaming",
    # "custom gaming pc" — cut 7/4: measured at 2 new listings for ~19s,
    # by far the worst new-per-second ratio of any query, and heavily
    # overlapping "prebuilt gaming"/"gaming desktop" wording.
    # ── Full systems — OEM brands ─────────────────────────────────────────────
    "ibuypower",
    # NOTE: "cyberpower" removed — also matches CyberPower UPS/battery backup
    # products which flood the scan with irrelevant listings
    "alienware",
    # ── Full systems — motivated/distress sellers ─────────────────────────────
    "broken pc",
    "moving out pc",
    "upgrading pc",
    # ── Standalone GPUs ───────────────────────────────────────────────────────
    "rtx",
    "graphics card",
    "rx 9070",
    "rx 7900",
    "gaming system",
    # ── Typo / shorthand variants — widens the raw ingestion funnel to
    # catch sellers who write model numbers without spaces or misspell
    # "nvidia" (both common on Facebook Marketplace titles) ──────────────────
    "3080ti",
    "rtx4070",
    "nvidea",
]


# ---------------------------------------------------------------------------
# 2. SCORING ENGINE
# ---------------------------------------------------------------------------

SCORE_WEIGHTS = {
    "part_quality_valuation":    40,
    "profitability_margin":      30,
    "picture_quality":           10,
    "seller_behavior_risk":      10,
    "title_description_quality": 10,
}

DEAL_SCORE_ALERT_THRESHOLD  = 75
FULL_SYSTEM_ALERT_THRESHOLD = 48
FULL_MARGIN_PROFIT_RATIO    = 0.30
MIN_NET_PROFIT_USD          = 100

EBAY_FEE_RATE     = 0.1325
GPU_SHIPPING_COST = 30
PC_SHIPPING_COST  = 0

GPU_ALERT_COOLDOWN_SECONDS = int(os.getenv("GPU_ALERT_COOLDOWN_SECONDS", "3600"))

# A GPU baseline value at/above this counts as "high-value tier" for vague-lead
# capture — covers RTX 30/40/50-series and RX 7000/9000-series without having
# to hardcode series names (see scoring.py's is_vague_lead detection).
VAGUE_LEAD_GPU_VALUE_THRESHOLD = float(os.getenv("VAGUE_LEAD_GPU_VALUE_THRESHOLD", "150"))

# Price ceiling for vague-lead capture, as a multiple of the detected GPU's
# own baseline resale value. A whole system (unknown CPU/RAM/storage/chassis)
# priced at or below this multiple of the bare card's used value means those
# other components are effectively being given away — worth flagging. Above
# it, the price already reflects normal full-prebuilt pricing for that GPU
# tier and there's no hidden upside to chase.
VAGUE_LEAD_PRICE_CEILING_MULTIPLIER = float(os.getenv("VAGUE_LEAD_PRICE_CEILING_MULTIPLIER", "1.5"))


# ---------------------------------------------------------------------------
# 3. SCAN TIMING
# ---------------------------------------------------------------------------

SCAN_INTERVAL_MINUTES        = int(os.getenv("SCAN_INTERVAL_MINUTES", "15"))
SCAN_INTERVAL_SECONDS        = SCAN_INTERVAL_MINUTES * 60
SCAN_INTERVAL_JITTER_SECONDS = 60
MIN_RETRY_SECONDS            = 30

DAILY_DIGEST_HOUR = int(os.getenv("DAILY_DIGEST_HOUR", "9"))


# ---------------------------------------------------------------------------
# 4. NOTIFICATION CHANNELS
# ---------------------------------------------------------------------------

NOTIFICATION_CHANNEL = os.getenv("NOTIFICATION_CHANNEL", "discord")
DISCORD_WEBHOOK_URL  = os.getenv("DISCORD_WEBHOOK_URL", "")

DISCORD_WEBHOOKS = {
    "GPU Only":      os.getenv("DISCORD_WEBHOOK_GPU_ONLY",     ""),
    "Full Systems":  os.getenv("DISCORD_WEBHOOK_FULL_SYSTEMS", ""),
    "PC Hardware":   os.getenv("DISCORD_WEBHOOK_PC_HARDWARE",  ""),
    "Apple Devices": os.getenv("DISCORD_WEBHOOK_APPLE",        ""),
    "default":       DISCORD_WEBHOOK_URL,
}

DISCORD_WEBHOOK_FLIP_TRACKER = os.getenv("DISCORD_WEBHOOK_FLIP_TRACKER", "")
DISCORD_WEBHOOK_DAILY_DIGEST = os.getenv("DISCORD_WEBHOOK_DAILY_DIGEST", DISCORD_WEBHOOK_URL)

# '#vague-leads' — system listings that name a high-value-tier GPU but give
# no CPU/RAM/storage specs at all. Too little data for the deterministic
# scorer to build a confident valuation, but too promising to just score low
# and disappear — routed here instead so a member can message the seller for
# the missing breakdown. Falls back to the default webhook if unset.
DISCORD_WEBHOOK_VAGUE_LEADS = os.getenv("DISCORD_WEBHOOK_VAGUE_LEADS", DISCORD_WEBHOOK_URL)

# Heartbeat — hourly "I'm alive" status ping to a private channel. This is
# presence-only; it cannot detect downtime by itself (see heartbeat.py).
DISCORD_WEBHOOK_HEARTBEAT = os.getenv("DISCORD_WEBHOOK_HEARTBEAT", "")

# Watchdog — a separate OS process (watchdog.py) that alerts when the scanner
# goes silent. Independent of DISCORD_WEBHOOK_HEARTBEAT above so a "down"
# alert still fires even if that webhook itself is the thing misconfigured;
# falls back to it if unset. Threshold is well above SCAN_INTERVAL_SECONDS
# (15 min normally) so ordinary scan-to-scan jitter never false-positives.
DISCORD_WEBHOOK_WATCHDOG            = os.getenv("DISCORD_WEBHOOK_WATCHDOG", "") or DISCORD_WEBHOOK_HEARTBEAT
WATCHDOG_CHECK_INTERVAL_SECONDS     = int(os.getenv("WATCHDOG_CHECK_INTERVAL_SECONDS", "120"))
WATCHDOG_DOWNTIME_THRESHOLD_SECONDS = int(os.getenv("WATCHDOG_DOWNTIME_THRESHOLD_SECONDS", "2400"))  # 40 min

DISCORD_BOT_TOKEN          = os.getenv("DISCORD_BOT_TOKEN", "")
DISCORD_COMMAND_CHANNEL_ID = os.getenv("DISCORD_COMMAND_CHANNEL_ID", "0")

TWILIO_ACCOUNT_SID  = os.getenv("TWILIO_ACCOUNT_SID",  "")
TWILIO_AUTH_TOKEN   = os.getenv("TWILIO_AUTH_TOKEN",    "")
TWILIO_FROM_NUMBER  = os.getenv("TWILIO_FROM_NUMBER",   "")
TWILIO_TO_NUMBER    = os.getenv("TWILIO_TO_NUMBER",     "")


# ---------------------------------------------------------------------------
# 5. EBAY
# ---------------------------------------------------------------------------

EBAY_CLIENT_ID     = os.getenv("EBAY_CLIENT_ID",     "")
EBAY_CLIENT_SECRET = os.getenv("EBAY_CLIENT_SECRET", "")
EBAY_USE_SANDBOX   = os.getenv("EBAY_USE_SANDBOX", "false").lower() == "true"

EBAY_OAUTH_URL = (
    "https://api.sandbox.ebay.com/identity/v1/oauth2/token"
    if EBAY_USE_SANDBOX
    else "https://api.ebay.com/identity/v1/oauth2/token"
)
EBAY_BROWSE_SEARCH_URL = (
    "https://api.sandbox.ebay.com/buy/browse/v1/item_summary/search"
    if EBAY_USE_SANDBOX
    else "https://api.ebay.com/buy/browse/v1/item_summary/search"
)
EBAY_PICKUP_RADIUS_UNIT = "mi"


# ---------------------------------------------------------------------------
# 6. CRAIGSLIST
# ---------------------------------------------------------------------------

CRAIGSLIST_SUBDOMAIN = os.getenv("CRAIGSLIST_SUBDOMAIN", "washingtondc")
CRAIGSLIST_ENABLED   = os.getenv("CRAIGSLIST_ENABLED", "true").lower() == "true"

# The Craigslist search-results page never has a real description (only the
# detail page does) and its thumbnail is frequently still a lazy-load
# placeholder. Fetching every result's own detail page would be far too
# slow — a query can return 50-100+ results — so only the top N candidates
# by a cheap title+price prelim score get the extra fetch, per query.
CRAIGSLIST_DETAIL_FETCH_LIMIT = int(os.getenv("CRAIGSLIST_DETAIL_FETCH_LIMIT", "10"))

# Facebook's equivalent: a hard ceiling on item pages opened across ALL
# queries in one scan cycle (facebook_scraper.py also keeps its own smaller
# per-query cap). Item pages are cached per listing, so in steady state only
# genuinely new listings need opening and this is never reached — it exists
# to bound the first cycle after a cold cache, which would otherwise open
# hundreds of pages back to back. Listings left over are picked up on the
# following cycles.
FB_DETAIL_FETCHES_PER_CYCLE = int(os.getenv("FB_DETAIL_FETCHES_PER_CYCLE", "60"))


# ---------------------------------------------------------------------------
# 7. OFFERUP (via Apify)
# ---------------------------------------------------------------------------

APIFY_API_TOKEN        = os.getenv("APIFY_API_TOKEN", "")
APIFY_OFFERUP_ACTOR_ID = os.getenv("APIFY_OFFERUP_ACTOR_ID", "haketa/offerup-scraper")
OFFERUP_ENABLED        = os.getenv("OFFERUP_ENABLED", "false").lower() == "true"


# ---------------------------------------------------------------------------
# 8. AI FEATURES (Claude API)
# ---------------------------------------------------------------------------

ANTHROPIC_API_KEY           = os.getenv("ANTHROPIC_API_KEY", "")
AI_PHOTO_ANALYSIS_THRESHOLD = int(os.getenv("AI_PHOTO_ANALYSIS_THRESHOLD", "80"))
AI_PHOTO_ENABLED            = os.getenv("AI_PHOTO_ENABLED",       "true").lower() == "true"
AI_NEGOTIATION_ENABLED      = os.getenv("AI_NEGOTIATION_ENABLED", "true").lower() == "true"
AI_EBAY_DRAFT_ENABLED       = os.getenv("AI_EBAY_DRAFT_ENABLED",  "true").lower() == "true"

# Vague-listing triage: a listing with a thin/generic title+description
# (e.g. "Gaming PC for sale, $200") never reaches AI_PHOTO_ANALYSIS_THRESHOLD
# because the deterministic text scorer has nothing to parse a GPU model
# from, even if the photo clearly shows something valuable. This runs a
# cheap Haiku vision glance (not Sonnet) on a bounded, loosely-filtered
# pool of otherwise-too-low-scoring listings to catch that gap without the
# cost of running full analysis on everything.
# Off by default: the Haiku photo-triage "rescue" invents a rough value from a
# guessed model, and the full analysis only verifies the model against the
# title, not the value. Turn on with AI_VAGUE_TRIAGE_ENABLED=true once values
# are verified.
AI_VAGUE_TRIAGE_ENABLED        = os.getenv("AI_VAGUE_TRIAGE_ENABLED", "false").lower() == "true"
# A genuinely vague listing ("Gaming PC for sale, works great, $150") scores
# ~10 from generic picture/seller/title-quality points alone — there's
# nothing GPU-specific for the deterministic scorer to find. A MIN_SCORE
# much above that would exclude exactly the listings this exists to catch.
# Hard-disqualified listings (parts-only, damaged, shipping-only, etc.)
# also score 0 but are excluded separately by their DISQUALIFIED tag, not
# by this floor.
AI_VAGUE_TRIAGE_MIN_SCORE      = float(os.getenv("AI_VAGUE_TRIAGE_MIN_SCORE", "5"))
AI_VAGUE_TRIAGE_MAX_SCORE      = float(os.getenv("AI_VAGUE_TRIAGE_MAX_SCORE", str(AI_PHOTO_ANALYSIS_THRESHOLD)))
AI_VAGUE_TRIAGE_PRICE_MIN      = float(os.getenv("AI_VAGUE_TRIAGE_PRICE_MIN", "30"))
AI_VAGUE_TRIAGE_PRICE_MAX      = float(os.getenv("AI_VAGUE_TRIAGE_PRICE_MAX", "1200"))
AI_VAGUE_TRIAGE_MAX_PER_CYCLE  = int(os.getenv("AI_VAGUE_TRIAGE_MAX_PER_CYCLE", "25"))


# ---------------------------------------------------------------------------
# 9. MULTI-REGION (Tier 2 infrastructure)
# ---------------------------------------------------------------------------
# Each dict in REGIONS defines one metro to scan. Only enabled=True regions
# run each cycle. Enabling a second region is what unlocks Tier 2 for members.
#
# To find a Facebook location ID for a new city:
#   Go to facebook.com/marketplace/<city-name> and copy the number from the URL.
#
# To add a region: copy the DMV block, fill in the fields, set enabled=True.

REGIONS = [
    {
        "name":                 "DMV",
        "zip_code":             ZIP_CODE,
        "radius_miles":         SEARCH_RADIUS_MILES,
        "craigslist_subdomain": CRAIGSLIST_SUBDOMAIN,
        # Was "106407649390721" (Rockville, MD) — recentered 7/4 after
        # confirming Rockville-based 25mi radius under-covered Northern
        # Virginia and southern Maryland. "dc" confirmed working directly
        # from a live facebook.com/marketplace/dc/search/ URL — Facebook
        # accepts a plain location slug here, not just numeric IDs.
        "fb_location_id":       "dc",
        "enabled":              True,
    },
    # ── Tier 2 expansion metros (set enabled=True to activate) ───────────────
    # {
    #     "name":                 "NYC",
    #     "zip_code":             "10001",
    #     "radius_miles":         20,
    #     "craigslist_subdomain": "newyork",
    #     "fb_location_id":       "108433195866648",
    #     "enabled":              False,
    # },
    # {
    #     "name":                 "LA",
    #     "zip_code":             "90012",
    #     "radius_miles":         30,
    #     "craigslist_subdomain": "losangeles",
    #     "fb_location_id":       "109391992406438",
    #     "enabled":              False,
    # },
    # {
    #     "name":                 "Chicago",
    #     "zip_code":             "60601",
    #     "radius_miles":         25,
    #     "craigslist_subdomain": "chicago",
    #     "fb_location_id":       "107854852568593",
    #     "enabled":              False,
    # },
    # {
    #     "name":                 "ATL",
    #     "zip_code":             "30301",
    #     "radius_miles":         25,
    #     "craigslist_subdomain": "atlanta",
    #     "fb_location_id":       "110220755683464",
    #     "enabled":              False,
    # },
]