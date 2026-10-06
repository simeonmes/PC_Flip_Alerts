"""
stats.py
========
Lightweight, persistence-backed scan statistics module.
Both main.py (writer) and discord_bot.py (reader) import from here —
no circular imports since neither file is imported by this module.

Stats auto-reset at midnight. Backed by scan_stats.json so numbers
survive a process restart mid-day.
"""

import json
import os
import time
from datetime import date

STATS_PATH  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scan_stats.json")
_start_time = time.time()   # process uptime anchor — set once at import


def _today_str() -> str:
    return date.today().isoformat()


def _load() -> dict:
    try:
        if os.path.exists(STATS_PATH):
            with open(STATS_PATH) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save(data: dict) -> None:
    try:
        with open(STATS_PATH, "w") as f:
            json.dump(data, f)
    except Exception as exc:
        print(f"⚠️  stats.py: could not write {STATS_PATH}: {exc}")


def _fresh_day() -> dict:
    return {
        "date":             _today_str(),
        "listings_scanned": 0,
        "alerts_sent":      0,
        "gpus_found":       0,
        "scan_count":       0,
    }


def _current() -> dict:
    """Returns today's block, resetting counters if the date has rolled over."""
    data = _load()
    if data.get("date") != _today_str():
        return _fresh_day()
    return data


# ── Public API ────────────────────────────────────────────────────────────────

def increment(listings_scanned: int = 0, alerts_sent: int = 0,
              gpus_found: int = 0, scan_count: int = 0) -> None:
    """Add to today's counters. Called once per scan cycle from main.py."""
    data = _current()
    data["listings_scanned"] += listings_scanned
    data["alerts_sent"]      += alerts_sent
    data["gpus_found"]       += gpus_found
    data["scan_count"]       += scan_count
    _save(data)


def get() -> dict:
    """
    Returns today's counters plus a human-readable process uptime string.
    Safe to call from the Discord bot's background thread.
    """
    data    = _current()
    elapsed = int(time.time() - _start_time)
    h, rem  = divmod(elapsed, 3600)
    m       = rem // 60
    data["uptime"] = f"{h}h {m}m"
    return data