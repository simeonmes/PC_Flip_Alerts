"""
ai_features.py
==============
Claude API-powered features that run on high-scoring listings after the
main alert fires. Three capabilities:

  1. analyze_listing_photo() — vision analysis of the listing image to
     assess condition, flag mining indicators, and verify the GPU model
     matches what the seller claims. Returns a condition modifier that
     adjusts the estimated value up or down.

  2. generate_negotiation_message() — writes a custom, casual opening
     message tailored to the specific listing. Members copy-paste it to
     message sellers immediately after the alert fires.

  3. generate_ebay_listing() — produces an optimized eBay title,
     description, and suggested price for when the member is ready to
     flip. Saves 15-20 minutes of listing work per flip.

Setup: add ANTHROPIC_API_KEY to .env. All three functions degrade
gracefully if the key is missing or the API call fails.
"""

import base64
import json
import os
import re

import requests

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"

# Sonnet for vision tasks (photo analysis) — quality matters here.
# Haiku for text-only tasks (negotiation messages, eBay drafts) — 3x cheaper.
SONNET_MODEL = "claude-sonnet-4-6"
HAIKU_MODEL  = "claude-haiku-4-5-20251001"

AI_ANALYSIS_SCORE_THRESHOLD = 80


def _claude(prompt: str, image_url: str = None, max_tokens: int = 400,
            model: str = SONNET_MODEL) -> str:
    """
    Thin wrapper around the Anthropic /v1/messages endpoint.
    Defaults to SONNET_MODEL. Pass HAIKU_MODEL for text-only tasks.
    """
    if not ANTHROPIC_API_KEY:
        return ""

    content = []

    if image_url:
        try:
            img_resp = requests.get(image_url, timeout=8)
            img_b64  = base64.b64encode(img_resp.content).decode()
            media_type = img_resp.headers.get("content-type", "image/jpeg").split(";")[0]
            content.append({
                "type": "image",
                "source": {
                    "type":       "base64",
                    "media_type": media_type,
                    "data":       img_b64,
                },
            })
        except Exception:
            pass  # proceed without image if download fails

    content.append({"type": "text", "text": prompt})

    try:
        resp = requests.post(
            ANTHROPIC_API_URL,
            headers={
                "x-api-key":         ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            },
            json={
                "model":      model,
                "max_tokens": max_tokens,
                "messages":   [{"role": "user", "content": content}],
            },
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["content"][0]["text"].strip()
    except Exception as exc:
        print(f"⚠️  Claude API error: {exc}")
        return ""


def analyze_listing_photo(listing: dict, score_data: dict) -> dict:
    """
    Uses Claude vision to assess the listing's primary photo.

    Returns a dict:
      {
        "condition":          "good",         # excellent/good/fair/poor/unknown
        "flags":              ["mining dust"], # list of red-flag strings
        "model_match":        True,            # does photo match claimed GPU?
        "condition_modifier": 0.05,           # value adjustment (%): +0.10 to -0.20
        "summary":            "Clean card...", # one-sentence verdict
      }

    condition_modifier is applied to estimated_value in notifier.py's embed.
    """
    image_url = listing.get("image_url", "")
    title     = listing.get("title", "Unknown")
    price     = listing.get("price", 0)

    if not image_url:
        return _unknown_photo()

    prompt = f"""You are evaluating a used PC hardware listing photo for a reseller.
Listing title: {title}
Listed price: ${price:.0f}

Assess the photo and respond with ONLY valid JSON (no other text):
{{
  "condition": "good",
  "flags": [],
  "model_match": true,
  "gpu_visible": "yes",
  "identified_gpu": "RTX 3080 Ti",
  "is_business_ad": false,
  "condition_modifier": 0.05,
  "summary": "Clean card, no visible damage"
}}

Rules:
- condition: one of excellent/good/fair/poor/unknown
- flags: list any red flags you see (e.g. "heavy dust", "bent heatsink fin", "missing fan", "mining rig background", "model mismatch")
- gpu_visible: "yes" if a graphics card is clearly visible, "no" if the photo shows a PC with no GPU or only the case/parts, "unknown" if you can't tell
- identified_gpu: the model you can actually read on the card (e.g. "RX 6700 XT"), or "" if you can't read one. Do not guess from the title.
- model_match: true only if the GPU you identified matches the title; false if it clearly doesn't; null if the title is generic or you could not identify a model
- is_business_ad: true if the photo is a builder's promo graphic, stock image, or a shop display rather than a photo of the seller's own item
- condition_modifier: +0.10 for excellent, +0.05 for good, 0.00 for fair, -0.15 for poor
- summary: one sentence, plain English verdict. Describe only what you can see."""

    raw = _claude(prompt, image_url=image_url, max_tokens=250)
    if not raw:
        return _unknown_photo()

    try:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            return json.loads(match.group())
    except Exception:
        pass

    return _unknown_photo()


def _unknown_photo() -> dict:
    # model_match is None (not True) when the check failed: a failed photo call
    # must never read as a confirmed match. notifier.py's vague-triage gate
    # treats anything but True as unconfirmed and holds the alert.
    return {
        "condition":          "unknown",
        "flags":              [],
        "model_match":        None,
        "gpu_visible":        "unknown",
        "is_business_ad":     False,
        "condition_modifier": 0.0,
        "summary":            "Photo analysis unavailable",
    }


def generate_negotiation_message(listing: dict, score_data: dict) -> str:
    """
    Writes a short, casual opening message for the seller — tailored to
    the specific listing. Members copy-paste it and message sellers within
    seconds of the alert firing.

    Returns empty string on failure so callers can skip the embed field.
    """
    title          = listing.get("title", "")
    ask_price      = listing.get("price", 0)
    estimated_val  = score_data.get("estimated_value", 0)
    platform       = listing.get("platform", "Facebook")

    # Target offer: 10-12% below ask, rounded to nearest $5
    raw_offer = ask_price * 0.88
    offer     = round(raw_offer / 5) * 5
    offer     = max(offer, ask_price - 100)  # don't lowball unreasonably

    prompt = f"""Write a short {platform} Marketplace message to a seller.

Item: {title}
Their asking price: ${ask_price:.0f}
Your opening offer: ${offer:.0f}

Requirements:
- 2-3 sentences MAXIMUM
- Sound like a real person, not a bot
- Mention you can do cash and meet today or tomorrow
- Make the ${offer:.0f} offer naturally — don't be pushy
- Do NOT mention you're a reseller

Reply with ONLY the message text, no quotes, no labels."""

    msg = _claude(prompt, max_tokens=150, model=HAIKU_MODEL)
    return msg if msg else ""


def generate_ebay_listing(listing: dict, score_data: dict) -> dict:
    """
    Produces an optimized eBay listing ready to copy-paste.

    Returns:
      {
        "title":           "...",  # ≤80 chars, keyword-rich
        "description":     "...",  # 3-4 sentences
        "suggested_price": 375,    # recommended sell price
        "condition":       "Used - Good",
      }
    """
    title         = listing.get("title", "")
    description   = listing.get("description", "")
    detected      = score_data.get("detected_parts", [])
    est_value     = score_data.get("estimated_value", 0)
    suggested_sell = round(est_value * 0.95 / 5) * 5  # 5% below market, rounded

    prompt = f"""Create an optimized eBay listing for used PC hardware.

Original listing title: {title}
Original description: {description or "Not provided"}
Detected specs: {", ".join(detected) if detected else "See title"}
Recommended sell price: ${suggested_sell:.0f}

Respond with ONLY valid JSON:
{{
  "title": "ASUS RTX 3080 10GB GDDR6X Gaming GPU",
  "description": "2-4 sentences, using only facts from the original listing or [CONFIRM: ...] placeholders.",
  "suggested_price": {suggested_sell:.0f},
  "condition": "Used - Good"
}}

Title rules: ≤80 characters, include the model named in the original listing, avoid ALL CAPS for entire title. If the original title is generic (e.g. "Partner listing", "Gaming PC"), use the model from the detected specs; if there is none, write "[CONFIRM MODEL]" in the title.

Description rules — this text will be posted publicly under the seller's name, so it must be checkable:
- Only state specs, condition, and inclusions that appear in the original title, description, or detected specs.
- Do NOT write "tested", "working", "fully functional", "fast shipping", "ships from ___", "original box", or "includes ___" unless the original listing says so. If the seller hasn't said it, write it as a bracketed placeholder such as "[CONFIRM: tested working]" or "[CONFIRM: shipping]" so the seller fills it in.
- Do NOT state a location, city, or shipping origin.
- If the listing is missing key specs (CPU, RAM, storage, GPU), say so in brackets rather than guessing.
- 2-4 sentences."""

    raw = _claude(prompt, max_tokens=350, model=HAIKU_MODEL)
    if not raw:
        return {
            "title":           title[:80],
            "description":     "",
            "suggested_price": suggested_sell,
            "condition":       "Used - Good",
        }

    try:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            return json.loads(match.group())
    except Exception:
        pass

    return {
        "title":           title[:80],
        "description":     "",
        "suggested_price": suggested_sell,
        "condition":       "Used - Good",
    }


def run_full_ai_analysis(listing: dict, score_data: dict) -> dict:
    """
    Convenience wrapper that runs all three AI features on a qualifying
    listing. Called by notifier.py for listings above AI_ANALYSIS_SCORE_THRESHOLD.

    Returns:
      {
        "photo":        {...},  # from analyze_listing_photo()
        "negotiation":  "...", # from generate_negotiation_message()
        "ebay_listing": {...},  # from generate_ebay_listing()
      }
    """
    print(f"   🤖 Running AI analysis on: {listing.get('title', '')[:40]}...")

    photo       = analyze_listing_photo(listing, score_data)
    negotiation = generate_negotiation_message(listing, score_data)
    ebay        = generate_ebay_listing(listing, score_data)

    return {
        "photo":        photo,
        "negotiation":  negotiation,
        "ebay_listing": ebay,
    }


def quick_photo_triage(listing: dict) -> dict:
    """
    Cheap first-pass check (Haiku, not Sonnet) for listings that scored too
    low on text alone to reach run_full_ai_analysis, but might be genuine
    deals hidden behind a vague title/description with a real photo. The
    deterministic text scorer can only value what it can parse — a listing
    titled "Gaming PC for sale, $200" with no GPU model mentioned scores
    low and never reaches AI_PHOTO_ANALYSIS_THRESHOLD, even if the photo
    clearly shows a $700 graphics card. This exists to catch exactly that
    gap, at Haiku's cost instead of Sonnet's, so it's affordable to run on
    a much larger, looser pool of borderline listings than full analysis
    ever would be.

    Returns:
      {
        "worth_full_analysis":  bool,
        "guessed_gpu_model":    "rtx 3080" | "",
        "rough_value_estimate": 450,
        "reason":               "short explanation",
      }
    Degrades to worth_full_analysis=False on any failure — never raises.
    """
    image_url = listing.get("image_url", "")
    title     = listing.get("title", "")
    price     = listing.get("price", 0) or 0
    fallback  = {"worth_full_analysis": False, "guessed_gpu_model": "", "rough_value_estimate": 0.0, "reason": ""}

    if not image_url:
        return fallback

    prompt = f"""You're doing a quick, cheap first-pass triage of a marketplace photo for a PC-hardware flip-alert bot.

The listing's TEXT is too vague for a keyword scorer to identify what's for sale — but the PHOTO might show something valuable the seller didn't describe well.

Listing title: {title}
Listed price: ${price:.0f}

Look at the photo. Is there a SPECIFIC, identifiable GPU or gaming PC visible whose resale value is meaningfully HIGHER than the ${price:.0f} asking price? Be conservative — only say yes if you can actually identify a specific model from the photo, not a vague guess.

Respond with ONLY valid JSON:
{{
  "worth_full_analysis": false,
  "guessed_gpu_model": "",
  "rough_value_estimate": 0,
  "reason": "one short sentence"
}}

worth_full_analysis: true only if you can identify a specific GPU/component model in the photo worth a closer, more expensive AI analysis pass
guessed_gpu_model: the specific model you identified (e.g. "rtx 3080"), empty string if none
rough_value_estimate: your rough USD resale value estimate for what's shown, 0 if worth_full_analysis is false
reason: one short sentence explaining what you saw"""

    raw = _claude(prompt, image_url=image_url, max_tokens=200, model=HAIKU_MODEL)
    if not raw:
        return fallback

    try:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            result = json.loads(match.group())
            return {
                "worth_full_analysis":  bool(result.get("worth_full_analysis", False)),
                "guessed_gpu_model":    str(result.get("guessed_gpu_model") or ""),
                "rough_value_estimate": float(result.get("rough_value_estimate") or 0),
                "reason":               str(result.get("reason") or ""),
            }
    except Exception:
        pass

    return fallback


# ---------------------------------------------------------------------------
# GPU generations Haiku must know about — prevents false scam flags on new
# hardware that postdates its training data (RTX 50-series, RX 9000-series, etc.)
# ---------------------------------------------------------------------------
_KNOWN_CURRENT_GPU_MODELS = """
RTX 5090, RTX 5080, RTX 5070 Ti, RTX 5070, RTX 5060 Ti, RTX 5060, RTX 5050  ← RTX 50-series: released Jan–Jun 2025, ALL REAL
RTX 4090, RTX 4080 Super, RTX 4080, RTX 4070 Ti Super, RTX 4070 Ti, RTX 4070 Super, RTX 4070, RTX 4060 Ti, RTX 4060  ← RTX 40-series
RTX 3090 Ti, RTX 3090, RTX 3080 Ti, RTX 3080, RTX 3070 Ti, RTX 3070, RTX 3060 Ti, RTX 3060, RTX 3050  ← RTX 30-series
RX 9070 XT, RX 9070, RX 9060 XT  ← AMD RDNA 4, released 2025, ALL REAL
RX 7900 XTX, RX 7900 XT, RX 7800 XT, RX 7700 XT, RX 7600 XT, RX 7600  ← AMD RDNA 3
Arc B580, Arc B570  ← Intel Arc Battlemage, released Dec 2024, ALL REAL
""".strip()


def check_listing_text_for_scams(listing: dict) -> dict:
    """
    Uses Haiku to scan listing text for BEHAVIORAL scam red flags only.

    Returns:
      {
        "risk_level": "low" | "medium" | "high",
        "flags":      ["specific red flag text found", ...],
        "summary":    "one-sentence explanation (empty string if low risk)",
      }

    IMPORTANT: This function does NOT compare the listed price against
    market value — the scoring algorithm already handles that, and a low
    price alone is never a red flag here (that would just false-positive
    on every good deal this bot exists to find). It DOES now check for a
    listed price that directly *contradicts itself* against a different
    price stated in the description (a real bait-and-switch pattern found
    in production: a $450 listing whose description stated the actual
    price was $3,450) — that's a self-consistency check, not a market
    comparison, so it stays distinct from ordinary "was $X retail, now
    $Y" framing, which is completely normal deal language.

    Degrades gracefully — returns low-risk placeholder if API is unavailable.
    """
    title       = listing.get("title", "")
    description = listing.get("description", "") or ""
    price       = listing.get("price", 0) or 0

    if not title:
        return {"risk_level": "low", "flags": [], "summary": ""}

    prompt = f"""You are a scam-detection assistant for a used PC hardware marketplace (DMV area, 2025-2026).

IMPORTANT CONTEXT: This bot finds deals — listings priced below market value. A low or "good" price is NEVER a red flag by itself. Your job is to detect explicit scam LANGUAGE, behavioral red flags, and internal price contradictions.

ALL of the following GPU models are real — do not flag them as suspicious:
{_KNOWN_CURRENT_GPU_MODELS}

Listing title: {title}
Listed price: ${price:.0f}
Description: {description[:2000] if description else "Not provided"}

ALSO ANSWER TWO COMPONENT QUESTIONS from the title and description together (these do NOT count as scam flags):
- gpu_included: "no" if the text says the GPU/graphics card is missing, removed, not included, sold separately, or the system "needs a GPU"; "yes" if the text says a GPU is included or names one as part of the system; "unknown" if it doesn't say. A listing for a graphics card itself is "yes". Quote the exact words in gpu_quote, or "" if none.
- is_business_ad: true if this reads like a builder's or shop's advertisement ("custom PCs built to your budget", "call or text", "shop now", "financing available", "our builds") rather than one person selling their own item.

ONLY flag these specific patterns as risk (look for actual text evidence):
1. Unusual payment demands: "PayPal Friends and Family only", "Zelle only", "Cash App", "gift card", "crypto only", "no PayPal"
2. Advance payment requests: "send deposit", "pay upfront to hold", "wire transfer required"
3. Emotional manipulation / urgency: "deployed overseas", "going through divorce", "family emergency", "need to sell fast due to" + personal crisis
4. Box-only or accessory-only bait: the listing is for an empty box, manual, or cooler while the title implies the whole product (a missing GPU in a PC is NOT this flag — report that via gpu_included)
5. Explicit no-meetup: seller EXPLICITLY states they will not meet, will only ship, refuses in-person verification
6. Price contradiction / bait-and-switch: the description states the "actual", "real", or "true" price is DIFFERENT from the ${price:.0f} listed price (e.g. "actual price is $3,450", "real price: $X", "true cost is $X, listing is wrong") — this means the visible price is bait and the seller intends to charge something else

DO NOT FLAG any of the following (these are all normal and not red flags):
- Prices that seem low or below retail — used prices ARE below retail, that is the point
- "Originally $X retail" / "paid $X new" / "was $X, selling for $Y" — this is normal context explaining a good deal, NOT a contradiction. Only flag when the description claims the CURRENT/ACTUAL/REAL asking price differs from what's listed, not when it references the original retail price for context.
- Sparse descriptions or short listings — very common on Facebook Marketplace
- Missing condition details, warranty info, or purchase history
- "Ships to you" or mentioning shipping as an option (sellers often offer both pickup and shipping)
- New GPU models you may not recognize (RTX 5000-series, RX 9000-series, Arc B-series are all real)
- Any listing from someone who seems like a normal private seller

Only return medium/high risk if you found SPECIFIC SCAM LANGUAGE from the list above.

Respond with ONLY valid JSON, no preamble or explanation:
{{
  "risk_level": "low",
  "flags": [],
  "summary": "",
  "gpu_included": "unknown",
  "gpu_quote": "",
  "is_business_ad": false
}}

risk_level: "low" (no behavioral red flags found), "medium" (one concerning behavioral signal), "high" (explicit scam language or multiple red flags)
flags: quote or paraphrase the specific scam text found — empty list [] if none
summary: one sentence describing what specific language triggered this; empty string "" if low risk
gpu_included / gpu_quote / is_business_ad: the component answers above — these never change risk_level by themselves"""

    raw = _claude(prompt, max_tokens=200, model=HAIKU_MODEL)
    if not raw:
        return {"risk_level": "low", "flags": [], "summary": ""}

    try:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            result = json.loads(match.group())
            if result.get("risk_level") not in ("low", "medium", "high"):
                result["risk_level"] = "low"
            if not isinstance(result.get("flags"), list):
                result["flags"] = []
            # setdefault only fills a *missing* key — Claude can legitimately
            # return "summary": null (present, but None), which setdefault
            # leaves untouched. Coerce explicitly so callers never see None.
            if not isinstance(result.get("summary"), str):
                result["summary"] = ""
            if result.get("gpu_included") not in ("yes", "no", "unknown"):
                result["gpu_included"] = "unknown"
            if not isinstance(result.get("gpu_quote"), str):
                result["gpu_quote"] = ""
            result["is_business_ad"] = result.get("is_business_ad") is True
            return result
    except Exception:
        pass

    return {"risk_level": "low", "flags": [], "summary": "",
            "gpu_included": "unknown", "gpu_quote": "", "is_business_ad": False}