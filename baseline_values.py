"""
baseline_values.py
==================
The "price book" the scoring engine cross-references against listing text.
UPDATED: July 12, 2026

MAJOR REVAMP: Individual CPU model pricing replaces flat tier estimates.
Previously a Ryzen 7 3700X (worth ~$80) and a Ryzen 7 7700X (worth ~$200)
both returned "$195" because the old system only used "ryzen 7" as a key.
Longest-match detection in scoring.py ensures specific models are matched
before the conservative flat-tier fallbacks at the bottom of each section.

JULY 12, 2026 REFRESH — THE MEMORY SHORTAGE REPRICING:
The AI-data-center memory crunch that was building in June has now dominated
the whole market. DRAM/GDDR/NAND are being routed to hyperscalers, so:
  • RAM: new DDR5 ~$11.5/GB, DDR4 ~$6.9/GB (both up ~4x vs 2024 lows).
    Used per-GB values raised accordingly (see RAM_VALUES_* below).
  • Storage: NAND up ~4-5x in nine months. Used 1TB NVMe ≈ $110 (was $75),
    4TB NVMe ≈ $430. Whole STORAGE_VALUES table repriced up.
  • GPUs: VRAM is now >80% of a high-end card's BOM, so the shortage lifts
    every tier. Budget used cards (GDDR6/6X) +8-12%, mid-range +10-25%.
    RTX 50 SKUs sit near new; older gens rose as buyers get priced out.
  • CPUs: largely unaffected by the memory crunch — only the SKUs with fresh
    July sold-data were repriced; the rest carry June values (still current).
  • PSUs & coolers: no memory exposure, but rising raw-material costs pushed
    both up ~8-10% (distributor warnings, mid-2026) — repriced accordingly.
  • Motherboards: flat vs June. New boards are actually crashing (builds
    stalled by unaffordable RAM, mobo sales down ~44% YoY), but used high-end
    boards hold value as buyers avoid inflated retail — net ≈ unchanged.

Sources (July 2026): bestvaluegpu.com, usedgamer.com, pcprice.watch,
  secondhandsilicon.com, Tom's Hardware RAM/SSD price trackers, TrendForce,
  and eBay sold listings.
"""

# ---------------------------------------------------------------------------
# GPUs — Used eBay market values, July 12, 2026. Repriced up across all tiers:
# the GDDR shortage means even older GDDR6/6X cards gained value as buyers
# priced out of RTX 50 / RX 9000 fall back to previous generations.
# ---------------------------------------------------------------------------
GPU_VALUES = {
    # ── NVIDIA RTX 50-series (Blackwell) ────────────────────────────────────
    "rtx 5090":          3700,   # used ~10% under new; came down off the 2026 peak
    "rtx 5080":          1130,   # used avg ~$1,100-1,140, ~9% under new
    "rtx 5070 ti":        850,   # used ~$850-880 vs ~$919 new
    "rtx 5070":           570,
    "rtx 5060 ti 16gb":   560,   # 16GB tier bid up — same memory AI buyers want
    "rtx 5060 ti 8gb":    385,
    "rtx 5060 ti":        385,
    "rtx 5060":           330,
    "rtx 5050":           215,

    # ── NVIDIA RTX 40-series (Ada Lovelace) ─────────────────────────────────
    "rtx 4090":          2250,   # still elevated — 24GB, halo demand, shortage floor
    "rtx 4080 super":     900,
    "rtx 4080":           800,
    "rtx 4070 ti super":  740,
    "rtx 4070 ti":        570,
    "rtx 4070 super":     540,
    "rtx 4070":           480,
    "rtx 4060 ti 16gb":   360,   # 16GB variant pulled up harder than the 8GB
    "rtx 4060 ti 8gb":    300,
    "rtx 4060 ti":        300,
    "rtx 4060":           275,

    # ── NVIDIA RTX 30-series (Ampere) ───────────────────────────────────────
    "rtx 3090 ti":       1225,   # 24GB GDDR6X — holds value, still bid up
    "rtx 3090":          1050,
    "rtx 3080 ti":        450,
    "rtx 3080 12gb":      320,
    "rtx 3080 10gb":      300,
    "rtx 3080":           300,
    "rtx 3070 ti":        245,
    "rtx 3070":           210,
    "rtx 3060 ti":        215,
    "rtx 3060 12gb":      180,   # 12GB budget card — rising as new mid-range soars
    "rtx 3060":           180,
    "rtx 3050 8gb":       105,
    "rtx 3050":           100,

    # ── NVIDIA RTX 20-series (Turing) — GDDR6, +8-12% on the shortage ────────
    "rtx 2080 ti":        270,
    "rtx 2080 super":     170,
    "rtx 2080":           195,
    "rtx 2070 super":     170,
    "rtx 2070":           140,
    "rtx 2060 super":     150,
    "rtx 2060":           140,

    # ── NVIDIA GTX 16-series — budget cards climbing on the shortage ─────────
    "gtx 1660 ti":         70,
    "gtx 1660 super":      65,
    "gtx 1660":            58,
    "gtx 1650 super":      54,
    "gtx 1650":            45,

    # ── NVIDIA GTX 10-series (Legacy) ───────────────────────────────────────
    "gtx 1080 ti":        100,
    "gtx 1080":            72,
    "gtx 1070 ti":         62,
    "gtx 1070":            54,
    "gtx 1060 6gb":        44,
    "gtx 1060 3gb":        32,
    "gtx 1060":            40,
    "gtx 1050 ti":         24,
    "gtx 1050":            15,

    # ── AMD Radeon RX 9000-series (RDNA 4) ──────────────────────────────────
    "rx 9070 xt":          600,   # used ~$598 vs ~$689 new
    "rx 9070":             545,
    "rx 9060 xt 16gb":     400,   # used ~$400 vs ~$448 new
    "rx 9060 xt 8gb":      300,
    "rx 9060 xt":          400,
    "rx 9060":             160,

    # ── AMD Radeon RX 7000-series (RDNA 3) ──────────────────────────────────
    "rx 7900 xtx":         815,   # used ~$820
    "rx 7900 xt":          540,   # used ~$535 — softened vs June as 9070 undercut it
    "rx 7900 gre":         470,
    "rx 7800 xt":          470,   # used ~$475
    "rx 7700 xt":          350,
    "rx 7600 xt 16gb":     370,
    "rx 7600 xt":          370,
    "rx 7600":             200,

    # ── AMD Radeon RX 6000-series (RDNA 2) — budget tier rising ─────────────
    "rx 6950 xt":          430,
    "rx 6900 xt":          400,
    "rx 6800 xt":          345,   # used ~$320-360
    "rx 6800":             315,
    "rx 6750 xt":          285,
    "rx 6700 xt":          260,   # used ~$256
    "rx 6650 xt":          190,
    "rx 6600 xt":          165,   # used ~$160
    "rx 6600":             150,
    "rx 6500 xt":           70,
    "rx 6400":              62,   # entry-level RDNA 2, no hardware encoding
    "rx 6300":              45,

    # ── AMD Legacy — +8-12% on the budget-card shortage ─────────────────────
    "rx 5700 xt":          110,
    "rx 5600 xt":           75,
    "rx 5500 xt":           68,
    "rx 5500":              52,
    "rx 580 8gb":           50,
    "rx 580":               46,
    "rx 570 4gb":           32,
    "rx 570":               28,

    # ── Intel Arc — B-series in demand as a budget option, bid up ───────────
    "arc b580":            290,   # used ~$300 vs ~$303 new
    "arc b570":            230,
    "arc a770 16gb":       265,
    "arc a770":            255,
    "arc a750":            200,
    "arc a580":             90,
    "arc a380":             48,
}

OUTDATED_GPU_KEYWORDS = [
    "gtx 1660", "gtx 1650", "gtx 1060", "gtx 1050",
    "gtx 960", "gtx 950", "gtx 750", "gtx 970", "gtx 980",
    "rx 570", "rx 580", "rx 560", "rx 550",
    "gt 1030", "gt 730",
    "gtx 1070", "gtx 1080",
    "quadro", "firepro", "radeon pro",
]


# ---------------------------------------------------------------------------
# CPUs — Individual model pricing, June 26, 2026.
#
# CRITICAL: keys must be lowercase. scoring.py compares against text.lower().
# Longest-match detection fires first — specific models always beat flat tiers.
# Flat tier fallbacks are at the BOTTOM with conservative values.
#
# Sources: usedgamer.com, secondhandsilicon.com, bestvaluegpu.com, eBay sold
# listings, July 2026. CPUs are not exposed to the memory shortage, so only
# SKUs with fresh July sold-data were repriced; the rest carry June values.
# ---------------------------------------------------------------------------
CPU_VALUES = {

    # ════════════════════════════════════════════════════════════════════════
    # AMD RYZEN — Individual models (longest match wins)
    # ════════════════════════════════════════════════════════════════════════

    # ── Ryzen 9000-series (Zen 5, AM5) ──────────────────────────────────────
    "ryzen 9 9950x3d":   550,   # used ~$549
    "ryzen 9 9950x":     380,
    "ryzen 9 9900x3d":   450,
    "ryzen 9 9900x":     295,
    "ryzen 7 9800x3d":   370,   # used ~$370, still the gaming default
    "ryzen 7 9700x":     225,
    "ryzen 5 9600x":     155,
    "ryzen 5 9600":      140,

    # ── Ryzen 8000-series (Phoenix APUs, AM5) ───────────────────────────────
    "ryzen 7 8700g":     195,
    "ryzen 5 8600g":     150,

    # ── Ryzen 7000-series (Zen 4, AM5) ──────────────────────────────────────
    "ryzen 9 7950x3d":   410,
    "ryzen 9 7950x":     250,   # used <$250 now — 9000-series pulled it down
    "ryzen 9 7900x3d":   430,
    "ryzen 9 7900x":     245,
    "ryzen 9 7900":      215,
    "ryzen 7 7800x3d":   285,   # used ~$285-300, still heavily demanded
    "ryzen 7 7700x":     185,
    "ryzen 7 7700":      160,
    "ryzen 5 7600x3d":   180,
    "ryzen 5 7600x":     145,
    "ryzen 5 7600":      130,
    "ryzen 5 7500f":     115,
    "ryzen 3 7300x":     110,

    # ── Ryzen 5000-series (Zen 3, AM4) ──────────────────────────────────────
    "ryzen 9 5950x":     190,
    "ryzen 9 5900x":     150,
    "ryzen 9 5900":      135,
    "ryzen 7 5800x3d":   185,   # legendary AM4 gaming chip — holds value
    "ryzen 7 5800x":     105,
    "ryzen 7 5800":       85,
    "ryzen 7 5700x3d":   160,   # used ~$150-180, best gaming $/perf on AM4
    "ryzen 7 5700x":     100,
    "ryzen 7 5700g":      90,
    "ryzen 5 5600x3d":   115,
    "ryzen 5 5600x":      78,   # used <$80, budget-build backbone
    "ryzen 5 5600g":      72,
    "ryzen 5 5600":       72,
    "ryzen 5 5500":       55,
    "ryzen 5 5500gt":     65,
    "ryzen 3 5300g":      60,
    "ryzen 3 5300":       45,

    # ── Ryzen 4000-series (Renoir APUs, AM4, retail) ────────────────────────
    "ryzen 7 4650g":      75,
    "ryzen 5 4600g":      62,
    "ryzen 3 4300g":      45,

    # ── Ryzen 3000-series (Zen 2, AM4) ──────────────────────────────────────
    "ryzen 9 3950x":     135,
    "ryzen 9 3900xt":    110,
    "ryzen 9 3900x":     100,
    "ryzen 9 3900":       90,
    "ryzen 7 3800xt":     95,
    "ryzen 7 3800x":      90,
    "ryzen 7 3700x":      80,   # ← THE FIX: was $195 with flat tier
    "ryzen 5 3600xt":     65,
    "ryzen 5 3600x":      60,
    "ryzen 5 3600":       55,
    "ryzen 5 3500x":      42,
    "ryzen 5 3500":       38,
    "ryzen 3 3300x":      60,
    "ryzen 3 3100":       40,

    # ── Ryzen 2000-series (Zen+, AM4) ───────────────────────────────────────
    "ryzen 7 2700x":      55,
    "ryzen 7 2700":       45,
    "ryzen 5 2600x":      40,
    "ryzen 5 2600":       35,
    "ryzen 3 2300x":      28,

    # ── Ryzen 1000-series (Zen, AM4) ────────────────────────────────────────
    "ryzen 7 1700x":      38,
    "ryzen 7 1700":       32,
    "ryzen 5 1600x":      30,
    "ryzen 5 1600":       28,
    "ryzen 5 1500x":      22,

    # ── Ryzen budget APUs (Raven Ridge / Picasso, AM4) ──────────────────────
    "ryzen 5 3400g":      48,
    "ryzen 3 3200g":      35,
    "ryzen 5 2400g":      38,
    "ryzen 3 2200g":      28,

    # ── AMD Threadripper (HEDT) ──────────────────────────────────────────────
    "threadripper 7980x":  1400,
    "threadripper 7970x":  1050,
    "threadripper 7960x":   650,
    "threadripper 3990x":   950,
    "threadripper 3970x":   700,
    "threadripper 3960x":   550,
    "threadripper 2990wx":  280,

    # ── AMD Legacy — FX (Bulldozer/Piledriver) & modern Athlon ─────────────
    "fx-9590":            22,
    "fx-8370":            18,
    "fx-8350":            16,
    "fx-8320":            14,
    "fx-6350":            12,
    "fx-6300":            10,
    "fx-4350":             8,
    "fx-4300":             7,
    "athlon 3000g":       18,
    "athlon 200ge":       15,

    # ════════════════════════════════════════════════════════════════════════
    # INTEL CORE — Individual models (longest match wins)
    # ════════════════════════════════════════════════════════════════════════

    # ── Intel Core Ultra (Arrow Lake, 15th gen, LGA1851) ────────────────────
    "core ultra 9 285k":  330,   # used ~$330 as new fell to ~$509
    "core ultra 7 265k":  275,
    "core ultra 7 265kf": 265,
    "core ultra 5 245k":  195,
    "core ultra 5 245kf": 180,
    "core ultra 5 235":   155,
    "core ultra 9 285":   275,
    "core ultra 7 265":   230,
    "core ultra 5 225f":  130,
    "core ultra 5 225":   140,

    # ── Intel 14th gen Raptor Lake Refresh (LGA1700) ────────────────────────
    "i9-14900ks":         390,
    "i9-14900k":          340,   # used ~$341
    "i9-14900kf":         320,
    "i9-14900":           290,
    "i7-14700k":          265,
    "i7-14700kf":         250,
    "i7-14700":           225,
    "i5-14600k":          185,
    "i5-14600kf":         170,
    "i5-14500":           145,
    "i5-14400f":          110,
    "i5-14400":           115,
    "i3-14100f":           75,
    "i3-14100":            80,

    # ── Intel 13th gen Raptor Lake (LGA1700) ────────────────────────────────
    "i9-13900k":          295,
    "i9-13900kf":         278,
    "i9-13900":           255,
    "i7-13700k":          225,
    "i7-13700kf":         210,
    "i7-13700f":          190,
    "i7-13700":           195,
    "i5-13600k":          165,
    "i5-13600kf":         150,
    "i5-13500":           130,
    "i5-13400f":           95,
    "i5-13400":           100,
    "i5-13600":           140,
    "i3-13100f":           65,
    "i3-13100":            70,

    # ── Intel 12th gen Alder Lake (LGA1700) ─────────────────────────────────
    "i9-12900ks":         195,
    "i9-12900k":          185,
    "i9-12900kf":         170,
    "i9-12900":           160,
    "i7-12700k":          155,
    "i7-12700kf":         140,
    "i7-12700f":          130,
    "i7-12700":           135,
    "i5-12600k":          120,
    "i5-12600kf":         110,
    "i5-12400f":           85,   # ← common in budget builds
    "i5-12400":            90,
    "i3-12100f":           60,
    "i3-12100":            65,

    # ── Intel 11th gen Rocket Lake (LGA1200) ────────────────────────────────
    "i9-11900k":          120,
    "i9-11900kf":         110,
    "i9-11900":           105,
    "i7-11700k":          100,
    "i7-11700kf":          92,
    "i7-11700":            88,
    "i5-11600k":           80,
    "i5-11600kf":          72,
    "i5-11400f":           60,
    "i5-11400":            65,
    "i3-11100":            45,

    # ── Intel 10th gen Comet Lake (LGA1200) ─────────────────────────────────
    "i9-10900k":           95,
    "i9-10900kf":          88,
    "i9-10900":            80,
    "i9-10850k":           90,
    "i7-10700k":           78,
    "i7-10700kf":          72,
    "i7-10700f":           62,
    "i7-10700":            65,
    "i5-10600k":           62,
    "i5-10600kf":          58,
    "i5-10400f":           45,
    "i5-10400":            50,
    "i3-10100f":           30,
    "i3-10100":            35,

    # ── Intel 9th gen Coffee Lake Refresh (LGA1151) ─────────────────────────
    "i9-9900ks":           90,
    "i9-9900k":            85,
    "i9-9900kf":           80,
    "i9-9900":             72,
    "i7-9700k":            65,
    "i7-9700kf":           60,
    "i7-9700":             58,
    "i5-9600k":            50,
    "i5-9600kf":           45,
    "i5-9400f":            38,
    "i5-9400":             40,
    "i3-9100f":            28,
    "i3-9100":             30,

    # ── Intel 8th gen Coffee Lake (LGA1151) ─────────────────────────────────
    "i7-8700k":            65,
    "i7-8700":             55,
    "i5-8600k":            50,
    "i5-8400":             35,
    "i5-8500":             38,
    "i3-8100":             25,
    "i3-8350k":            32,

    # ── Intel 7th gen Kaby Lake (LGA1151) ───────────────────────────────────
    "i7-7700k":            50,
    "i7-7700":             42,
    "i5-7600k":            35,
    "i5-7400":             25,
    "i3-7100":             18,

    # ── Intel 6th gen Skylake (LGA1151) ─────────────────────────────────────
    "i7-6700k":            30,
    "i7-6700":             24,
    "i5-6600k":            22,
    "i5-6600":             18,
    "i5-6500":             16,
    "i5-6400":             14,
    "i3-6100":             10,

    # ── Intel 4th gen Haswell (LGA1150) ──────────────────────────────────────
    "i7-4790k":            28,
    "i7-4790":             22,
    "i5-4690k":            20,
    "i5-4690":             17,
    "i5-4590":             16,
    "i5-4460":             13,
    "i3-4160":              8,
    "i3-4130":              7,

    # ── Intel 3rd gen Ivy Bridge (LGA1155) ───────────────────────────────────
    "i7-3770k":            20,
    "i7-3770":             16,
    "i5-3570k":            15,
    "i5-3570":             13,
    "i5-3470":             11,
    "i3-3220":              6,

    # ── Intel 2nd gen Sandy Bridge (LGA1155) ─────────────────────────────────
    "i7-2700k":            18,
    "i7-2600k":            16,
    "i7-2600":             13,
    "i5-2500k":            14,
    "i5-2500":             11,
    "i5-2400":              9,
    "i3-2100":              5,

    # ════════════════════════════════════════════════════════════════════════
    # FLAT-TIER FALLBACKS — Only fires when no specific model is matched.
    # Values are intentionally conservative to avoid the 3700X=$195 problem.
    # ════════════════════════════════════════════════════════════════════════

    # AMD flat tiers (conservative — older/unspecified chips)
    "threadripper":      320,
    "ryzen 9":           160,   # unknown Ryzen 9 gen — conservative
    "ryzen 7":            90,   # unknown Ryzen 7 gen — conservative (3700X-era range)
    "ryzen 5":            65,
    "ryzen 3":            38,

    # Intel Core Ultra flat tier
    "ultra 9":           290,
    "ultra 7":           250,
    "ultra 5":           165,

    # Intel i-series flat tiers (conservative — could be any gen)
    "i9":                170,
    "i7":                 95,   # unknown i7 gen — conservative (8700/10700 era)
    "i5":                 70,
    "i3":                 42,
    "pentium":            12,
    "celeron":             5,
}

OUTDATED_CPU_KEYWORDS = [
    # Intel LGA1151 and older (6th gen and below)
    "i3-2", "i3-3", "i3-4", "i3-5", "i3-6",
    "i5-2", "i5-3", "i5-4", "i5-5", "i5-6",
    "i7-2", "i7-3", "i7-4", "i7-5", "i7-6",
    # AMD legacy
    "fx-4", "fx-6", "fx-8", "fx-9",
    "ryzen 3 1", "ryzen 5 1", "ryzen 7 1",
    "ryzen 3 2", "ryzen 5 2", "ryzen 7 2",
    "phenom", "athlon",
]


# ---------------------------------------------------------------------------
# Motherboards — Used market values by chipset tier. Reviewed July 12, 2026;
# used values ≈ flat vs June (new-board retail is falling as RAM-starved
# builds stall, but used high-end chipsets hold as buyers dodge inflated
# retail — the two effects roughly cancel on the secondary market).
# Individual motherboard model pricing would require thousands of entries;
# chipset-level is the practical approach since resale tracks closely with
# chipset tier regardless of specific model.
# ---------------------------------------------------------------------------
MOTHERBOARD_VALUES = {
    # ── Intel LGA1851 (Arrow Lake, 15th gen) ────────────────────────────────
    "z890":   245,
    "b860":   145,
    "h810":    95,

    # ── Intel LGA1700 (Alder/Raptor Lake, 12th-14th gen) ────────────────────
    "z790":   205,
    "z690":   175,
    "b760":   125,
    "b660":   105,
    "h770":    92,
    "h670":    82,

    # ── AMD AM5 (Ryzen 7000/9000 series) ────────────────────────────────────
    "x670e":  255,
    "x670":   215,
    "b650e":  175,
    "b650":   145,
    "a620":    92,

    # ── AMD AM4 (Ryzen 1000-5000 series) ────────────────────────────────────
    "x570":   112,
    "b550":    88,
    "x470":    68,
    "b450":    58,
    "a520":    42,
    "x370":    45,
    "b350":    30,

    # ── Generic fallback ─────────────────────────────────────────────────────
    "motherboard":  80,
    "mobo":         80,
}


# ---------------------------------------------------------------------------
# Power Supplies — Used market values by wattage, July 12, 2026.
# Up ~10% vs June on rising raw-material costs (distributor price warnings).
# ---------------------------------------------------------------------------
PSU_VALUES = {
    "1600w":  230,
    "1500w":  198,
    "1200w":  160,
    "1000w":  105,
    "850w":    75,
    "750w":    58,
    "650w":    42,
    "550w":    31,
    "500w":    24,
    "450w":    20,
}


# ---------------------------------------------------------------------------
# CPU Coolers / AIOs — Used market values, July 12, 2026.
# Up ~8% vs June — same raw-material cost pressure flagged for PSUs.
# ---------------------------------------------------------------------------
COOLER_VALUES = {
    # ── AIO liquid coolers by radiator size ──────────────────────────────────
    "420mm aio":   118,
    "360mm aio":    92,
    "280mm aio":    70,
    "240mm aio":    54,
    "120mm aio":    35,

    # ── Brand-specific premium air coolers ───────────────────────────────────
    "noctua nh-d15":   80,
    "noctua nh-d14":   60,
    "noctua":          48,
    "be quiet dark rock pro":  65,
    "be quiet dark rock":      48,
    "be quiet":                38,
    "deepcool assassin":       54,

    # ── Generic fallbacks ─────────────────────────────────────────────────────
    "liquid cooled":   52,
    "liquid cooling":  52,
    "water cooled":    52,
    "aio cooler":      48,
    "cpu cooler":      22,
}


# ---------------------------------------------------------------------------
# RAM — DDR generation-aware per-GB pricing, July 12, 2026.
# The DRAM shortage has gone from "elevated" to acute: new DDR5 is now
# ~$11.5/GB and new DDR4 ~$6.9/GB (both up ~4x vs 2024 lows, TrendForce /
# Tom's Hardware). Used prices have surged in lockstep because buying used
# is now a hedge against inflated new kits, so the used-vs-new gap narrowed.
# DDR4 in particular stopped falling — with Micron/others exiting consumer
# DRAM, no new DDR4 is being made, so the used stock is what's left.
# ---------------------------------------------------------------------------
RAM_VALUES_PER_GB_DDR5    = 9.0   # ~32GB DDR5 used ≈ $290 (new ~$370)
RAM_VALUES_PER_GB_DDR4    = 5.5   # ~32GB DDR4 used ≈ $176 (new ~$220)
RAM_VALUES_PER_GB_DEFAULT = 4.0   # unknown gen — conservative


# ---------------------------------------------------------------------------
# Storage — Used market values, July 12, 2026.
# The NAND shortage caught up hard: NAND is up ~4-5x in nine months and the
# used market has now fully followed (the June "significant lag" is gone).
# Reference July used sold-prices: 1TB NVMe ≈ $110-120, 2TB NVMe ≈ $185-250,
# 4TB NVMe ≈ $430-475. SATA SSDs run a step below NVMe. HDDs are the one
# relative haven — spinning rust isn't NAND — so they moved the least.
# ---------------------------------------------------------------------------
STORAGE_VALUES = {
    # ── NVMe PCIe Gen 4 (fast drives) ───────────────────────────────────────
    "4tb nvme":    430,
    "2tb nvme":    210,
    "1tb nvme":    110,
    "512gb nvme":   60,
    "256gb nvme":   38,

    # ── SATA SSD ────────────────────────────────────────────────────────────
    "4tb ssd":     300,
    "2tb ssd":     155,
    "1tb ssd":      85,
    "512gb ssd":    48,
    "256gb ssd":    28,
    "120gb ssd":    15,
    "128gb ssd":    15,

    # ── Generic fallbacks ────────────────────────────────────────────────────
    "nvme":   85,
    "ssd":    55,
    "hdd":    10,
}


# ===========================================================================
# PRICE OVERRIDES  (baseline_overrides.json)
# ===========================================================================
# The values above are the hand-authored seed. baseline_overrides.json, when
# present, is merged ON TOP of them — that JSON is how a periodic price
# refresh updates the book without touching this module's structure/comments.
# (The refresh is currently done as an assisted pass: fresh used-market
# prices are researched and written to the JSON. An automated eBay-API
# refresher was scoped but shelved — eBay rejected the API-key application.)
# Design:
#   • The seed above stays the fallback — used whenever the JSON is absent, a
#     key was never refreshed, etc. Nothing breaks if the file is deleted;
#     delete it to revert to the seed values.
#   • Every refreshed change lives in one diffable/revertable JSON file; this
#     module's structure, comments, and longest-match ordering are untouched.
# Dicts are updated IN PLACE so existing `from baseline_values import X`
# references (e.g. in scoring.py) keep pointing at the merged tables.
# Only keys that already exist in the seed tables are overridden — a stale or
# mistyped key in the JSON is ignored rather than injected (which could break
# scoring.py's longest-match ordering).
# ---------------------------------------------------------------------------
import json as _json
import os as _os

_OVERRIDES_PATH = _os.path.join(_os.path.dirname(__file__), "baseline_overrides.json")

# Timestamp of the last applied refresh, or None. Handy for a "prices as of…"
# footer in Discord embeds or a staleness check in a health ping.
BASELINE_OVERRIDES_APPLIED_AT = None


def _apply_baseline_overrides(path: str = _OVERRIDES_PATH) -> bool:
    """Merge baseline_overrides.json onto the seed tables. Returns True if a
    valid override file was found and applied, False otherwise (safe no-op)."""
    global BASELINE_OVERRIDES_APPLIED_AT
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = _json.load(fh)
    except (FileNotFoundError, ValueError, OSError):
        return False

    for name, table in (
        ("GPU_VALUES",         GPU_VALUES),
        ("CPU_VALUES",         CPU_VALUES),
        ("MOTHERBOARD_VALUES", MOTHERBOARD_VALUES),
        ("PSU_VALUES",         PSU_VALUES),
        ("COOLER_VALUES",      COOLER_VALUES),
        ("STORAGE_VALUES",     STORAGE_VALUES),
    ):
        for key, val in (data.get(name) or {}).items():
            if key in table and isinstance(val, (int, float)) and val > 0:
                table[key] = val

    ram = data.get("RAM") or {}
    for json_key, global_name in (
        ("ddr5_per_gb",    "RAM_VALUES_PER_GB_DDR5"),
        ("ddr4_per_gb",    "RAM_VALUES_PER_GB_DDR4"),
        ("default_per_gb", "RAM_VALUES_PER_GB_DEFAULT"),
    ):
        val = ram.get(json_key)
        if isinstance(val, (int, float)) and val > 0:
            globals()[global_name] = val

    BASELINE_OVERRIDES_APPLIED_AT = data.get("updated_at")
    return True


_apply_baseline_overrides()