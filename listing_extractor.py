"""
listing_extractor.py
====================
SHADOW MODE — an alternative way of reading listings, run side by side with
the live scorer without affecting a single alert.

Haiku reads each listing into structured fields (listing type, GPU, CPU, RAM,
storage, damaged / trade / shipping flags). It never prices anything: the
values come from the same price book scoring.py uses (baseline_values.py),
and the go/no-go is a margin rule instead of the 0-100 score.

Why: the keyword scorer misread the parts in 22 of 62 hand-checked listings
(VRAM counted as RAM, "i7 14700F" without a hyphen, GPUs named only as a
comparison, multi-item posts...). In an offline test Haiku read 17 of those
22 correctly vs 6 for the keyword scorer. Shadow mode checks whether that
holds on listings neither of us tuned against, before anything switches.

Each listing is sent to Haiku once and cached; it's re-read only if its
title, description or price changes. Every verdict is appended to
shadow_log.jsonl when it first appears or changes — `python scoreboard.py
shadow` turns the disagreements into labelling work.
"""

import hashlib
import json
import re
import time
from pathlib import Path

import baseline_values as bv
from baseline_values import (
    GPU_VALUES, CPU_VALUES, MOTHERBOARD_VALUES, PSU_VALUES, COOLER_VALUES, STORAGE_VALUES,
)
from config import (
    ANTHROPIC_API_KEY,
    SHADOW_EXTRACTION_ENABLED,
    SHADOW_EXTRACTION_MAX_CALLS_PER_CYCLE,
    SHADOW_MIN_MARGIN,
    SHADOW_MIN_PROFIT_USD,
)

MODEL = "claude-haiku-4-5"

# Same ~/.pc_flip_scanner/ convention as the other runtime state files.
_STATE_DIR       = Path.home() / ".pc_flip_scanner"
EXTRACTION_CACHE = _STATE_DIR / "listing_extractions.json"
SHADOW_LOG_PATH  = _STATE_DIR / "shadow_log.jsonl"
_CACHE_TTL_SEC   = 14 * 24 * 60 * 60

# Mirrors scoring.py's _FULL_SYSTEM_CHASSIS_FLAT_RATE, so the two approaches
# value a complete PC on the same terms and only the reading differs.
_CHASSIS_VALUE = 75.0

SYSTEM_PROMPT = """You read used PC-hardware listings from Facebook Marketplace and Craigslist and extract exactly what is for sale.
Report only what the listing text states. Never guess a model the text doesn't name.

Rules:
- gpu: the graphics card that is FOR SALE (or inside the PC for sale). Ignore GPUs mentioned only as a
  comparison ("~4070 Ti performance"), a card the seller upgraded to or replaced it with, or a card they
  want in a trade. Write it lowercase like "rtx 4070 super", "rx 7800 xt", "arc b580", "gtx 1660 super".
  Fix obvious typos ("gtx 5060" -> "rtx 5060"). Use "" if no specific model is named.
- gpu_vram_gb: the card's VRAM if stated, else null.
- cpu: lowercase, Intel like "i7-14700f" or "core ultra 5 225f", AMD like "ryzen 7 7800x3d" or "ryzen 5 5600".
  Shorthand counts: "r5 5500" -> "ryzen 5 5500", "i7 12700k" -> "i7-12700k". If only the family is
  given ("Intel i7", "Ryzen 5 5000 series"), write just "i7" / "ryzen 5". "" if no CPU is mentioned.
- ram_gb: SYSTEM memory only. A GPU's VRAM is never system RAM, even if the seller calls it "ram".
- storage_gb / storage_type: the main drive (largest SSD/NVMe if several).
- motherboard: the chipset if named ("z790", "b550"), "unspecified" if a motherboard is included but its
  chipset isn't named, "" if not mentioned.
- cooler: "aio" with its radiator size in cooler_aio_mm if known, "air", or "" if not mentioned.
- listing_type:
  gpu_only = a graphics card by itself; full_pc = a complete working desktop;
  parts_bundle = several loose components sold together (no case/PSU making it a full PC);
  single_part = one non-GPU component; laptop = a laptop or all-in-one;
  multi_item_post = several items each priced separately, so the listed price isn't for one defined item;
  other = monitors, peripherals, cases, anything else.
  Laptop model lines are laptops even when the word "laptop" is missing (Lenovo Legion 5/5i/7i/Pro and
  Y-series, ASUS ROG Strix G/Zephyrus/TUF Gaming laptops, HP Omen 15/16/17 and Victus 15/16, Dell G15/G16,
  Acer Nitro 5/Predator Helios, Razer Blade, MSI Katana/Stealth/Raider). Desktop lines such as HP Omen
  25L/30L/45L, Legion Tower, Alienware Aurora and Acer Predator Orion are full_pc.
- damaged: says it doesn't work / for parts / needs repair.
- trade_only: the seller wants a trade INSTEAD of cash. A cash price with a trade also accepted
  ("will also trade for...", "open to trades") is NOT trade_only.
- shipping_only: shipped rather than local pickup, including eBay listings shown inside Marketplace.
  "Shipping available" alongside local pickup is NOT shipping_only.
- gpu_missing: a PC explicitly sold without its graphics card."""

EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "listing_type": {"type": "string", "enum": ["gpu_only", "full_pc", "parts_bundle", "single_part",
                                                     "laptop", "multi_item_post", "other"]},
        "gpu":           {"type": "string"},
        "gpu_vram_gb":   {"type": ["integer", "null"]},
        "cpu":           {"type": "string"},
        "ram_gb":        {"type": ["integer", "null"]},
        "ram_type":      {"type": "string", "enum": ["ddr5", "ddr4", "unknown"]},
        "storage_gb":    {"type": ["integer", "null"]},
        "storage_type":  {"type": "string", "enum": ["nvme", "ssd", "hdd", "unknown"]},
        "motherboard":   {"type": "string"},
        "psu_watts":     {"type": ["integer", "null"]},
        "cooler":        {"type": "string", "enum": ["aio", "air", ""]},
        "cooler_aio_mm": {"type": ["integer", "null"]},
        "damaged":       {"type": "boolean"},
        "trade_only":    {"type": "boolean"},
        "shipping_only": {"type": "boolean"},
        "gpu_missing":   {"type": "boolean"},
    },
    "required": ["listing_type", "gpu", "gpu_vram_gb", "cpu", "ram_gb", "ram_type", "storage_gb",
                 "storage_type", "motherboard", "psu_watts", "cooler", "cooler_aio_mm", "damaged",
                 "trade_only", "shipping_only", "gpu_missing"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# Haiku call + per-listing cache
# ---------------------------------------------------------------------------
_client         = None
_cache          = None   # {listing_key: {"sig", "ts", "extraction"}}
_disabled_noted = False


def _get_client():
    global _client
    if _client is None:
        import anthropic   # lazy: the scanner runs fine without the SDK when shadow mode is off
        _client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return _client


def listing_key(listing: dict) -> str:
    """Same shape as notifier.get_listing_key(), without importing notifier."""
    platform = (listing.get("platform") or "unknown").lower()
    ident    = listing.get("listing_id") or listing.get("url") or listing.get("title", "")[:50]
    return f"{platform}:{ident}"


def _signature(listing: dict) -> str:
    """Changes whenever the text or price the extraction was based on changes."""
    raw = f"{listing.get('title', '')}\n{listing.get('description', '')}\n{float(listing.get('price') or 0):.2f}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _load_cache() -> dict:
    global _cache
    if _cache is None:
        try:
            with open(EXTRACTION_CACHE, encoding="utf-8") as f:
                _cache = json.load(f)
        except Exception:
            _cache = {}
    return _cache


def _save_cache() -> None:
    global _cache
    cutoff = time.time() - _CACHE_TTL_SEC
    _cache = {k: v for k, v in _load_cache().items() if isinstance(v, dict) and v.get("ts", 0) >= cutoff}
    try:
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(EXTRACTION_CACHE, "w", encoding="utf-8") as f:
            json.dump(_cache, f, ensure_ascii=False)
    except Exception as exc:
        print(f"⚠️  Could not write listing_extractions.json: {exc}")


def _call_haiku(listing: dict):
    """Returns the extraction dict, or None on any API failure (never raises)."""
    import anthropic
    message = (f"Title: {listing.get('title', '')}\n"
               f"Asking price: ${float(listing.get('price') or 0):.0f}\n"
               f"Description: {listing.get('description') or '(none)'}")
    try:
        response = _get_client().messages.create(
            model=MODEL,
            max_tokens=1024,
            temperature=0,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": message}],
            output_config={"format": {"type": "json_schema", "schema": EXTRACTION_SCHEMA}},
        )
    except anthropic.APIError as exc:
        print(f"⚠️  Shadow extraction API error: {exc}")
        return None
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        return json.loads(text)
    except ValueError:
        return None


def extract_cached(listing: dict, allow_call: bool = True):
    """
    The listing's extraction from the cache, or — when allow_call — a fresh
    Haiku read (cached on success). None if neither is available.
    """
    key   = listing_key(listing)
    sig   = _signature(listing)
    entry = _load_cache().get(key)
    if isinstance(entry, dict) and entry.get("sig") == sig:
        return entry["extraction"]
    if not allow_call:
        return None
    extraction = _call_haiku(listing)
    if extraction is not None:
        _load_cache()[key] = {"sig": sig, "ts": time.time(), "extraction": extraction}
        _save_cache()
    return extraction


# ---------------------------------------------------------------------------
# Valuation — deterministic, price book only
# ---------------------------------------------------------------------------
def _gpu_value(x: dict) -> tuple:
    gpu = (x.get("gpu") or "").strip().lower()
    if not gpu:
        return 0.0, ""
    if x.get("gpu_vram_gb"):
        sized = f"{gpu} {x['gpu_vram_gb']}gb"
        if sized in GPU_VALUES:
            return GPU_VALUES[sized], sized
    if gpu in GPU_VALUES:
        return GPU_VALUES[gpu], gpu
    contained = [k for k in GPU_VALUES if k in gpu]
    if contained:
        key = max(contained, key=len)
        return GPU_VALUES[key], key
    return 0.0, f"unpriced gpu '{gpu}'"


def _cpu_value(x: dict) -> tuple:
    cpu = (x.get("cpu") or "").strip().lower()
    if not cpu:
        return 0.0, ""
    if re.match(r"i[3579]-[2-9]\d{3}", cpu):   # same 9th-gen-or-older Intel policy as scoring.py
        return 15.0, f"legacy {cpu}"
    if cpu in CPU_VALUES:
        return CPU_VALUES[cpu], cpu
    prefixes = [k for k in CPU_VALUES if cpu.startswith(k)]   # "i7-14700f" -> "i7-14700"
    if prefixes:
        key = max(prefixes, key=len)
        return CPU_VALUES[key], key
    return 0.0, f"unpriced cpu '{cpu}'"


def _ram_value(x: dict, assume_16gb: bool) -> float:
    rate = {"ddr5": bv.RAM_VALUES_PER_GB_DDR5,
            "ddr4": bv.RAM_VALUES_PER_GB_DDR4}.get(x.get("ram_type"), bv.RAM_VALUES_PER_GB_DEFAULT)
    gb = x.get("ram_gb") or (16 if assume_16gb else 0)
    return gb * rate if 0 < gb <= 128 else 0.0


def _storage_value(x: dict) -> float:
    kind = x.get("storage_type")
    if kind == "hdd":
        return STORAGE_VALUES["hdd"]
    if kind not in ("nvme", "ssd"):
        return 0.0
    gb = x.get("storage_gb")
    if not gb:
        return STORAGE_VALUES[kind]
    tier = min((256, 512, 1024, 2048, 4096), key=lambda size: abs(size - gb))
    key  = f"{tier // 1024}tb {kind}" if tier >= 1024 else f"{tier}gb {kind}"
    return STORAGE_VALUES.get(key, STORAGE_VALUES[kind])


def _motherboard_value(x: dict) -> float:
    board = (x.get("motherboard") or "").lower()
    if not board:
        return 0.0
    chipsets = [k for k in MOTHERBOARD_VALUES if k not in ("motherboard", "mobo") and k in board]
    return MOTHERBOARD_VALUES[max(chipsets, key=len)] if chipsets else MOTHERBOARD_VALUES["motherboard"]


def _psu_value(x: dict) -> float:
    watts = x.get("psu_watts")
    if not watts:
        return 0.0
    at_or_below = [w for w in sorted(int(k[:-1]) for k in PSU_VALUES) if w <= watts]
    return PSU_VALUES[f"{at_or_below[-1]}w"] if at_or_below else 0.0


def _cooler_value(x: dict) -> float:
    if x.get("cooler") == "aio":
        return COOLER_VALUES.get(f"{x.get('cooler_aio_mm')}mm aio", COOLER_VALUES["aio cooler"])
    if x.get("cooler") == "air":
        return COOLER_VALUES["cpu cooler"]
    return 0.0


def value_extraction(x: dict) -> tuple:
    """(estimated value, reason) — value is None when the listing should be skipped outright."""
    kind = x.get("listing_type")
    if kind in ("laptop", "multi_item_post", "other"):
        return None, kind
    for flag in ("damaged", "trade_only", "shipping_only"):
        if x.get(flag):
            return None, flag

    gpu_val, gpu_key = _gpu_value(x)
    cpu_val, cpu_key = _cpu_value(x)
    parts = " + ".join(p for p in (gpu_key, cpu_key) if p)

    if kind == "gpu_only":
        return (gpu_val or None), parts or "no priced GPU"
    if kind == "full_pc":
        if x.get("gpu_missing"):
            return None, "gpu_missing"
        if not gpu_val:
            return None, "full PC without a priced GPU"
        value = (gpu_val + cpu_val + _CHASSIS_VALUE + _ram_value(x, assume_16gb=True) + _storage_value(x)
                 + _motherboard_value(x) + _psu_value(x) + _cooler_value(x))
        return value, f"pc: {parts}"
    # parts_bundle / single_part — only what's named: no case, no assumed RAM
    value = (gpu_val + cpu_val + _ram_value(x, assume_16gb=False) + _storage_value(x)
             + _motherboard_value(x) + _psu_value(x) + _cooler_value(x))
    return (value or None), f"{kind}: {parts}" if parts else kind


def verdict(extraction: dict, price: float) -> dict:
    """Would the shadow approach alert? Margin rule: margin >= SHADOW_MIN_MARGIN and profit >= SHADOW_MIN_PROFIT_USD."""
    value, reason = value_extraction(extraction)
    if value is None:
        return {"alert": False, "est": None, "profit": None, "margin": None, "reason": reason}
    profit = value - price
    margin = profit / value
    return {
        "alert":  profit >= SHADOW_MIN_PROFIT_USD and margin >= SHADOW_MIN_MARGIN,
        "est":    round(value, 2),
        "profit": round(profit, 2),
        "margin": round(margin, 3),
        "reason": reason,
    }


# ---------------------------------------------------------------------------
# Shadow run inside the live scan — never touches alerts
# ---------------------------------------------------------------------------
_cycle = {"calls": 0, "seen": 0, "both": 0, "current_only": 0, "haiku_only": 0, "unread": 0}
_last_logged: dict = {}   # listing_key -> (price, current_alert, haiku_alert), so the log only grows on change


def begin_cycle() -> None:
    for k in _cycle:
        _cycle[k] = 0


def shadow_check(listing: dict, score_data: dict, current_alert: bool) -> None:
    """
    Reads the listing with Haiku (cached; at most SHADOW_EXTRACTION_MAX_CALLS_PER_CYCLE
    new reads per cycle, the rest wait for the next cycle) and logs both verdicts.
    """
    global _disabled_noted
    if not SHADOW_EXTRACTION_ENABLED or not ANTHROPIC_API_KEY:
        if not _disabled_noted:
            print("ℹ️  Shadow extraction off (SHADOW_EXTRACTION_ENABLED=false or no ANTHROPIC_API_KEY).")
            _disabled_noted = True
        return

    _cycle["seen"] += 1
    allow_call = _cycle["calls"] < SHADOW_EXTRACTION_MAX_CALLS_PER_CYCLE
    cached     = extract_cached(listing, allow_call=False)
    extraction = cached if cached is not None else (extract_cached(listing) if allow_call else None)
    if cached is None and allow_call:
        _cycle["calls"] += 1
    if extraction is None:
        _cycle["unread"] += 1
        return

    price = float(listing.get("price") or 0)
    shadow = verdict(extraction, price)
    bucket = ("both" if current_alert and shadow["alert"] else
              "current_only" if current_alert else
              "haiku_only" if shadow["alert"] else None)
    if bucket:
        _cycle[bucket] += 1

    key   = listing_key(listing)
    state = (price, bool(current_alert), shadow["alert"])
    if _last_logged.get(key) == state:
        return
    _last_logged[key] = state
    record = {
        "time":        time.strftime("%Y-%m-%dT%H:%M:%S"),
        "ts":          time.time(),
        "key":         key,
        "platform":    listing.get("platform", ""),
        "listing_id":  listing.get("listing_id", ""),
        "url":         listing.get("url", ""),
        "title":       listing.get("title", ""),
        "description": listing.get("description", ""),
        "price":       price,
        "current": {
            "alert":    bool(current_alert),
            "score":    score_data.get("total_score"),
            "est":      score_data.get("estimated_value"),
            "profit":   score_data.get("predicted_profit"),
            "category": listing.get("category_override"),
        },
        "haiku":      shadow,
        "extraction": extraction,
    }
    try:
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(SHADOW_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:
        print(f"⚠️  Could not write shadow log: {exc}")
    if shadow["alert"] != bool(current_alert):
        who = "Haiku only" if shadow["alert"] else "current only"
        print(f"   🕵️  Shadow disagrees ({who}): {listing.get('title', '')[:45]} | "
              f"haiku {shadow['reason']} est {shadow['est']} margin {shadow['margin']}")


def cycle_summary() -> None:
    if not SHADOW_EXTRACTION_ENABLED or not ANTHROPIC_API_KEY or not _cycle["seen"]:
        return
    print(f"🕵️  Shadow: {_cycle['seen']} listings, {_cycle['calls']} new Haiku read(s), "
          f"{_cycle['unread']} waiting for a later cycle | would alert — both: {_cycle['both']}, "
          f"current only: {_cycle['current_only']}, Haiku only: {_cycle['haiku_only']}")
