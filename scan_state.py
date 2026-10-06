"""
scan_state.py
==============
Tiny shared state file recording when the scanner last completed a scan
cycle. Written by main.py at the end of every successful run_scan_cycle();
read by watchdog.py, a *separate* OS process, to detect when the scanner
has gone silent (crashed, hung, or the terminal/machine was closed) — even
though nothing left running inside main.py's own process could report that
itself. Deliberately dumb and dependency-free (just a JSON file) so it
can't be the thing that breaks.
"""

import json
import time
from pathlib import Path

STATE_PATH = Path.home() / ".pc_flip_scanner" / "scan_state.json"


def record_scan_complete() -> None:
    """Called by main.py at the end of every successful scan cycle."""
    state = {
        "last_scan_completed_ts":  time.time(),
        "last_scan_completed_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception as exc:
        print(f"⚠️  Could not write scan state: {exc}")


def read_last_scan_ts() -> float:
    """Returns 0.0 if the state file doesn't exist yet or can't be read."""
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return float(json.load(f).get("last_scan_completed_ts", 0.0))
    except Exception:
        return 0.0
