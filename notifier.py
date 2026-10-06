"""
notifier.py
===========
UPDATED: Adds net profit after eBay fees/shipping, GPU alert cooldown,
AI feature integration (photo analysis, negotiation message, eBay draft),
live eBay market data fields, member watchlist @mentions, and daily
digest deal tracking. All new features degrade gracefully when their
dependencies are not configured.
"""

import datetime
import json
import os
import time
from pathlib import Path
from typing import Optional

import requests

from config import (
    NOTIFICATION_CHANNEL,
    DISCORD_WEBHOOK_URL,
    DISCORD_WEBHOOKS,
    DISCORD_WEBHOOK_VAGUE_LEADS,
    DEAL_SCORE_ALERT_THRESHOLD,
    FULL_SYSTEM_ALERT_THRESHOLD,
    SCORE_WEIGHTS,
    TWILIO_ACCOUNT_SID,
    TWILIO_AUTH_TOKEN,
    TWILIO_FROM_NUMBER,
    TWILIO_TO_NUMBER,
    EBAY_FEE_RATE,
    GPU_SHIPPING_COST,
    PC_SHIPPING_COST,
    GPU_ALERT_COOLDOWN_SECONDS,
    ANTHROPIC_API_KEY,
    AI_PHOTO_ANALYSIS_THRESHOLD,
    AI_PHOTO_ENABLED,
    AI_NEGOTIATION_ENABLED,
    AI_EBAY_DRAFT_ENABLED,
    MIN_NET_PROFIT_USD,
    AI_VAGUE_TRIAGE_ENABLED,
    AI_VAGUE_TRIAGE_MIN_SCORE,
    AI_VAGUE_TRIAGE_MAX_SCORE,
    AI_VAGUE_TRIAGE_PRICE_MIN,
    AI_VAGUE_TRIAGE_PRICE_MAX,
    AI_VAGUE_TRIAGE_MAX_PER_CYCLE,
)
from categories import detect_category


# ---------------------------------------------------------------------------
# De-duplication + price-tracking cache
# ---------------------------------------------------------------------------
SEEN_LISTINGS_PATH    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen_listings.json")
SEEN_LISTINGS_TTL_SEC = 14 * 24 * 60 * 60
PRICE_DROP_THRESHOLD  = 0.10


def _load_seen() -> dict:
    if not os.path.exists(SEEN_LISTINGS_PATH):
        return {}
    try:
        with open(SEEN_LISTINGS_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_seen(seen: dict) -> None:
    cutoff = time.time() - SEEN_LISTINGS_TTL_SEC
    pruned = {}
    for k, v in seen.items():
        ts = v.get("ts", 0) if isinstance(v, dict) else float(v)
        if ts >= cutoff:
            pruned[k] = v
    try:
        with open(SEEN_LISTINGS_PATH, "w") as f:
            json.dump(pruned, f)
    except Exception as exc:
        print(f"⚠️  Could not write seen_listings.json: {exc}")


def _cached_entry(key: str, seen: dict) -> Optional[dict]:
    entry = seen.get(key)
    if entry is None:
        return None
    if isinstance(entry, (int, float)):
        return {"ts": float(entry), "price": None}
    return entry


def get_listing_key(listing: dict) -> str:
    platform   = (listing.get("platform") or "unknown").lower()
    listing_id = listing.get("listing_id")
    if listing_id:
        return f"{platform}:{listing_id}"
    url = listing.get("url")
    if url:
        return f"{platform}:{url}"
    return f"{platform}:{listing.get('title','')[:50]}:{listing.get('price',0)}"


def check_duplicate_and_price_drop(listing: dict):
    seen  = _load_seen()
    entry = _cached_entry(get_listing_key(listing), seen)
    if entry is None:
        return False, False, None
    old_price = entry.get("price")
    cur_price = listing.get("price", 0)
    if old_price and cur_price and old_price > 0:
        if (old_price - cur_price) / old_price >= PRICE_DROP_THRESHOLD:
            return True, True, old_price
    return True, False, None


def mark_seen(listing: dict) -> None:
    seen = _load_seen()
    seen[get_listing_key(listing)] = {"ts": time.time(), "price": listing.get("price", 0)}
    _save_seen(seen)


def mark_seen_triage_only(listing: dict) -> None:
    """
    Marks a listing as seen without recording a real alert price — used when
    the vague-triage Haiku pass looked at a listing and decided it wasn't a
    deal. Without this, a listing Haiku rejects never enters seen_listings.json
    at all, so it gets re-submitted to Haiku every single cycle for as long
    as it stays listed, even with the seen-check in maybe_rescue_vague_listing.
    price=None (not 0 or negative) so check_duplicate_and_price_drop's price-
    drop math (`old_price and cur_price and old_price > 0`) never treats it as
    a real prior price. Won't downgrade an existing real-alert record — this
    can be reached again on a price-drop re-triage of an already-alerted
    listing, and that recorded price must survive for future drop comparisons.
    """
    seen = _load_seen()
    key  = get_listing_key(listing)
    existing = seen.get(key)
    if isinstance(existing, dict) and existing.get("price"):
        return
    seen[key] = {"ts": time.time(), "price": None}
    _save_seen(seen)


# ---------------------------------------------------------------------------
# GPU alert cooldown — prevents the same GPU model flooding the channel
# ---------------------------------------------------------------------------
_cooldown_cache: dict = {}   # {gpu_model_lower: last_alert_timestamp}
_COOLDOWN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cooldown_cache.json")


def _load_cooldown_cache() -> dict:
    """Load persisted cooldown cache, pruning any entries that have expired."""
    if not os.path.exists(_COOLDOWN_PATH):
        return {}
    try:
        with open(_COOLDOWN_PATH) as f:
            data = json.load(f)
        now = time.time()
        return {k: v for k, v in data.items()
                if (now - float(v)) < GPU_ALERT_COOLDOWN_SECONDS}
    except Exception:
        return {}


def _save_cooldown_cache() -> None:
    try:
        with open(_COOLDOWN_PATH, "w") as f:
            json.dump(_cooldown_cache, f)
    except Exception as exc:
        print(f"⚠️  Could not save cooldown cache: {exc}")


_cooldown_cache = _load_cooldown_cache()


def _gpu_model_from_listing(listing: dict) -> Optional[str]:
    """Extracts the GPU model string from detected_parts if present."""
    return listing.get("_detected_gpu_model")


def is_gpu_on_cooldown(gpu_model: Optional[str]) -> bool:
    if not gpu_model or not GPU_ALERT_COOLDOWN_SECONDS:
        return False
    last = _cooldown_cache.get(gpu_model.lower(), 0)
    return (time.time() - last) < GPU_ALERT_COOLDOWN_SECONDS


def mark_gpu_alerted(gpu_model: Optional[str]) -> None:
    if gpu_model:
        _cooldown_cache[gpu_model.lower()] = time.time()
        _save_cooldown_cache()


# ---------------------------------------------------------------------------
# Net profit calculator
# ---------------------------------------------------------------------------

def calculate_net_profit(estimated_value: float, purchase_price: float,
                         is_full_system: bool = False) -> float:
    """Real profit after eBay fees and estimated shipping."""
    shipping  = PC_SHIPPING_COST if is_full_system else GPU_SHIPPING_COST
    ebay_fees = estimated_value * EBAY_FEE_RATE
    return round(estimated_value - purchase_price - ebay_fees - shipping, 2)


# ---------------------------------------------------------------------------
# Vague-listing rescue — cheap Haiku triage for listings the deterministic
# text scorer under-values because there's nothing to parse a GPU model
# from, even when the photo shows something worth real money.
# ---------------------------------------------------------------------------
_vague_triage_budget_used = 0

# Temporary verification counters — track how many vague-triage candidates
# get skipped by the seen-cache check below vs. how many actually reach
# Haiku, so the fix's impact can be confirmed against a live cycle instead
# of just trusting the code path is there. Reset alongside the budget.
_vague_triage_skipped_seen  = 0
_vague_triage_calls_made    = 0


def reset_vague_triage_budget() -> None:
    """Called once per scan cycle so the per-cycle Haiku-call cap actually
    resets, instead of draining across the whole day's run."""
    global _vague_triage_budget_used, _vague_triage_skipped_seen, _vague_triage_calls_made
    _vague_triage_budget_used   = 0
    _vague_triage_skipped_seen  = 0
    _vague_triage_calls_made    = 0


def log_vague_triage_cycle_summary() -> None:
    """Prints the seen-check hit rate for this cycle. Temporary — for
    confirming the redundant-Haiku-call fix is actually catching repeats,
    not just technically present in the code. Safe to remove once confirmed."""
    skipped = _vague_triage_skipped_seen
    made    = _vague_triage_calls_made
    total   = skipped + made
    if total == 0:
        return
    pct = (skipped / total) * 100
    print(f"🔍 Vague-triage seen-check: {made} fresh Haiku call(s), "
          f"{skipped} skipped as already-triaged ({pct:.0f}% of {total} candidates avoided).")


def maybe_rescue_vague_listing(listing: dict, score_data: dict) -> Optional[dict]:
    """
    For a listing that failed the normal score+profit alert threshold: if
    it's a plausible candidate (real photo, sane price band, not already
    hard-disqualified, within the per-cycle Haiku-call budget), asks Haiku
    for a quick, cheap look at the photo. If Haiku identifies something
    genuinely valuable, returns an adjusted score_data built from its
    rough estimate (with a real margin-of-safety requirement, since this
    is a rougher guess than the deterministic scorer) so the caller can
    route it through notify() same as any other qualifying deal. Returns
    None if the listing doesn't qualify or Haiku doesn't think it's worth it.
    """
    global _vague_triage_budget_used, _vague_triage_skipped_seen, _vague_triage_calls_made

    if not AI_VAGUE_TRIAGE_ENABLED or not ANTHROPIC_API_KEY:
        return None

    # Same seen-cache notify() already gates on at notifier.py:1138. Without
    # this, a listing that's still active gets a fresh Haiku vision call
    # every ~15 min for as long as it stays listed — nothing here previously
    # consulted seen_listings.json at all, so "already triaged 15 minutes
    # ago" and "never seen before" looked identical to this function.
    is_seen, is_price_drop, _ = check_duplicate_and_price_drop(listing)
    if is_seen and not is_price_drop:
        _vague_triage_skipped_seen += 1
        return None

    if _vague_triage_budget_used >= AI_VAGUE_TRIAGE_MAX_PER_CYCLE:
        return None

    score = score_data.get("total_score", 0)
    price = listing.get("price", 0) or 0
    image_url = listing.get("image_url", "") or ""

    # Hard-disqualified listings (parts-only, damaged, shipping-only, trade,
    # component bundle) also score nowhere near AI_VAGUE_TRIAGE_MIN_SCORE,
    # but for an explicit reason, not just "nothing parseable" — a photo
    # triage second-guessing that guard would defeat the point of it.
    if any("DISQUALIFIED" in p for p in score_data.get("detected_parts", [])):
        return None
    if not (AI_VAGUE_TRIAGE_MIN_SCORE <= score < AI_VAGUE_TRIAGE_MAX_SCORE):
        return None
    if not image_url.startswith(("http://", "https://")):
        return None
    if not (AI_VAGUE_TRIAGE_PRICE_MIN <= price <= AI_VAGUE_TRIAGE_PRICE_MAX):
        return None

    _vague_triage_budget_used += 1
    _vague_triage_calls_made += 1
    try:
        from ai_features import quick_photo_triage
        triage = quick_photo_triage(listing)
    except Exception as exc:
        # Transient failure (network, API hiccup) — don't mark seen, so this
        # listing gets a real shot at triage next cycle instead of being
        # locked out by a failed attempt that never actually got Haiku's view.
        print(f"⚠️  Vague-listing triage failed: {exc}")
        return None

    if not triage.get("worth_full_analysis"):
        mark_seen_triage_only(listing)
        return None

    rough_value = float(triage.get("rough_value_estimate", 0) or 0)
    if rough_value <= price:
        mark_seen_triage_only(listing)
        return None

    is_fs = (listing.get("category_override") == "Full Systems")
    predicted_profit = calculate_net_profit(rough_value, price, is_full_system=is_fs)
    # This is a Haiku glance, not the deterministic scorer — require a real
    # margin of safety over the normal floor before trusting it enough to
    # actually alert on, rather than the bare MIN_NET_PROFIT_USD cutoff.
    if predicted_profit < MIN_NET_PROFIT_USD * 1.5:
        mark_seen_triage_only(listing)
        return None

    guessed_gpu = triage.get("guessed_gpu_model", "") or ""
    new_score_data = dict(score_data)
    # Keep the scorer's real total. Forcing it up to the photo-analysis
    # threshold made a $90 GTX 1070 show as 80/100 — the number on the alert
    # was a floor, not a score.
    new_score_data["total_score"]      = score
    new_score_data["estimated_value"]  = rough_value
    new_score_data["predicted_profit"] = predicted_profit
    # notify() checks this: the cheap Haiku triage guessed a specific GPU
    # model from a blurry/unclear photo. The full Sonnet analysis it
    # triggers next might not actually confirm that guess — when it
    # doesn't, the alert needs to be held, not sent with confident-looking
    # numbers built on a guess the bot's own closer look couldn't verify.
    new_score_data["_from_vague_triage"] = True
    if guessed_gpu:
        new_score_data["detected_gpu_model"] = guessed_gpu
        listing["_detected_gpu_model"] = guessed_gpu
    new_score_data["detected_parts"] = list(score_data.get("detected_parts", [])) + [
        f"🔍 AI-detected from photo (vague listing): {triage.get('reason', '')}"[:200]
    ]

    print(f"   🔍 Vague-listing triage RESCUED: '{listing.get('title','')[:40]}' "
          f"-> est ${rough_value:.0f}, profit ${predicted_profit:.0f} ({guessed_gpu or 'unspecified'})")
    return new_score_data


# ---------------------------------------------------------------------------
# Daily digest deal store (persisted — survives bot restarts mid-day)
# ---------------------------------------------------------------------------
_digest_deals: list = []
_last_digest_sent: float = 0.0
_DIGEST_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "digest_cache.json")


def _load_digest_cache() -> list:
    """Load today's digest deals from disk. Returns empty list if stale or missing."""
    if not os.path.exists(_DIGEST_PATH):
        return []
    try:
        with open(_DIGEST_PATH) as f:
            data = json.load(f)
        import datetime
        if data.get("date") != datetime.date.today().isoformat():
            return []
        return [tuple(entry) for entry in data.get("deals", [])]
    except Exception:
        return []


def _save_digest_cache() -> None:
    try:
        import datetime
        with open(_DIGEST_PATH, "w") as f:
            json.dump({
                "date":  datetime.date.today().isoformat(),
                "deals": list(_digest_deals),
            }, f)
    except Exception as exc:
        print(f"⚠️  Could not save digest cache: {exc}")


_digest_deals = _load_digest_cache()


def register_deal_for_digest(listing: dict, score_data: dict) -> None:
    """Called for every qualifying alert to build the daily digest."""
    score = score_data.get("total_score", 0)
    _digest_deals.append((score, dict(listing), dict(score_data)))
    # Keep only top 20 to bound memory
    _digest_deals.sort(key=lambda x: x[0], reverse=True)
    if len(_digest_deals) > 20:
        _digest_deals.pop()
    _save_digest_cache()


def send_daily_digest_if_due() -> None:
    """
    Called each scan cycle. Sends a digest embed once per day at
    DAILY_DIGEST_HOUR if there are qualifying deals and a webhook is set.
    """
    from config import DAILY_DIGEST_HOUR, DISCORD_WEBHOOK_DAILY_DIGEST
    import datetime

    global _last_digest_sent
    now = datetime.datetime.now()

    if now.hour != DAILY_DIGEST_HOUR:
        return
    if (time.time() - _last_digest_sent) < 23 * 3600:
        return  # already sent today
    if not _digest_deals:
        return
    if not DISCORD_WEBHOOK_DAILY_DIGEST:
        return

    top5 = _digest_deals[:5]
    fields = []
    for i, (score, lst, sd) in enumerate(top5, 1):
        fields.append({
            "name": f"#{i} — {lst.get('title', 'Unknown')[:50]}",
            "value": (
                f"Score: **{score}/100** | Price: **${lst.get('price', 0):.0f}** "
                f"| Est. Value: **${sd.get('estimated_value', 0):.0f}**\n"
                f"[View listing]({lst.get('url', '')})"
            ),
            "inline": False,
        })

    embed = {
        "title":  f"☀️ Daily Deal Digest — Top {len(top5)} Finds",
        "color":  0x5865F2,
        "fields": fields,
        "footer": {"text": f"PC Flip Scanner | {now.strftime('%B %d, %Y')}"},
    }

    try:
        requests.post(DISCORD_WEBHOOK_DAILY_DIGEST, json={"embeds": [embed]}, timeout=10)
        _last_digest_sent = time.time()
        _digest_deals.clear()
        print(f"📋 Daily digest sent ({len(top5)} deals).")
    except Exception as exc:
        print(f"⚠️  Daily digest failed: {exc}")


# ---------------------------------------------------------------------------
# Discord rate-limit guard
# ---------------------------------------------------------------------------
DISCORD_MIN_INTERVAL = 2.0
_last_discord_send   = 0.0


def _wait_rate_limit() -> None:
    elapsed = time.time() - _last_discord_send
    if elapsed < DISCORD_MIN_INTERVAL:
        time.sleep(DISCORD_MIN_INTERVAL - elapsed)


# ---------------------------------------------------------------------------
# Embed helpers
# ---------------------------------------------------------------------------

def _score_color(score: float, is_price_drop: bool = False) -> int:
    if is_price_drop: return 0x9B59B6
    if score >= 90:   return 0x2ECC71
    if score >= 85:   return 0xF1C40F
    return 0xE67E22


# Discord hard limits: embed field name <= 256 chars, value <= 1024 chars.
# AI-generated text (flags, summaries, negotiation messages, eBay drafts) has
# no guaranteed length from the model, so every field built from it must be
# clamped before it reaches the API — a verbose model response should degrade
# to a truncated field, not a rejected embed.
_FIELD_NAME_LIMIT  = 256
_FIELD_VALUE_LIMIT = 1024


def _safe_name(text: str) -> str:
    text = (text or "").strip() or "—"
    if len(text) > _FIELD_NAME_LIMIT:
        text = text[:_FIELD_NAME_LIMIT - 1].rstrip() + "…"
    return text


def _safe_value(text: str, fallback: str = "—") -> str:
    text = (text or "").strip() or fallback
    if len(text) > _FIELD_VALUE_LIMIT:
        text = text[:_FIELD_VALUE_LIMIT - 1].rstrip() + "…"
    return text


_BREAKDOWN_LABELS = {
    "part_quality_valuation":    "Value",
    "profitability_margin":      "Margin",
    "picture_quality":           "Photos",
    "seller_behavior_risk":      "Seller",
    "title_description_quality": "Listing",
}


def _score_breakdown_line(score_data: dict) -> str:
    """
    A bare "91.2/100" asks members to trust a number they can't audit.
    Renders the 5 scoring pillars scoring.py already computes (and already
    returns in score_data["breakdown"]) as a compact per-pillar tier, so a
    member can see *why* a listing scored the way it did instead of just
    what it scored.
    """
    breakdown = score_data.get("breakdown") or {}
    if not breakdown:
        return ""

    parts = []
    for key, label in _BREAKDOWN_LABELS.items():
        max_points = SCORE_WEIGHTS.get(key, 0)
        points     = breakdown.get(key, 0)
        if max_points <= 0:
            continue
        ratio = points / max_points
        if ratio >= 0.8:
            tier = "Strong"
        elif ratio >= 0.5:
            tier = "Moderate"
        else:
            tier = "Weak"
        parts.append(f"{label}: {tier}")
    return " · ".join(parts)


_PICKUP_BADGE = {
    "confirmed": "📍 Local Pickup Confirmed",
    "stated":    "📍 Local Pickup (seller-stated)",
    "unknown":   "📍 Local Pickup — not explicitly confirmed",
}


def build_discord_embed(listing: dict, score_data: dict, category: str,
                        is_price_drop: bool = False, old_price: float = None,
                        ai_data: dict = None, market_data: dict = None,
                        scam_data: dict = None) -> dict:
    title     = (listing.get("title") or "Unknown Listing").strip()
    price     = listing.get("price", 0) or 0
    url       = listing.get("url") or None
    image_url = listing.get("image_url") or ""
    # A scraper can hand back a lazy-load placeholder (a data: URI, or
    # some other non-http scheme) instead of the real photo — Discord
    # rejects a non-http(s) thumbnail.url outright, taking the whole
    # embed down with a cryptic {"embeds": ["0"]} 400. Better to send the
    # alert with no image than lose it entirely over a bad thumbnail.
    if not image_url.startswith(("http://", "https://")):
        image_url = ""
    platform  = listing.get("platform", "?")
    score     = score_data.get("total_score", 0)
    est_value = score_data.get("estimated_value", 0)
    days      = listing.get("days_listed")

    is_fs          = (category == "Full Systems")
    predicted_profit = score_data.get("predicted_profit", 0)

    display_title = f"🔻 PRICE DROP — {title}"[:256] if is_price_drop else title[:256]

    # Override embed color for scam risk level
    scam_risk = (scam_data or {}).get("risk_level", "low")
    if scam_risk == "high":
        embed_color = 0xE74C3C   # red
    elif scam_risk == "medium":
        embed_color = 0xE67E22   # orange
    else:
        embed_color = _score_color(score, is_price_drop)

    fields = []

    if is_price_drop and old_price:
        pct = int((old_price - price) / old_price * 100)
        fields.append({
            "name":   "🔻 Price Drop",
            "value":  _safe_value(f"~~${old_price:.0f}~~ → **${price:.0f}** ({pct}% off)"),
            "inline": False,
        })

    fields += [
        {"name": "Deal Score",        "value": _safe_value(f"{score}/100"),                 "inline": True},
        {"name": "Price",             "value": _safe_value(f"${price:.0f}"),                 "inline": True},
        {"name": "Est. Value",        "value": _safe_value(f"${est_value:.0f}"),             "inline": True},
        {"name": "Predicted Profit",  "value": _safe_value(f"**${predicted_profit:.0f}**"),  "inline": True},
        {"name": "Platform",          "value": _safe_value(platform),                        "inline": True},
        {"name": "Category",          "value": _safe_value(category),                        "inline": True},
    ]

    # Score breakdown — why it scored what it scored, not just the number
    breakdown_line = _score_breakdown_line(score_data)
    if breakdown_line:
        fields.append({"name": "Score Breakdown", "value": _safe_value(breakdown_line), "inline": False})

    # Local-pickup confidence — the core premise of this bot is in-person
    # verification, so surface that trust signal on every alert, not just
    # use it to silently disqualify the worst shipping-only cases.
    pickup_confidence = score_data.get("pickup_confidence")
    if pickup_confidence in _PICKUP_BADGE:
        fields.append({"name": "Pickup", "value": _safe_value(_PICKUP_BADGE[pickup_confidence]), "inline": True})

    # Scam warning — shown early so members see it before acting
    if scam_data and scam_risk in ("medium", "high"):
        icon    = "🚨" if scam_risk == "high" else "⚠️"
        label   = "HIGH RISK — Likely Scam" if scam_risk == "high" else "MEDIUM RISK — Proceed Carefully"
        flags   = scam_data.get("flags") or []
        summary = scam_data.get("summary") or ""
        flag_str = "\n• " + "\n• ".join(flags) if flags else ""
        fields.append({
            "name":   _safe_name(f"{icon} Scam Check: {label}"),
            "value":  _safe_value(summary + flag_str, fallback="See flags above."),
            "inline": False,
        })

    # Live eBay market data
    if market_data:
        sold_med = market_data.get("sold_median")
        cheapest = market_data.get("cheapest_active")
        if sold_med:
            fields.append({"name": "📊 eBay Sold Avg",    "value": _safe_value(f"${sold_med:.0f}"), "inline": True})
        if cheapest:
            fields.append({"name": "🛒 Cheapest eBay Now", "value": _safe_value(f"${cheapest:.0f}"), "inline": True})

    # Listing age
    if days is not None:
        age_str = "Today" if days == 0 else f"{days}d ago"
        leverage = " (negotiate harder)" if days >= 5 else ""
        fields.append({"name": "📅 Listed",
                        "value": _safe_value(f"{age_str}{leverage}"), "inline": True})

    # AI photo verdict
    # NOTE: flags used to be appended to the field *name* (Discord limit 256
    # chars) rather than the value (limit 1024). A verbose flags list from
    # Claude routinely pushed the name past 256 and Discord rejected the
    # whole embed with a 400. Flags now go in the value, and both name and
    # value are clamped regardless.
    if ai_data and ai_data.get("photo"):
        photo   = ai_data["photo"]
        cond    = (photo.get("condition") or "unknown").title()
        flags   = photo.get("flags") or []
        summary = photo.get("summary", "")
        flag_str = ("\n⚠️ " + ", ".join(flags)) if flags else ""
        fields.append({
            "name":   _safe_name(f"🤖 AI Condition: {cond}"),
            "value":  _safe_value((summary or "") + flag_str, fallback="See listing photos."),
            "inline": False,
        })

    # AI negotiation message
    if ai_data and ai_data.get("negotiation"):
        # Reserve 6 chars for the ``` fences so the wrapped text still fits
        # under the 1024 value limit after truncation.
        msg = _safe_value(ai_data["negotiation"], fallback="")
        if len(msg) > _FIELD_VALUE_LIMIT - 6:
            msg = msg[:_FIELD_VALUE_LIMIT - 7].rstrip() + "…"
        fields.append({
            "name":   _safe_name("💬 Copy-Paste Message"),
            "value":  f"```{msg}```",
            "inline": False,
        })

    # AI eBay listing draft
    if ai_data and ai_data.get("ebay_listing"):
        ebay = ai_data["ebay_listing"]
        if ebay.get("title"):
            name = _safe_name(f"📦 eBay Draft — List at ${ebay.get('suggested_price', 0):.0f}")
            value = _safe_value(
                f"**Title:** {ebay['title'][:80]}\n{ebay.get('description', '')[:200]}"
            )
            fields.append({"name": name, "value": value, "inline": False})

    footer_text = "PC Flip Scanner"
    _clamp_total_embed_length(display_title, footer_text, fields)

    embed = {
        "title":  display_title,
        "url":    url,
        "color":  embed_color,
        "fields": fields,
        "footer": {"text": footer_text},
    }
    if image_url:
        embed["thumbnail"] = {"url": image_url}

    return embed


# Discord's overall embed limit (title + description + fields + footer, etc.)
# is 6000 chars. Individual fields are already clamped above, but several
# maxed-out AI fields together (photo condition + negotiation + eBay draft +
# scam warning) can still stack past the total budget. Shrink the longest
# field values in place until the embed fits, rather than let Discord 400 it.
_EMBED_TOTAL_LIMIT = 6000


def _clamp_total_embed_length(title: str, footer_text: str, fields: list) -> None:
    def total() -> int:
        return (len(title) + len(footer_text)
                + sum(len(f["name"]) + len(f["value"]) for f in fields))

    while total() > _EMBED_TOTAL_LIMIT and fields:
        longest = max(fields, key=lambda f: len(f["value"]))
        overshoot = total() - _EMBED_TOTAL_LIMIT
        new_len = max(len(longest["value"]) - overshoot - 1, 0)
        longest["value"] = (longest["value"][:new_len].rstrip() + "…") if new_len else "—"


def _enforce_embed_limits(embed: dict) -> dict:
    """
    Last-line-of-defense validation, run immediately before the network
    call — does NOT trust that build_discord_embed (or any other caller,
    present or future) already applied _safe_name/_safe_value upstream.
    Anything out of spec gets forcibly corrected in place, with a printed
    warning, rather than letting Discord silently reject the whole embed
    with a terse {"embeds": ["0"]} response and no indication of why.
    """
    warnings = []

    title = embed.get("title")
    if not isinstance(title, str) or not title.strip():
        warnings.append(f"title was {type(title).__name__}={title!r} -> replaced with fallback")
        embed["title"] = "Untitled Listing"
    elif len(title) > _FIELD_NAME_LIMIT:
        warnings.append(f"title was {len(title)} chars -> truncated to {_FIELD_NAME_LIMIT}")
        embed["title"] = title[:_FIELD_NAME_LIMIT - 1].rstrip() + "…"

    url = embed.get("url")
    if url is not None and not isinstance(url, str):
        warnings.append(f"url was {type(url).__name__}={url!r} -> dropped")
        embed.pop("url", None)
    elif isinstance(url, str) and url and not url.startswith(("http://", "https://")):
        warnings.append(f"url was non-http(s) scheme {url[:60]!r} -> dropped")
        embed.pop("url", None)

    thumbnail = embed.get("thumbnail")
    if isinstance(thumbnail, dict):
        thumb_url = thumbnail.get("url")
        if not isinstance(thumb_url, str) or not thumb_url.startswith(("http://", "https://")):
            warnings.append(f"thumbnail.url was {thumb_url!r} (non-http(s)) -> thumbnail dropped")
            embed.pop("thumbnail", None)

    color = embed.get("color")
    if color is not None and not isinstance(color, int):
        try:
            embed["color"] = int(color)
            warnings.append(f"color was {type(color).__name__}={color!r} -> coerced to int {embed['color']}")
        except (TypeError, ValueError):
            warnings.append(f"color was {type(color).__name__}={color!r} -> dropped (uncoercible)")
            embed.pop("color", None)

    fields = embed.get("fields")
    if not isinstance(fields, list):
        warnings.append(f"fields was {type(fields).__name__}={fields!r} -> replaced with []")
        fields = []
        embed["fields"] = fields

    for i, f in enumerate(fields):
        if not isinstance(f, dict):
            warnings.append(f"fields[{i}] was {type(f).__name__}={f!r} -> replaced with placeholder")
            fields[i] = {"name": "—", "value": "—", "inline": False}
            continue

        name = f.get("name")
        if not isinstance(name, str) or not name.strip():
            warnings.append(f"fields[{i}]['name'] was {type(name).__name__}={name!r} -> replaced with fallback")
            f["name"] = "—"
        elif len(name) > _FIELD_NAME_LIMIT:
            warnings.append(f"fields[{i}]['name'] was {len(name)} chars -> truncated to {_FIELD_NAME_LIMIT}")
            f["name"] = name[:_FIELD_NAME_LIMIT - 1].rstrip() + "…"

        value = f.get("value")
        if not isinstance(value, str) or not value.strip():
            warnings.append(f"fields[{i}]['value'] was {type(value).__name__}={value!r} -> replaced with fallback")
            f["value"] = "—"
        elif len(value) > _FIELD_VALUE_LIMIT:
            warnings.append(f"fields[{i}]['value'] was {len(value)} chars -> truncated to {_FIELD_VALUE_LIMIT}")
            f["value"] = value[:_FIELD_VALUE_LIMIT - 1].rstrip() + "…"

        if not isinstance(f.get("inline"), bool):
            f["inline"] = bool(f.get("inline", False))

    footer_text = ((embed.get("footer") or {}).get("text") or "")

    def total_len() -> int:
        return (len(embed.get("title", "")) + len(footer_text)
                + sum(len(f["name"]) + len(f["value"]) for f in fields))

    if total_len() > _EMBED_TOTAL_LIMIT:
        before = total_len()
        while total_len() > _EMBED_TOTAL_LIMIT and fields:
            longest = max(fields, key=lambda f: len(f["value"]))
            overshoot = total_len() - _EMBED_TOTAL_LIMIT
            new_len = max(len(longest["value"]) - overshoot - 1, 0)
            longest["value"] = (longest["value"][:new_len].rstrip() + "…") if new_len else "—"
        warnings.append(f"total embed length was {before} chars (limit {_EMBED_TOTAL_LIMIT}) "
                         f"-> shrank field values until it fit ({total_len()} chars)")

    if warnings:
        print("⚠️  send_discord_embed: embed failed last-line-of-defense validation and was corrected:")
        for w in warnings:
            print(f"     - {w}")

    return embed


def send_discord_embed(embed: dict, webhook_url: str) -> bool:
    global _last_discord_send
    if not webhook_url:
        return False
    # Discord rejects the whole request with a terse, unhelpful
    # {"embeds": ["0"]} if the embed *item* itself is degenerate — not just
    # non-dict types like 0/None, but even a syntactically valid empty dict
    # {}. Confirmed directly against the real API: posting {"embeds": [0]}
    # or {"embeds": [{}]} both return exactly that response. Whatever
    # upstream cause might one day make build_discord_embed hand back
    # something degenerate (a bug in a future edit, a bad merge, a caller
    # passing the wrong variable), this is the last line of defense: never
    # let a non-dict or empty embed reach the API silently.
    if not isinstance(embed, dict) or not embed:
        print(f"❌ send_discord_embed refused to send a malformed embed: "
              f"type={type(embed).__name__} repr={repr(embed)[:200]}")
        return False
    embed = _enforce_embed_limits(embed)
    _wait_rate_limit()
    for attempt in range(3):
        resp = None
        try:
            payload = {"embeds": [embed]}
            serialized = json.dumps(payload, ensure_ascii=False)
            print(f"   🔎 send_discord_embed payload check: type(embed)={type(embed).__name__} | "
                  f"serialized_len={len(serialized)} | full_json={serialized}")
            for i, f in enumerate(embed.get("fields", [])):
                print(f"       field[{i}] name_len={len(f.get('name',''))} value_len={len(f.get('value',''))} "
                      f"inline={f.get('inline')!r} keys={sorted(f.keys())}")
            resp = requests.post(webhook_url, json=payload, timeout=10)
            _last_discord_send = time.time()
            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", 5))
                print(f"⚠️  Discord 429 — waiting {retry_after:.1f}s (attempt {attempt+1}/3)...")
                time.sleep(retry_after + 0.5)
                continue   # retry the send
            resp.raise_for_status()
            return True
        except requests.RequestException as exc:
            _last_discord_send = time.time()
            body = ""
            if resp is not None:
                try:
                    body = json.dumps(resp.json())
                except Exception:
                    body = resp.text
            print(f"⚠️  Discord send failed (attempt {attempt+1}/3): {exc} | response body: {body}")
            if attempt < 2:
                time.sleep(2)
    return False


# ---------------------------------------------------------------------------
# Vague lead alerts — '#vague-leads' channel
# ---------------------------------------------------------------------------
# scoring.py flags a listing "is_vague_lead" when it's a system listing
# (gaming PC / desktop / prebuilt context) naming a high-value-tier GPU but
# giving no CPU/RAM/storage specs at all. The normal scoring pipeline can't
# build a confident valuation from that little, so it would otherwise score
# low and vanish — this routes it to its own channel instead, so a member
# can message the seller directly for the missing hardware breakdown.
def _vague_lead_urgency(gap: float) -> tuple:
    """
    Sizes the alert's icon/headline/call-to-action to how far under the
    reasonable full-system price ceiling for this GPU tier the asking price
    actually sits (see scoring.py's vague_lead_ceiling = GPU baseline value
    x VAGUE_LEAD_PRICE_CEILING_MULTIPLIER). A $400 gap and a $50 gap both
    clear the flagging bar, but they aren't the same deal — this keeps the
    urgency language from reading as identical boilerplate either way.
    """
    if gap >= 200:
        return "🔥🔥", "MASSIVE GAP", "Message seller immediately — this could be a steal."
    elif gap >= 75:
        return "🔥", "Strong gap", "Worth messaging the seller right away for the full hardware breakdown."
    else:
        return "⚠️", "Modest gap", "Still worth a quick message to confirm what's actually inside."


def _vague_lead_metrics(listing: dict, score_data: dict) -> dict:
    """
    Shared arithmetic for the embed builder and the JSONL logger, so the
    two can never silently drift apart — both report the exact same price,
    baseline, ceiling, gap, and urgency tier for a given listing.
    """
    price     = listing.get("price", 0) or 0
    gpu_value = float(score_data.get("vague_lead_gpu_value", 0) or 0)
    ceiling   = float(score_data.get("vague_lead_ceiling", 0) or 0)
    gap       = max(0.0, ceiling - price)
    icon, headline, cta = _vague_lead_urgency(gap)
    return {
        "price": price, "gpu_value": gpu_value, "ceiling": ceiling,
        "gap": gap, "icon": icon, "headline": headline, "cta": cta,
    }


def build_vague_lead_embed(listing: dict, score_data: dict) -> dict:
    """
    Pure embed-builder for the vague-lead alert, kept separate from
    notify_vague_lead() so the message logic can be exercised in a test
    without any network call. Shows the detected GPU's own baseline resale
    value right next to the asking price, and scales the urgency copy to
    the actual dollar gap under a reasonable full-system ceiling for that
    GPU tier — not identical text regardless of how big the gap is.
    """
    title     = (listing.get("title") or "Unknown Listing").strip()
    url       = listing.get("url") or None
    image_url = listing.get("image_url") or ""
    if not image_url.startswith(("http://", "https://")):
        image_url = ""
    platform  = listing.get("platform", "?")
    gpu_model = score_data.get("vague_lead_gpu", "") or ""

    m = _vague_lead_metrics(listing, score_data)

    summary = (
        f"Asking **${m['price']:.0f}** for a full system built around a "
        f"**{gpu_model.upper() or 'high-value GPU'}** (~${m['gpu_value']:.0f} used, alone) "
        f"with no CPU/RAM/storage listed — **${m['gap']:.0f}** under typical full-system "
        f"pricing for this GPU tier. {m['cta']}"
    )

    fields = [
        {"name": "GPU Detected",               "value": _safe_value(gpu_model.upper() or "High-value GPU"), "inline": True},
        {"name": "Asking Price",                "value": _safe_value(f"${m['price']:.0f}"),                 "inline": True},
        {"name": "GPU Baseline (used, alone)",  "value": _safe_value(f"${m['gpu_value']:.0f}"),              "inline": True},
        {"name": "Platform",                    "value": _safe_value(platform),                              "inline": True},
    ]
    pickup_confidence = score_data.get("pickup_confidence")
    if pickup_confidence in _PICKUP_BADGE:
        fields.append({"name": "Pickup", "value": _safe_value(_PICKUP_BADGE[pickup_confidence]), "inline": True})
    fields.append({
        "name":  _safe_name(f"{m['icon']} POSSIBLE UNDERVALUED SYSTEM — {m['headline']}"),
        "value": _safe_value(summary),
        "inline": False,
    })

    embed = {
        "title":  title[:256],
        "url":    url,
        "color":  0xF1C40F,
        "fields": fields,
        "footer": {"text": "PC Flip Scanner — Vague Leads"},
    }
    if image_url:
        embed["thumbnail"] = {"url": image_url}
    return embed


# ---------------------------------------------------------------------------
# Vague lead log — append-only JSONL for after-the-fact debugging.
# seen_listings.json/scan_stats.json don't retain listing details (only
# price/timestamp and aggregate counters), which makes it impossible to
# review vague-lead behavior after the fact — this is the persistent record.
# Same ~/.pc_flip_scanner/ convention as PLAYWRIGHT_PROFILE_DIR and the
# cl_debug/ dumps in craigslist_scraper.py.
# ---------------------------------------------------------------------------
VAGUE_LEAD_LOG_PATH = Path.home() / ".pc_flip_scanner" / "vague_leads_log.jsonl"


def _log_vague_lead(listing: dict, score_data: dict, metrics: dict, sent: bool) -> None:
    """
    Appends one JSON line for a flagged vague lead — only ones that actually
    reach an alert attempt (past the webhook-configured and de-dup checks in
    notify_vague_lead()), not every listing scanned. Records whether the
    Discord send itself succeeded so a run of failures is visible later,
    not just silently retried into the void. Best-effort: a logging failure
    never blocks the actual alert.
    """
    entry = {
        "timestamp":    datetime.datetime.now().isoformat(timespec="seconds"),
        "ts":           time.time(),
        "platform":     listing.get("platform", "?"),
        "title":        listing.get("title", ""),
        "url":          listing.get("url", ""),
        "gpu":          score_data.get("vague_lead_gpu", ""),
        "price":        metrics["price"],
        "gpu_value":    metrics["gpu_value"],
        "ceiling":      metrics["ceiling"],
        "gap":          round(metrics["gap"], 2),
        "urgency_tier": metrics["headline"],
        "sent":         bool(sent),
    }
    try:
        VAGUE_LEAD_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(VAGUE_LEAD_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as exc:
        print(f"⚠️  Could not write vague lead log: {exc}")


def summarize_vague_leads(days: int = 7) -> dict:
    """
    Manual diagnostic helper — no CLI wiring, just call it (e.g. from a
    REPL or a throwaway script) when you want to check how the vague-lead
    feature is actually performing. Reads vague_leads_log.jsonl and
    summarizes the last `days` days: total flagged, how many actually sent,
    a breakdown by urgency tier, and the average gap size.
    """
    result = {"days": days, "total": 0, "sent_count": 0, "by_tier": {}, "avg_gap": 0.0}
    if not VAGUE_LEAD_LOG_PATH.exists():
        return result

    cutoff = time.time() - days * 86400
    gaps: list = []
    with open(VAGUE_LEAD_LOG_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except Exception:
                continue
            if entry.get("ts", 0) < cutoff:
                continue

            result["total"] += 1
            if entry.get("sent"):
                result["sent_count"] += 1
            tier = entry.get("urgency_tier", "Unknown")
            result["by_tier"][tier] = result["by_tier"].get(tier, 0) + 1
            gaps.append(entry.get("gap", 0) or 0)

    if gaps:
        result["avg_gap"] = round(sum(gaps) / len(gaps), 2)
    return result


# ---------------------------------------------------------------------------
# Price-parse-failure log — a "price" under MIN_PRICE_USD is a scraper
# mis-extraction, not a real listing (see main.py's filter_by_price_cap).
# Logged here rather than silently dropped, so a spike in bad extractions
# is visible after the fact instead of just quietly losing listings.
# ---------------------------------------------------------------------------
PRICE_PARSE_FAILURE_LOG_PATH = Path.home() / ".pc_flip_scanner" / "price_parse_failures.jsonl"


def log_price_parse_failure(listing: dict, min_price: float) -> None:
    entry = {
        "timestamp":  datetime.datetime.now().isoformat(timespec="seconds"),
        "ts":         time.time(),
        "platform":   listing.get("platform", "?"),
        "title":      listing.get("title", ""),
        "url":        listing.get("url", ""),
        "price":      listing.get("price", 0) or 0,
        "min_price":  min_price,
    }
    try:
        PRICE_PARSE_FAILURE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(PRICE_PARSE_FAILURE_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as exc:
        print(f"⚠️  Could not write price parse failure log: {exc}")


def notify_vague_lead(listing: dict, score_data: dict) -> bool:
    """
    Sends the vague-lead alert built by build_vague_lead_embed(), then logs
    the attempt (regardless of outcome) to VAGUE_LEAD_LOG_PATH. Shares the
    same de-dup cache as notify() so the same listing doesn't re-alert
    every scan cycle.
    """
    webhook_url = DISCORD_WEBHOOK_VAGUE_LEADS or DISCORD_WEBHOOKS.get("default", "")
    if not webhook_url:
        return False

    is_seen, is_price_drop, _ = check_duplicate_and_price_drop(listing)
    if is_seen and not is_price_drop:
        return False

    embed   = build_vague_lead_embed(listing, score_data)
    sent    = send_discord_embed(embed, webhook_url)
    metrics = _vague_lead_metrics(listing, score_data)
    _log_vague_lead(listing, score_data, metrics, sent)

    if sent:
        title = (listing.get("title") or "Unknown Listing").strip()
        print(f"⚠️  Vague lead sent: {title[:45]} → [#vague-leads]")
        mark_seen(listing)
    return sent


def send_sms_alert(message: str) -> bool:
    if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER, TWILIO_TO_NUMBER]):
        return False
    try:
        from twilio.rest import Client
        Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN).messages.create(
            body=message, from_=TWILIO_FROM_NUMBER, to=TWILIO_TO_NUMBER
        )
        return True
    except Exception as exc:
        print(f"⚠️  SMS failed: {exc}")
        return False


# ---------------------------------------------------------------------------
# Watchlist @mentions
# ---------------------------------------------------------------------------

def _get_watchlist_mentions(listing: dict, score_data: dict) -> str:
    """Returns a string of <@user_id> mentions for members watching this GPU."""
    try:
        from discord_bot import get_members_watching
        gpu_model = listing.get("_detected_gpu_model", "")
        if not gpu_model:
            return ""
        watchers = get_members_watching(gpu_model)
        if watchers:
            return " ".join(f"<@{uid}>" for uid in watchers)
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# AI photo-verdict adjustments — pulled out of notify() so it's testable
# without a network call.
# ---------------------------------------------------------------------------

def apply_ai_photo_adjustments(score_data: dict, ai_data: Optional[dict],
                                category: str) -> tuple:
    """
    Claude's photo analysis returns a condition_modifier (+0.10 to -0.15) that
    was already being folded into estimated_value/predicted_profit. But
    total_score was left untouched — a listing that cleared the text-only bar
    at, say, 91.2/100 kept showing 91.2 even when the photo verdict came back
    model_match=False or condition="unknown" (can't assess: shrink-wrapped,
    glare, partial view). The uncertainty was rendered as a caption in the
    embed but never actually lowered the number driving the alert.

    Returns (new_score_data, hold_reason). hold_reason is None if the alert
    should still go out, or a short human-readable string if the post-
    inspection score no longer clears the threshold it was let through on
    (caller should hold the alert instead of sending it).
    """
    if not ai_data or not ai_data.get("photo"):
        return score_data, None

    photo    = ai_data["photo"]
    modifier = float(photo.get("condition_modifier", 0.0))

    score_penalty = 0.0
    if photo.get("model_match") is False:
        score_penalty += 30.0   # photo doesn't clearly match the claimed GPU
    if photo.get("condition") == "unknown":
        score_penalty += 20.0   # can't assess condition at all
    score_penalty += min(len(photo.get("flags") or []) * 5.0, 20.0)

    if modifier == 0.0 and score_penalty == 0.0:
        return score_data, None

    score_data = dict(score_data)   # don't mutate the shared original

    if modifier != 0.0:
        base_value = float(score_data.get("estimated_value", 0))
        adj_value  = round(base_value * (1 + modifier), 2)
        delta      = adj_value - base_value
        score_data["estimated_value"]  = adj_value
        score_data["predicted_profit"] = round(
            float(score_data.get("predicted_profit", 0)) + delta, 2
        )

    if score_penalty > 0.0:
        original = float(score_data.get("total_score", 0))
        score_data["total_score"] = max(0.0, original - score_penalty)

        is_fs     = (category == "Full Systems")
        threshold = FULL_SYSTEM_ALERT_THRESHOLD if is_fs else DEAL_SCORE_ALERT_THRESHOLD
        if score_data["total_score"] < threshold:
            hold_reason = (
                f"AI uncertainty penalty (-{score_penalty:.0f}) drops score "
                f"{original:.1f} -> {score_data['total_score']:.1f}, below "
                f"alert threshold ({threshold})"
            )
            return score_data, hold_reason

    return score_data, None


# ---------------------------------------------------------------------------
# Main dispatcher
# ---------------------------------------------------------------------------

def notify(listing: dict, score_data: dict) -> bool:
    """
    Full alert pipeline:
      1. De-dup check + price drop detection
      2. GPU cooldown check
      3. AI analysis (photo + negotiation + eBay draft) for high scorers
      4. Live eBay market data fetch
      5. Build and send Discord embed with all enrichments
      6. Watchlist @mentions
      7. Register deal for daily digest
      8. Mark listing and GPU as seen
    """
    # ── De-dup ────────────────────────────────────────────────────────────────
    is_seen, is_price_drop, old_price = check_duplicate_and_price_drop(listing)
    if is_seen and not is_price_drop:
        print(f"🔁 Already alerted on '{listing.get('title','')[:40]}' — skipping.")
        return False

    if is_price_drop:
        print(f"🔻 Price drop: '{listing.get('title','')[:40]}' "
              f"${old_price:.0f} → ${listing.get('price',0):.0f}")

    # ── GPU cooldown ──────────────────────────────────────────────────────────
    gpu_model = listing.get("_detected_gpu_model")
    category  = listing.get("category_override") or detect_category(
        f"{listing.get('title','')} {listing.get('description','')}"
    )

    if category == "GPU Only" and is_gpu_on_cooldown(gpu_model) and not is_price_drop:
        print(f"⏱  GPU cooldown active for '{gpu_model}' — skipping.")
        return False

    # ── AI analysis (negotiation message + eBay draft for all high scorers;
    #    photo verdict additionally when there's a real image) ────────────────
    # NOTE: this used to require listing.get("image_url") to run ANY of the
    # three AI features, not just the photo one. Negotiation messages and
    # eBay drafts are text-only (Haiku) and never touch the image — gating
    # them on it meant listings with no photo (or a scraper that failed to
    # extract one, e.g. Craigslist's lazy-loaded thumbnails) silently lost
    # the whole ai_data block, not just the photo verdict.
    # analyze_listing_photo() already degrades gracefully with no image_url
    # (returns _unknown_photo()), so run_full_ai_analysis is safe to call
    # unconditionally once the score threshold is met.
    ai_data = None
    score   = score_data.get("total_score", 0)
    if ANTHROPIC_API_KEY and score >= AI_PHOTO_ANALYSIS_THRESHOLD:
        try:
            from ai_features import run_full_ai_analysis
            ai_data = run_full_ai_analysis(listing, score_data)
        except Exception as exc:
            print(f"⚠️  AI analysis failed: {exc}")

    # ── Reconcile vague-triage guesses against the closer look ────────────────
    # This listing only exists in the alert pipeline because a cheap Haiku
    # glance guessed a specific, valuable GPU from an otherwise-vague
    # listing. The fuller, more careful photo analysis that just ran might
    # not actually confirm that guess (seen in production: Haiku guessed
    # "RTX 4090" from a glare-obscured photo; the full analysis explicitly
    # said "no RTX model number visible" and couldn't verify it) — in that
    # case the whole basis for this alert existing has fallen through, and
    # sending it would ship a confident-looking profit number built on a
    # guess the bot's own closer look couldn't verify. Hold it instead.
    if score_data.get("_from_vague_triage"):
        photo = (ai_data or {}).get("photo") or {}
        if not photo.get("model_match", False):
            print(f"   🔍 Vague-triage guess not confirmed by full analysis — "
                  f"holding alert for '{listing.get('title','')[:40]}'")
            return False

    # ── Scam check (runs on all alerts, cheap Haiku call) ────────────────────
    # HIGH risk holds the alert outright — a confirmed scam/bait-and-switch
    # listing (e.g. "GPU removed before listing") was previously still
    # alerting with a large, confident profit number computed as if the
    # missing part were there; recoloring the embed wasn't enough since the
    # headline numbers stayed just as misleading. MEDIUM risk isn't held
    # (one ambiguous signal shouldn't kill a real deal outright) but takes a
    # score penalty so it needs to be stronger elsewhere to still clear the
    # alert threshold.
    scam_data = None
    if ANTHROPIC_API_KEY:
        try:
            from ai_features import check_listing_text_for_scams
            scam_data = check_listing_text_for_scams(listing)
            risk = scam_data.get("risk_level", "low")
            if risk != "low":
                print(f"   🚨 Scam check [{risk.upper()}]: {listing.get('title','')[:40]}")
            if risk == "high":
                print(f"   🚫 Holding alert — high scam risk: {listing.get('title','')[:40]}")
                return False
            # Builder/shop ads aren't a seller's item, so there's no deal to alert on.
            if scam_data.get("is_business_ad"):
                print(f"   🚫 Holding alert — business ad, not a single listing: {listing.get('title','')[:40]}")
                return False
            # A full-system alert whose own text says the GPU is missing or sold
            # separately is not a flippable gaming PC — the profit number assumes
            # a GPU that isn't there.
            if scam_data.get("gpu_included") == "no" and listing.get("category_override") == "Full Systems":
                print(f"   🚫 Holding alert — no GPU in a full-system listing "
                      f"('{scam_data.get('gpu_quote','')[:60]}'): {listing.get('title','')[:40]}")
                return False
            if risk == "medium":
                score_data["total_score"] = max(0.0, score_data.get("total_score", 0) - 20.0)
        except Exception as exc:
            print(f"⚠️  Scam check failed: {exc}")

    # ── Live eBay market data ─────────────────────────────────────────────────
    market_data = None
    if gpu_model:
        try:
            from ebay_sold_comps import get_market_data
            market_data = get_market_data(gpu_model)
        except Exception:
            pass

    # ── Apply AI photo verdict to value AND score ─────────────────────────────
    score_data, ai_hold_reason = apply_ai_photo_adjustments(score_data, ai_data, category)
    score = score_data.get("total_score", score)
    if ai_hold_reason:
        print(f"   🔍 {ai_hold_reason} — holding alert for '{listing.get('title','')[:40]}'")
        return False

    # ── Build and send ────────────────────────────────────────────────────────
    channel = NOTIFICATION_CHANNEL.lower()
    sent    = False

    if channel in ("discord", "both"):
        webhook_url = DISCORD_WEBHOOKS.get(category) or DISCORD_WEBHOOKS.get("default", "")
        if not webhook_url:
            print(f"⚠️  No webhook for '{category}' — skipping Discord alert.")
        else:
            embed = build_discord_embed(
                listing, score_data, category,
                is_price_drop=is_price_drop,
                old_price=old_price,
                ai_data=ai_data,
                market_data=market_data,
                scam_data=scam_data,
            )
            sent = send_discord_embed(embed, webhook_url) or sent

            # Watchlist @mentions as a follow-up message
            mentions = _get_watchlist_mentions(listing, score_data)
            if mentions and sent:
                try:
                    requests.post(webhook_url,
                                  json={"content": f"👆 {mentions} — this GPU is on your watchlist!"},
                                  timeout=5)
                except Exception:
                    pass

    if channel in ("sms", "both"):
        is_fs  = (category == "Full Systems")
        np     = calculate_net_profit(
            score_data.get("estimated_value", 0),
            listing.get("price", 0),
            is_full_system=is_fs
        )
        msg = (
            f"{'🔻 PRICE DROP! ' if is_price_drop else '🔥 DEAL FOUND!'}\n"
            f"{listing.get('title','')}\n"
            f"Price: ${listing.get('price',0):.0f} | Value: ${score_data.get('estimated_value',0):.0f}\n"
            f"Predicted profit: ${score_data.get('predicted_profit',0):.0f}\n"
            f"Score: {score}/100\n"
            f"{listing.get('url','')}"
        )
        sent = send_sms_alert(msg) or sent

    if sent:
        tag = " [PRICE DROP]" if is_price_drop else ""
        print(f"✅ Alert sent{tag}: {listing.get('title','')[:45]} → [{category}]")
        mark_seen(listing)
        mark_gpu_alerted(gpu_model)
        register_deal_for_digest(listing, score_data)
        return True

    print(f"⚠️  No channel succeeded for: {listing.get('title','')[:45]}")
    return False