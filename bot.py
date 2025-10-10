import os
import asyncio
import time
import random
import httpx
from datetime import datetime, timezone
from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

load_dotenv()

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAIN = os.getenv("DEFAULT_DEX_CHAIN", "").strip()
CONTRACT = os.getenv("DEFAULT_DEX_CONTRACT", "").strip()

DS_BASE = "https://api.dexscreener.com/latest/dex"
HEADERS = {
    # Identify yourself; many APIs rate-limit anonymous/default clients harder
    "User-Agent": "SendPriceBot/1.0 (+telegram)",
    "Accept": "application/json",
}

# --- Simple in-memory cache and cooldowns ---
CACHE_TTL_SEC = 20
CACHE = {}  # key: (chain, contract) -> {"t": epoch, "data": dict}
LAST_BY_CHAT = {}  # chat_id -> epoch of last call
CHAT_COOLDOWN_SEC = 3

async def fetch_dex_with_retries(url: str, max_retries: int = 3):
    """GET with polite backoff & Retry-After support; returns (json, rate_limited_info)"""
    retry_after_seen = None
    async with httpx.AsyncClient(timeout=15, headers=HEADERS) as client:
        for attempt in range(max_retries):
            r = await client.get(url)
            if r.status_code == 429:
                # Respect Retry-After if present; otherwise exponential backoff + jitter
                retry_after = r.headers.get("Retry-After")
                if retry_after:
                    try:
                        delay = float(retry_after)
                    except ValueError:
                        delay = 2 ** attempt + random.uniform(0.0, 0.5)
                else:
                    delay = 2 ** attempt + random.uniform(0.0, 0.5)
                retry_after_seen = delay
                await asyncio.sleep(delay)
                continue
            r.raise_for_status()
            return r.json(), None
    # Exceeded retries
    return None, retry_after_seen

async def ds_get_price(contract: str, chain_filter: str | None):
    # Cache check
    cache_key = (chain_filter or "", contract.lower())
    now = time.time()
    if (cached := CACHE.get(cache_key)) and (now - cached["t"] < CACHE_TTL_SEC):
        return cached["data"]

    url = f"{DS_BASE}/tokens/{contract}"
    data, ratelimit_delay = await fetch_dex_with_retries(url)
    if data is None:
        # Bubble up a friendly marker so we can tell the user
        return {"_rate_limited": True, "_retry_in": ratelimit_delay}

    pairs = data.get("pairs") or []
    if chain_filter:
        pairs = [p for p in pairs if p.get("chainId") == chain_filter]
    if not pairs:
        return None

    best = max(pairs, key=lambda p: (p.get("liquidity", {}) or {}).get("usd", 0))
    result = {
        "chain": best.get("chainId"),
        "dex": best.get("dexId"),
        "base": (best.get("baseToken") or {}).get("symbol"),
        "quote": (best.get("quoteToken") or {}).get("symbol"),
        "price_usd": float(best.get("priceUsd") or 0),
        "chg": (best.get("priceChange") or {}).get("h24"),
        "vol24": (best.get("volume") or {}).get("h24"),
        "liq": (best.get("liquidity") or {}).get("usd"),
    }
    CACHE[cache_key] = {"t": now, "data": result}
    return result

def indicator_circle(chg):
    if chg is None or abs(chg) < 1e-9:
        return "⚪"
    return "🟢" if chg > 0 else "🔴"

def trend_emoji(chg):
    if chg is None:
        return ""
    # Gains
    if chg > 50:
        return "🚀"
    if 25 < chg <= 50:
        return "✈️"
    if 10 < chg <= 25:
        return "🚁"
    if 0 < chg <= 10:
        return "🚚"
    # Losses
    if chg < -50:
        return "🏥"
    if -50 <= chg < -25:
        return "🚑"
    if -25 <= chg < -10:
        return "🤕"
    if -10 <= chg < 0:
        return "🩹"
    return ""

def fmt_msg(d: dict):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    circle = indicator_circle(d["chg"])
    tier = trend_emoji(d["chg"])
    chg_txt = f"{d['chg']:.2f}%" if d["chg"] is not None else "N/D"
    lines = [
        f"*{d['base']}/{d['quote']}* — {d['chain']} • {d['dex']} — {now}",
        f"{circle} ${d['price_usd']:.8f}",           # price line (no "USD:")
        f"• 24h: {tier} {chg_txt}",
        f"• Vol 24h: ${d['vol24']:,}" if d['vol24'] is not None else "• Vol 24h: N/D",
        f"• Liquidity: ${d['liq']:,}" if d['liq'] is not None else "• Liquidity: N/D",
        "_DexScreener_"
    ]
    return "\n".join(lines)

async def cmd_precio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Per-chat cooldown
    chat_id = update.effective_chat.id if update.effective_chat else None
    now = time.time()
    last = LAST_BY_CHAT.get(chat_id, 0)
    if now - last < CHAT_COOLDOWN_SEC:
        await update.message.reply_text("Give it a sec ⏳ (anti-spam cooldown).")
        return
    LAST_BY_CHAT[chat_id] = now

    if not CHAIN or not CONTRACT:
        await update.message.reply_text("Please configure DEFAULT_DEX_CHAIN and DEFAULT_DEX_CONTRACT.")
        return

    data = await ds_get_price(CONTRACT, CHAIN)
    if not data:
        await update.message.reply_text("No pairs found for the configured contract.")
        return

    # Handle rate-limit bubble-up
    if isinstance(data, dict) and data.get("_rate_limited"):
        delay = data.get("_retry_in")
        if delay is not None:
            await update.message.reply_text(f"Rate limited by DexScreener. Try again in ~{int(delay)}s.")
        else:
            await update.message.reply_text("Rate limited by DexScreener. Try again shortly.")
        return

    await update.message.reply_text(fmt_msg(data), parse_mode=ParseMode.MARKDOWN)

def main():
    if not TG_TOKEN:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN in environment.")
    app = ApplicationBuilder().token(TG_TOKEN).build()
    app.add_handler(CommandHandler("precio", cmd_precio))
    print("Bot ready (/precio).")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    asyncio.run(main())

