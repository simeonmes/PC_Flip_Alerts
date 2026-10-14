"""
scoreboard.py
=============
Standalone accuracy check for the scoring engine. Runs a fixed set of REAL
listings (scoreboard_listings.json) through the same scorer and alert gate
the live scan uses, and compares each result to a label you gave it:
"should this have alerted, yes or no?"

Why this exists: without it, the only way to judge a scoring change is to
watch the next few live alerts — which is mostly luck, and never shows the
listings a change quietly broke. With it, a change either moves the number
or it doesn't, and anything that flips from right to wrong is named.

Offline and read-only against the outside world: no scraping, no Discord,
no AI calls. It measures the deterministic scorer + gate only — the AI
photo/scam checks in notifier.py run after this point and can still hold an
alert the scorer let through.

Usage:
    python scoreboard.py                  # score every labelled listing
    python scoreboard.py label            # label the unlabelled ones (y/n)
    python scoreboard.py add <fb url>     # add a listing the scanner has read
"""

import json
import os
import re
import sys
from pathlib import Path

# Importing main is what guarantees this uses the live scan's own gate. It
# also applies the stdout UTF-8 fix this script needs for the same reason.
from main import passes_alert_gate

from config import MAX_PRICE_USD, MIN_PRICE_USD
from facebook_scraper import detect_description_bundle_price, _DETAIL_CACHE_PATH
from scoring import calculate_deal_score, is_junk_category_listing

_HERE         = os.path.dirname(os.path.abspath(__file__))
LISTINGS_PATH = os.path.join(_HERE, "scoreboard_listings.json")
# Last run's verdicts, for the "changed since last run" section. Generated
# state, so it lives with the other runtime files rather than in the repo.
LAST_RUN_PATH = Path.home() / ".pc_flip_scanner" / "scoreboard_last_run.json"


def _load_cases() -> list:
    if not os.path.exists(LISTINGS_PATH):
        return []
    with open(LISTINGS_PATH, encoding="utf-8") as f:
        return json.load(f)


def _save_cases(cases: list) -> None:
    with open(LISTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(cases, f, indent=2, ensure_ascii=False)


def _verdict(case: dict) -> dict:
    """
    Runs one case through the same steps as main.run_scan_cycle(): junk-title
    drop, price band, scorer, alert gate. Returns what the bot would do and why.
    """
    title = case.get("title", "")
    desc  = case.get("description", "")
    price = float(case.get("price", 0) or 0)

    if is_junk_category_listing(title):
        return {"alert": False, "why": "dropped: junk-category title"}
    if not (MIN_PRICE_USD <= price <= MAX_PRICE_USD):
        return {"alert": False, "why": f"dropped: price ${price:.0f} outside ${MIN_PRICE_USD:.0f}-${MAX_PRICE_USD:.0f}"}

    listing = {
        "title":                    title,
        "description":              desc,
        "price":                    price,
        "image_count":              case.get("image_count", 1),
        "platform":                 case.get("platform", "Facebook"),
        "url":                      case.get("url", ""),
        "description_bundle_price": detect_description_bundle_price(desc),
    }
    score_data = calculate_deal_score(listing)
    parts      = score_data.get("detected_parts", [])
    return {
        "alert":    passes_alert_gate(listing, score_data),
        "why":      (f"score {score_data['total_score']} | est ${score_data['estimated_value']:.0f} "
                     f"| profit ${score_data['predicted_profit']:.0f} "
                     f"| {listing.get('category_override') or 'no category'}"),
        "parts":    parts,
    }


def _print_case(case: dict, verdict: dict) -> None:
    print(f"   • ${case['price']:.0f}  {case['title'][:70]}")
    print(f"       {verdict['why']}")
    for part in verdict.get("parts", [])[:6]:
        print(f"         - {part}")
    if case.get("note"):
        print(f"       your note: {case['note']}")
    if case.get("url"):
        print(f"       {case['url']}")


def run() -> None:
    cases = _load_cases()
    if not cases:
        print(f"❌ No listings in {LISTINGS_PATH}.")
        return

    labelled   = [c for c in cases if c.get("label") in ("alert", "skip")]
    unlabelled = len(cases) - len(labelled)
    if not labelled:
        print(f"ℹ️  {len(cases)} listings loaded but none are labelled yet — run:  python scoreboard.py label")
        return

    caught, missed, rejected, false_alerts = [], [], [], []
    this_run = {}
    for case in labelled:
        verdict = _verdict(case)
        correct = verdict["alert"] == (case["label"] == "alert")
        this_run[case["id"]] = correct
        bucket = (caught if verdict["alert"] else missed) if case["label"] == "alert" \
            else (false_alerts if verdict["alert"] else rejected)
        bucket.append((case, verdict))

    good_total = len(caught) + len(missed)
    bad_total  = len(rejected) + len(false_alerts)
    right      = len(caught) + len(rejected)

    print("\n" + "=" * 60)
    print(f"SCOREBOARD — {len(labelled)} labelled listing(s)"
          + (f"  ({unlabelled} still unlabelled)" if unlabelled else ""))
    print("=" * 60)
    print(f"  Good deals caught:      {len(caught)} / {good_total}")
    print(f"  Bad listings rejected:  {len(rejected)} / {bad_total}")
    print(f"  Overall:                {right} / {len(labelled)}  ({right / len(labelled):.0%})")

    if missed:
        print(f"\n❌ MISSED — you said alert, the bot would skip ({len(missed)}):")
        for case, verdict in missed:
            _print_case(case, verdict)
    if false_alerts:
        print(f"\n❌ FALSE ALERTS — you said skip, the bot would alert ({len(false_alerts)}):")
        for case, verdict in false_alerts:
            _print_case(case, verdict)

    # ── What changed since the previous run ──────────────────────────────────
    try:
        with open(LAST_RUN_PATH, encoding="utf-8") as f:
            last_run = json.load(f)
    except Exception:
        last_run = {}
    by_id  = {c["id"]: c for c in labelled}
    fixed  = [i for i, ok in this_run.items() if ok and last_run.get(i) is False]
    broke  = [i for i, ok in this_run.items() if not ok and last_run.get(i) is True]
    if fixed or broke:
        print("\n🔁 Changed since last run:")
        for i in fixed:
            print(f"   ✅ now right:  {by_id[i]['title'][:70]}")
        for i in broke:
            print(f"   ⚠️  now WRONG:  {by_id[i]['title'][:70]}")
    elif last_run:
        print("\n🔁 No listing changed result since last run.")

    try:
        LAST_RUN_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LAST_RUN_PATH, "w", encoding="utf-8") as f:
            json.dump(this_run, f)
    except Exception as exc:
        print(f"⚠️  Could not save last-run results: {exc}")
    print()


def label() -> None:
    """
    Walks the unlabelled listings one at a time. Deliberately does NOT show
    what the bot currently thinks of each one — the label should be your own
    judgement of the listing, not agreement or disagreement with the scorer.
    """
    cases   = _load_cases()
    pending = [c for c in cases if c.get("label") not in ("alert", "skip", "unsure")]
    if not pending:
        print("✅ Every listing is already labelled.")
        return

    print(f"\n{len(pending)} listing(s) to label. For each one:")
    print("   y = yes, I'd want this alert      n = no, shouldn't alert")
    print("   u = unsure (left out of the score) q = save and quit\n")

    for n, case in enumerate(pending, 1):
        print("-" * 60)
        print(f"[{n}/{len(pending)}]  ${case['price']:.0f}  —  {case['title']}")
        print(f"{case.get('description', '') or '(no description)'}")
        if case.get("url"):
            print(case["url"])
        answer = ""
        while answer not in ("y", "n", "u", "q"):
            answer = input("alert? [y/n/u/q] ").strip().lower()
        if answer == "q":
            break
        case["label"] = {"y": "alert", "n": "skip", "u": "unsure"}[answer]
        note = input("why? (optional, Enter to skip) ").strip()
        if note:
            case["note"] = note
        _save_cases(cases)   # saved after every answer, so quitting never loses work

    done = sum(1 for c in cases if c.get("label") in ("alert", "skip"))
    print(f"\n✅ Saved. {done} of {len(cases)} labelled.  Run:  python scoreboard.py")


def add(ref: str) -> None:
    """
    Adds a Facebook listing to the set by URL or listing ID, using the title
    and description the scanner already read from its item page — so a bad
    alert (or a deal the bot missed) can be turned into a permanent test case
    the moment you notice it.
    """
    match      = re.search(r"(\d{8,})", ref)
    listing_id = match.group(1) if match else ref
    cases      = _load_cases()
    if any(c["id"] == listing_id for c in cases):
        print(f"ℹ️  {listing_id} is already in the set.")
        return
    try:
        with open(_DETAIL_CACHE_PATH, encoding="utf-8") as f:
            entry = json.load(f).get(listing_id)
    except Exception:
        entry = None
    if not entry:
        print(f"❌ The scanner hasn't read listing {listing_id}'s page (not in {_DETAIL_CACHE_PATH}).\n"
              f"   Add it by hand to {LISTINGS_PATH} with its title, description and price.")
        return
    cases.append({
        "id":          listing_id,
        "url":         f"https://www.facebook.com/marketplace/item/{listing_id}/",
        "platform":    "Facebook",
        "title":       entry.get("title", ""),
        "description": entry.get("description", ""),
        "price":       entry.get("price", 0),
        "label":       None,
        "note":        "",
    })
    _save_cases(cases)
    print(f"✅ Added '{entry.get('title', '')[:60]}'. Label it with:  python scoreboard.py label")


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "run"
    if command == "label":
        label()
    elif command == "add" and len(sys.argv) > 2:
        add(sys.argv[2])
    elif command == "run":
        run()
    else:
        print(__doc__)
