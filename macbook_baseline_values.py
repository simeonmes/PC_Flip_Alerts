"""
macbook_baseline_values.py
===========================
Lightweight valuation table for M-series MacBooks — built for
macbook_finder.py, a standalone personal-use tool, NOT the main PC-flipping
scan loop. Deliberately simpler than baseline_values.py's PC tables: a
MacBook is one fixed SKU (chip+RAM+storage soldered together at purchase),
not a build of swappable parts, so there's no need for chassis/PSU/cooler-
style component summing.

Values are BASE prices at a representative "typical" configuration per
model+chip, with additive adjustments for RAM/storage/screen-size deltas
detected in the listing text. Real used-market data (RefurbMe, Back Market,
ValueSnap resale guides, mid-2026) grounds the M1-M4 values; M5 values are
necessarily rough since the chip only launched days ago and essentially no
real used-market sales history exists yet — treat any "used M5" listing
with extra scrutiny (implausibly steep discounts on a weeks-old chip are a
scam signal, not a deal).

All prices are approximate "good enough for a personal buy decision"
estimates, not the rigor of the PC-side scoring engine.
"""

# ---------------------------------------------------------------------------
# Base values: (model, chip) -> (baseline config, base used-market value $)
# Baseline configs noted so RAM/storage adjustments below make sense.
# ---------------------------------------------------------------------------

MACBOOK_AIR_BASE_VALUES = {
    # chip: (baseline RAM/storage, base value at 13" for that config)
    "m1":  {"baseline": "8gb/256gb",  "value": 340},
    "m2":  {"baseline": "8gb/256gb",  "value": 650},
    "m3":  {"baseline": "8gb/256gb",  "value": 900},
    "m4":  {"baseline": "16gb/256gb", "value": 950},   # Apple made 16GB the RAM floor on M4 Air
    "m5":  {"baseline": "16gb/256gb", "value": 1150},  # SPECULATIVE — brand new, ~10% off $1,299 retail
}

MACBOOK_AIR_15IN_PREMIUM = 175  # 15" adds ~$150-200 over 13" equivalent, per market data

MACBOOK_PRO_BASE_VALUES = {
    # chip: (baseline RAM/storage, base value at 14" for that config)
    "m1":      {"baseline": "16gb/512gb", "value": 950},
    "m1 pro":  {"baseline": "16gb/512gb", "value": 1000},
    "m1 max":  {"baseline": "32gb/512gb", "value": 1350},
    "m2":      {"baseline": "8gb/256gb",  "value": 750},
    "m2 pro":  {"baseline": "16gb/512gb", "value": 1300},
    "m2 max":  {"baseline": "32gb/1tb",   "value": 2100},
    "m3":      {"baseline": "8gb/512gb",  "value": 1200},
    "m3 pro":  {"baseline": "18gb/512gb", "value": 1550},   # directly grounded: $1,999 new -> $1,450-1,650 used
    "m3 max":  {"baseline": "36gb/1tb",   "value": 2600},   # holds 75-80% of original value per market data
    "m4":      {"baseline": "16gb/512gb", "value": 1350},
    "m4 pro":  {"baseline": "24gb/512gb", "value": 1750},
    "m4 max":  {"baseline": "36gb/1tb",   "value": 2850},
    # SPECULATIVE below — M5 launched within days of this table being built,
    # essentially zero real used-market sales exist yet. Estimated as ~10%
    # off current (post price-hike) retail: base $1,999 / Pro $2,499 / Max $4,099.
    "m5":      {"baseline": "16gb/512gb", "value": 1800},
    "m5 pro":  {"baseline": "24gb/512gb", "value": 2250},
    "m5 max":  {"baseline": "36gb/1tb",   "value": 3700},
}

MACBOOK_PRO_16IN_PREMIUM = 200  # 16" chassis adds a premium over 14" at the same chip/config

# ---------------------------------------------------------------------------
# RAM adjustment — $ added/subtracted per step away from the chip's baseline
# RAM, applied per 8GB step. Rough but directionally correct: extra unified
# memory retains real resale value since it can't be upgraded after purchase.
# ---------------------------------------------------------------------------
RAM_STEP_VALUE = 90       # $ per 8GB step above/below baseline
RAM_STEP_SIZE_GB = 8

# ---------------------------------------------------------------------------
# Storage adjustment — $ added/subtracted per doubling of storage relative
# to the chip's baseline (256->512->1TB->2TB->4TB...).
# ---------------------------------------------------------------------------
STORAGE_DOUBLING_VALUE = 100  # $ per storage doubling above/below baseline


def _parse_baseline(baseline_str: str) -> tuple:
    """'16gb/512gb' -> (16, 512). Storage always normalized to GB (1tb = 1024)."""
    ram_part, storage_part = baseline_str.split("/")
    ram_gb = int(ram_part.replace("gb", ""))
    if "tb" in storage_part:
        storage_gb = int(storage_part.replace("tb", "")) * 1024
    else:
        storage_gb = int(storage_part.replace("gb", ""))
    return ram_gb, storage_gb
