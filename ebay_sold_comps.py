"""
ebay_sold_comps.py
==================
Two eBay data lookups that run at alert time to make valuations live
instead of relying solely on the static baseline_values.py price book:

  1. get_sold_median_price() — queries eBay's Finding API for the last
     10 sold/completed listings of a specific GPU model and returns the
     median sale price. Used by notifier.py to show a "Market Sold Avg"
     field in the embed, giving members real comp data at a glance.

  2. get_cheapest_active_listing() — queries the Browse API for the
     lowest-priced currently active eBay listing of the same GPU. Shown
     as "Cheapest eBay Now" in the embed so members can see immediately
     what they'd be competing against when reselling.

Both functions cache results for 1 hour per GPU model to stay well within
eBay's rate limits during a scan cycle that evaluates many listings.

Setup: requires EBAY_CLIENT_ID in .env (already there if you set up eBay).
The Finding API uses the Client ID directly — no OAuth needed.
The Browse API uses the same OAuth token as ebay_scraper.py.
"""

import os
import time
from typing import Optional

import requests

EBAY_CLIENT_ID     = os.getenv("EBAY_CLIENT_ID", "")
EBAY_CLIENT_SECRET = os.getenv("EBAY_CLIENT_SECRET", "")
EBAY_USE_SANDBOX   = os.getenv("EBAY_USE_SANDBOX", "false").lower() == "true"

FINDING_API_URL = "https://svcs.ebay.com/services/search/FindingService/v1"
BROWSE_API_URL  = (
    "https://api.sandbox.ebay.com/buy/browse/v1/item_summary/search"
    if EBAY_USE_SANDBOX
    else "https://api.ebay.com/buy/browse/v1/item_summary/search"
)
OAUTH_URL = (
    "https://api.sandbox.ebay.com/identity/v1/oauth2/token"
    if EBAY_USE_SANDBOX
    else "https://api.ebay.com/identity/v1/oauth2/token"
)

# ── In-memory cache: {cache_key: (timestamp, value)} ────────────────────────
_cache: dict = {}
_CACHE_TTL   = 3600  # 1 hour


def _cached(key: str, fn):
    """Simple TTL cache wrapper."""
    entry = _cache.get(key)
    if entry and (time.time() - entry[0]) < _CACHE_TTL:
        return entry[1]
    result = fn()
    _cache[key] = (time.time(), result)
    return result


# ── eBay OAuth token (reused from ebay_scraper.py pattern) ──────────────────
_token_cache = {"access_token": None, "expires_at": 0}


def _get_browse_token() -> Optional[str]:
    """
    Gets a Browse API OAuth token. Delegates to ebay_scraper.get_oauth_token()
    so both modules share the same token cache — prevents two independent OAuth
    handshakes per scan cycle.
    Falls back to a local implementation if ebay_scraper is unavailable.
    """
    # Prefer the shared token from ebay_scraper (avoids duplicate handshakes)
    try:
        from ebay_scraper import get_oauth_token
        return get_oauth_token()
    except Exception:
        pass

    # Local fallback (same logic as ebay_scraper._get_oauth_token)
    if not EBAY_CLIENT_ID or not EBAY_CLIENT_SECRET:
        return None

    now = time.time()
    if _token_cache["access_token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["access_token"]

    import base64
    creds = base64.b64encode(f"{EBAY_CLIENT_ID}:{EBAY_CLIENT_SECRET}".encode()).decode()
    try:
        resp = requests.post(
            OAUTH_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "Authorization": f"Basic {creds}"},
            data={"grant_type": "client_credentials",
                  "scope": "https://api.ebay.com/oauth/api_scope"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        _token_cache["access_token"] = data["access_token"]
        _token_cache["expires_at"]   = now + data.get("expires_in", 7200)
        return _token_cache["access_token"]
    except Exception:
        return None


# ── Finding API (sold comps) ─────────────────────────────────────────────────

def get_sold_median_price(gpu_model: str) -> Optional[float]:
    """
    Returns the median price of the last 10 sold eBay listings for
    `gpu_model`, or None if the Finding API is unavailable/misconfigured.

    Uses the eBay Finding API `findCompletedItems` operation which only
    requires the App ID (Client ID) — no OAuth needed.
    """
    if not EBAY_CLIENT_ID:
        return None

    cache_key = f"sold:{gpu_model}"
    return _cached(cache_key, lambda: _fetch_sold_median(gpu_model))


def _fetch_sold_median(gpu_model: str) -> Optional[float]:
    params = {
        "OPERATION-NAME":             "findCompletedItems",
        "SERVICE-VERSION":            "1.13.0",
        "SECURITY-APPNAME":           EBAY_CLIENT_ID,
        "RESPONSE-DATA-FORMAT":       "JSON",
        "keywords":                   gpu_model,
        "itemFilter(0).name":         "SoldItemsOnly",
        "itemFilter(0).value":        "true",
        "itemFilter(1).name":         "Condition",
        "itemFilter(1).value":        "3000",  # Used
        "itemFilter(2).name":         "ListingType",
        "itemFilter(2).value":        "AuctionWithBIN,FixedPrice",
        "sortOrder":                  "EndTimeSoonest",
        "paginationInput.entriesPerPage": "10",
    }

    try:
        resp = requests.get(FINDING_API_URL, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        items = (
            data
            .get("findCompletedItemsResponse", [{}])[0]
            .get("searchResult", [{}])[0]
            .get("item", [])
        )

        prices = []
        for item in items:
            try:
                price = float(
                    item["sellingStatus"][0]["currentPrice"][0]["__value__"]
                )
                prices.append(price)
            except (KeyError, IndexError, ValueError):
                continue

        if not prices:
            return None

        prices.sort()
        return prices[len(prices) // 2]  # median

    except Exception as exc:
        print(f"⚠️  eBay sold comps failed for '{gpu_model}': {exc}")
        return None


# ── Browse API (cheapest active listing) ────────────────────────────────────

def get_cheapest_active_listing(gpu_model: str) -> Optional[float]:
    """
    Returns the lowest price of currently active (not sold) eBay listings
    for `gpu_model`, or None on failure.

    This is what the member would be competing against when reselling,
    shown as "Cheapest eBay Now" in the Discord embed.
    """
    cache_key = f"active:{gpu_model}"
    return _cached(cache_key, lambda: _fetch_cheapest_active(gpu_model))


def _fetch_cheapest_active(gpu_model: str) -> Optional[float]:
    token = _get_browse_token()
    if not token:
        return None

    try:
        resp = requests.get(
            BROWSE_API_URL,
            headers={
                "Authorization":          f"Bearer {token}",
                "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
            },
            params={
                "q":      gpu_model,
                "filter": "conditionIds:{3000}",  # Used
                "sort":   "price",
                "limit":  "5",
            },
            timeout=10,
        )
        resp.raise_for_status()
        data  = resp.json()
        items = data.get("itemSummaries", [])

        prices = []
        for item in items:
            try:
                prices.append(float(item["price"]["value"]))
            except (KeyError, ValueError):
                continue

        return min(prices) if prices else None

    except Exception as exc:
        print(f"⚠️  eBay active listing check failed for '{gpu_model}': {exc}")
        return None


# ── Convenience wrapper ───────────────────────────────────────────────────────

def get_market_data(gpu_model: str) -> dict:
    """
    Runs both lookups and returns a dict for notifier.py to embed.
    Runs silently — returns None values on any failure.
    """
    if not gpu_model or not EBAY_CLIENT_ID:
        return {"sold_median": None, "cheapest_active": None}

    return {
        "sold_median":      get_sold_median_price(gpu_model),
        "cheapest_active":  get_cheapest_active_listing(gpu_model),
    }