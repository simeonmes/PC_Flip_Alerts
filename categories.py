"""
categories.py
==============
Maps listing keywords to a routing "category" — the thing notifier.py uses
to decide which Discord channel/webhook a given alert gets pushed to.

UPDATED: added "GPU Only" and "Full Systems" as first-class categories with
their own Discord channel slots in config.py. scoring.py sets
listing["category_override"] for these two — this file's keyword matching
acts as a fallback when that override isn't set.

Matching rules:
- Checked in dict-definition order, so more specific categories come first.
- Longest keyword wins within a category (same convention as scoring.py).
- Matching is case-insensitive substring match.
"""

CATEGORY_KEYWORDS = {
    # ── Standalone GPUs — highest flip urgency, alert immediately ──────────
    "GPU Only": [
        "graphics card", "video card", "gpu",
        "rtx 5090", "rtx 5080", "rtx 5070 ti", "rtx 5070", "rtx 5060 ti", "rtx 5060", "rtx 5050",
        "rtx 4090", "rtx 4080 super", "rtx 4080", "rtx 4070 ti super", "rtx 4070 ti",
        "rtx 4070 super", "rtx 4070", "rtx 4060 ti", "rtx 4060",
        "rtx 3090 ti", "rtx 3090", "rtx 3080 ti", "rtx 3080", "rtx 3070 ti", "rtx 3070",
        "rtx 3060 ti", "rtx 3060", "rtx 3050",
        "rtx 2080 ti", "rtx 2080 super", "rtx 2080", "rtx 2070 super", "rtx 2070",
        "rtx 2060 super", "rtx 2060",
        "rx 9070 xt", "rx 9070", "rx 9060 xt", "rx 9060",
        "rx 7900 xtx", "rx 7900 xt", "rx 7800 xt", "rx 7700 xt", "rx 7600 xt", "rx 7600",
        "rx 6950 xt", "rx 6900 xt", "rx 6800 xt", "rx 6800",
        "rx 6750 xt", "rx 6700 xt", "rx 6650 xt", "rx 6600 xt", "rx 6600",
        "gtx 1080 ti", "gtx 1080", "gtx 1070 ti", "gtx 1070",
        "gtx 1660 super", "gtx 1660 ti", "gtx 1660",
        "radeon", "geforce",
    ],

    # ── Complete systems — detected by scoring.py spec extractor ──────────
    # Keyword matching here is the fallback; scoring.py sets
    # category_override = "Full Systems" when it detects both CPU + GPU.
    "Full Systems": [
        "gaming pc", "gaming desktop", "custom pc", "pc build", "prebuilt",
        "custom built", "full pc", "complete pc", "full setup", "pc setup",
        "custom gaming", "gaming computer", "gaming tower", "desktop computer",
        "prebuilt pc", "custom build",
        # OEM brands — almost always a full system listing
        "ibuypower", "cyberpower", "alienware", "rog desktop",
        "asus rog strix", "msi gaming desktop",
    ],

    # ── Individual components (CPUs, RAM, storage, mobos, cases) ──────────
    "PC Hardware": [
        "ryzen 9", "ryzen 7", "ryzen 5", "ryzen 3", "ryzen",
        "threadripper",
        "intel core ultra", "core ultra", "core i9", "core i7", "core i5",
        "i9-", "i7-", "i5-", "i3-",
        "motherboard", "mobo", "am5", "am4", "lga1700", "lga1200",
        "power supply", "psu", "modular psu",
        "nvme", "m.2 ssd", "ssd", "ddr5", "ddr4",
        "cpu cooler", "liquid cooler", "aio cooler",
        "pc case", "atx case", "itx case",
        "ram", "memory",
    ],

    # ── Apple products ─────────────────────────────────────────────────────
    "Apple Devices": [
        "iphone 16", "iphone 15", "iphone 14", "iphone 13", "iphone 12", "iphone 11",
        "iphone se", "iphone",
        "ipad pro", "ipad air", "ipad mini", "ipad",
        "macbook pro", "macbook air", "macbook",
        "imac", "mac mini", "mac studio", "mac pro",
        "apple watch ultra", "apple watch",
        "airpods pro", "airpods max", "airpods",
    ],
}

DEFAULT_CATEGORY = "Uncategorized"


def detect_category(text: str) -> str:
    """
    Returns the first category whose keyword list matches anywhere in
    `text` (case-insensitive substring match), or DEFAULT_CATEGORY if
    nothing matches.

    Note: scoring.py may set listing["category_override"] = "GPU Only" or
    "Full Systems" before this function is called. notifier.py checks the
    override first and only calls detect_category() as a fallback.
    """
    text_lower = text.lower()

    for category, keywords in CATEGORY_KEYWORDS.items():
        for keyword in sorted(keywords, key=len, reverse=True):
            if keyword in text_lower:
                return category

    return DEFAULT_CATEGORY