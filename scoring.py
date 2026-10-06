"""
scoring.py
==========
The scoring brain of the PC Flip Scanner.
UPDATED: June 18, 2026

Changes in this version:
- DDR4/DDR5/default RAM detection — no longer a flat $/GB rate
- Motherboard chipset detection (z790, b650, x570, etc.)
- PSU wattage detection (850w, 1000w, etc.)
- CPU cooler/AIO detection (360mm AIO, noctua, etc.)
- FULL_SYSTEM_CHASSIS_FLAT_RATE reduced to $75 since components now tracked
- Laptop detection: title/description mentioning laptop keywords now
  applies a hard score cap and profit penalty — laptop GPUs are not desktop flips
- Mining card detection: "mining", "eth", "ethereum" in text penalizes GPU value
- GPU shorthand expansion: "3060" → "rtx 3060", hyphen normalization "rtx-4060"
- All previous fixes retained: bundle price override, full-system extraction,
  PC-context CPU/GPU estimation, scam keyword blocking, legacy platform penalties
"""

import re

from baseline_values import (
    GPU_VALUES,
    OUTDATED_GPU_KEYWORDS,
    CPU_VALUES,
    OUTDATED_CPU_KEYWORDS,
    RAM_VALUES_PER_GB_DDR5,
    RAM_VALUES_PER_GB_DDR4,
    RAM_VALUES_PER_GB_DEFAULT,
    STORAGE_VALUES,
    MOTHERBOARD_VALUES,
    PSU_VALUES,
    COOLER_VALUES,
)
from config import (
    SCORE_WEIGHTS,
    FULL_MARGIN_PROFIT_RATIO,
    VAGUE_LEAD_GPU_VALUE_THRESHOLD,
    VAGUE_LEAD_PRICE_CEILING_MULTIPLIER,
)


# ---------------------------------------------------------------------------
# GPU shorthand normalizer
# ---------------------------------------------------------------------------
_GPU_SHORTHAND_RE = re.compile(
    r'\b(5090|5080|5070|5060|5050'
    r'|4090|4080|4070|4060'
    r'|3090|3080|3070|3060|3050'
    r'|2080|2070|2060'
    r'|1080|1070|1660|1650)\b'
)
_GPU_PREFIX_MAP = {
    "5090": "rtx", "5080": "rtx", "5070": "rtx", "5060": "rtx", "5050": "rtx",
    "4090": "rtx", "4080": "rtx", "4070": "rtx", "4060": "rtx",
    "3090": "rtx", "3080": "rtx", "3070": "rtx", "3060": "rtx", "3050": "rtx",
    "2080": "rtx", "2070": "rtx", "2060": "rtx",
    "1080": "gtx", "1070": "gtx", "1660": "gtx", "1650": "gtx",
}

# Office / workstation PC brand names whose model numbers collide with GPU numbers.
# e.g. "Dell OptiPlex 3070" contains "3070" but it is NOT an RTX 3070.
# Checked in a wider 15-char lookback window before expanding any bare number.
_OFFICE_PC_CONTEXT = [
    "optiplex", "thinkcentre", "elitedesk", "prodesk", "thinkstation",
]


def _expand_gpu_shorthand(text: str, price: float = 0.0) -> str:
    """
    Expands bare GPU numbers to full names only when not already prefixed.
    Also normalizes hyphens: "rtx-4060" → "rtx 4060".

    Guard against office PC model numbers (OptiPlex 3070, EliteDesk 5070, etc.)
    by checking a wider 15-char window for known office PC brand names before
    expanding. Without this, "OptiPlex 3070" falsely becomes "OptiPlex rtx 3070".

    Guard against the listing's own asking price (e.g. "$1080 OBO") colliding
    with a GPU shorthand number like "1080" — if the bare number matches the
    listing price exactly, treat it as a price mention, not a GPU model.
    """
    text = text.replace("rtx-", "rtx ").replace("gtx-", "gtx ").replace("rx-", "rx ")

    text = re.sub(r'(\d)(ti|super|xt|gre)\b', r'\1 \2', text)

    price_int = int(round(price)) if price and price > 0 else None

    def _replace(m):
        num = m.group(1)
        prefix = _GPU_PREFIX_MAP.get(num, "rtx")
        start = m.start()
        # Skip if this bare number is actually the listing's asking price
        if price_int is not None and int(num) == price_int:
            return m.group(0)
        # Short lookback: skip if a GPU prefix is already written immediately before
        preceding_short = text[max(0, start - 4):start].lower()
        if "rtx" in preceding_short or "gtx" in preceding_short:
            return m.group(0)
        # Wide lookback: skip if this number is part of an office PC model name
        preceding_wide = text[max(0, start - 15):start].lower()
        if any(model in preceding_wide for model in _OFFICE_PC_CONTEXT):
            return m.group(0)
        return f"{prefix} {num}"

    return _GPU_SHORTHAND_RE.sub(_replace, text)


# ---------------------------------------------------------------------------
# AMD GPU shorthand normalizer
#
# NVIDIA's shorthand expansion above has no AMD equivalent, so listings like
# "7800XT" or "7900 GRE" — written without "RX" or "AMD" anywhere — were never
# detected at all. This is a bigger miss than the NVIDIA case: those fell
# back to a cheaper key, these score at the floor as if no GPU was found.
# ---------------------------------------------------------------------------
_AMD_SHORTHAND_RE = re.compile(
    r'\b(9070|9060'
    r'|7900|7800|7700|7600'
    r'|6950|6900|6800|6750|6700|6650|6600|6500|6400|6300'
    r'|5700|5600|5500'
    r'|580|570)\b'
)

# Numbers that double as common DDR4/DDR5 marketing speeds (e.g. "DDR5-5600",
# "DDR5-6400"). Without a guard, "32GB DDR5 5600MHz" would misread as an
# RX 5600 GPU and corrupt the whole valuation.
_AMD_RAM_COLLISION_NUMBERS = {"5600", "6400", "6600", "5700"}
_RAM_CONTEXT_SIGNALS = ["mhz", "ddr4", "ddr5", "cl30", "cl32", "cl34", "cl36", "cl38", "cl40"]

# AMD's own CPU model numbers collide with its GPU shorthand digits too —
# e.g. "Ryzen 5 7600" (a CPU) vs "RX 7600" (a GPU). Skip expansion when a
# CPU brand name immediately precedes the number.
_AMD_CPU_CONTEXT_SIGNALS = ["ryzen", "threadripper", "epyc"]


def _expand_amd_shorthand(text: str, price: float = 0.0) -> str:
    """
    Expands bare AMD Radeon numbers to full names, mirroring
    _expand_gpu_shorthand() but for AMD's numbering scheme. Skips numbers
    already prefixed with 'rx'/'radeon', applies a RAM-speed collision
    guard for numbers that also appear as common DDR4/DDR5 speeds, skips
    the number if it matches the listing's own asking price (e.g. "$580
    firm" colliding with "rx 580"), and skips numbers immediately preceded
    by an AMD CPU brand name — Ryzen 7000-series model numbers (7600,
    7700, 7800, 7900) and some Ryzen 5000-series ones (5500, 5600, 5700)
    collide exactly with AMD's own GPU shorthand digits, e.g. "Ryzen 5
    7600" would otherwise be misread as mentioning an "RX 7600" GPU.
    """
    price_int = int(round(price)) if price and price > 0 else None

    def _replace(m):
        num = m.group(1)
        start, end = m.start(), m.end()

        if price_int is not None and int(num) == price_int:
            return m.group(0)

        preceding_short = text[max(0, start - 8):start].lower()
        if "rx" in preceding_short or "radeon" in preceding_short:
            return m.group(0)

        preceding_cpu = text[max(0, start - 16):start].lower()
        if any(sig in preceding_cpu for sig in _AMD_CPU_CONTEXT_SIGNALS):
            return m.group(0)

        if num in _AMD_RAM_COLLISION_NUMBERS:
            window = text[max(0, start - 12):min(len(text), end + 12)].lower()
            if any(sig in window for sig in _RAM_CONTEXT_SIGNALS):
                return m.group(0)

        return f"rx {num}"

    return _AMD_SHORTHAND_RE.sub(_replace, text)


# ---------------------------------------------------------------------------
# GPU match disambiguation — picks the right GPU when a listing mentions
# more than one model, e.g. "Previously used with an RTX 4080 Super, now
# selling just this RTX 3070, $180". Without this, GPU detection took
# whichever GPU_VALUES key was longest and appeared anywhere in the text,
# regardless of whether it was the item actually for sale.
# ---------------------------------------------------------------------------
_SALE_INTENT_PHRASES = [
    "selling", "for sale", "asking", "obo", "or best offer",
    "firm price", "price is", "priced at", "$",
]
_PRIOR_OWNERSHIP_PHRASES = [
    "upgraded from", "previously had", "previously used", "used to have",
    "had a", "replaced my", "replaced with", "used with my old",
    "old card", "my old", "sold my old", "no longer need",
]


def _find_gpu_candidates(text_lower: str) -> list:
    """
    Finds every distinct GPU mentioned in the text. Longest match wins for
    overlapping spans (e.g. "rtx 3080 ti" claims that span so bare
    "rtx 3080" isn't also counted as a second, separate mention). Returns
    (gpu_name, start_index) tuples in left-to-right reading order.
    """
    candidates = []
    claimed = []

    def _is_claimed(start, end):
        return any(start < c_end and end > c_start for c_start, c_end in claimed)

    for gpu_name in sorted(GPU_VALUES, key=len, reverse=True):
        start = 0
        while True:
            idx = text_lower.find(gpu_name, start)
            if idx == -1:
                break
            end = idx + len(gpu_name)
            if not _is_claimed(idx, end):
                candidates.append((gpu_name, idx))
                claimed.append((idx, end))
            start = idx + 1

    candidates.sort(key=lambda c: c[1])
    return candidates


def _nearest_phrase_distance(text_lower: str, pos: int, phrases: list) -> int:
    """Character distance from `pos` to the closest occurrence of any phrase."""
    best = None
    for phrase in phrases:
        start = 0
        while True:
            idx = text_lower.find(phrase, start)
            if idx == -1:
                break
            dist = abs(idx - pos)
            if best is None or dist < best:
                best = dist
            start = idx + 1
    return best if best is not None else 100_000


def _select_best_gpu_match(text_lower: str, price: float = 0.0):
    """
    Returns the GPU name most likely to be the item actually for sale.
    Preference order:
      1. Closer to sale-intent language ("selling", "asking $X", "$price")
         than to prior-ownership language ("upgraded from", "previously had")
      2. Whichever model's baseline value is closest to the listing's
         actual asking price
      3. Longest string match (the previous, sole heuristic)
    With only one GPU mentioned, that's returned directly — the
    disambiguation logic only matters when there's more than one candidate.
    """
    candidates = _find_gpu_candidates(text_lower)
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0][0]

    scored = []
    for gpu_name, pos in candidates:
        sale_dist  = _nearest_phrase_distance(text_lower, pos, _SALE_INTENT_PHRASES)
        prior_dist = _nearest_phrase_distance(text_lower, pos, _PRIOR_OWNERSHIP_PHRASES)
        context_score = sale_dist - prior_dist
        price_gap = abs(GPU_VALUES[gpu_name] - price) if price and price > 0 else 0
        scored.append((context_score, price_gap, -len(gpu_name), gpu_name))

    scored.sort()
    return scored[0][3]


# ---------------------------------------------------------------------------
# Laptop detection — hard guard before scoring begins
# ---------------------------------------------------------------------------
_LAPTOP_SIGNALS = [
    "laptop", "notebook", "gaming laptop", "gaming notebook",
    "portable", "thunderbolt", "ultrabook", "chromebook",
    # Laptop-specific screen size notation (desktop monitors say "27 inch", not "15.6")
    "15.6", "15.6\"", "17.3", "13.3", "14 inch display", "15 inch display",
]

_LAPTOP_GPU_SIGNALS = [
    "max-q", "max-p", "mobile gpu", "laptop gpu", "notebook gpu",
    "gddr6 laptop", "gddr5 laptop",
]

# Specific laptop chassis names that never appear on desktop products
_LAPTOP_CHASSIS_SIGNALS = [
    "razer blade",
    "lenovo legion", "lenovo ideapad gaming",
    "acer nitro 5", "acer predator helios",
    "hp omen", "hp victus",
    "dell g15", "dell g16", "dell g17",
    "asus rog zephyrus", "asus tuf f15", "asus tuf f17", "asus tuf gaming a15",
    "msi stealth", "msi katana",
    # Alienware laptop lines — "Alienware 15/17" are laptops; Aurora is desktop
    "alienware 15", "alienware 17", "alienware m15", "alienware m17",
    "alienware x14", "alienware x15", "alienware x17",
]

# Mobile CPU suffix regex — Intel/AMD mobile processors end in HK/HQ/HX/H/U/HS
# e.g. i7-6820HK, i9-12900HX, Ryzen 7 5800H, i5-8250U
_MOBILE_CPU_RE = re.compile(r'\b\d{4,5}(hk|hq|hx|hs|hu|h|u)\b', re.IGNORECASE)

# Trade/swap listings — seller wants to barter, not sell for cash
_TRADE_SIGNALS = [
    "trade only", "trades only", "open to trades", "willing to trade",
    "looking to trade", "want to trade", "swap for", "swap only",
    "wtt ",   # "Want To Trade" abbreviation common on marketplace
]


def _is_laptop_listing(text_lower: str) -> bool:
    """
    Returns True if the text strongly suggests a laptop rather than a
    desktop GPU or complete gaming PC. Catches:
    - Keyword signals ("laptop", "notebook", etc.)
    - Laptop chassis model names ("Razer Blade", "Alienware 15", etc.)
    - Mobile CPU suffixes (HK/HQ/HX/H/U in model numbers like 6820HK, 5800H)
    """
    if any(sig in text_lower for sig in _LAPTOP_SIGNALS + _LAPTOP_GPU_SIGNALS):
        return True
    if any(sig in text_lower for sig in _LAPTOP_CHASSIS_SIGNALS):
        return True
    if _MOBILE_CPU_RE.search(text_lower):
        return True
    return False


def _is_trade_listing(title_lower: str) -> bool:
    """
    Returns True if the listing title indicates a trade/swap rather than a
    cash sale. Facebook sellers often prefix titles with 'TRADE' to signal
    they want to barter — these can't be flipped.
    """
    return (
        title_lower.startswith("trade ")
        or title_lower.startswith("wtt ")
        or any(sig in title_lower for sig in _TRADE_SIGNALS)
    )


# ---------------------------------------------------------------------------
# Component bundle detection — partial component sets mislabelled as full
# systems. Phrases that strongly indicate the seller is listing loose parts
# rather than a complete, ready-to-use PC.
#
# NOTE: "recently upgraded" intentionally excluded — commonly means
# "I upgraded my PC and am selling the old one" (a legitimate full system
# sale), so including it produces too many false negatives on real deals.
# ---------------------------------------------------------------------------
_COMPONENT_BUNDLE_SIGNALS = [
    "not an entire pc",
    "not a full pc",
    "parts only",
    "bundle only",
    "pulled from my gaming pc",
    "pulled from pc",
    "cpu + gpu",
    "gpu + cpu",
    "cpu + motherboard",
    "gpu + motherboard",
    "motherboard bundle",
    "parts combo",
    "parts bundle",
    "+ motherboard",     # title ending in "+ [brand] motherboard" = parts sale
    "cpu and gpu and",
    "no case",
    "no power supply",
    "missing psu",
    "no psu included",
    # Explicit "PC not included" — seller is listing a component only, not a full system
    "pc not included",
    "computer not included",
    "system not included",
    "tower not included",
]


def _is_component_bundle(text_lower: str) -> bool:
    """
    Returns True if the listing is a partial component bundle rather than a
    complete PC. Prevents 'Ryzen 5 2600 + RTX 2060 + B450M-A motherboard'
    type listings from scoring as full systems and alerting in the wrong channel.
    Two checks:
      1. Explicit phrase signals ("cpu + gpu", "parts only", etc.)
      2. Title ending with "motherboard" — dead giveaway it's a parts sale,
         since a complete PC listing names the chipset/brand, not the word
         "motherboard" itself (e.g. "Z690" not "Z690 motherboard").
    """
    if any(sig in text_lower for sig in _COMPONENT_BUNDLE_SIGNALS):
        return True
    # "... + ASUS PRIME B450M-A motherboard" ends with "motherboard"
    title_stripped = text_lower.rstrip(".!? ")
    if title_stripped.endswith("motherboard"):
        return True
    return False


# ---------------------------------------------------------------------------
# GPU-missing full-system detection — "gaming PC" listings that explicitly
# say the GPU isn't included. Before this guard, CPU+RAM+storage value
# alone was enough to clear the Full Systems alert threshold (seen in
# production: a listing stating its RTX 4090 had been pulled before
# listing still scored 85/100 with a $2884 predicted profit, because
# nothing told the valuator the headline component wasn't there). A system
# with no dedicated GPU isn't really a flippable "gaming PC" regardless of
# how strong the rest of the spec sheet is.
# ---------------------------------------------------------------------------
_GPU_MISSING_SIGNALS = [
    "without gpu", "without a gpu", "without graphics card", "without a graphics card",
    "no gpu", "no graphics card", "no dedicated gpu", "no dedicated graphics card",
    "gpu not included", "graphics card not included",
    "gpu removed", "graphics card removed", "gpu was removed",
    "missing gpu", "missing graphics card",
    "gpu sold separately", "graphics card sold separately",
]


def _is_gpu_missing_system(text_lower: str) -> bool:
    """Returns True if the listing explicitly states the GPU isn't included."""
    return any(sig in text_lower for sig in _GPU_MISSING_SIGNALS)


# ---------------------------------------------------------------------------
# Mining card detection — penalizes GPU value for suspected mining use
# ---------------------------------------------------------------------------
_MINING_SIGNALS = [
    "mining", "mined", "crypto", "ethereum", "eth mining",
    "bitcoin", "rig", "hashrate", "hash rate", "dag size",
    "mining bios", "mining mod",
]


def _is_mining_card(text_lower: str) -> bool:
    return sum(1 for sig in _MINING_SIGNALS if sig in text_lower) >= 2


# ---------------------------------------------------------------------------
# Peripheral-only listing detection — disqualifies headsets, mics, keyboards,
# mice. Checked against the TITLE only (not the description) and first,
# ahead of every other guard — these are never GPU/PC flips regardless of
# what else the listing text happens to mention.
# ---------------------------------------------------------------------------
_PERIPHERAL_TITLE_SIGNALS = [
    "headset", "microphone", "keyboard", "mouse", "gamepad", "controller",
    "joystick", "webcam", "speaker", "adapter", "cable", "monitor",
    "video games", "pc games", "games bundle", "disc bundle",
]


def _is_peripheral_listing(title_lower: str) -> bool:
    return any(sig in title_lower for sig in _PERIPHERAL_TITLE_SIGNALS)


def is_junk_category_listing(title: str) -> bool:
    """
    Public wrapper around the peripheral-title check, for callers outside
    this module (main.py) that want to drop obvious non-GPU/PC accessory
    listings — mics, cables, adapters, disc bundles — before they reach any
    price-based filtering. A $10 USB microphone parsed correctly at $10 is
    not a price-parse failure; it's just not what this bot is looking for,
    and logging it as one pollutes that log's actual signal.
    """
    return _is_peripheral_listing((title or "").lower())


# ---------------------------------------------------------------------------
# GPU parts-only detection — disqualifies heatsinks, water blocks, shrouds
# ---------------------------------------------------------------------------
_GPU_PARTS_ONLY_SIGNALS = [
    # "Only" variants
    "heatsink only", "shroud only", "cooler only", "backplate only",
    "water block only", "waterblock only", "fans only", "fan only",
    # Water block brands / phrases
    "ek-quantum", "ek quantum", "ekwb",
    "gpu water block", "gpu waterblock", "gpu water cooling",
    # Explicit component-only phrasing
    "gpu shroud", "gpu heatsink", "gpu fans",
    "just the heatsink", "just the shroud", "just the cooler",
    "no pcb", "pcb only",
]


def _is_gpu_parts_only(text_lower: str) -> bool:
    """Returns True if the listing is for a GPU component rather than a whole card."""
    return any(sig in text_lower for sig in _GPU_PARTS_ONLY_SIGNALS)


# ---------------------------------------------------------------------------
# Damaged / defective item detection — disqualifies broken hardware
# ---------------------------------------------------------------------------
_DAMAGED_ITEM_SIGNALS = [
    # Artifacting
    "artifacting", "gpu artifacts", "displays artifacts", "visual artifacts",
    "image artifacts", "has artifacts",
    # Pin / connector damage
    "bent pins", "bent pin", "damaged pins", "broken pins",
    "missing pins",
    # No video / signal
    "no video output", "no display output", "no video signal",
    "no signal output", "no hdmi output", "no display signal",
    # System / functional failure
    "for parts or not working", "for parts or repair", "for parts/not working",
    "for parts / not working",
    # Bare phrases — just as common in real listings as the compound ones
    # above ("RTX 3080 - For Parts - $60", "Card not working, as-is"), and
    # previously only hit a soft -3 in RED_FLAG_PHRASES instead of a hard
    # disqualification, letting broken hardware score as if it were fine.
    "for parts", "not working", "no longer working", "stopped working",
    "doesn't work", "does not work", "not functional", "non functional",
    "needs repair", "repair needed", "needs to be repaired", "need repair",
    "dead on arrival", "doa",
    "won't boot", "will not boot", "won't post", "will not post",
    "no post", "fails to post",
    "cpu died", "gpu died", "card died", "died on me",
    # "Dead" as a standalone descriptor in title context
    "**dead**", "- dead", "(dead)", "is dead", "gone dead",
    # Physical / liquid damage
    "water damaged", "liquid damaged", "spill damage",
    "burn marks", "burnt component", "scorched",
]


def _is_damaged_item(text_lower: str) -> bool:
    """Returns True if the listing clearly describes broken or defective hardware."""
    return any(sig in text_lower for sig in _DAMAGED_ITEM_SIGNALS)


# ---------------------------------------------------------------------------
# Shipping-only detection — DMV Flip Alerts targets local in-person pickup
# ---------------------------------------------------------------------------
_SHIPPING_ONLY_HARD_SIGNALS = [
    "ships nationwide", "ships to all 50", "shipping nationwide",
    "no local pickup", "pickup not available",
    "online sale only", "online only",
    "ship only", "shipping only",
    # Broadened from the original narrow list — real ship-first/ship-only
    # listings (a classic FB Marketplace scam shape: too-good price + no
    # verifiable local meetup) were slipping through by just saying "I can
    # ship it" instead of one of the exact original hard-signal phrases.
    "will ship", "can ship", "i can ship", "i will ship", "able to ship",
    "happy to ship", "shipping available", "will only ship",
    "ship it to you", "ship anywhere", "i only ship",
]

_LOCAL_PICKUP_POSITIVE_SIGNALS = [
    "local pickup", "local pick up", "local only", "no shipping",
    "pickup only", "pick up only", "in person only",
    "cash in person", "meet in person", "meet locally",
    # Broadened alongside the hard-signal list above so a listing that
    # genuinely offers real local meetup isn't wrongly disqualified just
    # because it also mentions shipping as a secondary option.
    "can meet", "meet up", "meetup", "pickup available", "pickup in",
    "local to", "will meet", "can pickup", "can pick up",
]


_NEGATION_PREFIXES = ("no ", "not ", "n't ", "without ", "isn't ", "isnt ")


def _has_unnegated_signal(text_lower: str, signals: list) -> bool:
    """
    True if any signal phrase appears without an immediately-preceding
    negation word. Plain substring matching would let "no local pickup"
    satisfy "local pickup" as if it were a *positive* signal — the exact
    opposite of what the seller wrote — silently defeating that hard
    signal every time. Checking a short window before each match for a
    negation word fixes that without needing real NLP.
    """
    for sig in signals:
        start = 0
        while True:
            idx = text_lower.find(sig, start)
            if idx == -1:
                break
            window = text_lower[max(0, idx - 12):idx]
            if not any(window.endswith(neg) or neg in window for neg in _NEGATION_PREFIXES):
                return True
            start = idx + 1
    return False


def _is_shipping_only(text_lower: str) -> bool:
    """
    Returns True if the listing is shipping-only (not a local DMV pickup deal).
    Checks hard shipping signals first; if the seller also mentions local pickup
    anywhere in the text (and doesn't negate it — "no local pickup" doesn't
    count), we treat it as a local deal and let it through.
    """
    if _has_unnegated_signal(text_lower, _SHIPPING_ONLY_HARD_SIGNALS):
        # Still allow through if they explicitly offer local pickup too
        if _has_unnegated_signal(text_lower, _LOCAL_PICKUP_POSITIVE_SIGNALS):
            return False
        return True
    return False


# ---------------------------------------------------------------------------
# RAM value helper — DDR4/DDR5/default aware
# ---------------------------------------------------------------------------
def _ram_value_per_gb(text_lower: str) -> float:
    """Returns the correct $/GB rate based on DDR generation detected in text."""
    if "ddr5" in text_lower:
        return RAM_VALUES_PER_GB_DDR5
    elif "ddr4" in text_lower:
        return RAM_VALUES_PER_GB_DDR4
    else:
        return RAM_VALUES_PER_GB_DEFAULT


# ---------------------------------------------------------------------------
# RAM-vs-VRAM disambiguation — a "<number>GB" right after a GPU model name
# (e.g. "RTX 3080 12GB", "RX 7900 XTX 24GB") is the card's VRAM capacity,
# not a separate stick of system RAM. The storage-suffix exclusion alone
# doesn't catch this since VRAM callouts aren't followed by "ssd"/"nvme"/etc.
# ---------------------------------------------------------------------------
_RAM_GB_RE = re.compile(
    r"\b(\d{1,3})\s*gb(?!\s*(?:ssd|nvme|hdd|m\.2|pcie|nand|solid.state|flash|hard\s*drive))\b"
)


def _looks_like_vram_mention(text_lower: str, match_start: int, match_end: int) -> bool:
    """
    Returns True if the "<number>GB" match describes GPU VRAM rather than
    system RAM.

    A number closely followed by "ram"/"ddr4"/"ddr5"/"memory" is treated as
    real system RAM regardless of what precedes it — this covers listings
    like "RTX 4070, 32GB DDR5 RAM" where a comma-separated GPU mention
    happens to come right before an entirely separate RAM clause.

    Otherwise, if the number immediately follows a known GPU model name
    (allowing a comma/colon/dash separator, e.g. "RTX 3080 12GB" or
    "RTX 4070 Super, 12GB"), it's describing that card's VRAM.
    """
    following = text_lower[match_end:match_end + 15]
    if any(kw in following for kw in ("ram", "ddr4", "ddr5", "memory")):
        return False
    # Look back a bit further than the GPU name itself: board-partner words
    # ("Challenger", "Ultra", "FTW3") often sit between the model and the VRAM
    # figure, e.g. "RX 6700 XT Challenger 12GB" — that 12GB is still VRAM.
    # Only a short gap of card/partner words counts. A connecting phrase
    # ("with", "and", "pc") means the figure belongs to the system, not the card.
    preceding = text_lower[max(0, match_start - 40):match_start]
    for gpu_name in GPU_VALUES:
        idx = preceding.rfind(gpu_name)
        if idx == -1:
            continue
        gap = preceding[idx + len(gpu_name):].strip(" ,:-–—/")
        if len(gap) <= 25 and not any(w in gap.split() for w in ("with", "and", "pc", "system", "build")):
            return True
    return False


def _extract_ram_gb(text_lower: str):
    """
    Finds the first "<number>GB" match that represents system RAM rather
    than storage capacity or GPU VRAM. Returns the re.Match or None.
    """
    for m in _RAM_GB_RE.finditer(text_lower):
        if _looks_like_vram_mention(text_lower, m.start(), m.end()):
            continue
        return m
    return None


# ---------------------------------------------------------------------------
# Cooler brand disambiguation — "noctua" and "be quiet" also make case fans
# (Noctua) and power supplies ("be quiet!"), so the bare brand name alone
# isn't reliable evidence of a CPU cooler. Specific model keys (e.g.
# "noctua nh-d15", "be quiet dark rock pro") are unambiguous and skip this
# check entirely.
# ---------------------------------------------------------------------------
_AMBIGUOUS_COOLER_BRAND_KEYS = {"noctua", "be quiet"}
_COOLER_CONTEXT_QUALIFIERS = [
    "cooler", "aio", "liquid", "air cooler", "heatsink", "cpu cooler",
    "nh-", "dark rock",
]


def _has_cooler_context(text_lower: str, cooler_kw: str, match_pos: int) -> bool:
    if cooler_kw not in _AMBIGUOUS_COOLER_BRAND_KEYS:
        return True
    window = text_lower[max(0, match_pos - 20):match_pos + len(cooler_kw) + 20]
    return any(qualifier in window for qualifier in _COOLER_CONTEXT_QUALIFIERS)


# ---------------------------------------------------------------------------
# Live eBay price lookup — overrides static baseline for GPU valuation
# ---------------------------------------------------------------------------
def _get_live_gpu_value(gpu_name: str, static_fallback: float) -> tuple:
    """
    Tries eBay sold comps for a real median price on this GPU model.
    Returns (value, is_live). Falls back to static_fallback silently if:
      - EBAY_CLIENT_ID is not configured
      - The API call fails or returns no data
      - The live price is outside 0.25x-3x of baseline (outlier guard)
    Results are cached 1 hour in ebay_sold_comps.py — free after first hit.
    """
    try:
        from ebay_sold_comps import get_sold_median_price
        live = get_sold_median_price(gpu_name)
        if live and live > 0:
            if static_fallback > 0:
                ratio = live / static_fallback
                if 0.25 <= ratio <= 3.0:
                    return round(live), True
    except Exception:
        pass
    return static_fallback, False


# ---------------------------------------------------------------------------
# PILLAR 1 — Part Quality & Valuation (40 points)
# ---------------------------------------------------------------------------
_SCAM_KEYWORDS = [
    "box only", "box-only", "no gpu", "gpu missing", "not an", "not a",
    "adapter cable", "power cable", "pcie cable", "12vhpwr", "16-pin cable",
    "empty box", "accessories only",
]

_FULL_SYSTEM_CHASSIS_FLAT_RATE = 75.0  # case + fans + cables + miscellaneous hardware (cooler tracked separately below)

# ---------------------------------------------------------------------------
# Legacy Intel CPU detection (2nd-9th gen Core i3/i5/i7/i9, ~2011-2019).
# Matches "i5-6500T", "i5 6400", "i7-2600", "i7-9700k" etc. regardless of
# hyphen/space formatting — real listing titles are inconsistent about this.
# The single-digit generation prefix (2-9) is what makes this regex safe:
# 10th-gen-and-newer model numbers ("i7-10700k", "i9-13900k") use a two-digit
# generation prefix, so they structurally can't match here.
#
# 9th-gen-and-older Intel Core chips are treated as heavily devalued
# regardless of which specific CPU_VALUES entry they'd otherwise match —
# calculate_deal_score() also hard-caps the whole listing's score below the
# alert threshold for these, since even a strong GPU/margin pairing on
# ancient Intel silicon isn't worth chasing.
# ---------------------------------------------------------------------------
_LEGACY_INTEL_GEN_RE = re.compile(r'\bi([3579])[\s-]?([2-9])\d{3}[a-z]*\b', re.IGNORECASE)


def _is_legacy_intel_cpu(text_lower: str) -> bool:
    return bool(_LEGACY_INTEL_GEN_RE.search(text_lower))


def score_part_quality_and_valuation(text: str, price: float = 0.0, max_points: int = SCORE_WEIGHTS["part_quality_valuation"]):
    """
    Pillar 1: estimates the used-market value of components mentioned in
    the listing text and converts that value into 0-40 points.

    Component classes detected: GPU, CPU, RAM (DDR4/5-aware), storage
    (capacity + type), motherboard chipset, PSU wattage, and CPU coolers.
    Applies penalties for outdated hardware, mining cards, and mismatched
    component pairs (e.g. high-end GPU with no CPU). `price` (the listing's
    asking price) is used to disambiguate multiple GPU mentions and to
    guard against the price itself colliding with GPU shorthand numbers.
    """
    text_lower = _expand_amd_shorthand(_expand_gpu_shorthand(text.lower(), price), price)
    estimated_value = 0.0
    detected_parts = []
    outdated_hits = 0

    # ── Scam/false-value guard ───────────────────────────────────────────────
    if any(kw in text_lower for kw in _SCAM_KEYWORDS):
        return 0, 0.0, ["⚠️ WARNING: Potential Empty Box or Negative Keyword Match"], False, None

    # ── GPU detection ────────────────────────────────────────────────────────
    # When more than one GPU model is mentioned (e.g. "upgraded from X,
    # selling my old Y"), _select_best_gpu_match picks the one actually
    # for sale instead of just the longest string match anywhere in the text.
    best_gpu_value = 0
    detected_gpu_name = _select_best_gpu_match(text_lower, price)
    if detected_gpu_name:
        static_value        = GPU_VALUES[detected_gpu_name]
        live_value, is_live = _get_live_gpu_value(detected_gpu_name, static_value)
        best_gpu_value      = live_value
        source_tag          = "📡 live" if is_live else "est."
        # Mining card penalty — cut GPU value by 30%
        if _is_mining_card(text_lower):
            best_gpu_value = int(best_gpu_value * 0.70)
            detected_parts.append(f"GPU: {detected_gpu_name.upper()} (MINING RISK ~${best_gpu_value} {source_tag})")
        else:
            detected_parts.append(f"GPU: {detected_gpu_name.upper()} (~${best_gpu_value:.0f} {source_tag})")

    for outdated_kw in OUTDATED_GPU_KEYWORDS:
        if outdated_kw in text_lower:
            outdated_hits += 1
            break

    estimated_value += best_gpu_value

    # ── CPU detection ────────────────────────────────────────────────────────
    best_cpu_value = 0
    is_old_intel = _is_legacy_intel_cpu(text_lower)
    if is_old_intel:
        outdated_hits += 2

    for cpu_name in sorted(CPU_VALUES, key=len, reverse=True):
        if cpu_name in text_lower:
            # Applies whenever is_old_intel matched anywhere in the text, not
            # just when the specific matched key is a bare "i7"/"i5" flat-tier
            # fallback — a specific model entry like "i5-8400" used to slip
            # past this override entirely and keep its normal (higher) value.
            if is_old_intel:
                best_cpu_value = 15.0
                detected_parts.append(f"CPU: LEGACY {cpu_name.upper()} (~$15 — 9th-gen or older)")
            else:
                best_cpu_value = CPU_VALUES[cpu_name]
                detected_parts.append(f"CPU: {cpu_name.upper()} (~${best_cpu_value})")
            break

    # Mutually exclusive with is_old_intel above — that already applies a
    # +2 outdated penalty (and floors the valuation to $15) for legacy Intel
    # i3/i5/i7/i9 chips, which overlap the models OUTDATED_CPU_KEYWORDS'
    # "i3-2".."i7-6" entries match. Without this guard a legacy Intel CPU was
    # penalized twice for the same fact, while GPUs only ever get counted once.
    for outdated_kw in OUTDATED_CPU_KEYWORDS:
        if outdated_kw in text_lower:
            if is_old_intel and outdated_kw.startswith(("i3-", "i5-", "i7-", "i9-")):
                break
            outdated_hits += 1
            break

    estimated_value += best_cpu_value

    # ── RAM detection (DDR4/DDR5-aware) ──────────────────────────────────────
    # Excludes storage capacities ("64GB SSD") and GPU VRAM callouts
    # ("RTX 3080 12GB") — see _extract_ram_gb / _looks_like_vram_mention.
    ram_match = _extract_ram_gb(text_lower)
    if ram_match:
        ram_gb = int(ram_match.group(1))
        if ram_gb <= 64:
            per_gb = _ram_value_per_gb(text_lower)
            ram_value = ram_gb * per_gb
            gen = "DDR5" if "ddr5" in text_lower else ("DDR4" if "ddr4" in text_lower else "")
            label = f"RAM: {ram_gb}GB{' ' + gen if gen else ''} (~${ram_value:.0f})"
            estimated_value += ram_value
            detected_parts.append(label)

    # ── Storage detection ────────────────────────────────────────────────────
    for storage_kw, storage_val in STORAGE_VALUES.items():
        if storage_kw in text_lower:
            estimated_value += storage_val
            detected_parts.append(f"Storage: {storage_kw.upper()} (~${storage_val})")
            break

    # ── Motherboard detection ────────────────────────────────────────────────
    for mobo_kw in sorted(MOTHERBOARD_VALUES, key=len, reverse=True):
        if mobo_kw in text_lower:
            mobo_val = MOTHERBOARD_VALUES[mobo_kw]
            # Only add value if we're not in a standalone GPU listing
            if best_cpu_value > 0 or best_gpu_value == 0:
                estimated_value += mobo_val
                detected_parts.append(f"Mobo: {mobo_kw.upper()} (~${mobo_val})")
            break

    # ── PSU detection ────────────────────────────────────────────────────────
    psu_context = "psu" in text_lower or "power supply" in text_lower or "watt" in text_lower
    if psu_context:
        for psu_kw in sorted(PSU_VALUES, key=len, reverse=True):
            if psu_kw in text_lower:
                psu_val = PSU_VALUES[psu_kw]
                estimated_value += psu_val
                detected_parts.append(f"PSU: {psu_kw.upper()} (~${psu_val})")
                break

    # ── Cooler detection ─────────────────────────────────────────────────────
    for cooler_kw in sorted(COOLER_VALUES, key=len, reverse=True):
        idx = text_lower.find(cooler_kw)
        if idx != -1 and _has_cooler_context(text_lower, cooler_kw, idx):
            cooler_val = COOLER_VALUES[cooler_kw]
            estimated_value += cooler_val
            detected_parts.append(f"Cooler: {cooler_kw.upper()} (~${cooler_val})")
            break

    # ── Standalone GPU check ─────────────────────────────────────────────────
    is_standalone_gpu = (
        best_gpu_value > 200
        and best_cpu_value == 0
        and "motherboard" not in text_lower
        and "mobo" not in text_lower
    )

    # ── Convert value to 0-40 score ──────────────────────────────────────────
    HIGH_QUALITY_VALUE_THRESHOLD = 300 if is_standalone_gpu else 700

    raw_points = min(max_points, (estimated_value / HIGH_QUALITY_VALUE_THRESHOLD) * max_points)

    mismatch_penalty = 0
    if not is_standalone_gpu:
        if best_gpu_value > 250 and best_cpu_value == 0:
            mismatch_penalty += 8
        if best_cpu_value > 150 and best_gpu_value == 0:
            mismatch_penalty += 5

    # Coefficient softened from 6 -> 4 per hit, capped at 16 total: a legacy
    # CPU+GPU pairing was previously able to wipe out nearly half of the
    # 40-point part-quality pillar on its own, which regularly buried
    # otherwise-viable $100+ margin flips (legacy hardware is still cheap to
    # buy and still resells for real money — it just isn't premium). The
    # margin pillar (Pillar 2) already prices legacy parts correctly via
    # baseline_values.py; this penalty only needs to nudge quality scoring,
    # not double-count the same fact against the deal.
    outdated_penalty = min(outdated_hits * 4, 16)
    points = max(0, raw_points - mismatch_penalty - outdated_penalty)

    return min(points, max_points), estimated_value, detected_parts, is_standalone_gpu, detected_gpu_name


# ---------------------------------------------------------------------------
# PILLAR 1b — Hidden full-system spec extraction
# ---------------------------------------------------------------------------
_PC_BUILD_KEYWORDS = [
    "gaming pc", "gaming desktop", "desktop pc", "custom pc", "prebuilt",
    "pc build", "complete pc", "full pc", "computer", "tower", "desktop", " pc ",
    # Generic gaming/custom descriptors (no OEM brands — they live in categories.py only)
    "gaming computer", "gaming tower", "prebuilt pc", "custom build",
]

_LEGACY_GPU_SIGNALS = [
    "hd 6", "hd6", "hd 7", "hd7", "gtx 9", "gtx 7", "gtx 8",
    "gtx970", "gtx 970", "gtx980", "gtx 980", "gtx 750", "r9 ", "r7 ",
    "quadro", "firepro",
]

# ---------------------------------------------------------------------------
# Office / business PC detection — these machines rarely have discrete gaming
# GPUs. If one of these signals fires and no GPU is explicitly detected, skip
# the $180 GPU estimator so the bot doesn't invent phantom value.
# ---------------------------------------------------------------------------
_OFFICE_PC_SIGNALS = [
    "optiplex", "thinkcentre", "elitedesk", "prodesk", "eliteone",
    "thinkpad", "elitebook", "probook", "latitude", "vostro",
    "office pc", "business pc", "corporate pc",
    "mini pc", "nuc", "all-in-one", "all in one",
]

# Signals in the TITLE that indicate a standalone CPU product (e.g. Intel's
# "Gaming Desktop Processor" naming, or plain "i7-8700 Processor").
# If these appear, we skip the GPU estimation step so the listing isn't
# falsely routed to Full Systems.
_STANDALONE_CPU_TITLE_SIGNALS = [
    "desktop processor",
    "gaming desktop processor",
    "unlocked processor",
    "processor only",
    "cpu only",
    "am5 processor",
    "am4 processor",
    "lga processor",
    "box processor",
    "oem processor",
    "tray processor",
    # Broad catch: any title where "processor" is the main subject
    # e.g. "Intel Core i7-8700 Processor – Ideal for Amateur Computer Builders"
    " processor",
]

_FULL_SYSTEM_TITLE_OVERRIDES = [
    "gaming pc", "gaming desktop", "custom pc", "prebuilt",
    "full pc", "complete pc", "gaming computer", "gaming tower",
    "pc build", "custom build", "ibuypower", "cyberpower", "alienware",
]


def _is_standalone_cpu_listing(title_lower: str) -> bool:
    """
    Returns True if the title strongly indicates a standalone CPU product
    rather than a full PC build. Prevents Intel/AMD naming conventions like
    'Core i9-14900KF Gaming Desktop Processor' from triggering the full-system
    GPU estimator and routing the alert to the wrong channel.
    Has no effect if the title also contains explicit full-system language.
    """
    has_cpu_product_signal  = any(s in title_lower for s in _STANDALONE_CPU_TITLE_SIGNALS)
    has_full_system_signal  = any(s in title_lower for s in _FULL_SYSTEM_TITLE_OVERRIDES)
    return has_cpu_product_signal and not has_full_system_signal


# Phrases where a "pc"-type keyword only names where the GPU came FROM
# ("my gaming PC's RTX 4070", "pulled from my desktop") rather than
# indicating the whole system is for sale. Without this, is_pc_context
# fires on the bare substring "pc" and invents a phantom CPU value for a
# GPU-only listing that merely mentions its host machine in passing.
_GPU_SOURCE_CONTEXT_SIGNALS = [
    "pc's", "computer's", "desktop's", "tower's",
    "from my pc", "from my computer", "from my desktop", "from my tower",
    "from my gaming pc", "out of my pc", "out of my gaming pc",
]


def _is_gpu_source_reference(text_lower: str) -> bool:
    return any(sig in text_lower for sig in _GPU_SOURCE_CONTEXT_SIGNALS)


def extract_specs_and_value(title: str, description: str, price: float = 0.0) -> dict:
    """
    Catches full-system builds hiding behind generic titles ("Gaming PC",
    "Desktop Computer") where the spec sheet is buried in the description.
    score_part_quality_and_valuation() misses these because it only gets
    high estimated_value when the title has strong part keywords.

    Detects CPU+GPU pairs, estimates chassis/RAM/storage when not present,
    and signals category_override = "Full Systems" for Discord routing.

    Now also detects motherboard chipset, PSU wattage, and CPU coolers in
    the combined text to give a more accurate system valuation. `price` (the
    listing's asking price) disambiguates multiple GPU mentions and guards
    against the price itself colliding with a GPU shorthand number.
    """
    combined_text = _expand_amd_shorthand(_expand_gpu_shorthand(f"{title} {description}".lower(), price), price)
    detected_parts: list[str] = []

    # ── GPU ──────────────────────────────────────────────────────────────────
    gpu_value = 0.0
    detected_gpu_name = _select_best_gpu_match(combined_text, price)
    if detected_gpu_name:
        gpu_value = GPU_VALUES[detected_gpu_name]
        if _is_mining_card(combined_text):
            gpu_value = int(gpu_value * 0.70)
            detected_parts.append(f"GPU: {detected_gpu_name.upper()} (MINING RISK ~${gpu_value})")
        else:
            detected_parts.append(f"GPU: {detected_gpu_name.upper()} (~${gpu_value})")

    # ── CPU ──────────────────────────────────────────────────────────────────
    cpu_value = 0.0
    is_legacy_intel = _is_legacy_intel_cpu(combined_text)
    for cpu_name in sorted(CPU_VALUES, key=len, reverse=True):
        if cpu_name in combined_text:
            # See score_part_quality_and_valuation()'s matching comment — this
            # must apply for any is_legacy_intel match, not just a bare
            # "i7"/"i5" flat-tier key, or specific old-gen models slip through.
            if is_legacy_intel:
                cpu_value = 15.0
                detected_parts.append(f"CPU: LEGACY {cpu_name.upper()} (~$15 — 9th-gen or older)")
            else:
                cpu_value = CPU_VALUES[cpu_name]
                detected_parts.append(f"CPU: {cpu_name.upper()} (~${cpu_value})")
            break

    # Captured here, before the PC-context estimation below invents a phantom
    # CPU/RAM/storage value — vague-lead detection needs to know what the
    # seller actually wrote, not what we guessed on their behalf.
    had_explicit_cpu     = cpu_value > 0
    had_explicit_ram      = _extract_ram_gb(combined_text) is not None
    had_explicit_storage  = any(kw in combined_text for kw in STORAGE_VALUES)

    # ── PC context estimation when one component is missing ─────────────────
    is_pc_context         = any(kw in combined_text for kw in _PC_BUILD_KEYWORDS)
    has_legacy_signal     = any(sig in combined_text for sig in _LEGACY_GPU_SIGNALS)
    is_office_pc          = any(sig in combined_text for sig in _OFFICE_PC_SIGNALS)

    # ── Vague lead detection ──────────────────────────────────────────────────
    # A "system" listing (gaming PC / desktop / prebuilt context) that names a
    # high-value-tier GPU but gives literally nothing else to go on — no CPU,
    # no RAM, no storage — is exactly the case the deterministic scorer can't
    # value with any confidence. Rather than let it silently score low (or get
    # padded out with the phantom CPU/RAM estimates below, which would hide
    # just how little the seller actually disclosed), flag it so notifier.py
    # can route it to a dedicated lead channel for a human to follow up on.
    #
    # Price ceiling: a vague listing is only worth flagging if the asking
    # price for the WHOLE system doesn't already exceed a reasonable multiple
    # of what the bare GPU alone typically resells for used. Without this, a
    # $1050 prebuilt with a $260 GPU inside gets the same "possibly
    # undervalued" alert as a genuine $300-for-everything bargain — the
    # $1050 price is completely normal for a full system at that GPU tier,
    # there's no hidden upside just because the spec sheet is thin.
    gpu_price_ceiling = gpu_value * VAGUE_LEAD_PRICE_CEILING_MULTIPLIER
    is_vague_lead = (
        is_pc_context
        and bool(detected_gpu_name)
        and gpu_value >= VAGUE_LEAD_GPU_VALUE_THRESHOLD
        and not had_explicit_cpu
        and not had_explicit_ram
        and not had_explicit_storage
        and not is_office_pc
        and not has_legacy_signal
        and 0 < price <= gpu_price_ceiling
    )

    # No CPU or GPU is ever invented here. A full-system valuation requires the
    # seller to actually name both parts. Earlier versions estimated a missing
    # CPU at $120 and a missing GPU at $250, which inflated plain desktops
    # (a $200 i5 Dell got valued at $561) and GPU-only listings (a lone RX 6700
    # XT got a $706 value). A listing that names only one part is left to the
    # standard single-component path, which values exactly what it names.
    is_full_system = gpu_value > 0 and cpu_value > 0
    estimated_value = 0.0

    if not is_full_system:
        return {
            "is_full_system":    False,
            "estimated_value":   0.0,
            "detected_parts":    detected_parts,
            "category_override": None,
            "is_vague_lead":         is_vague_lead,
            "vague_lead_gpu":        detected_gpu_name or "",
            "vague_lead_gpu_value":  gpu_value if is_vague_lead else 0.0,
            "vague_lead_ceiling":    gpu_price_ceiling if is_vague_lead else 0.0,
        }

    # ── Full system value assembly ────────────────────────────────────────────
    estimated_value = gpu_value + cpu_value + _FULL_SYSTEM_CHASSIS_FLAT_RATE
    detected_parts.append(f"Chassis buffer (+${_FULL_SYSTEM_CHASSIS_FLAT_RATE:.0f})")

    # RAM (DDR4/DDR5-aware) — excludes storage capacities and GPU VRAM
    # callouts (see _extract_ram_gb / _looks_like_vram_mention)
    ram_match = _extract_ram_gb(combined_text)
    if ram_match and int(ram_match.group(1)) <= 64:
        ram_gb = int(ram_match.group(1))
        per_gb = _ram_value_per_gb(combined_text)
        ram_value = ram_gb * per_gb
        gen = "DDR5" if "ddr5" in combined_text else ("DDR4" if "ddr4" in combined_text else "")
        estimated_value += ram_value
        detected_parts.append(f"RAM: {ram_gb}GB{' ' + gen if gen else ''} (~${ram_value:.0f})")
    else:
        # Default 16GB assumption for any complete PC
        per_gb = _ram_value_per_gb(combined_text)
        default_ram = 16 * per_gb
        estimated_value += default_ram
        detected_parts.append(f"RAM: 16GB assumed (~${default_ram:.0f})")

    # Storage
    for storage_kw, storage_val in STORAGE_VALUES.items():
        if storage_kw in combined_text:
            estimated_value += storage_val
            detected_parts.append(f"Storage: {storage_kw.upper()} (~${storage_val})")
            break

    # Motherboard
    for mobo_kw in sorted(MOTHERBOARD_VALUES, key=len, reverse=True):
        if mobo_kw in combined_text:
            mobo_val = MOTHERBOARD_VALUES[mobo_kw]
            estimated_value += mobo_val
            detected_parts.append(f"Mobo: {mobo_kw.upper()} (~${mobo_val})")
            break

    # PSU
    psu_context = "psu" in combined_text or "power supply" in combined_text or "watt" in combined_text
    if psu_context:
        for psu_kw in sorted(PSU_VALUES, key=len, reverse=True):
            if psu_kw in combined_text:
                psu_val = PSU_VALUES[psu_kw]
                estimated_value += psu_val
                detected_parts.append(f"PSU: {psu_kw.upper()} (~${psu_val})")
                break

    # Cooler
    for cooler_kw in sorted(COOLER_VALUES, key=len, reverse=True):
        idx = combined_text.find(cooler_kw)
        if idx != -1 and _has_cooler_context(combined_text, cooler_kw, idx):
            cooler_val = COOLER_VALUES[cooler_kw]
            estimated_value += cooler_val
            detected_parts.append(f"Cooler: {cooler_kw.upper()} (~${cooler_val})")
            break

    return {
        "is_full_system":    True,
        "estimated_value":   estimated_value,
        "detected_parts":    detected_parts,
        "category_override": "Full Systems",
        "is_vague_lead":         is_vague_lead,
        "vague_lead_gpu":        detected_gpu_name or "",
        "vague_lead_gpu_value":  gpu_value if is_vague_lead else 0.0,
        "vague_lead_ceiling":    gpu_price_ceiling if is_vague_lead else 0.0,
    }


# ---------------------------------------------------------------------------
# PILLAR 2 — Profitability Margin (30 points)
# ---------------------------------------------------------------------------
def score_profitability_margin(price: float, estimated_value: float, max_points: int = SCORE_WEIGHTS["profitability_margin"]):
    """
    Compares ask price to estimated value. Both the score and the displayed
    profit are computed off the actual listed price — not an assumed
    post-negotiation discount, since a negotiation win isn't guaranteed
    (and is sometimes explicitly contradicted by "firm price" language the
    seller behavior pillar already penalizes separately).
    """
    # Block placeholder/junk prices
    if price in [0.0, 1.0, 2.0, 5.0, 123.0, 1234.0]:
        return 0, 0.0, 0.0

    if estimated_value <= 0 or price <= 0:
        return 0, 0.0, 0.0

    profit_usd   = estimated_value - price
    margin_ratio = profit_usd / estimated_value

    if margin_ratio <= 0:
        points = 0
    elif margin_ratio >= FULL_MARGIN_PROFIT_RATIO:
        points = max_points
    else:
        points = (margin_ratio / FULL_MARGIN_PROFIT_RATIO) * max_points

    return points, margin_ratio, profit_usd


# ---------------------------------------------------------------------------
# PILLAR 3 — Picture Quality (10 points)
# ---------------------------------------------------------------------------
def score_picture_quality(image_count: int, image_paths: list = None,
                          max_points: int = SCORE_WEIGHTS["picture_quality"]):
    if image_count <= 1:
        return max_points * 0.3
    elif image_count <= 3:
        return max_points * 0.6
    else:
        return max_points * 1.0


# ---------------------------------------------------------------------------
# PILLAR 4 — Seller Behavior & Risk (10 points)
# ---------------------------------------------------------------------------
GREEN_FLAG_PHRASES = [
    "can test before buying", "test before you buy", "power it on",
    "original box", "original boxes included", "still under warranty",
    "receipts included", "barely used", "smoke free home", "no pets",
    "negotiable", "open to offers", "can show it working",
    "just bought", "rarely used", "like new",
]

RED_FLAG_PHRASES = [
    "cash only", "no testing", "no returns", "as-is", "as is",
    "sold as is", "parts only", "not responsible", "no refunds",
    "pickup only no exceptions", "firm price no holds", "do not ask",
    "will not negotiate", "no warranty",
    # NOTE: "for parts" / "not working" removed — now caught by the hard
    # _is_damaged_item() disqualifier (see _DAMAGED_ITEM_SIGNALS) instead of
    # only costing -3 points here, since bare damage phrasing is common
    # enough that a soft penalty let broken hardware still score as a "deal".
    "laptop", "notebook",          # laptop GPU listings — not desktop flips
    "broken screen", "no display", # damaged hardware signals
]


def score_seller_behavior_risk(text: str, max_points: int = SCORE_WEIGHTS["seller_behavior_risk"]):
    text_lower = text.lower()
    green_hits = sum(1 for phrase in GREEN_FLAG_PHRASES if phrase in text_lower)
    red_hits   = sum(1 for phrase in RED_FLAG_PHRASES   if phrase in text_lower)

    # Red-flag coefficient softened from 3 -> 2: most of RED_FLAG_PHRASES
    # ("cash only", "as-is", "no warranty", "firm price no holds", etc.) are
    # standard private-seller boilerplate on used PC hardware, not real risk
    # signals — two ordinary phrases used to be enough to floor this entire
    # 10-point pillar at 0, dragging down otherwise-solid $100+ margin deals
    # over language that's completely normal for this market.
    baseline = max_points / 2
    points   = baseline + (green_hits * 2) - (red_hits * 2)
    return max(0, min(max_points, points))


# ---------------------------------------------------------------------------
# PILLAR 5 — Title & Description Quality (10 points)
# ---------------------------------------------------------------------------
GENERIC_TITLE_PHRASES = [
    "gaming computer", "gaming pc", "computer for sale",
    "pc for sale", "desktop for sale",
]


def score_title_description_quality(title: str, description: str,
                                    max_points: int = SCORE_WEIGHTS["title_description_quality"]):
    title       = title.strip()
    description = description.strip()
    combined_word_count = len(title.split()) + len(description.split())

    if combined_word_count >= 40:
        length_points = 7
    elif combined_word_count >= 20:
        length_points = 5
    elif combined_word_count >= 8:
        length_points = 3
    else:
        length_points = 1

    specificity_points = 3
    title_lower = title.lower()
    if title_lower in GENERIC_TITLE_PHRASES or len(title_lower) < 4:
        specificity_points -= 2

    if not re.search(r"\d", title + " " + description):
        specificity_points -= 1

    return max(0, min(max_points, length_points + max(0, specificity_points)))


# ---------------------------------------------------------------------------
# ORCHESTRATOR — combines all 5 pillars into the final composite score
# ---------------------------------------------------------------------------
def calculate_deal_score(listing: dict) -> dict:
    """
    Main entry point called by main.py for every listing. Runs all 5
    scoring pillars, applies the full-system extractor for generic-title
    PC listings, handles bundle/split-price overrides, and returns a
    complete score dict. Never returns None — always returns a valid dict.
    """
    if not listing or not isinstance(listing, dict):
        return {
            "total_score": 0.0, "estimated_value": 0.0, "predicted_profit": 0.0,
            "margin_ratio": 0.0, "breakdown": {}, "detected_parts": []
        }

    title       = listing.get("title", "") or ""
    description = listing.get("description", "") or ""
    price       = listing.get("price", 0.0) or 0.0
    image_count = listing.get("image_count", 0) or 0
    combined_text = f"{title} {description}"
    combined_lower = combined_text.lower()

    try:
        # ── Peripheral-only hard guard — headset/mic/keyboard/mouse, checked
        #    first, ahead of every other guard ─────────────────────────────
        if _is_peripheral_listing(title.lower()):
            return {
                "total_score": 0.0, "estimated_value": 0.0, "predicted_profit": 0.0,
                "margin_ratio": 0.0, "breakdown": {}, "detected_gpu_model": "",
                "detected_parts": ["🚫 DISQUALIFIED: Peripheral listing (headset/microphone/keyboard/mouse)"]
            }

        # ── Parts-only hard guard — heatsinks, water blocks, shrouds, etc. ───
        if _is_gpu_parts_only(combined_lower):
            return {
                "total_score": 0.0, "estimated_value": 0.0, "predicted_profit": 0.0,
                "margin_ratio": 0.0, "breakdown": {}, "detected_gpu_model": "",
                "detected_parts": ["🚫 DISQUALIFIED: GPU parts-only (heatsink / water block / shroud)"]
            }

        # ── Damaged / defective hard guard ───────────────────────────────────
        if _is_damaged_item(combined_lower):
            return {
                "total_score": 0.0, "estimated_value": 0.0, "predicted_profit": 0.0,
                "margin_ratio": 0.0, "breakdown": {}, "detected_gpu_model": "",
                "detected_parts": ["🚫 DISQUALIFIED: Damaged or defective hardware detected"]
            }

        # ── Shipping-only hard guard — local DMV pickup deals only ───────────
        # Text-keyword check catches sellers who type it out; the structured
        # signal (when the scraper could extract one — see facebook_scraper.py's
        # shipping_only_structured) catches sellers who only set it via
        # Marketplace's UI toggle and never mention it in the text at all.
        if _is_shipping_only(combined_lower) or listing.get("shipping_only_structured") is True:
            return {
                "total_score": 0.0, "estimated_value": 0.0, "predicted_profit": 0.0,
                "margin_ratio": 0.0, "breakdown": {}, "detected_gpu_model": "",
                "detected_parts": ["🚫 DISQUALIFIED: Shipping-only listing (not local DMV pickup)"]
            }

        # ── Local-pickup confidence — surfaced in the alert embed so members
        # can see it, not just used to disqualify the worst cases above ──────
        if listing.get("shipping_only_structured") is False:
            pickup_confidence = "confirmed"   # FB's own delivery-type data confirms pickup
        elif _has_unnegated_signal(combined_lower, _LOCAL_PICKUP_POSITIVE_SIGNALS):
            pickup_confidence = "stated"      # seller said so in the text
        else:
            pickup_confidence = "unknown"     # no shipping signal found, but not positively confirmed either

        # ── Trade/swap listing guard — seller wants to barter, not sell ──────
        if _is_trade_listing(title.lower()):
            return {
                "total_score": 0.0, "estimated_value": 0.0, "predicted_profit": 0.0,
                "margin_ratio": 0.0, "breakdown": {}, "detected_gpu_model": "",
                "detected_parts": ["🚫 DISQUALIFIED: Trade/swap listing — no cash sale"]
            }

        # ── Component bundle guard — partial builds, not complete PCs ─────────
        # Catches titles like "Ryzen 5 2600 + RTX 2060 + B450M-A motherboard"
        # which score high as full systems but are actually loose parts bundles.
        if _is_component_bundle(combined_lower):
            return {
                "total_score": 0.0, "estimated_value": 0.0, "predicted_profit": 0.0,
                "margin_ratio": 0.0, "breakdown": {}, "detected_gpu_model": "",
                "detected_parts": ["🚫 DISQUALIFIED: Component bundle — not a complete system"]
            }

        # ── GPU-missing guard — "gaming PC" with no dedicated GPU ────────────
        if _is_gpu_missing_system(combined_lower):
            return {
                "total_score": 0.0, "estimated_value": 0.0, "predicted_profit": 0.0,
                "margin_ratio": 0.0, "breakdown": {}, "detected_gpu_model": "",
                "detected_parts": ["🚫 DISQUALIFIED: No GPU included — not a flippable gaming PC"]
            }

        # ── Laptop hard guard — cap score and mark before anything else ──────
        is_laptop = _is_laptop_listing(combined_lower)

        # ── Legacy Intel CPU guard — 9th-gen-or-older Core i3/i5/i7/i9 caps
        # the whole listing's score, independent of GPU/margin strength ─────
        is_old_intel_cpu = _is_legacy_intel_cpu(combined_lower)

        # ── Split-pricing / bundle-price override ────────────────────────────
        bundle_price = listing.get("description_bundle_price", 0.0) or 0.0
        bundle_override_note = None
        if bundle_price > price:
            bundle_override_note = (
                f"⚠️ Price overridden: description bundle ${bundle_price:.0f} "
                f"> listed ${price:.0f}"
            )
            price = bundle_price

        # ── Pillar 1: standard single-component path ──────────────────────────
        part_points, estimated_value, detected_parts, is_standalone_gpu, detected_gpu_name = \
            score_part_quality_and_valuation(combined_text, price)

        # ── Full-system extraction (hidden spec builds) ───────────────────────
        full_system = extract_specs_and_value(title, description, price)
        if full_system["is_full_system"] and full_system["estimated_value"] > estimated_value:
            estimated_value  = full_system["estimated_value"]
            detected_parts   = full_system["detected_parts"]
            is_standalone_gpu = False
            part_points = min(
                SCORE_WEIGHTS["part_quality_valuation"],
                (estimated_value / 700) * SCORE_WEIGHTS["part_quality_valuation"],
            )
            listing["category_override"] = full_system["category_override"]

        if bundle_override_note:
            detected_parts = detected_parts + [bundle_override_note]

        # ── Pillar 2: margin (full systems use 15% threshold vs 30%) ──────────
        margin_points = 0
        if full_system["is_full_system"] and full_system["estimated_value"] > 0:
            fs_margin = (estimated_value - price) / estimated_value if estimated_value > 0 else 0
            if fs_margin >= 0.15:
                margin_points = SCORE_WEIGHTS["profitability_margin"]
            elif fs_margin > 0:
                margin_points = (fs_margin / 0.15) * SCORE_WEIGHTS["profitability_margin"]
            _, margin_ratio, predicted_profit = score_profitability_margin(price, estimated_value)
        else:
            margin_points, margin_ratio, predicted_profit = \
                score_profitability_margin(price, estimated_value)

        # ── Pillars 3, 4, 5 ──────────────────────────────────────────────────
        picture_points = score_picture_quality(image_count)
        seller_points  = score_seller_behavior_risk(combined_text)
        title_points   = score_title_description_quality(title, description)

        total_score = part_points + margin_points + picture_points + seller_points + title_points

        # ── Standalone GPU bonus ──────────────────────────────────────────────
        if is_standalone_gpu and price > 5.0:
            listing["category_override"] = "GPU Only"
            gpu_discount = estimated_value - price
            if gpu_discount >= 150.0:
                total_score += 35.0
            elif gpu_discount >= 75.0:
                total_score += 25.0
            elif gpu_discount >= 50.0:
                total_score += 15.0
            elif gpu_discount > 10.0:
                total_score += 8.0

        # ── Full system bonus ─────────────────────────────────────────────────
        elif full_system["is_full_system"] and price > 5.0:
            system_discount = estimated_value - price
            if system_discount >= 200.0:
                total_score += 20.0
            elif system_discount >= 100.0:
                total_score += 12.0
            elif system_discount >= 50.0:
                total_score += 6.0

        # ── Laptop penalty — applied after bonuses so it always overrides ─────
        if is_laptop:
            total_score = min(total_score, 45.0)  # hard cap below alert threshold
            predicted_profit = predicted_profit * 0.5  # laptop GPU resale is ~50% of desktop
            detected_parts = detected_parts + ["⚠️ LAPTOP — score capped, profit halved"]

        # ── Legacy Intel CPU penalty — applied after bonuses so it always
        # overrides; a 9th-gen-or-older Intel Core chip caps the final score
        # below 40 regardless of how well GPU/margin/photos otherwise score ──
        if is_old_intel_cpu:
            total_score = min(total_score, 39.0)  # strictly below 40
            detected_parts = detected_parts + ["⚠️ 9TH-GEN-OR-OLDER INTEL CPU — score capped below 40"]

        return {
            "total_score":      round(min(100.0, total_score), 1),
            "estimated_value":  round(estimated_value, 2),
            "predicted_profit": round(predicted_profit, 2),
            "margin_ratio":     round(margin_ratio, 3),
            "detected_gpu_model": detected_gpu_name or "",
            "breakdown": {
                "part_quality_valuation":    round(part_points, 1),
                "profitability_margin":      round(margin_points, 1),
                "picture_quality":           round(picture_points, 1),
                "seller_behavior_risk":      round(seller_points, 1),
                "title_description_quality": round(title_points, 1),
            },
            "detected_parts": detected_parts,
            # A laptop listing is never routed as a vague system lead — laptop
            # GPUs aren't desktop flips, so "missing CPU/RAM/storage" there
            # isn't a signal worth chasing the way it is for a desktop build.
            "is_vague_lead":        full_system.get("is_vague_lead", False) and not is_laptop,
            "vague_lead_gpu":       full_system.get("vague_lead_gpu", ""),
            "vague_lead_gpu_value": full_system.get("vague_lead_gpu_value", 0.0),
            "vague_lead_ceiling":   full_system.get("vague_lead_ceiling", 0.0),
            "pickup_confidence":    pickup_confidence,
        }

    except Exception as e:
        print(f"❌ Internal scoring error on '{title}': {e}")
        return {
            "total_score": 0.0, "estimated_value": 0.0, "predicted_profit": 0.0,
            "margin_ratio": 0.0, "breakdown": {}, "detected_parts": [f"Error: {e}"]
        }