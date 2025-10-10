import os
import asyncio
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

async def ds_get_price(contract: str, chain_filter: str | None):
    url = f"{DS_BASE}/tokens/{contract}"
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(url)
        r.raise_for_status()
        data = r.json()

    pairs = data.get("pairs") or []
    if chain_filter:
        pairs = [p for p in pairs if p.get("chainId") == chain_filter]
    if not pairs:
        return None

    best = max(pairs, key=lambda p: (p.get("liquidity", {}) or {}).get("usd", 0))
    return {
        "chain": best.get("chainId"),
        "dex": best.get("dexId"),
        "base": (best.get("baseToken") or {}).get("symbol"),
        "quote": (best.get("quoteToken") or {}).get("symbol"),
        "price_usd": float(best.get("priceUsd") or 0),
        "chg": (best.get("priceChange") or {}).get("h24"),   # % float o None
        "vol24": (best.get("volume") or {}).get("h24"),
        "liq": (best.get("liquidity") or {}).get("usd"),
    }

def indicator_circle(chg):
    if chg is None or abs(chg) < 1e-9:
        return "⚪"
    return "🟢" if chg > 0 else "🔴"

def trend_emoji(chg):
    if chg is None:
        return ""
    # Subidas
    if chg > 50:
        return "🚀"
    if 25 < chg <= 50:
        return "✈️"
    if 10 < chg <= 25:
        return "🚁"
    if 0 < chg <= 10:
        return "🚚"
    # Bajadas
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
        f"{circle} ${d['price_usd']:.8f}",                 # ← sin 'USD:'
        f"• 24h: {tier} {chg_txt}",
        f"• Vol 24h: ${d['vol24']:,}" if d['vol24'] is not None else "• Vol 24h: N/D",
        f"• Liquidity: ${d['liq']:,}" if d['liq'] is not None else "• Liquidez: N/D",
        "_DexScreener_"
    ]
    return "\n".join(lines)

async def cmd_precio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not CHAIN or not CONTRACT:
        await update.message.reply_text("Configura DEFAULT_DEX_CHAIN y DEFAULT_DEX_CONTRACT.")
        return
    data = await ds_get_price(CONTRACT, CHAIN)
    if not data:
        await update.message.reply_text("Sin pares para el contrato configurado.")
        return
    await update.message.reply_text(fmt_msg(data), parse_mode=ParseMode.MARKDOWN)

def main():
    if not TG_TOKEN:
        raise RuntimeError("Falta TELEGRAM_BOT_TOKEN en .env")
    app = ApplicationBuilder().token(TG_TOKEN).build()
    app.add_handler(CommandHandler("precio", cmd_precio))
    print("Bot listo (/precio).")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    asyncio.run(main())

