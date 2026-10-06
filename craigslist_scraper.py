"""
craigslist_scraper.py — UPDATED: now uses the shared Playwright browser
(reused from facebook_scraper.py) instead of plain HTTP requests.

WHY THE SWITCH: Craigslist's current search UI renders listing data
client-side after the initial page load. Diagnostic logging confirmed
`requests.get()` was pulling down substantial, real HTML (tens to
hundreds of KB) that nonetheless contained zero real listing links —
only schema.org SEO metadata. A plain HTTP request never executes the
page's JavaScript, so it always saw an empty shell regardless of which
URL or category was used. Playwright drives a real browser that runs
that JS, so the listing links actually exist in the DOM by the time we
read it.

`subdomain` is threaded through every helper so main.py's region loop
can scan any CL metro without touching this file. Age detection preserved.
"""

import random
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup

from config import (
    MAX_PRICE_USD, CRAIGSLIST_SUBDOMAIN as _DEFAULT_SUBDOMAIN,
    CRAIGSLIST_DETAIL_FETCH_LIMIT,
)

_CL_URL_RE = re.compile(r"/d/[^/]+/\d+\.html|/view/d/[^/]+/[A-Za-z0-9]+")


def _real_image_url(img_el) -> str:
    """
    Returns a real http(s) image URL from an <img> tag, or "" if none.

    Craigslist lazy-loads result thumbnails via IntersectionObserver: the
    <img src> holds a tiny inline `data:image/...;base64,...` placeholder
    until the element actually scrolls into the viewport, with no real-URL
    fallback attribute anywhere in the DOM (confirmed by inspecting a live
    page — the <img> tag and its ancestors carry no data-src/data-ids/etc.,
    just the placeholder and a `data-image-index`). fetch_craigslist_listings
    now scrolls the full page before reading the HTML so real loads have a
    chance to fire, but on a long results page some can still lag behind.
    That placeholder used to get handed to Discord as the embed thumbnail
    URL, which Discord rejects outright (data: URIs aren't a valid
    thumbnail.url), taking the whole alert down with a cryptic
    {"embeds": ["0"]} 400. Only ever return something Discord will accept:
    a real http(s) URL, or "" (no thumbnail) otherwise.
    """
    if not img_el:
        return ""
    for attr in ("src", "data-src", "data-lazy-src", "data-original"):
        val = img_el.get(attr, "")
        if val and val.startswith(("http://", "https://")):
            return val
    return ""


def _clean_title(raw: str) -> str:
    raw = re.sub(r'\$[\d,]+.*$', '', raw, flags=re.DOTALL).strip()
    raw = re.sub(r'\s{2,}.*$', '', raw).strip()
    return raw


def _parse_price(text: str) -> float:
    digits = re.sub(r"[^\d.]", "", text.replace(",", ""))
    try:
        return float(digits) if digits else 0.0
    except ValueError:
        return 0.0


def _parse_days_listed(text: str) -> int:
    text = text.lower().strip()
    if not text or "just now" in text:
        return 0
    m = re.search(r"(\d+)\s*(minute|min|hour|hr|day|week)", text)
    if not m:
        return 0
    n, unit = int(m.group(1)), m.group(2)
    if "minute" in unit or "min" in unit or "hour" in unit or "hr" in unit:
        return 0
    if "day" in unit:
        return n
    if "week" in unit:
        return n * 7
    return 0


def _extract_listing_id(url: str) -> str:
    m = re.search(r"(\d+)\.html", url)
    if m:
        return m.group(1)
    # Current CL format: https://www.craigslist.org/view/d/<slug>/<alphanumeric-id>
    m = re.search(r"/([A-Za-z0-9]+)/?(?:\?.*)?$", url)
    return m.group(1) if m else url


def _make_absolute(href: str, subdomain: str) -> str:
    if href.startswith("http"):
        return href
    return f"https://{subdomain}.craigslist.org{href}"


def _strategy_new(soup: BeautifulSoup, subdomain: str) -> list:
    items = []
    # NOTE: Craigslist's result card is a <div class="cl-search-result ...">
    # as of the current search UI, not the <li> the class name implies from
    # older markup. Matching on the class alone (not the tag) survives that
    # kind of tag-only churn going forward.
    for li in soup.select(".cl-search-result"):
        try:
            anchor = li.select_one("a.cl-app-anchor")
            if not anchor:
                continue
            href = anchor.get("href") or anchor.get("data-href", "")
            if not href or not _CL_URL_RE.search(href):
                continue

            title_el    = li.select_one("span.titlestring, span.label, .cl-search-result-title")
            title       = _clean_title(title_el.get_text(strip=True) if title_el else anchor.get_text(strip=True))
            price_el    = li.select_one("span.priceinfo, .priceinfo")
            price       = _parse_price(price_el.get_text()) if price_el else 0.0
            desc_el     = li.select_one("p.body, .body")
            desc        = desc_el.get_text(strip=True) if desc_el else ""
            img_el      = li.select_one("img.cl-image, img")
            img_url     = _real_image_url(img_el)

            time_el     = li.select_one("time, .result-date, .result-posted-date, .cl-app-anchor-meta")
            days_listed = 0
            if time_el:
                days_listed = _parse_days_listed(time_el.get("title", "") or time_el.get_text())

            items.append({
                "title": title, "description": desc, "price": price,
                "image_url": img_url, "url": _make_absolute(href, subdomain),
                "days_listed": days_listed,
            })
        except Exception:
            continue
    return items


def _strategy_legacy(soup: BeautifulSoup, subdomain: str) -> list:
    items = []
    for li in soup.select("li.result-row"):
        try:
            title_el = li.select_one("a.result-title, a.hdrlnk")
            if not title_el:
                continue
            href = title_el.get("href", "")
            if not href or not _CL_URL_RE.search(href):
                continue
            title    = _clean_title(title_el.get_text(strip=True))
            price_el = li.select_one("span.result-price, .price")
            price    = _parse_price(price_el.get_text()) if price_el else 0.0
            img_el   = li.select_one("img")
            img_url  = _real_image_url(img_el)

            time_el     = li.select_one("time.result-date")
            days_listed = 0
            if time_el:
                days_listed = _parse_days_listed(time_el.get("title", "") or time_el.get_text())

            items.append({
                "title": title, "description": "", "price": price,
                "image_url": img_url, "url": _make_absolute(href, subdomain),
                "days_listed": days_listed,
            })
        except Exception:
            continue
    return items


def _strategy_universal(soup: BeautifulSoup, subdomain: str) -> list:
    items = []
    seen: set = set()
    for anchor in soup.find_all("a", href=_CL_URL_RE):
        try:
            href = anchor.get("href", "")
            if href in seen:
                continue
            seen.add(href)
            title = _clean_title(anchor.get_text(strip=True))
            if not title or len(title) < 3:
                parent = anchor.parent
                if parent:
                    title = _clean_title(parent.get_text(strip=True)[:100])
            parent      = anchor.find_parent(["li", "div", "article"])
            price       = 0.0
            img_url     = ""
            days_listed = 0
            if parent:
                price_el = parent.find(string=re.compile(r"\$\d+"))
                if price_el:
                    price = _parse_price(str(price_el))
                img_el = parent.find("img")
                if img_el:
                    img_url = _real_image_url(img_el)
                time_el = parent.find("time")
                if time_el:
                    days_listed = _parse_days_listed(time_el.get("title", "") or time_el.get_text())
            items.append({
                "title": title, "description": "", "price": price,
                "image_url": img_url, "url": _make_absolute(href, subdomain),
                "days_listed": days_listed,
            })
        except Exception:
            continue
    return items


def _strip_nearby_area_results(soup: BeautifulSoup) -> None:
    """
    Craigslist's unified search page renders genuine local results, then
    (when local inventory is thin) a literal boundary marker —
    <div class="nearby-separator">more from nearby areas (sorted by
    distance)...</div> — followed by an expansion pulling in listings from
    completely unrelated metros (confirmed: NYC, Philadelphia, Raleigh,
    etc. bleeding into a "washingtondc" search for "alienware", where only
    6 of 54 total cards were actually local). Removes every result card at
    or after that marker, in document order, before any strategy parses
    the page. No-ops if the marker isn't present (plenty of local results).
    """
    separator = soup.find(class_="nearby-separator")
    if not separator:
        return
    for card in separator.find_all_next(class_="cl-search-result"):
        card.decompose()
    separator.decompose()


def _parse_page(soup: BeautifulSoup, subdomain: str) -> list:
    _strip_nearby_area_results(soup)
    for strategy in [_strategy_new, _strategy_legacy, _strategy_universal]:
        results = strategy(soup, subdomain)
        if results:
            return results
    return []


def _normalize(raw: dict) -> dict:
    from facebook_scraper import detect_description_bundle_price
    url  = raw.get("url", "")
    desc = (raw.get("description") or "").strip()
    return {
        "title":                    (raw.get("title") or "No Title").strip(),
        "description":              desc,
        "price":                    float(raw.get("price") or 0),
        "image_count":              1 if raw.get("image_url") else 0,
        "image_url":                raw.get("image_url") or "",
        "platform":                 "Craigslist",
        "url":                      url,
        "listing_id":               _extract_listing_id(url),
        "description_bundle_price": detect_description_bundle_price(desc),
        "days_listed":              raw.get("days_listed", 0),
    }


def _unified_search_url(subdomain: str) -> str:
    """
    Craigslist's current search UI serves from the canonical domain with
    the region as a path segment. The classic per-region subdomain URL
    still works but always redirects here — confirmed identical final_url
    and html_len across two full scan cycles — so there's no reason to
    request both; it was pure wasted time, not a real fallback.
    """
    return f"https://www.craigslist.org/search/area/{subdomain}"


_EMBEDDED_DATA_MARKERS = [
    "__NEXT_DATA__", "window.__INITIAL_STATE__", "application/ld+json",
    "postingtitletext", "__APOLLO_STATE__", "window.__PRELOADED_STATE__",
]


def _sniff_embedded_data(html_text: str) -> str:
    """
    When zero listings parse out of substantial HTML, checks for common
    signs the real data is embedded as JSON (React/Next.js hydration data,
    Apollo cache, etc.) rather than rendered as plain <a> tags. If found,
    a JSON-parsing fix may be possible instead of needing full browser
    automation. Returns a short diagnostic string.
    """
    for marker in _EMBEDDED_DATA_MARKERS:
        idx = html_text.find(marker)
        if idx != -1:
            snippet = html_text[idx:idx + 120].replace("\n", " ")
            return f"found '{marker}' at offset {idx}: {snippet}..."
    anchor_count = html_text.count("<a ")
    listing_anchor_count = len(_CL_URL_RE.findall(html_text))
    return (
        f"no known embedded-data markers found. "
        f"{anchor_count} total <a> tags, {listing_anchor_count} match the listing-URL pattern."
    )


# ---------------------------------------------------------------------------
# Per-listing detail-page enrichment
# ---------------------------------------------------------------------------
# Unlike the search-results page (client-side rendered, needs Playwright),
# an individual Craigslist posting page is classic server-rendered static
# HTML — a plain request is enough, no browser needed.
_POSTING_BODY_RE = re.compile(r'<section id="postingbody">(.*?)</section>', re.DOTALL)
_QR_CODE_PREFIX_RE = re.compile(r"^\s*QR Code Link to This Post\s*", re.IGNORECASE)
_CL_IMAGE_RE = re.compile(r"https://images\.craigslist\.org/[a-zA-Z0-9_]+\.\w+")


def _fetch_listing_detail(url: str, timeout: float = 8.0) -> dict:
    """
    Fetches a listing's own detail page for its real description and photo
    gallery. Returns {} on any failure so callers just keep what they had
    from the search-results page instead of erroring out.
    """
    try:
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
        if resp.status_code != 200:
            return {}
        html = resp.text
    except Exception:
        return {}

    description = ""
    match = _POSTING_BODY_RE.search(html)
    if match:
        text = re.sub(r"<[^>]+>", " ", match.group(1))
        text = _QR_CODE_PREFIX_RE.sub("", text)
        description = re.sub(r"\s+", " ", text).strip()

    # Full-size gallery images share the same base name as their 50x50 nav
    # thumbnails — drop the thumbnail crops, keep one entry per real photo.
    images = [u for u in dict.fromkeys(_CL_IMAGE_RE.findall(html)) if "50x50" not in u]

    return {
        "description": description,
        "image_url": images[0] if images else "",
        "image_count": len(images),
    }


def _enrich_with_detail_pages(listings: list) -> None:
    """
    Fetches real descriptions/photos for the most promising candidates in
    place. Ranks by a cheap title+price-only prelim score (no network
    calls) so the bounded fetch budget goes to listings actually worth the
    extra look, not spent in raw scrape order.
    """
    if not listings:
        return
    from scoring import calculate_deal_score

    for listing in listings:
        prelim = calculate_deal_score(listing)
        listing["_prelim_score"] = prelim.get("total_score", 0)

    candidates = sorted(
        (l for l in listings if l["_prelim_score"] > 0),
        key=lambda l: l["_prelim_score"], reverse=True,
    )[:CRAIGSLIST_DETAIL_FETCH_LIMIT]

    if candidates:
        with ThreadPoolExecutor(max_workers=min(8, len(candidates))) as pool:
            future_to_listing = {
                pool.submit(_fetch_listing_detail, c["url"]): c for c in candidates
            }
            for future in as_completed(future_to_listing):
                listing = future_to_listing[future]
                try:
                    detail = future.result()
                except Exception:
                    continue
                if detail.get("description"):
                    listing["description"] = detail["description"]
                if detail.get("image_url"):
                    listing["image_url"] = detail["image_url"]
                    listing["image_count"] = detail.get("image_count", 1)

    for listing in listings:
        listing.pop("_prelim_score", None)


def fetch_craigslist_listings(search_query: str, subdomain: str = None) -> list:
    """
    Searches Craigslist for `search_query` using the shared Playwright
    browser instead of plain HTTP requests — see the module docstring for
    why the switch was necessary.

    Reuses the exact same browser Facebook already has open this cycle
    (facebook_scraper.get_shared_browser_context) rather than launching a
    second Chromium instance, since Craigslist needs no login of its own.

    Simplified to a single "sss" (for sale - all) category: it already
    covers computers/electronics from both owners and dealers in one
    search, and testing showed the old "sys" category silently redirected
    to "sya" (owner-only) with an unwanted filter neither asked for nor
    useful.
    """
    effective_subdomain = subdomain or _DEFAULT_SUBDOMAIN
    from facebook_scraper import get_shared_browser_context

    params = {
        "query":     search_query,
        "max_price": int(MAX_PRICE_USD),
        "min_price": 10,
        "sort":      "date",
        "cat":       "sss",
    }
    url = f"{_unified_search_url(effective_subdomain)}?{urlencode(params)}"

    html_text = ""
    page = None
    try:
        context = get_shared_browser_context()
        page = context.new_page()
        page.goto(url, timeout=20000, wait_until="domcontentloaded")
        # Wait for network activity to actually settle, not just a fixed
        # timer — if results load via a background fetch/XHR after the
        # initial page, "networkidle" gives that a real chance to finish
        # instead of guessing at a fixed number of seconds.
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass  # fine if it never fully idles — proceed with what we have
        page.wait_for_timeout(random.randint(2000, 3000))

        # Result thumbnails lazy-load via IntersectionObserver: the <img
        # src> stays a tiny data: URI placeholder — with no real-URL
        # fallback attribute anywhere in the DOM — until the element
        # actually scrolls into the viewport. A single short scroll only
        # covers the first ~10-15 of what can be 100+ results, so anything
        # further down keeps the placeholder forever, which used to reach
        # Discord as an invalid thumbnail URL and 400 the whole alert.
        # Walk the full page in viewport-sized steps so every result's
        # image observer actually fires before we capture the HTML.
        try:
            page.evaluate("""
                async () => {
                    const step = window.innerHeight;
                    const maxScrolls = 60;  // generous cap, still bounded
                    let last = -1;
                    for (let i = 0; i < maxScrolls; i++) {
                        window.scrollBy(0, step);
                        await new Promise(r => setTimeout(r, 120));
                        const height = document.body.scrollHeight;
                        if (window.scrollY + window.innerHeight >= height) break;
                        if (height === last) break;
                        last = height;
                    }
                }
            """)
            # Let the last batch of images that just entered the viewport
            # actually finish loading before we read the DOM.
            page.wait_for_timeout(1500)
        except Exception:
            pass

        html_text = page.content()
    except Exception as exc:
        print(f"   ⚠️  CL [{effective_subdomain}] Playwright navigation failed: {exc}")
        return []
    finally:
        if page is not None:
            # On a zero-result page, save a screenshot + the rendered HTML
            # before closing — this is the fastest way to actually SEE what
            # Playwright loaded instead of guessing from text logs alone.
            try:
                if html_text and not _CL_URL_RE.search(html_text):
                    from pathlib import Path
                    debug_dir = Path.home() / ".pc_flip_scanner" / "cl_debug"
                    debug_dir.mkdir(parents=True, exist_ok=True)
                    safe_query = re.sub(r'[^a-z0-9]+', '_', search_query.lower())[:40]
                    page.screenshot(path=str(debug_dir / f"{safe_query}.png"), full_page=True)
                    (debug_dir / f"{safe_query}.html").write_text(html_text, encoding="utf-8")
                    print(f"   📸 Saved debug screenshot + HTML to {debug_dir} for '{search_query}'")
            except Exception:
                pass
            try:
                page.close()
            except Exception:
                pass

    soup     = BeautifulSoup(html_text, "html.parser")
    raw_list = _parse_page(soup, effective_subdomain)

    if not raw_list:
        # Diagnostic breadcrumb — if this is STILL empty after real JS
        # execution, that's a genuinely different problem than the one
        # already diagnosed (schema.org-only HTML), worth knowing about
        # immediately rather than silently returning nothing again.
        print(
            f"   ℹ️  CL [{effective_subdomain}] 0 parseable listings even after "
            f"full render — html_len={len(html_text)} | {_sniff_embedded_data(html_text)}"
        )

    listings: list = []
    seen_urls: set = set()
    for raw in raw_list:
        normed = _normalize(raw)
        if not (0 < normed["price"] <= MAX_PRICE_USD):
            continue
        key = normed["url"] or normed["title"]
        if key in seen_urls:
            continue
        seen_urls.add(key)
        listings.append(normed)

    _enrich_with_detail_pages(listings)

    print(f"   → {len(listings)} CL listings for '{search_query}' [{effective_subdomain}] ({len(raw_list)} raw).")
    return listings