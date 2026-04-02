"""
Telegram bot for polymarket-taker.

Two-tier access:
  - Admin (by telegram user ID): full control via /commands
  - Granted users: can only send signals via [4] [6] [W] inline buttons

Communicates with the Rust taker binary via its HTTP API (default localhost:3000).
"""

import json
import asyncio
import logging
from pathlib import Path

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent / "config.json"

# ── Config ────────────────────────────────────────────────────────────────────


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return json.load(f)


def save_config(cfg: dict):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=4)


CFG = load_config()
TAKER_URL = CFG["taker_url"]
ADMIN_ID = int(CFG["telegram_admin_id"])
GRANTED_USERS: set[int] = set(CFG.get("granted_users", []))

# Shared async HTTP client
http = httpx.AsyncClient(timeout=10.0)

# ── Helpers ───────────────────────────────────────────────────────────────────

SIGNAL_KEYBOARD = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("4", callback_data="4"),
        InlineKeyboardButton("6", callback_data="6"),
        InlineKeyboardButton("W", callback_data="W"),
    ]
])


def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


def is_granted(user_id: int) -> bool:
    return user_id in GRANTED_USERS or is_admin(user_id)


async def taker_get(path: str) -> dict | list | None:
    try:
        r = await http.get(f"{TAKER_URL}{path}")
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.error(f"GET {path} failed: {e}")
        return None


async def taker_post(path: str, body: dict | None = None) -> tuple[bool, str]:
    try:
        r = await http.post(f"{TAKER_URL}{path}", json=body or {})
        data = r.json() if r.status_code < 500 else {}
        if r.status_code >= 400:
            err = data.get("error", "") or r.text
            return False, str(err)
        return True, json.dumps(data)
    except Exception as e:
        return False, str(e)


def fmt_status(s: dict) -> str:
    return (
        f"*Phase:* `{s.get('phase', '?')}`\n"
        f"*Innings:* {s.get('innings', '?')} | "
        f"*Batting:* {s.get('batting', '?')} | *Bowling:* {s.get('bowling', '?')}\n"
        f"*{s.get('team_a_name', 'A')}:* {s.get('team_a_tokens', 0)} tokens\n"
        f"*{s.get('team_b_name', 'B')}:* {s.get('team_b_tokens', 0)} tokens\n"
        f"*Spent:* {s.get('total_spent', 0)} / {s.get('total_budget', 0)} "
        f"(remaining: {s.get('remaining', 0)})\n"
        f"*Trades:* {s.get('trade_count', 0)} | "
        f"*Live orders:* {s.get('live_orders', 0)} | "
        f"*Pending reverts:* {s.get('pending_reverts', 0)}\n"
        f"*Dry run:* {s.get('dry_run', '?')}"
    )


# ── Admin commands ────────────────────────────────────────────────────────────


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("unauthorized")
        return
    data = await taker_get("/api/status")
    if data:
        await update.message.reply_text(fmt_status(data), parse_mode="Markdown")
    else:
        await update.message.reply_text("failed to reach taker")


async def cmd_setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("unauthorized")
        return
    if not context.args:
        await update.message.reply_text("usage: /setup <market-slug>")
        return
    slug = context.args[0]
    ok, resp = await taker_post("/api/fetch-market", {"slug": slug})
    if ok:
        await update.message.reply_text(f"market set: {resp}")
    else:
        await update.message.reply_text(f"setup failed: {resp}")


async def cmd_limits(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("unauthorized")
        return
    if len(context.args) < 2:
        await update.message.reply_text("usage: /limits <budget> <max_trade>")
        return
    body = {
        "total_budget_usdc": context.args[0],
        "max_trade_usdc": context.args[1],
    }
    ok, resp = await taker_post("/api/limits", body)
    await update.message.reply_text(
        f"limits updated: budget={context.args[0]}, max_trade={context.args[1]}" if ok
        else f"failed: {resp}"
    )


async def cmd_dryrun(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("unauthorized")
        return
    if not context.args or context.args[0] not in ("on", "off"):
        await update.message.reply_text("usage: /dryrun on|off")
        return
    val = context.args[0] == "on"
    ok, resp = await taker_post("/api/limits", {"dry_run": val})
    await update.message.reply_text(f"dry_run={val}" if ok else f"failed: {resp}")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("unauthorized")
        return
    ok, resp = await taker_post("/api/start-innings")
    await update.message.reply_text("innings started" if ok else f"failed: {resp}")


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("unauthorized")
        return
    ok, resp = await taker_post("/api/stop-innings")
    await update.message.reply_text("innings stopped" if ok else f"failed: {resp}")


async def cmd_matchover(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("unauthorized")
        return
    ok, resp = await taker_post("/api/match-over")
    await update.message.reply_text("match ended" if ok else f"failed: {resp}")


async def cmd_book(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("unauthorized")
        return
    data = await taker_get("/api/book")
    if data:
        lines = []
        for team_key in ("team_a", "team_b"):
            t = data.get(team_key, {})
            name = t.get("name", team_key)
            bids = t.get("bids", [])
            asks = t.get("asks", [])
            best_bid = f"{bids[0]['price']}" if bids else "-"
            best_ask = f"{asks[0]['price']}" if asks else "-"
            lines.append(f"*{name}*: bid={best_bid} ask={best_ask}")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    else:
        await update.message.reply_text("failed to fetch book")


async def cmd_trades(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("unauthorized")
        return
    data = await taker_get("/api/trades")
    if data and isinstance(data, list):
        recent = data[-5:] if len(data) > 5 else data
        if not recent:
            await update.message.reply_text("no trades yet")
            return
        lines = []
        for t in recent:
            lines.append(
                f"`{t.get('ts', '')}` {t.get('side', '')} "
                f"{t.get('team', '')} {t.get('size', '')}@{t.get('price', '')} "
                f"[{t.get('order_type', '')}]"
            )
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    else:
        await update.message.reply_text("failed to fetch trades")


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("unauthorized")
        return
    ok, resp = await taker_post("/api/cancel-all")
    await update.message.reply_text("all orders cancelled" if ok else f"failed: {resp}")


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("unauthorized")
        return
    ok, resp = await taker_post("/api/reset")
    await update.message.reply_text("match reset" if ok else f"failed: {resp}")


async def cmd_grant(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("unauthorized")
        return
    if not context.args:
        await update.message.reply_text("usage: /grant <telegram_user_id>")
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("invalid user ID")
        return
    GRANTED_USERS.add(uid)
    CFG["granted_users"] = list(GRANTED_USERS)
    save_config(CFG)
    await update.message.reply_text(f"user {uid} granted signal access")


async def cmd_revoke(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("unauthorized")
        return
    if not context.args:
        await update.message.reply_text("usage: /revoke <telegram_user_id>")
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("invalid user ID")
        return
    GRANTED_USERS.discard(uid)
    CFG["granted_users"] = list(GRANTED_USERS)
    save_config(CFG)
    await update.message.reply_text(f"user {uid} revoked")


# ── User signal buttons ──────────────────────────────────────────────────────


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Any message from a granted user shows the signal keyboard."""
    uid = update.effective_user.id
    if not is_granted(uid):
        return  # silently ignore non-granted users

    await update.message.reply_text(
        "tap a signal:", reply_markup=SIGNAL_KEYBOARD
    )


async def handle_signal_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle [4] [6] [W] button presses."""
    query = update.callback_query
    uid = query.from_user.id
    if not is_granted(uid):
        await query.answer("unauthorized")
        return

    signal = query.data  # "4", "6", or "W"
    await query.answer()  # dismiss the loading spinner

    ok, resp = await taker_post("/api/signal", {"signal": signal})

    signal_names = {"4": "BOUNDARY 4", "6": "BOUNDARY 6", "W": "WICKET"}
    name = signal_names.get(signal, signal)

    if ok:
        await query.edit_message_text(
            f"{name} sent",
            reply_markup=SIGNAL_KEYBOARD,
        )
    else:
        await query.edit_message_text(
            f"{name} failed: {resp}",
            reply_markup=SIGNAL_KEYBOARD,
        )


# ── Event notifications to admin ─────────────────────────────────────────────

NOTIFY_KINDS = {"filled", "error", "innings", "wicket", "boundary", "breakeven", "revert"}


async def event_poller(app: Application):
    """Background task: poll /api/events every 3s, forward new events to admin."""
    last_ts = ""
    bot = app.bot

    while True:
        await asyncio.sleep(3)
        try:
            data = await taker_get("/api/events")
            if not data or not isinstance(data, list):
                continue

            new_events = []
            for evt in data:
                ts = evt.get("ts", "")
                if ts > last_ts:
                    kind = evt.get("kind", "")
                    if kind in NOTIFY_KINDS:
                        new_events.append(evt)

            if data:
                last_ts = data[-1].get("ts", last_ts)

            for evt in new_events:
                msg = f"`{evt.get('ts', '')}` [{evt.get('kind', '')}] {evt.get('detail', '')}"
                try:
                    await bot.send_message(
                        chat_id=ADMIN_ID, text=msg, parse_mode="Markdown"
                    )
                except Exception as e:
                    logger.warning(f"failed to notify admin: {e}")

        except Exception as e:
            logger.warning(f"event poller error: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────


async def post_init(app: Application):
    """Send startup message to admin and start event poller."""
    data = await taker_get("/api/status")
    phase = data.get("phase", "unknown") if data else "unreachable"
    try:
        await app.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"bot connected, taker at {TAKER_URL}, phase={phase}",
        )
    except Exception as e:
        logger.warning(f"failed to send startup message: {e}")

    asyncio.create_task(event_poller(app))


def main():
    token = CFG.get("telegram_bot_token", "")
    if not token:
        logger.error("telegram_bot_token not set in config.json")
        return

    if ADMIN_ID == 0:
        logger.error("telegram_admin_id not set in config.json")
        return

    app = Application.builder().token(token).post_init(post_init).build()

    # Admin commands
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("setup", cmd_setup))
    app.add_handler(CommandHandler("limits", cmd_limits))
    app.add_handler(CommandHandler("dryrun", cmd_dryrun))
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("matchover", cmd_matchover))
    app.add_handler(CommandHandler("book", cmd_book))
    app.add_handler(CommandHandler("trades", cmd_trades))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("grant", cmd_grant))
    app.add_handler(CommandHandler("revoke", cmd_revoke))

    # Signal buttons callback
    app.add_handler(CallbackQueryHandler(handle_signal_button))

    # Any text from granted user → show signal keyboard
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info(f"starting bot, admin={ADMIN_ID}, granted={GRANTED_USERS}")
    app.run_polling()


if __name__ == "__main__":
    main()
