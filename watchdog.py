"""
watchdog.py
===========
Standalone downtime detector for the PC Flip Scanner. Run this as its OWN
OS process, separate from main.py — NOT imported by it, NOT a thread inside
it. That separation is the entire point.

Why a separate process instead of a check inside main.py's own loop:
  heartbeat.py's send_if_due() only runs as a side effect of main.py
  successfully completing a scan cycle. If main.py crashes, hangs on a
  blocking call, gets OOM-killed, or the terminal it's running in is closed,
  every piece of code that lives inside that process — including any
  in-process "have I scanned recently?" check — stops running at the exact
  same moment. Nothing is left alive to notice the silence or send an alert.
  A confirmed real-world case of this: 5 gaps of 19.8-24.9 hours across two
  weeks in seen_listings.json, none of which produced any alert, because the
  only thing that could have raised one had already gone down with the bot.

  This script has no dependency on main.py's process being alive. It reads
  a small timestamp file (scan_state.py) that main.py writes to on every
  successful scan cycle, and independently checks whether that timestamp is
  getting stale. As long as the OS itself is up, this process keeps running
  and keeps checking — including the whole time main.py is down — which is
  the only way to actually detect "main.py is down" instead of just hoping
  main.py notices its own death.

Usage: run alongside main.py (see run_watchdog.bat), e.g. in a second
terminal window, or registered as its own Windows Scheduled Task so it
starts automatically and survives independently of how main.py is launched.
"""

import json
import sys
import time
from pathlib import Path

import requests

# Same fix as main.py: standalone Windows Python defaults stdout/stderr to
# the legacy cp1252 console codepage, which crashes on this script's emoji.
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

from config import (
    DISCORD_WEBHOOK_WATCHDOG,
    WATCHDOG_CHECK_INTERVAL_SECONDS,
    WATCHDOG_DOWNTIME_THRESHOLD_SECONDS,
    SCAN_INTERVAL_SECONDS,
)
from scan_state import read_last_scan_ts

WATCHDOG_LOG_PATH = Path.home() / ".pc_flip_scanner" / "watchdog_log.jsonl"


def _log(status: str, seconds_since_last_scan: float) -> None:
    entry = {
        "timestamp":                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ts":                       time.time(),
        "status":                   status,   # "ok" | "down" | "recovered" | "never_started"
        "seconds_since_last_scan":  round(seconds_since_last_scan, 1),
    }
    try:
        WATCHDOG_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(WATCHDOG_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as exc:
        print(f"⚠️  Could not write watchdog log: {exc}")


def _send_discord_alert(title: str, description: str, color: int) -> bool:
    if not DISCORD_WEBHOOK_WATCHDOG:
        print("⚠️  DISCORD_WEBHOOK_WATCHDOG (and DISCORD_WEBHOOK_HEARTBEAT) both "
              "unset — watchdog has no way to alert. Set one in .env.")
        return False
    embed = {
        "title":       title,
        "description": description,
        "color":       color,
        "timestamp":   time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "footer":      {"text": "PC Flip Scanner Watchdog"},
    }
    try:
        resp = requests.post(DISCORD_WEBHOOK_WATCHDOG, json={"embeds": [embed]}, timeout=10)
        resp.raise_for_status()
        return True
    except Exception as exc:
        print(f"⚠️  Watchdog alert send failed: {exc}")
        return False


def check_once(now: float = None) -> str:
    """
    Pure-ish single check: reads scan_state, decides ok/down/recovered,
    sends an alert exactly on the ok->down and down->ok transitions (never
    repeats an alert every poll cycle while still down), logs every check
    regardless. Returns the status string. `now` is injectable for testing.
    """
    now = now if now is not None else time.time()
    last_scan_ts = read_last_scan_ts()

    if last_scan_ts == 0.0:
        _log("never_started", 0.0)
        return "never_started"

    gap = now - last_scan_ts
    is_down = gap > WATCHDOG_DOWNTIME_THRESHOLD_SECONDS

    state = _read_alert_state()
    was_alerted_down = state.get("alerted_down", False)

    if is_down and not was_alerted_down:
        minutes = gap / 60
        _send_discord_alert(
            title="🔴 PC Flip Scanner — SCANNER APPEARS DOWN",
            description=(
                f"No scan cycle has completed in **{minutes:.0f} minutes** "
                f"(normal cycle is every {SCAN_INTERVAL_SECONDS // 60} min). "
                f"main.py has likely crashed, hung, or stopped running."
            ),
            color=0xE74C3C,
        )
        _write_alert_state({"alerted_down": True})
        _log("down", gap)
        return "down"

    if is_down and was_alerted_down:
        _log("down", gap)
        return "down"

    if not is_down and was_alerted_down:
        _send_discord_alert(
            title="🟢 PC Flip Scanner — Scanner Recovered",
            description="Scan cycles have resumed.",
            color=0x2ECC71,
        )
        _write_alert_state({"alerted_down": False})
        _log("recovered", gap)
        return "recovered"

    _log("ok", gap)
    return "ok"


_ALERT_STATE_PATH = Path.home() / ".pc_flip_scanner" / "watchdog_alert_state.json"


def _read_alert_state() -> dict:
    try:
        with open(_ALERT_STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _write_alert_state(state: dict) -> None:
    try:
        _ALERT_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_ALERT_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception as exc:
        print(f"⚠️  Could not write watchdog alert state: {exc}")


def run_forever() -> None:
    print(f"🐕 Watchdog started — checking every {WATCHDOG_CHECK_INTERVAL_SECONDS}s, "
          f"alerting after {WATCHDOG_DOWNTIME_THRESHOLD_SECONDS // 60} min of silence.")
    while True:
        try:
            status = check_once()
            if status != "ok":
                print(f"🐕 Watchdog check: {status}")
        except Exception as exc:
            print(f"⚠️  Watchdog check error: {exc}")
        time.sleep(WATCHDOG_CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    run_forever()
