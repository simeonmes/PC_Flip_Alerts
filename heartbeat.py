"""
heartbeat.py
============
Sends a one-embed Discord status ping once per hour to DISCORD_WEBHOOK_HEARTBEAT,
and logs every send attempt (success or failure) to a persistent JSONL file so
that history survives past whatever console window happened to be open at the
time. Called from the main scan loop every cycle — no-ops if the webhook isn't
set.

This module is an "I'm alive" ping ONLY. It cannot detect the bot going down —
by definition, the code that would report a crash isn't running once main.py
has actually crashed or hung. Real downtime detection lives in watchdog.py, a
separate OS process that watches scan_state.py's timestamp file independently
of whether main.py is still alive. See watchdog.py's docstring for why that
has to be a separate process rather than logic inside this loop.
"""

import json
import os
import time
from pathlib import Path

import requests

DISCORD_WEBHOOK_HEARTBEAT  = os.getenv("DISCORD_WEBHOOK_HEARTBEAT", "")
HEARTBEAT_INTERVAL_SECONDS = 3600   # one ping per hour

HEARTBEAT_LOG_PATH = Path.home() / ".pc_flip_scanner" / "heartbeat_log.jsonl"

_last_sent: float = 0.0


def _log(success: bool, detail: str = "") -> None:
    entry = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ts":        time.time(),
        "success":   success,
        "detail":    detail,
    }
    try:
        HEARTBEAT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(HEARTBEAT_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as exc:
        print(f"⚠️  Could not write heartbeat log: {exc}")


def send_if_due() -> None:
    """
    Reads today's stats from stats.py and posts a heartbeat embed if the
    interval has elapsed. Safe to call every scan cycle — does nothing if
    too early or if the webhook is not configured.
    """
    global _last_sent

    if not DISCORD_WEBHOOK_HEARTBEAT:
        return
    if time.time() - _last_sent < HEARTBEAT_INTERVAL_SECONDS:
        return

    try:
        import stats as bot_stats
        s = bot_stats.get()
    except Exception:
        s = {}

    embed = {
        "title":  "💚 PC Flip Scanner — Online",
        "color":  0x2ECC71,
        "fields": [
            {"name": "Scans Today",            "value": str(s.get("scan_count", 0)),           "inline": True},
            {"name": "Listings Scanned Today", "value": f"{s.get('listings_scanned', 0):,}",  "inline": True},
            {"name": "Alerts Sent Today",      "value": str(s.get("alerts_sent", 0)),          "inline": True},
            {"name": "Process Uptime",         "value": s.get("uptime", "unknown"),             "inline": True},
        ],
        "footer":    {"text": "PC Flip Scanner Heartbeat — if this stops, the bot is down"},
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    try:
        resp = requests.post(
            DISCORD_WEBHOOK_HEARTBEAT,
            json={"embeds": [embed]},
            timeout=10,
        )
        resp.raise_for_status()
        _last_sent = time.time()
        print("💚 Heartbeat sent.")
        _log(success=True)
    except Exception as exc:
        print(f"⚠️  Heartbeat failed: {exc}")
        _log(success=False, detail=str(exc))
