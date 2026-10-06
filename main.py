"""
main.py
=======
Entry point. UPDATED: Multi-region scanning, stats tracking, heartbeat
monitoring, monthly leaderboard trigger.
"""

import gc
import random
import sys
import time

# Standalone Windows Python defaults stdout/stderr to the legacy cp1252
# console codepage, which crashes on the emoji in these scripts' print()
# calls. Force UTF-8 so output survives regardless of how this is launched.
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import stats as bot_stats
import heartbeat
import scan_state

from config import (
    SEARCH_QUERIES, MAX_PRICE_USD, MIN_PRICE_USD,
    DEAL_SCORE_ALERT_THRESHOLD, FULL_SYSTEM_ALERT_THRESHOLD, MIN_NET_PROFIT_USD,
    EBAY_CLIENT_ID, CRAIGSLIST_ENABLED, OFFERUP_ENABLED,
    SCAN_INTERVAL_SECONDS, SCAN_INTERVAL_JITTER_SECONDS, MIN_RETRY_SECONDS,
    DISCORD_BOT_TOKEN,
    REGIONS,
)
from facebook_scraper import fetch_facebook_listings
from ebay_scraper import fetch_ebay_listings
from craigslist_scraper import fetch_craigslist_listings
from scoring import calculate_deal_score, is_junk_category_listing
from notifier import (
    notify, send_daily_digest_if_due, maybe_rescue_vague_listing,
    reset_vague_triage_budget, notify_vague_lead, log_price_parse_failure,
    log_vague_triage_cycle_summary,
)


def collect_all_listings() -> list:
    all_listings = []
    # Tracks every listing key (url or title) seen so far THIS CYCLE, across
    # all queries — lets us measure how many of each query's results are
    # genuinely new vs. already turned up by an earlier query this cycle.
    # This is the data needed to know which queries are actually redundant,
    # instead of guessing from query wording alone.
    seen_keys_this_cycle: set = set()

    try:
        for region in REGIONS:
            if not region.get("enabled"):
                continue

            region_name    = region["name"]
            zip_code       = region["zip_code"]
            radius_miles   = region["radius_miles"]
            fb_location_id = region["fb_location_id"]
            cl_subdomain   = region["craigslist_subdomain"]

            for query in SEARCH_QUERIES:

                # ── Facebook ──────────────────────────────────────────────────────
                print(f"🔍 [FB/{region_name}] '{query}'...")
                try:
                    _fb_start = time.time()
                    listings = fetch_facebook_listings(
                        query,
                        zip_code=zip_code,
                        radius_miles=radius_miles,
                        fb_location_id=fb_location_id,
                    )
                    _fb_elapsed = time.time() - _fb_start

                    new_count = 0
                    for l in listings:
                        l["region"] = region_name
                        key = l.get("url") or l.get("title", "")
                        if key and key not in seen_keys_this_cycle:
                            seen_keys_this_cycle.add(key)
                            new_count += 1
                    overlap_count = len(listings) - new_count

                    print(
                        f"   → {len(listings)} Facebook listings within price cap for '{query}'. "
                        f"({new_count} new, {overlap_count} overlap with earlier queries this cycle, "
                        f"{_fb_elapsed:.1f}s)"
                    )
                    all_listings.extend(listings)
                except Exception as e:
                    print(f"❌ FB [{region_name}] error: {e}")

                # ── eBay (single ZIP — extend later for per-region support) ───────
                if EBAY_CLIENT_ID and EBAY_CLIENT_ID.strip():
                    print(f"🔍 [eBay] '{query}'...")
                    try:
                        listings = fetch_ebay_listings(query)
                        for l in listings:
                            l["region"] = region_name
                        all_listings.extend(listings)
                    except Exception as e:
                        print(f"❌ eBay error: {e}")

                # ── Craigslist ────────────────────────────────────────────────────
                if CRAIGSLIST_ENABLED:
                    print(f"🔍 [CL/{region_name}] '{query}'...")
                    try:
                        _cl_start = time.time()
                        listings = fetch_craigslist_listings(query, subdomain=cl_subdomain)
                        _cl_elapsed = time.time() - _cl_start

                        cl_new_count = 0
                        for l in listings:
                            l["region"] = region_name
                            key = l.get("url") or l.get("title", "")
                            if key and key not in seen_keys_this_cycle:
                                seen_keys_this_cycle.add(key)
                                cl_new_count += 1
                        cl_overlap_count = len(listings) - cl_new_count

                        print(
                            f"   → {len(listings)} CL listings within price cap for '{query}'. "
                            f"({cl_new_count} new, {cl_overlap_count} overlap with earlier queries this cycle, "
                            f"{_cl_elapsed:.1f}s)"
                        )
                        all_listings.extend(listings)
                    except Exception as e:
                        print(f"❌ CL [{region_name}] error: {e}")

                # ── OfferUp (global search, not region-specific) ──────────────────
                if OFFERUP_ENABLED:
                    print(f"🔍 [OfferUp] '{query}'...")
                    try:
                        from offerup_scraper import fetch_offerup_listings  # lazy: only when enabled
                        listings = fetch_offerup_listings(query)
                        for l in listings:
                            l["region"] = region_name
                        all_listings.extend(listings)
                    except Exception as e:
                        print(f"❌ OfferUp error: {e}")

    finally:
        # Close the shared Playwright browser reused across all FB *and*
        # CL queries this cycle (Craigslist was moved onto this same
        # browser 7/4 — see facebook_scraper.get_shared_browser_context) —
        # one window open total instead of launching a second Chromium
        # instance just for Craigslist.
        try:
            from facebook_scraper import close_browser_session
            close_browser_session()
        except Exception as e:
            print(f"⚠️  Browser close error: {e}")

    return all_listings


def filter_by_price_cap(listings: list) -> list:
    kept = []
    for l in listings:
        price = l.get("price", 0)
        # Anything under MIN_PRICE_USD (default $20) is almost never a real
        # asking price for a GPU/PC — it's the scraper's DOM-fallback regex
        # grabbing the wrong "$N" out of a card's text (e.g. a "$1/mo
        # financing" teaser) rather than the actual listed price. Scoring it
        # anyway lets a garbage price of $1 trivially pass every price-based
        # gate downstream (margin, vague-lead ceiling, etc.), so treat it as
        # a parsing failure and drop it here instead.
        if 0 < price < MIN_PRICE_USD:
            print(f"⚠️  Dropping likely price-parse failure: "
                  f"'{l.get('title','')[:40]}' parsed at ${price:.0f}")
            try:
                log_price_parse_failure(l, MIN_PRICE_USD)
            except Exception as e:
                print(f"⚠️  Price parse failure log error: {e}")
            continue
        if 0 < price <= MAX_PRICE_USD:
            kept.append(l)
    return kept


def deduplicate_listings(listings: list) -> list:
    seen, unique = set(), []
    for l in listings:
        key = l.get("url") or l.get("title", "")
        if key and key not in seen:
            seen.add(key)
            unique.append(l)
    return unique


def drop_junk_categories(listings: list) -> list:
    """
    Cheap title-only filter for obvious non-GPU/PC accessories (mics,
    cables, adapters, disc bundles, etc.) — run before the price-floor
    check so a correctly-parsed $10 USB microphone doesn't get logged as a
    price-parse failure. It was never a parsing bug; it's just not a GPU/PC
    listing, and calculate_deal_score() would disqualify it anyway later —
    this just does that earlier, cheaper, and without polluting that log.
    """
    kept = []
    dropped = 0
    for l in listings:
        if is_junk_category_listing(l.get("title", "")):
            dropped += 1
            continue
        kept.append(l)
    if dropped:
        print(f"🗑️  Filtered {dropped} junk-category listing(s) (mic/cable/adapter/etc.) before scoring.")
    return kept


def run_scan_cycle():
    print("\n" + "=" * 60)
    print("Starting scan cycle across all platforms and regions...")
    print("=" * 60 + "\n")

    # Dedupe first: the same listing routinely shows up across multiple
    # search queries within one cycle, and running price/category filters
    # before dedup meant checking (and logging) it that many times over for
    # no benefit — everything downstream only needs to see it once.
    raw      = collect_all_listings()
    raw      = deduplicate_listings(raw)
    raw      = drop_junk_categories(raw)
    listings = filter_by_price_cap(raw)

    print(f"\n📦 {len(listings)} unique listings within price cap (${MAX_PRICE_USD:.0f}).\n")

    reset_vague_triage_budget()
    alert_count = 0
    gpu_count   = 0

    for listing in listings:
        score_data = calculate_deal_score(listing)
        if not score_data:
            continue

        total_score      = score_data["total_score"]
        predicted_profit = score_data.get("predicted_profit", 0)
        title            = listing.get("title", "Unknown")
        price            = listing.get("price", 0.0)
        url              = listing.get("url", "")
        platform         = listing.get("platform", "?")
        region           = listing.get("region", "")
        platform_label   = f"{platform}/{region}" if region else platform

        # Tag listing with detected GPU so notifier.py can use it for
        # watchlist @mentions, cooldown tracking, and market data lookups
        if score_data.get("detected_gpu_model"):
            listing["_detected_gpu_model"] = score_data["detected_gpu_model"]

        if any("GPU:" in p for p in score_data.get("detected_parts", [])):
            gpu_count += 1

        print(f"  [{platform_label:<16}] {title[:40].ljust(40)} | ${price:<6.0f} | {total_score:>5.1f}/100")
        print(f"  {'':18}🔗 {url}")
        print("-" * 90)

        # ── Vague lead capture ────────────────────────────────────────────────
        # A high-value-tier GPU named in a system listing with no CPU/RAM/
        # storage specs scores too low on text alone to ever clear the normal
        # threshold below — route it to its own channel instead of letting it
        # silently disappear. Independent of the normal alert path, so it
        # doesn't block (or get blocked by) the checks that follow.
        if score_data.get("is_vague_lead"):
            try:
                was_sent = notify_vague_lead(listing, score_data)
                if was_sent:
                    alert_count += 1
            except Exception as e:
                print(f"❌ Vague lead dispatch error: {e}")

        is_full_system = listing.get("category_override") == "Full Systems"
        threshold      = FULL_SYSTEM_ALERT_THRESHOLD if is_full_system else DEAL_SCORE_ALERT_THRESHOLD

        if total_score >= threshold and predicted_profit >= MIN_NET_PROFIT_USD:
            try:
                was_sent = notify(listing, score_data)
                if was_sent:
                    alert_count += 1
            except Exception as e:
                print(f"❌ Alert dispatch error: {e}")
        else:
            # Didn't clear the normal bar on text alone — for a bounded,
            # cheap pool of plausible-but-vague candidates, give Haiku a
            # quick look at the photo before giving up on this listing.
            try:
                rescued_score_data = maybe_rescue_vague_listing(listing, score_data)
                if rescued_score_data:
                    was_sent = notify(listing, rescued_score_data)
                    if was_sent:
                        alert_count += 1
            except Exception as e:
                print(f"❌ Vague-listing triage error: {e}")

    # ── Post-cycle tasks ──────────────────────────────────────────────────────

    try:
        send_daily_digest_if_due()
    except Exception as e:
        print(f"⚠️  Digest error: {e}")

    try:
        from discord_bot import post_monthly_leaderboard_if_due
        post_monthly_leaderboard_if_due()
    except Exception as e:
        print(f"⚠️  Leaderboard error: {e}")

    bot_stats.increment(
        listings_scanned=len(listings),
        alerts_sent=alert_count,
        gpus_found=gpu_count,
        scan_count=1,
    )

    # Records completion of this cycle for watchdog.py — a separate process —
    # to check. This, not the hourly Discord ping below, is the real signal
    # of scanner health: it updates every ~15 min instead of every 60.
    scan_state.record_scan_complete()

    heartbeat.send_if_due()

    log_vague_triage_cycle_summary()

    print(f"\n✅ Scan complete. {alert_count} new alert(s) sent.\n")


if __name__ == "__main__":
    if DISCORD_BOT_TOKEN:
        try:
            from discord_bot import start_bot_thread
            start_bot_thread()
        except Exception as e:
            print(f"⚠️  Could not start Discord bot: {e}")

    MAX_RETRY = SCAN_INTERVAL_SECONDS
    failures  = 0

    active_regions = [r["name"] for r in REGIONS if r.get("enabled")]
    platforms      = ["Facebook", "Craigslist"]
    if OFFERUP_ENABLED: platforms.append("OfferUp")
    if EBAY_CLIENT_ID:  platforms.append("eBay")

    print(f"🚀 PC Flip Scanner — scanning every {SCAN_INTERVAL_SECONDS // 60} min")
    print(f"   Regions:   {', '.join(active_regions)}")
    print(f"   Platforms: {', '.join(platforms)}\n")

    while True:
        try:
            run_scan_cycle()
            failures = 0
            gc.collect()
            jitter        = random.randint(-SCAN_INTERVAL_JITTER_SECONDS, SCAN_INTERVAL_JITTER_SECONDS)
            sleep_duration = max(MIN_RETRY_SECONDS, SCAN_INTERVAL_SECONDS + jitter)

        except Exception as e:
            failures      += 1
            sleep_duration = min(MIN_RETRY_SECONDS * (2 ** (failures - 1)), MAX_RETRY)
            print(f"❌ Scan loop error #{failures}: {e}\n   Retrying in {sleep_duration}s.")

        m, s = sleep_duration // 60, sleep_duration % 60
        print(f"😴 Sleeping {m}m {s}s...\n")
        time.sleep(sleep_duration)