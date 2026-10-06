"""
discord_bot.py
==============
UPDATED: Added !market, !stats, !leaderboard commands; fixed !check for
Facebook URLs (now accepts pasted text too); flip persistence to flips.json
for leaderboards; monthly auto-post of community leaderboard to #flip-tracker.
"""

import datetime
import json
import os
import re
import threading
import time

import requests as req_lib

DISCORD_BOT_TOKEN            = os.getenv("DISCORD_BOT_TOKEN", "")
DISCORD_COMMAND_CHANNEL_ID   = int(os.getenv("DISCORD_COMMAND_CHANNEL_ID", "0"))
DISCORD_WEBHOOK_FLIP_TRACKER = os.getenv("DISCORD_WEBHOOK_FLIP_TRACKER", "")

WATCHLIST_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "watchlists.json")
FLIPS_PATH     = os.path.join(os.path.dirname(os.path.abspath(__file__)), "flips.json")

EBAY_FEE_RATE     = 0.1325
SHIPPING_COST_GPU = 30
SHIPPING_COST_PC  = 0

_last_leaderboard_month: int = -1


# ---------------------------------------------------------------------------
# Watchlist persistence
# ---------------------------------------------------------------------------

def _load_watchlists() -> dict:
    if not os.path.exists(WATCHLIST_PATH):
        return {}
    try:
        with open(WATCHLIST_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_watchlists(data: dict) -> None:
    try:
        with open(WATCHLIST_PATH, "w") as f:
            json.dump(data, f)
    except Exception as exc:
        print(f"⚠️  Could not save watchlists: {exc}")


def get_watchlist(user_id: str) -> list:
    return _load_watchlists().get(str(user_id), [])


def add_to_watchlist(user_id: str, gpu: str) -> bool:
    data = _load_watchlists()
    uid  = str(user_id)
    gpu  = gpu.lower().strip()
    if gpu not in data.get(uid, []):
        data.setdefault(uid, []).append(gpu)
        _save_watchlists(data)
        return True
    return False


def remove_from_watchlist(user_id: str, gpu: str) -> bool:
    data = _load_watchlists()
    uid  = str(user_id)
    gpu  = gpu.lower().strip()
    lst  = data.get(uid, [])
    if gpu in lst:
        lst.remove(gpu)
        data[uid] = lst
        _save_watchlists(data)
        return True
    return False


def get_members_watching(gpu_model: str) -> list:
    gpu_lower = gpu_model.lower()
    data      = _load_watchlists()
    return [uid for uid, gpus in data.items()
            if any(g in gpu_lower or gpu_lower in g for g in gpus)]


# ---------------------------------------------------------------------------
# Flip persistence & leaderboard
# ---------------------------------------------------------------------------

def _load_flips() -> list:
    if not os.path.exists(FLIPS_PATH):
        return []
    try:
        with open(FLIPS_PATH) as f:
            return json.load(f)
    except Exception:
        return []


def _save_flip(record: dict) -> None:
    flips = _load_flips()
    flips.append(record)
    try:
        with open(FLIPS_PATH, "w") as f:
            json.dump(flips, f)
    except Exception as exc:
        print(f"⚠️  Could not save flip: {exc}")


def _build_leaderboard(month: int, year: int) -> list:
    """Returns sorted [(uid, username, net_profit, flip_count)] for the given month."""
    totals = {}
    for flip in _load_flips():
        ts = flip.get("timestamp", 0)
        dt = datetime.datetime.fromtimestamp(ts)
        if dt.month != month or dt.year != year:
            continue
        uid = flip.get("user_id", "unknown")
        if uid not in totals:
            totals[uid] = {"username": flip.get("username", "Unknown"), "net": 0.0, "count": 0}
        totals[uid]["net"]   += flip.get("net", 0.0)
        totals[uid]["count"] += 1
    ranked = sorted(totals.items(), key=lambda x: x[1]["net"], reverse=True)
    return [(uid, d["username"], d["net"], d["count"]) for uid, d in ranked]


def _format_leaderboard_embed(month: int, year: int) -> dict:
    month_name = datetime.date(year, month, 1).strftime("%B %Y")
    ranked     = _build_leaderboard(month, year)

    if not ranked:
        return {
            "title":       f"🏆 Leaderboard — {month_name}",
            "color":       0xF1C40F,
            "description": "No flips logged yet this month. Use `!flip` to get on the board.",
            "footer":      {"text": "PC Flip Scanner"},
        }

    medals = ["🥇", "🥈", "🥉"]
    lines  = []
    for i, (uid, username, net, count) in enumerate(ranked[:10]):
        medal = medals[i] if i < 3 else f"**#{i+1}**"
        noun  = "flip" if count == 1 else "flips"
        lines.append(f"{medal} **{username}** — ${net:.0f} net ({count} {noun})")

    return {
        "title":       f"🏆 Monthly Leaderboard — {month_name}",
        "color":       0xF1C40F,
        "description": "\n".join(lines),
        "footer":      {"text": "Use !flip to log your wins • PC Flip Scanner"},
    }


def post_monthly_leaderboard_if_due() -> None:
    """
    Called from main.py's scan loop. Posts last month's leaderboard to
    #flip-tracker on the 1st of each new month, exactly once per month.
    """
    global _last_leaderboard_month
    if not DISCORD_WEBHOOK_FLIP_TRACKER:
        return

    now = datetime.datetime.now()
    if now.day != 1 or now.month == _last_leaderboard_month:
        return

    month = 12 if now.month == 1 else now.month - 1
    year  = now.year - 1 if now.month == 1 else now.year

    embed = _format_leaderboard_embed(month, year)
    try:
        req_lib.post(DISCORD_WEBHOOK_FLIP_TRACKER, json={"embeds": [embed]}, timeout=10)
        _last_leaderboard_month = now.month
        print(f"🏆 Monthly leaderboard posted for {datetime.date(year, month, 1).strftime('%B %Y')}.")
    except Exception as exc:
        print(f"⚠️  Leaderboard post failed: {exc}")


# ---------------------------------------------------------------------------
# Profit calculator
# ---------------------------------------------------------------------------

def calculate_net_profit(buy_price: float, sell_price: float,
                         is_full_system: bool = False) -> dict:
    ebay_fees    = sell_price * EBAY_FEE_RATE
    shipping     = SHIPPING_COST_PC if is_full_system else SHIPPING_COST_GPU
    gross        = sell_price - buy_price
    net          = sell_price - buy_price - ebay_fees - shipping
    roi_pct      = (net / buy_price * 100) if buy_price > 0 else 0
    return {
        "buy_price":    buy_price,
        "sell_price":   sell_price,
        "ebay_fees":    round(ebay_fees, 2),
        "shipping":     shipping,
        "gross_profit": round(gross, 2),
        "net_profit":   round(net, 2),
        "roi_pct":      round(roi_pct, 1),
    }


# ---------------------------------------------------------------------------
# Text-based listing helper (for !check without a URL)
# ---------------------------------------------------------------------------

def _extract_price_from_text(text: str) -> float:
    """
    Pulls a price from text. Only matches numbers explicitly preceded by a
    '$' sign — avoids false-matching GPU model numbers like '3080' or '4070'
    as prices when the user pastes listing details without a dollar sign.
    Returns 0.0 if no dollar-prefixed price is found.
    """
    m = re.search(r'\$\s*([\d,]+(?:\.\d{1,2})?)', text)
    if m:
        try:
            return float(m.group(1).replace(",", ""))
        except ValueError:
            pass
    return 0.0


def _build_listing_from_text(text: str) -> dict:
    """
    Builds a minimal listing dict from pasted listing details.
    The scoring engine runs identically on this dict — same result as
    if the bot had fetched the listing from Facebook directly.
    """
    return {
        "title":       text[:100],
        "description": text,
        "price":       _extract_price_from_text(text),
        "image_count": 0,
        "image_url":   "",
        "platform":    "Manual",
        "url":         "",
    }


# ---------------------------------------------------------------------------
# URL fetcher (Craigslist only — Facebook fetched via browser)
# ---------------------------------------------------------------------------

async def _fetch_listing_from_url(url: str) -> dict:
    """Fetches a Craigslist listing URL and returns a normalized dict."""
    if "craigslist.org" not in url.lower():
        return None

    import requests as r
    from bs4 import BeautifulSoup

    try:
        resp = r.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        soup = BeautifulSoup(resp.text, "html.parser")

        title_el = soup.select_one("#titletextonly, .postingtitletext") or soup.find("h2")
        title    = title_el.get_text(strip=True) if title_el else "Craigslist Listing"

        price_el = soup.select_one(".price")
        price    = 0.0
        if price_el:
            m = re.search(r"[\d,]+", price_el.get_text())
            if m:
                price = float(m.group().replace(",", ""))

        desc_el = soup.select_one("#postingbody, .body")
        desc    = desc_el.get_text(strip=True) if desc_el else ""

        lid_m      = re.search(r"(\d{10})\.html", url)
        listing_id = lid_m.group(1) if lid_m else url

        return {
            "title":       title,
            "description": desc,
            "price":       price,
            "image_count": 1,
            "image_url":   "",
            "platform":    "Craigslist",
            "url":         url,
            "listing_id":  listing_id,
        }
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Bot runner
# ---------------------------------------------------------------------------

def _run_bot():
    if not DISCORD_BOT_TOKEN:
        print("ℹ️  DISCORD_BOT_TOKEN not set — Discord bot commands disabled.")
        return

    try:
        import discord
    except ImportError:
        print("ℹ️  discord.py not installed — run: pip install discord.py")
        return

    intents                 = discord.Intents.default()
    intents.message_content = True
    client                  = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        print(f"🤖 Discord bot online as {client.user}")

    @client.event
    async def on_message(message):
        if message.author.bot:
            return
        if DISCORD_COMMAND_CHANNEL_ID and message.channel.id != DISCORD_COMMAND_CHANNEL_ID:
            return

        content = message.content.strip()
        parts   = content.split(None, 1)
        if not parts:
            return

        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        # ── !check ───────────────────────────────────────────────────────────
        if cmd == "!check":
            if not arg:
                await message.reply(
                    "**Usage:**\n"
                    "`!check <Craigslist URL>` — fetch and score a live listing\n"
                    "`!check RTX 3080, $350, 32GB RAM, Ryzen 7` — score from pasted details"
                )
                return

            # Facebook URLs can't be fetched — redirect to text input
            if "facebook.com" in arg.lower():
                await message.reply(
                    "📋 **Facebook listings can't be fetched by URL** — Facebook blocks direct access.\n\n"
                    "Paste the listing details instead:\n"
                    "`!check RTX 3080 Gaming PC, $450, 32GB RAM, 1TB SSD, Ryzen 7 5800X`\n\n"
                    "The bot scores it identically to a live scrape."
                )
                return

            await message.add_reaction("⏳")

            try:
                if arg.startswith("http"):
                    listing = await _fetch_listing_from_url(arg)
                    if not listing:
                        await message.reply(
                            "❌ Couldn't fetch that URL. Supported: Craigslist URLs.\n"
                            "For Facebook, paste the listing details as text instead."
                        )
                        return
                else:
                    listing = _build_listing_from_text(arg)
                    if listing["price"] == 0.0:
                        await message.reply(
                            "⚠️  Include a price so I can score profitability.\n"
                            "Example: `!check RTX 3080, **$350**, 16GB RAM, i7`"
                        )
                        return

                from scoring import calculate_deal_score
                score_data = calculate_deal_score(listing)
                score      = score_data["total_score"]
                profit     = score_data.get("predicted_profit", 0)
                est_val    = score_data.get("estimated_value", 0)
                parts_list = score_data.get("detected_parts", [])

                emoji   = "🔥" if score >= 75 else ("🟡" if score >= 55 else "🔴")
                verdict = "**BUY**" if score >= 75 else ("Maybe" if score >= 55 else "**Pass**")

                reply = (
                    f"{emoji} **Score: {score}/100 — {verdict}**\n"
                    f"Est. Value: **${est_val:.0f}** | Predicted Profit: **${profit:.0f}**\n"
                    f"Detected: {', '.join(parts_list[:4]) if parts_list else 'No parts detected — add more specs'}"
                )
                if arg.startswith("http"):
                    reply += f"\nLink: {arg}"

                await message.reply(reply)

            except Exception as exc:
                await message.reply(f"❌ Error scoring listing: {exc}")

            finally:
                try:
                    await message.remove_reaction("⏳", client.user)
                except Exception:
                    pass

        # ── !market ──────────────────────────────────────────────────────────
        elif cmd == "!market":
            if not arg:
                await message.reply(
                    "Usage: `!market rtx 3080` — live eBay sold comps and cheapest current listings."
                )
                return

            await message.add_reaction("⏳")

            try:
                from ebay_sold_comps import get_market_data
                data     = get_market_data(arg)
                sold     = data.get("sold_median")
                cheapest = data.get("cheapest_active")

                if sold is None and cheapest is None:
                    await message.reply(
                        f"❌ No eBay data found for **{arg}**.\n"
                        "Try a more specific model name, e.g. `rtx 3080 10gb`."
                    )
                    return

                lines = [f"📊 **eBay Market — {arg.upper()}**"]
                if sold:
                    lines.append(f"Sold median (last 10): **${sold:.0f}**")
                if cheapest:
                    lines.append(f"Cheapest active listing: **${cheapest:.0f}**")
                if sold and cheapest:
                    spread = sold - cheapest
                    lines.append(f"Buy-sell spread: **${spread:.0f}** potential margin before fees")

                await message.reply("\n".join(lines))

            except Exception as exc:
                await message.reply(f"❌ Market data error: {exc}")

            finally:
                try:
                    await message.remove_reaction("⏳", client.user)
                except Exception:
                    pass

        # ── !stats ───────────────────────────────────────────────────────────
        elif cmd == "!stats":
            try:
                import stats as bot_stats
                s     = bot_stats.get()
                emoji = "🟢" if s.get("scan_count", 0) > 0 else "🟡"
                await message.reply(
                    f"{emoji} **Bot Stats — Today**\n"
                    f"Scan cycles: **{s.get('scan_count', 0)}**\n"
                    f"Listings evaluated: **{s.get('listings_scanned', 0):,}**\n"
                    f"Alerts sent: **{s.get('alerts_sent', 0)}**\n"
                    f"GPUs detected: **{s.get('gpus_found', 0)}**\n"
                    f"Process uptime: **{s.get('uptime', 'unknown')}**"
                )
            except Exception:
                await message.reply("❌ Stats unavailable — bot may have just restarted.")

        # ── !leaderboard ─────────────────────────────────────────────────────
        elif cmd == "!leaderboard":
            now   = datetime.datetime.now()
            embed = _format_leaderboard_embed(now.month, now.year)
            try:
                e = discord.Embed(
                    title       = embed["title"],
                    description = embed.get("description", ""),
                    color       = embed.get("color", 0xF1C40F),
                )
                await message.reply(embed=e)
            except Exception:
                await message.reply(
                    f"**{embed['title']}**\n{embed.get('description', 'No data.')}"
                )

        # ── !watchlist ────────────────────────────────────────────────────────
        elif cmd == "!watchlist":
            sub_parts = arg.split(None, 1)
            sub_cmd   = sub_parts[0].lower() if sub_parts else ""
            gpu_name  = sub_parts[1].strip() if len(sub_parts) > 1 else ""

            if sub_cmd == "add":
                if not gpu_name:
                    await message.reply("Usage: `!watchlist add rtx 3080`")
                    return
                added = add_to_watchlist(str(message.author.id), gpu_name)
                if added:
                    await message.reply(f"✅ Added **{gpu_name}** to your watchlist. You'll be @mentioned when one appears.")
                else:
                    await message.reply(f"Already watching **{gpu_name}**.")

            elif sub_cmd == "remove":
                if not gpu_name:
                    await message.reply("Usage: `!watchlist remove rtx 3080`")
                    return
                removed = remove_from_watchlist(str(message.author.id), gpu_name)
                await message.reply(
                    f"✅ Removed **{gpu_name}** from your watchlist." if removed
                    else f"**{gpu_name}** wasn't on your watchlist."
                )

            elif sub_cmd == "show":
                wl = get_watchlist(str(message.author.id))
                if wl:
                    await message.reply("📋 Your watchlist:\n" + "\n".join(f"• {g}" for g in wl))
                else:
                    await message.reply("Your watchlist is empty. Add GPUs with `!watchlist add rtx 3080`")

            elif sub_cmd == "clear":
                data = _load_watchlists()
                data.pop(str(message.author.id), None)
                _save_watchlists(data)
                await message.reply("✅ Watchlist cleared.")

            else:
                await message.reply(
                    "**Watchlist commands:**\n"
                    "`!watchlist add <gpu>` — watch for a GPU\n"
                    "`!watchlist remove <gpu>` — stop watching\n"
                    "`!watchlist show` — see your list\n"
                    "`!watchlist clear` — clear everything"
                )

        # ── !profit ───────────────────────────────────────────────────────────
        elif cmd == "!profit":
            nums = arg.replace("$", "").split()
            if len(nums) < 2:
                await message.reply("Usage: `!profit <buy> <sell>` e.g. `!profit 300 390`")
                return
            try:
                buy  = float(nums[0])
                sell = float(nums[1])
                p    = calculate_net_profit(buy, sell)
                emoji = "✅" if p["net_profit"] > 0 else "❌"
                await message.reply(
                    f"{emoji} **Flip Calculator**\n"
                    f"Buy: **${buy:.0f}** → Sell: **${sell:.0f}**\n"
                    f"Gross: ${p['gross_profit']:.0f}\n"
                    f"eBay fees (13.25%): −${p['ebay_fees']:.0f}\n"
                    f"Shipping: −${p['shipping']:.0f}\n"
                    f"**Net profit: ${p['net_profit']:.0f} ({p['roi_pct']:.1f}% ROI)**"
                )
            except ValueError:
                await message.reply("❌ Prices must be numbers. Example: `!profit 300 390`")

        # ── !flip ─────────────────────────────────────────────────────────────
        elif cmd == "!flip":
            flip_parts = arg.replace("$", "").split(None, 2)
            if len(flip_parts) < 3:
                await message.reply(
                    "Usage: `!flip <buy> <sell> <item>` e.g. `!flip 300 390 RTX 3080`"
                )
                return
            try:
                buy  = float(flip_parts[0])
                sell = float(flip_parts[1])
                item = flip_parts[2]
                p    = calculate_net_profit(buy, sell)

                # Persist for leaderboard
                _save_flip({
                    "user_id":   str(message.author.id),
                    "username":  message.author.display_name,
                    "buy":       buy,
                    "sell":      sell,
                    "net":       p["net_profit"],
                    "item":      item,
                    "timestamp": time.time(),
                })

                win_msg = (
                    f"🏆 **Flip Win by {message.author.display_name}!**\n"
                    f"Item: **{item}**\n"
                    f"Bought: ${buy:.0f} → Sold: ${sell:.0f}\n"
                    f"**Net profit: ${p['net_profit']:.0f}** after fees & shipping\n"
                    f"ROI: {p['roi_pct']:.1f}%"
                )

                if DISCORD_WEBHOOK_FLIP_TRACKER:
                    req_lib.post(DISCORD_WEBHOOK_FLIP_TRACKER, json={"content": win_msg}, timeout=10)

                await message.reply(f"✅ Flip logged!\n{win_msg}")

            except ValueError:
                await message.reply("❌ Prices must be numbers.")

        # ── !help ─────────────────────────────────────────────────────────────
        elif cmd == "!help":
            await message.reply(
                "**📋 PC Flip Scanner Commands**\n"
                "`!check <url or listing details>` — score any listing\n"
                "`!market <gpu model>` — live eBay sold comps & current prices\n"
                "`!profit <buy> <sell>` — real net profit after eBay fees\n"
                "`!flip <buy> <sell> <item>` — log a flip win to #flip-tracker\n"
                "`!leaderboard` — this month's top flippers\n"
                "`!watchlist add/remove/show/clear <gpu>` — GPU alert watchlist\n"
                "`!stats` — bot scan stats for today\n"
                "`!help` — this message"
            )

    import asyncio
    asyncio.run(client.start(DISCORD_BOT_TOKEN))


def start_bot_thread():
    if not DISCORD_BOT_TOKEN:
        return
    thread = threading.Thread(target=_run_bot, daemon=True, name="DiscordBot")
    thread.start()
    print("🤖 Discord bot thread started.")