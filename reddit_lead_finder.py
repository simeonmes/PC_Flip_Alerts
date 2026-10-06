"""
reddit_lead_finder.py
======================
Standalone, personal-use tool for finding Reddit threads where people are
genuinely asking how to find good PC/GPU deals, and drafting a genuinely
helpful reply for MANUAL review. Deliberately SEPARATE from main.py's scan
loop — same pattern as macbook_finder.py: its own file, its own config,
does not touch SEARCH_QUERIES or run on the 15-minute schedule.

This script is READ-ONLY against Reddit. It never calls submit(), reply(),
comment(), or any other posting method — PRAW is instantiated without a
username/password so it runs as a read-only client by construction (see
`reddit.read_only` check below). Several of the target subreddits explicitly
ban AI/bot-generated content, and Reddit's site rules restrict automated
posting, so every draft this script produces is meant to be read, edited (or
rejected), and posted by a human from their own account — never auto-posted.

Run it on demand whenever you want to check for leads:
    python reddit_lead_finder.py

Setup required (see _env.example):
    REDDIT_CLIENT_ID       - from a Reddit "script" app at reddit.com/prefs/apps
    REDDIT_CLIENT_SECRET   - same app
    REDDIT_USER_AGENT      - free-text string identifying the app, e.g.
                              "reddit-lead-finder/1.0 by u/yourusername"
    ANTHROPIC_API_KEY      - already used by ai_features.py; reused here
"""

import json
import os
import re
import time

from dotenv import load_dotenv

load_dotenv(override=True)

import praw

from ai_features import _claude, HAIKU_MODEL

# ---------------------------------------------------------------------------
# Config — separate from the PC bot's config.py entirely
# ---------------------------------------------------------------------------

REDDIT_CLIENT_ID     = os.getenv("REDDIT_CLIENT_ID", "")
REDDIT_CLIENT_SECRET = os.getenv("REDDIT_CLIENT_SECRET", "")
REDDIT_USER_AGENT    = os.getenv("REDDIT_USER_AGENT", "reddit-lead-finder/1.0")

SUBREDDITS = [
    "buildapc",
    "pcmasterrace",
    "flipping",
    "pcflipping",
]

# Phrases people actually use when they're asking how to *find* deals, not
# just discussing hardware in general. Configurable so the list can be
# tuned over time without touching the search/scoring logic below.
SEARCH_KEYWORDS = [
    "how do you find deals",
    "how do you find good deals",
    "where do you find deals",
    "underpriced gpu",
    "underpriced pc",
    "before everyone else",
    "manually checking marketplace",
    "checking facebook marketplace",
    "how to find cheap gpu",
    "spot good deals",
    "scan marketplace",
    "flipping pcs for profit",
    "deal alert",
    "sniping deals",
]

RELEVANCE_THRESHOLD = int(os.getenv("REDDIT_RELEVANCE_THRESHOLD", "6"))
MAX_RESULTS_PER_RUN = int(os.getenv("REDDIT_MAX_RESULTS_PER_RUN", "10"))

# How hard each subreddit gets hit per run — kept small on purpose so this
# never comes close to Reddit's API rate limits.
SEARCH_LIMIT_PER_KEYWORD = 6
COMMENT_SCAN_LIMIT       = 75
SEARCH_TIME_FILTER       = "month"

SEEN_IDS_FILE   = "reddit_seen_ids.json"
OUTPUT_DIR      = "reddit_leads"

_KEYWORD_RE_LIST = [re.compile(re.escape(k), re.IGNORECASE) for k in SEARCH_KEYWORDS]


# ---------------------------------------------------------------------------
# Seen-ids tracking (same pattern as seen_listings.json)
# ---------------------------------------------------------------------------

def _load_seen_ids() -> dict:
    if os.path.exists(SEEN_IDS_FILE):
        try:
            with open(SEEN_IDS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _save_seen_ids(seen: dict) -> None:
    with open(SEEN_IDS_FILE, "w", encoding="utf-8") as f:
        json.dump(seen, f)


# ---------------------------------------------------------------------------
# Reddit client — read-only by construction (no username/password supplied)
# ---------------------------------------------------------------------------

def _make_reddit_client() -> praw.Reddit:
    reddit = praw.Reddit(
        client_id=REDDIT_CLIENT_ID,
        client_secret=REDDIT_CLIENT_SECRET,
        user_agent=REDDIT_USER_AGENT,
    )
    if not reddit.read_only:
        # Should be unreachable — no credentials that would grant a write
        # session are ever passed in — but fail loudly instead of silently
        # proceeding if that ever stops being true.
        raise RuntimeError("Reddit client is not read-only — refusing to proceed.")
    return reddit


def _text_matches_keyword(text: str) -> bool:
    return any(rx.search(text) for rx in _KEYWORD_RE_LIST)


# ---------------------------------------------------------------------------
# Candidate collection
# ---------------------------------------------------------------------------

def _collect_candidates(reddit: praw.Reddit, seen: dict) -> list:
    """
    Returns a list of candidate dicts:
      {kind, id, subreddit, title, url, author, text}
    kind is "post" or "comment". Already-seen ids are skipped.
    """
    candidates = {}

    for sub_name in SUBREDDITS:
        subreddit = reddit.subreddit(sub_name)

        # --- Posts: Reddit's native search, once per keyword ---
        for keyword in SEARCH_KEYWORDS:
            try:
                for submission in subreddit.search(
                    f'"{keyword}"', sort="new", time_filter=SEARCH_TIME_FILTER,
                    limit=SEARCH_LIMIT_PER_KEYWORD,
                ):
                    fullname = submission.fullname
                    if fullname in seen or fullname in candidates:
                        continue
                    body = f"{submission.title} {submission.selftext or ''}"
                    if not _text_matches_keyword(body):
                        continue  # Reddit search is fuzzy; re-check locally
                    candidates[fullname] = {
                        "kind":      "post",
                        "id":        fullname,
                        "subreddit": sub_name,
                        "title":     submission.title,
                        "url":       f"https://reddit.com{submission.permalink}",
                        "author":    str(submission.author) if submission.author else "[deleted]",
                        "text":      body.strip()[:1500],
                    }
            except Exception as exc:
                print(f"   ⚠️  search error r/{sub_name} '{keyword}': {exc}")

        # --- Comments: no native keyword-search endpoint via the public
        # API, so scan the subreddit's recent comment stream once and
        # match locally against the same keyword list. ---
        try:
            for comment in subreddit.comments(limit=COMMENT_SCAN_LIMIT):
                fullname = comment.fullname
                if fullname in seen or fullname in candidates:
                    continue
                body = comment.body or ""
                if not _text_matches_keyword(body):
                    continue
                try:
                    submission = comment.submission
                    title = submission.title
                except Exception:
                    title = "(comment — parent post unavailable)"
                candidates[fullname] = {
                    "kind":      "comment",
                    "id":        fullname,
                    "subreddit": sub_name,
                    "title":     title,
                    "url":       f"https://reddit.com{comment.permalink}",
                    "author":    str(comment.author) if comment.author else "[deleted]",
                    "text":      body.strip()[:1500],
                }
        except Exception as exc:
            print(f"   ⚠️  comment scan error r/{sub_name}: {exc}")

    return list(candidates.values())


# ---------------------------------------------------------------------------
# Claude scoring + drafting
# ---------------------------------------------------------------------------

_OWNER_CONTEXT = (
    "The tool's owner is an 18-year-old, self-taught Python developer who "
    "built an automated bot that scans Facebook Marketplace and Craigslist "
    "for underpriced PC/GPU listings, after previously doing all of this "
    "manually by refreshing marketplace tabs."
)


def _score_and_draft(candidate: dict) -> dict:
    """
    One combined Claude call: relevance score + draft reply.
    Returns candidate with "relevance", "reason", "draft_reply" added.
    Degrades to relevance=0 on failure so the caller filters it out.
    """
    prompt = f"""You're helping evaluate a Reddit {candidate['kind']} for a PC-flipper's lead-finding tool.

{_OWNER_CONTEXT} He wants to find threads where people are genuinely asking how to find good PC/GPU deals, and reply with real, helpful advice — never spammy self-promotion.

Subreddit: r/{candidate['subreddit']}
Thread title: {candidate['title']}
{candidate['kind'].capitalize()} text: \"\"\"{candidate['text']}\"\"\"

Step 1 — Score RELEVANCE 1-10: how genuinely is this person asking about finding/spotting good PC or GPU deals (vs. just mentioning hardware tangentially, asking for build help, troubleshooting, or something unrelated)? Score low (1-3) if hardware is mentioned but deal-finding isn't really what they're asking about.

Step 2 — Regardless of score, draft a short, casual, genuinely helpful Reddit reply:
- Lead with real, concrete advice (specific techniques: saved searches, price alerts, checking multiple platforms, timing, known price ranges, red flags to watch for, etc.)
- Casual tone, like a normal Redditor typing a quick reply, not corporate or salesy
- Only mention that you built a personal bot/script for this IF it naturally fits as a small part of genuinely answering the question — never force it, never include a link, never oversell it
- 3-6 sentences
- No hashtags, no "DM me", no emoji spam

Respond with ONLY valid JSON, no other text:
{{
  "relevance": 7,
  "reason": "one sentence explaining the score",
  "draft_reply": "the drafted reply text"
}}"""

    raw = _claude(prompt, max_tokens=400, model=HAIKU_MODEL)
    result = {**candidate, "relevance": 0, "reason": "Claude call failed", "draft_reply": ""}
    if not raw:
        return result

    try:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            parsed = json.loads(match.group())
            result["relevance"]   = int(parsed.get("relevance", 0))
            result["reason"]      = str(parsed.get("reason", ""))
            result["draft_reply"] = str(parsed.get("draft_reply", ""))
    except Exception as exc:
        result["reason"] = f"parse error: {exc}"

    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_reddit_lead_finder():
    print("\n" + "=" * 60)
    print("Reddit lead finder — standalone, read-only, personal use")
    print("=" * 60 + "\n")

    if not REDDIT_CLIENT_ID or not REDDIT_CLIENT_SECRET:
        print("❌ Missing REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET in .env — see _env.example.")
        print("   Register a free 'script' app at https://www.reddit.com/prefs/apps to get these.")
        return

    reddit = _make_reddit_client()
    print(f"✅ Reddit client ready (read_only={reddit.read_only})\n")

    seen = _load_seen_ids()

    print(f"🔍 Scanning {len(SUBREDDITS)} subreddits for {len(SEARCH_KEYWORDS)} keyword phrases...")
    candidates = _collect_candidates(reddit, seen)
    print(f"📦 {len(candidates)} new, unseen candidate thread(s)/comment(s) found.\n")

    surfaced = []
    for candidate in candidates:
        if len(surfaced) >= MAX_RESULTS_PER_RUN:
            break

        scored = _score_and_draft(candidate)
        seen[scored["id"]] = {"ts": time.time(), "relevance": scored["relevance"]}

        if scored["relevance"] < RELEVANCE_THRESHOLD:
            continue

        surfaced.append(scored)
        print(f"🎯 r/{scored['subreddit']} [{scored['kind']}] relevance={scored['relevance']}/10")
        print(f"   {scored['title'][:80]}")
        print(f"   🔗 {scored['url']}")
        print(f"   Why: {scored['reason']}")
        print(f"   Draft reply:\n      {scored['draft_reply']}\n")

    _save_seen_ids(seen)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out_path = os.path.join(OUTPUT_DIR, f"leads_{time.strftime('%Y%m%d_%H%M%S')}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(surfaced, f, indent=2)

    print(f"✅ Scan complete. {len(candidates)} candidate(s) evaluated, {len(surfaced)} passed "
          f"relevance >= {RELEVANCE_THRESHOLD}.")
    print(f"📝 Saved to {out_path}\n")
    print("⚠️  Nothing was posted. Review each draft yourself before replying manually.\n")


if __name__ == "__main__":
    run_reddit_lead_finder()
