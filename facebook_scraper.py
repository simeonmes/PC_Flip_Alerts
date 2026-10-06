"""
facebook_scraper.py
===================
Fetches Facebook Marketplace listings using a LOCAL Playwright browser.
Replaces the previous Apify-based implementation entirely.

Why Playwright instead of requests/BeautifulSoup?
Facebook Marketplace requires an active login session and renders all its
content client-side via JavaScript — a plain HTTP request returns an empty
shell with no listing data. Playwright drives a real Chromium browser that
executes the page JS, giving us the fully-rendered DOM and, more usefully,
lets us intercept the raw GraphQL network calls Facebook makes as the page
loads (which are cleaner than parsing minified React HTML).

One-time setup:
    pip install playwright
    playwright install chromium

On FIRST RUN: a visible Chromium window will open. If you're not already
logged in to Facebook, do so manually — Playwright saves the session to
PLAYWRIGHT_PROFILE_DIR. All subsequent runs are fully automatic.

UPDATED: adds listing_id extraction (stable de-dup key for notifier.py's
seen-listings cache) and description-based bundle/combo price detection
(feeds scoring.py's placeholder-price override).
"""

import random
import re
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from config import MAX_PRICE_USD, SEARCH_RADIUS_MILES, ZIP_CODE


# ---------------------------------------------------------------------------
# Persistent browser profile directory
# ---------------------------------------------------------------------------
# Playwright stores cookies / localStorage here between runs.
# The folder is created automatically on first use. Change the path if you
# want to store it somewhere else (e.g., alongside the project folder).
PLAYWRIGHT_PROFILE_DIR = str(Path.home() / ".pc_flip_scanner" / "browser_profile")

# Fallback default if a caller doesn't pass fb_location_id explicitly.
# The real value in normal use comes from config.py's REGIONS list, which
# was recentered from Rockville's numeric ID to "dc" on 7/4 for full DMV
# coverage — this constant only matters if fetch_facebook_listings() is
# ever called directly without going through main.py's region loop.
_FB_LOCATION_ID = "dc"

# Facebook's search URL expects radius in km, not miles.
_SEARCH_RADIUS_KM = int(SEARCH_RADIUS_MILES * 1.60934)


# ---------------------------------------------------------------------------
# Shared Playwright browser context — reused across all queries in one cycle
# ---------------------------------------------------------------------------
# Opening and closing a full Chromium window for every search query is slow
# (19 queries × ~20 s = 6+ min just for browser overhead) and looks robotic
# to Facebook's bot-detection. Instead, we open ONE context at the start of
# the first FB fetch, reuse it for all subsequent queries, and then call
# close_browser_session() from main.py's finally block at the end of each
# scan cycle.

_playwright_instance = None   # sync_playwright() handle
_shared_context      = None   # the persistent browser context


def _get_or_create_context():
    """Returns the shared browser context, launching Playwright if needed."""
    global _playwright_instance, _shared_context
    if _shared_context is not None:
        return _shared_context

    from playwright.sync_api import sync_playwright as _sync_pw
    _playwright_instance = _sync_pw().start()
    _shared_context = _playwright_instance.chromium.launch_persistent_context(
        user_data_dir=PLAYWRIGHT_PROFILE_DIR,
        headless=False,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ],
        viewport={"width": 1366, "height": 768},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        locale="en-US",
        timezone_id="America/New_York",
    )
    return _shared_context


def get_shared_browser_context():
    """
    Public accessor so OTHER scraper modules (e.g. craigslist_scraper.py)
    can reuse this exact same browser instead of launching a second
    Chromium instance. Craigslist needs no login of its own, so sharing a
    context that also holds Facebook's session cookies is safe — cookies
    are scoped per-origin by the browser itself, there's no cross-site
    leakage risk. This is purely a resource/time optimization: one browser
    process open per scan cycle instead of two.
    """
    return _get_or_create_context()


def close_browser_session() -> None:
    """
    Close the shared Playwright browser at the end of a scan cycle.
    Called by main.py's collect_all_listings() finally block so the
    browser is fully torn down once per cycle, not once per query.
    """
    global _playwright_instance, _shared_context
    if _shared_context is not None:
        try:
            _shared_context.close()
        except Exception:
            pass
        _shared_context = None
    if _playwright_instance is not None:
        try:
            _playwright_instance.stop()
        except Exception:
            pass
        _playwright_instance = None


# ---------------------------------------------------------------------------
# Timing helpers — all delays are randomised to avoid mechanical patterns
# ---------------------------------------------------------------------------

def _human_delay(min_sec: float = 3.0, max_sec: float = 7.0) -> None:
    """Sleep for a random duration within [min_sec, max_sec]."""
    delay = random.uniform(min_sec, max_sec)
    print(f"   ⏳ Pausing {delay:.1f}s...")
    time.sleep(delay)


def _micro_delay() -> None:
    """Short pause between scroll increments."""
    time.sleep(random.uniform(0.8, 2.2))


# ---------------------------------------------------------------------------
# Session validation — detects an expired/invalidated Facebook login
# ---------------------------------------------------------------------------
# Anything other than a clean Marketplace URL after navigation usually means
# the saved cookies in PLAYWRIGHT_PROFILE_DIR no longer authenticate us —
# Facebook silently bounces logged-out sessions to one of these auth walls
# instead of returning an HTTP error, so we have to detect it by URL shape.
_LOGIN_WALL_MARKERS = ("login", "checkpoint", "two_step_verification", "recover")


def _looks_like_login_wall(current_url: str) -> bool:
    """True if `current_url` matches a known Facebook auth-wall pattern."""
    url_lower = current_url.lower()
    return any(marker in url_lower for marker in _LOGIN_WALL_MARKERS)


def _is_session_invalid(current_url: str) -> bool:
    """
    Stricter check used right before we trust the page enough to extract
    data from it: true if the URL is either an auth wall OR has drifted
    away from Marketplace entirely (e.g. bounced to facebook.com/home.php).
    """
    return _looks_like_login_wall(current_url) or "marketplace" not in current_url.lower()


def _warn_session_expired(current_url: str) -> None:
    """Prints an unmissable console warning that the saved login has expired."""
    print(
        "\n" + "!" * 70 +
        "\n⚠️  SESSION EXPIRED — Facebook redirected away from Marketplace.\n"
        f"    Current URL: {current_url}\n"
        f"    Your saved cookies in {PLAYWRIGHT_PROFILE_DIR} have likely\n"
        "    expired or been invalidated (e.g. password change, security\n"
        "    checkpoint, or normal session timeout).\n"
        "    Fix: run the scanner, log back in manually in the Chromium\n"
        "    window that opens, or delete the profile folder above to\n"
        "    force a completely fresh login.\n" +
        "!" * 70 + "\n"
    )


# ---------------------------------------------------------------------------
# Scrolling — triggers Facebook's lazy-loaded listing cards
# ---------------------------------------------------------------------------

def _scroll_to_load_more(page, passes: int = 4) -> None:
    """
    Scroll down in randomised increments rather than jumping straight to
    page-bottom. Smooth, variable behaviour looks more like a real user.
    Stops early if Facebook starts showing out-of-area results, which means
    all local inventory for this query has been exhausted.
    """
    for _ in range(passes):
        # Check for Facebook's "Results from outside your search area" header
        # before each scroll pass — if present, local results are exhausted
        # and continuing would collect listings from outside the DMV.
        try:
            page_text = page.inner_text("body", timeout=2000)
            if "results from outside" in page_text.lower():
                print("   ⚠️  'Results from outside your search area' detected — stopping scroll.")
                break
        except Exception:
            pass  # inner_text timeout is fine, just skip the check

        scroll_px = random.randint(550, 950)
        page.evaluate(f"""
            window.scrollBy({{
                top: {scroll_px},
                behavior: 'smooth'
            }});
        """)
        _micro_delay()

    # Longer pause after scrolling to let lazy-loaded images/cards settle.
    _human_delay(2.5, 5.0)


# ---------------------------------------------------------------------------
# Strategy 1 — intercept GraphQL network responses (primary, most reliable)
# ---------------------------------------------------------------------------

def _extract_from_network_responses(payloads: list) -> list:
    """
    Walk every intercepted JSON payload looking for nodes that match the
    shape of a Facebook Marketplace listing object (has 'name' +
    'listing_price' + 'id' siblings). Returns a list of raw dicts.

    This approach is more stable than HTML parsing because the JSON schema
    drifts less often than React's hashed class-names do.
    """
    raw_items: list[dict] = []
    for payload in payloads:
        _walk_json(payload, raw_items, depth=0)
    return raw_items


def _walk_json(node, results: list, depth: int) -> None:
    """
    Recursive DFS over arbitrary JSON. Captures nodes whose structure
    matches a marketplace_listing GraphQL object.
    """
    if depth > 25:          # guard against pathologically deep nesting
        return

    if isinstance(node, dict):
        # A listing node has at minimum a name (title) and a price block.
        if "listing_price" in node and "name" in node and node.get("name"):
            price_obj  = node.get("listing_price", {})
            amount_raw = (
                price_obj.get("amount")
                or price_obj.get("formatted_amount", "0")
            )
            try:
                price = float(re.sub(r"[^\d.]", "", str(amount_raw)))
            except ValueError:
                price = 0.0

            listing_id = node.get("id", "")
            url = (
                f"https://www.facebook.com/marketplace/item/{listing_id}/"
                if listing_id else ""
            )

            # Description lives under a sub-object in various shapes
            desc_node = (
                    node.get("redacted_description")
                    or node.get("description")
                    or node.get("marketplace_listing_description")
                    or node.get("listing_description")
                    or {}
            )
            description = ""
            if isinstance(desc_node, dict):
                description = (
                        desc_node.get("text", "")
                        or desc_node.get("formatted_text", {}).get("text", "")
                        or ""
                )
            elif isinstance(desc_node, str):
                description = desc_node

            # Structured shipping/delivery signal, when FB's payload exposes
            # one. Best-effort: Marketplace's GraphQL schema for this isn't
            # documented and the exact field/shape drifts like everything
            # else here, so this tries a few plausible shapes and falls back
            # to None ("unknown") rather than guessing wrong — None means
            # "fall through to the text-based check," not "not shipping."
            # This exists because sellers who set shipping via Marketplace's
            # UI toggle (instead of typing "ships only" in the description)
            # produce listings with zero text signal for the keyword-based
            # guard in scoring.py to catch.
            delivery_types = (
                node.get("delivery_types")
                or node.get("shipping_delivery_types")
                or (node.get("marketplace_listing_delivery_metadata") or {}).get("delivery_types")
            )
            shipping_only_structured = None
            if isinstance(delivery_types, list) and delivery_types:
                types_upper = [str(t).upper() for t in delivery_types]
                has_shipping = any("SHIP" in t for t in types_upper)
                has_pickup   = any("PICKUP" in t or "LOCAL" in t for t in types_upper)
                if has_shipping and not has_pickup:
                    shipping_only_structured = True
                elif has_pickup:
                    shipping_only_structured = False

            photos = node.get("listing_photos", {}).get("edges", []) or []

            # First photo's URL, if present, becomes the Discord embed
            # thumbnail. Schema is photos[i]['node']['image']['uri'] in the
            # shapes Facebook's GraphQL has used historically; guarded with
            # .get() chains since FB's payload shape drifts over time and
            # this is a "nice to have", not something worth crashing on.
            image_url = ""
            if photos:
                first_node = photos[0].get("node", {}) if isinstance(photos[0], dict) else {}
                image_url = (first_node.get("image") or {}).get("uri", "")

            results.append({
                "title":                    node["name"],
                "description":              description,
                "price":                    price,
                "image_count":              len(photos),
                "image_url":                image_url,
                "url":                      url,
                "shipping_only_structured": shipping_only_structured,
            })
            # Don't recurse further into this node — we already captured it.
            return

        for value in node.values():
            _walk_json(value, results, depth + 1)

    elif isinstance(node, list):
        for item in node:
            _walk_json(item, results, depth + 1)


# ---------------------------------------------------------------------------
# Strategy 2 — DOM traversal fallback
# ---------------------------------------------------------------------------

def _extract_from_dom(page) -> list:
    """
    Walk the live DOM looking for marketplace item anchors and the card
    containers around them. Uses structural / semantic selectors (href
    patterns, price-text regexes) that survive Facebook's class-name churn.
    Returns a list of raw dicts.
    """
    raw_items = page.evaluate("""
        () => {
            const results = [];
            const seen    = new Set();

            // Every marketplace card's primary anchor points to /marketplace/item/
            const links = document.querySelectorAll('a[href*="/marketplace/item/"]');

            for (const link of links) {
                // Strip tracking query-params so we dedup correctly
                const href = link.href.split('?')[0];
                if (seen.has(href)) continue;
                seen.add(href);

                // Walk up to find the nearest meaningful card container
                const card    = link.closest('div[class]') || link.parentElement;
                const rawText = card ? (card.innerText || '') : (link.innerText || '');
                const rawLines = rawText.split('\\n').map(l => l.trim()).filter(l => l.length > 0);

                // Prefer a line that IS a price and nothing else (e.g. "$450")
                // over the first "$" found anywhere in the card's text. A bare
                // substring match also catches financing teasers Facebook
                // renders inline ("$1/mo with Affirm", "as low as $1/wk") —
                // that's the source of listings showing a parsed price of $1
                // that has nothing to do with the actual asking price.
                let price = 0;
                for (const l of rawLines) {
                    const standalone = l.match(/^\\$([\\d,]+)$/);
                    if (standalone) {
                        price = parseFloat(standalone[1].replace(/,/g, ''));
                        break;
                    }
                }
                if (price === 0) {
                    // No clean standalone price line — fall back to the old,
                    // lower-confidence substring match. main.py's price-floor
                    // check catches it downstream if this still grabs a
                    // financing teaser instead of the real price.
                    const priceMatch = rawText.match(/\\$([\\d,]+)/);
                    price = priceMatch ? parseFloat(priceMatch[1].replace(/,/g, '')) : 0;
                }

                // Non-price, non-trivial text lines
                const lines = rawLines
                    .filter(l => l.length > 3
                        && !l.startsWith('$')
                        && l !== 'Just listed'
                        && l !== 'Listed today'
                        && l !== 'New listing');

                const title       = lines[0] || '';
                const description = lines.slice(1).join(' ');
                const imgs        = card ? card.querySelectorAll('img') : [];
                const imageUrl    = imgs.length > 0 ? imgs[0].src : '';

                results.push({
                    title,
                    description,
                    price,
                    image_count: imgs.length,
                    image_url: imageUrl,
                    url: href,
                });
            }

            return results;
        }
    """)

    return raw_items or []


# ---------------------------------------------------------------------------
# Listing ID extraction — stable de-dup key
# ---------------------------------------------------------------------------
def extract_listing_id(url: str) -> str:
    """
    Pulls Facebook's numeric listing ID out of a marketplace item URL.
    Stays constant even if the listing's title/price/photos get edited
    between scans, unlike a title-based key. Falls back to the raw URL
    if no ID pattern is found, so notifier.py's de-dup cache always gets
    *some* usable key even on malformed/unexpected URLs.
    """
    if not url:
        return ""
    match = re.search(r"/marketplace/item/(\d+)", url)
    return match.group(1) if match else url


# ---------------------------------------------------------------------------
# Description-based bundle/combo price detection — placeholder-price guard
# ---------------------------------------------------------------------------
_BUNDLE_PRICE_PATTERN = re.compile(
    r"\b(combo|bundle|total|both|everything|package|all together)\b"
    r"[^\d$]{0,20}\$?\s?(\d{2,5}(?:,\d{3})*(?:\.\d{1,2})?)",
    re.IGNORECASE,
)


def detect_description_bundle_price(description: str) -> float:
    """
    Catches the placeholder-price trap: a seller puts a low number in
    Marketplace's actual price box but discloses a higher real total in
    the description ("CPU alone - $300 | RAM alone - $350 | Combo -
    $600"). Returns the highest dollar amount attached to a bundle-style
    word (combo/total/both/etc.), or 0.0 if none is found. scoring.py
    compares this against the listing's primary price and overrides the
    acquisition cost used in profitability math when it's higher.
    """
    if not description:
        return 0.0

    bundle_prices = []
    for _, amount_str in _BUNDLE_PRICE_PATTERN.findall(description):
        try:
            bundle_prices.append(float(amount_str.replace(",", "")))
        except ValueError:
            continue

    return max(bundle_prices) if bundle_prices else 0.0


# ---------------------------------------------------------------------------
# Normalization — same output schema as the old Apify version so that
# main.py / scoring.py / notifier.py need zero changes for the core fields.
# Adds "listing_id" and "description_bundle_price" for the de-dup cache
# and split-pricing override respectively.
# ---------------------------------------------------------------------------

def _normalize_listing(raw: dict) -> dict:
    raw_price = raw.get("price", 0)
    if isinstance(raw_price, str):
        price = float("".join(ch for ch in raw_price if ch.isdigit() or ch == ".") or 0)
    else:
        price = float(raw_price or 0)

    url = raw.get("url") or ""
    description = (raw.get("description") or "").strip()

    return {
        "title":                    (raw.get("title") or "No Title").strip(),
        "description":              description,
        "price":                    price,
        "image_count":              int(raw.get("image_count", 0)),
        "image_url":                raw.get("image_url") or "",
        "platform":                 "Facebook",
        "url":                      url,
        "listing_id":               extract_listing_id(url),
        "description_bundle_price": detect_description_bundle_price(description),
        # True/False when FB's payload exposes a structured delivery-type
        # signal (GraphQL path only), None when unavailable (DOM fallback,
        # or the field wasn't present) — see _walk_json's shipping_only_structured.
        "shipping_only_structured": raw.get("shipping_only_structured"),
    }


# ---------------------------------------------------------------------------
# Public entry point — identical signature to the old Apify version
# ---------------------------------------------------------------------------

# Search cards carry only a title, price and location. The spec sheet, the
# "NO GPU" notes and the real title live on each item page, so candidates get
# opened individually. Capped per query so a scan doesn't open dozens of pages.
_MAX_DETAIL_FETCHES_PER_QUERY = 20
def _needs_detail_fetch(listing: dict) -> bool:
    """
    Every candidate gets its item page. Card text is unreliable: it can be a
    body-less title, a menu label, or page chrome that isn't the listing at
    all, and a non-empty card body used to skip the fetch entirely.
    """
    return True


def _fetch_listing_details(context, url: str) -> dict:
    """
    Opens one item page in its own tab and returns its real title and full
    description. Returns {} on any failure so one bad page never stops a scan.
    The "Details" block holds the seller's description; it ends at the
    "Listed in" line that follows the location map.
    """
    if not url:
        return {}
    page = context.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        if _looks_like_login_wall(page.url):
            return {}
        details: dict = {}
        h1 = page.locator("h1").first
        if h1.count():
            details["title"] = (h1.inner_text(timeout=5_000) or "").strip()
        body = page.inner_text("body", timeout=5_000)
        m = re.search(r"\nDetails\s*\n(.*?)(?:\nListed in|\nRelated searches|$)", body, re.S)
        if m:
            details["description"] = " ".join(m.group(1).split())
        return details
    except Exception as exc:
        print(f"   ⚠️  Detail fetch failed for {url}: {exc}")
        return {}
    finally:
        page.close()


def fetch_facebook_listings(search_query: str, zip_code: str = None,
                            radius_miles: int = None,
                            fb_location_id: str = None) -> list:
    """
    Navigate the shared Playwright browser to the Facebook Marketplace search
    URL for `search_query`, wait for listings to load, extract and return them.

    Uses a single persistent browser context shared across all queries in a
    scan cycle (opened on first call, closed by close_browser_session() in
    main.py's finally block) instead of launching a new window per query.
    """
    effective_location_id = fb_location_id or _FB_LOCATION_ID
    effective_radius_km   = int(
        (radius_miles if radius_miles is not None else SEARCH_RADIUS_MILES) * 1.60934
    )
    safe_query = search_query.replace(" ", "%20")
    search_url = (
        f"https://www.facebook.com/marketplace/{effective_location_id}/search"
        f"?query={safe_query}&maxPrice={int(MAX_PRICE_USD)}&radius={effective_radius_km}"
    )

    print(f"🔎 [Facebook] Navigating for: '{search_query}'...")

    intercepted_payloads: list[dict] = []

    def _on_response(response) -> None:
        try:
            if (
                    "facebook.com" in response.url.lower()
                    and response.status == 200
                    and "application/json" in response.headers.get("content-type", "")
            ):
                intercepted_payloads.append(response.json())
        except Exception:
            pass

    try:
        context = _get_or_create_context()
        page    = context.new_page()
        page.on("response", _on_response)

        try:
            page.goto(search_url, wait_until="domcontentloaded", timeout=30_000)
            _human_delay()

            if _looks_like_login_wall(page.url):
                print(
                    "\n⚠️  Facebook is showing a login screen.\n"
                    "    Please log in in the Chromium window.\n"
                    "    Waiting up to 2 minutes for you to complete login...\n"
                )
                try:
                    page.wait_for_url("**/marketplace/**", timeout=120_000)
                    _human_delay()
                except PlaywrightTimeoutError:
                    _warn_session_expired(page.url)
                    return []

            if _is_session_invalid(page.url):
                _warn_session_expired(page.url)
                return []

            _scroll_to_load_more(page, passes=4)

            if _is_session_invalid(page.url):
                _warn_session_expired(page.url)
                return []

            raw_items: list[dict] = []

            if intercepted_payloads:
                raw_items = _extract_from_network_responses(intercepted_payloads)
                if raw_items:
                    print(f"   ✅ Pulled {len(raw_items)} raw items from network interception.")

            if not raw_items:
                print("   ℹ️  No usable network payloads — falling back to DOM traversal.")
                raw_items = _extract_from_dom(page)

            listings: list[dict] = []
            seen_urls: set[str]  = set()

            for raw in raw_items:
                normed = _normalize_listing(raw)
                if not (0 < normed["price"] <= MAX_PRICE_USD):
                    continue
                key = normed["url"] or normed["title"]
                if key in seen_urls:
                    continue
                seen_urls.add(key)
                listings.append(normed)

            # Open item pages for listings whose card had no body or a generic
            # title, so scoring sees the real specs and any "NO GPU" note.
            detail_budget = _MAX_DETAIL_FETCHES_PER_QUERY
            for listing in listings:
                if detail_budget <= 0:
                    break
                if not _needs_detail_fetch(listing):
                    continue
                detail_budget -= 1
                details = _fetch_listing_details(context, listing["url"])
                # The item page replaces the card's text outright when it loads.
                if details.get("title"):
                    listing["title"] = details["title"]
                if details.get("description"):
                    listing["description"] = details["description"]
                    listing["description_bundle_price"] = detect_description_bundle_price(
                        listing["description"]
                    )
                _human_delay()
            print(f"   ↳ Opened {_MAX_DETAIL_FETCHES_PER_QUERY - detail_budget} item page(s) for details.")

            print(f"   → {len(listings)} Facebook listings within price cap for '{search_query}'.")
            return listings

        finally:
            page.close()   # close just this tab; context stays open for next query

    except Exception as exc:
        print(f"⚠️  Facebook Playwright fetch failed for '{search_query}': {exc}")
        return []