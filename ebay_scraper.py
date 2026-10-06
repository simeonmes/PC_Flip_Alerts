"""
scrapers/ebay_scraper.py
=========================
Fetches eBay listings via eBay's OFFICIAL Browse API.

Why the official API instead of requests + BeautifulSoup?
You gave both options in the spec, and it's worth explaining the trade-off:
eBay's search-results HTML changes its CSS classes/structure frequently
(breaking scrapers without warning), is explicitly disallowed by eBay's
User Agreement for automated data collection, and gives you no reliable
way to filter by pickup distance. The Browse API is free for this volume
of usage, returns clean structured JSON, and has a documented filter
(pickupPostalCode + pickupRadius) that does exactly the radius search you
asked for. For those reasons this module uses the official API as the
primary path. A minimal BeautifulSoup fallback stub is included at the
bottom of this file, clearly marked, in case you want it for comparison —
but expect it to need maintenance over time.

Setup required: register a free eBay Developer account at
https://developer.ebay.com, create an application to get a Client ID and
Client Secret, and put them in your .env file as EBAY_CLIENT_ID /
EBAY_CLIENT_SECRET.
"""

import base64
import time

import requests

from config import (
    EBAY_CLIENT_ID,
    EBAY_CLIENT_SECRET,
    EBAY_OAUTH_URL,
    EBAY_BROWSE_SEARCH_URL,
    ZIP_CODE,
    SEARCH_RADIUS_MILES,
    EBAY_PICKUP_RADIUS_UNIT,
    MAX_PRICE_USD,
)

# Module-level cache so we don't mint a brand new OAuth token on every
# single search query — tokens are valid for ~2 hours, so we reuse one
# across the whole scan run and only refresh it once it's close to expiry.
_token_cache = {"access_token": None, "expires_at": 0}


def _get_oauth_token() -> str:
    """
    Implements eBay's "client credentials grant" flow: exchange your
    Client ID + Secret for a short-lived Application access token, which
    is what authorizes Browse API search requests.

    This is "application-level" auth (no eBay user login involved) — it's
    the right flow for read-only public search, which is all this scanner
    needs.
    """
    now = time.time()
    if _token_cache["access_token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["access_token"]  # cached token still valid

    if not EBAY_CLIENT_ID or not EBAY_CLIENT_SECRET:
        raise RuntimeError("EBAY_CLIENT_ID / EBAY_CLIENT_SECRET not configured")

    # eBay requires the Client ID and Secret to be Base64-encoded together
    # as "id:secret" and sent in a standard HTTP Basic Authorization header.
    credentials = f"{EBAY_CLIENT_ID}:{EBAY_CLIENT_SECRET}"
    encoded_credentials = base64.b64encode(credentials.encode()).decode()

    response = requests.post(
        EBAY_OAUTH_URL,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {encoded_credentials}",
        },
        data={
            "grant_type": "client_credentials",
            # This scope is sufficient for public, read-only Browse API search.
            "scope": "https://api.ebay.com/oauth/api_scope",
        },
        timeout=10,
    )
    response.raise_for_status()
    token_data = response.json()

    _token_cache["access_token"] = token_data["access_token"]
    _token_cache["expires_at"] = now + token_data.get("expires_in", 7200)
    return _token_cache["access_token"]


def get_oauth_token() -> str:
    """Public wrapper — shared with ebay_sold_comps.py so both modules reuse
    the same OAuth token cache instead of minting independent tokens."""
    return _get_oauth_token()


def _normalize_listing(raw_item: dict) -> dict:
    """
    Converts one raw Browse API "itemSummary" object into the common
    listing schema. Now includes listing_id (itemId) and days_listed
    (calculated from itemCreationDate) so eBay deals have the same
    dedup key and listing-age leverage fields as FB/CL deals.
    """
    import datetime as _dt
    price_obj = raw_item.get("price", {})
    images = raw_item.get("additionalImages", [])
    # additionalImages doesn't include the primary thumbnail, so +1 if present.
    image_count = len(images) + (1 if raw_item.get("image") else 0)
    # Primary thumbnail the Browse API returns inline (no extra call needed).
    image_url = (raw_item.get("image") or {}).get("imageUrl", "")

    # Days listed — derived from itemCreationDate (ISO 8601)
    days_listed = 0
    creation_date_str = raw_item.get("itemCreationDate", "")
    if creation_date_str:
        try:
            created = _dt.datetime.fromisoformat(creation_date_str.replace("Z", "+00:00"))
            now     = _dt.datetime.now(_dt.timezone.utc)
            days_listed = max(0, (now - created).days)
        except Exception:
            pass

    return {
        "title":                    raw_item.get("title", ""),
        "description":              raw_item.get("shortDescription", ""),
        "price":                    float(price_obj.get("value", 0) or 0),
        "image_count":              image_count,
        "image_url":                image_url,
        "platform":                 "eBay",
        "url":                      raw_item.get("itemWebUrl", ""),
        "listing_id":               raw_item.get("itemId", ""),
        "days_listed":              days_listed,
        "description_bundle_price": 0.0,   # eBay has firm prices, no placeholder trap
    }


def fetch_ebay_listings(search_query: str) -> list:
    """
    Searches eBay's Browse API for `search_query`, restricted to local
    pickup within SEARCH_RADIUS_MILES of ZIP_CODE and priced at or below
    MAX_PRICE_USD.

    Returns a list of normalized listing dicts (empty list on failure).
    """
    try:
        token = _get_oauth_token()
    except Exception as exc:
        print(f"⚠️  eBay OAuth failed: {exc}")
        return []

    # The Browse API's "filter" parameter is a single comma-separated
    # string. Local-pickup distance filtering requires all four of
    # pickupPostalCode / pickupCountry / pickupRadius / pickupRadiusUnit
    # together — eBay's API will reject the request if any one is missing.
    filter_str = (
        f"pickupPostalCode:{ZIP_CODE},"
        f"pickupCountry:US,"
        f"pickupRadius:{SEARCH_RADIUS_MILES},"
        f"pickupRadiusUnit:{EBAY_PICKUP_RADIUS_UNIT},"
        f"price:[..{MAX_PRICE_USD}],"
        f"priceCurrency:USD"
    )

    headers = {
        "Authorization": f"Bearer {token}",
        # Biases results/shipping estimates toward the buyer's real location.
        "X-EBAY-C-ENDUSERCTX": f"contextualLocation=country=US,zip={ZIP_CODE}",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
    }
    params = {
        "q": search_query,
        "filter": filter_str,
        "sort": "distance",  # closest listings first, matching our local-flip use case
        "limit": 50,
    }

    try:
        print(f"🔎 [eBay] Searching for: '{search_query}'...")
        response = requests.get(EBAY_BROWSE_SEARCH_URL, headers=headers, params=params, timeout=15)
        response.raise_for_status()
        data = response.json()

        raw_items = data.get("itemSummaries", [])
        listings = [_normalize_listing(item) for item in raw_items]
        print(f"   → {len(listings)} eBay listings retrieved.")
        return listings

    except requests.RequestException as exc:
        print(f"⚠️  eBay fetch failed for '{search_query}': {exc}")
        return []


# ---------------------------------------------------------------------------
# OPTIONAL FALLBACK — requests + BeautifulSoup HTML scraping
# ---------------------------------------------------------------------------
# Left here only for reference, per the spec's "requests/BeautifulSoup OR
# eBay Developer API" phrasing. NOT wired into main.py by default — the
# official API above is the recommended path. If you choose to use this,
# be aware:
#   1. eBay's search-results page markup changes without notice; selectors
#      below WILL eventually break and need updating.
#   2. eBay's User Agreement restricts automated data extraction from the
#      site itself — the official API path avoids that gray area entirely.
#   3. There's no clean way to filter by pickup radius from the public
#      search HTML, so true "25 miles from zip" filtering isn't available
#      this way — you'd be filtering by zip-as-search-bias only.
#
# def fetch_ebay_listings_via_scraping(search_query: str) -> list:
#     import requests
#     from bs4 import BeautifulSoup
#
#     url = "https://www.ebay.com/sch/i.html"
#     params = {"_nkw": search_query, "_udlo": 0, "_udhi": MAX_PRICE_USD}
#     resp = requests.get(url, params=params, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
#     soup = BeautifulSoup(resp.text, "html.parser")
#     # ... parse soup.select(...) for listing cards here; structure changes
#     # often enough that hardcoding selectors in this reference file would
#     # likely be stale by the time you run it.
#     return []