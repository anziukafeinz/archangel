"""Telegram control bot for Archangel.

Commands::

    /start                            — show help
    /balance                          — show wallet balance & equity
    /positions                        — list open positions
    /risk                             — show active risk configuration
    /long  SYMBOL  STOP  [TP]         — open a long with auto position sizing
    /short SYMBOL  STOP  [TP]         — open a short with auto position sizing
    /close SYMBOL                     — close one symbol
    /flatten                          — kill switch: close everything
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

from archangel.config import get_settings
from archangel.exchange import BinanceFuturesClient, OrderSide
from archangel.risk import RiskGuard, RiskViolation
from archangel.trading import TradingService

logger = logging.getLogger(__name__)


HELP_TEXT = (
    "*Archangel*\n"
    "/balance — wallet balance & equity\n"
    "/positions — open positions\n"
    "/risk — risk configuration\n"
    "/long SYMBOL STOP \\[TP\\] — open long with auto sizing\n"
    "/short SYMBOL STOP \\[TP\\] — open short with auto sizing\n"
    "/close SYMBOL — close a symbol\n"
    "/flatten — kill switch: close everything"
)


def _authorized(update: Update, allowed_chat_id: str) -> bool:
    if not allowed_chat_id:
        return False
    chat = update.effective_chat
    return chat is not None and str(chat.id) == str(allowed_chat_id)


async def _reject_unauthorized(update: Update) -> bool:
    settings = get_settings()
    if not _authorized(update, settings.telegram_chat_id):
        if update.effective_chat is not None:
            logger.warning("Unauthorized chat %s", update.effective_chat.id)
        return True
    return False


def _build_service() -> tuple[TradingService, BinanceFuturesClient]:
    settings = get_settings()
    client = BinanceFuturesClient(
        api_key=settings.binance_api_key,
        api_secret=settings.binance_api_secret,
        testnet=settings.binance_testnet,
    )
    guard = RiskGuard(settings)
    return TradingService(client, guard, settings), client


# ---- Handlers --------------------------------------------------------


async def cmd_start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    await update.message.reply_markdown(HELP_TEXT)


async def cmd_balance(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    service, client = _build_service()
    try:
        await client.connect()
        snap = await service.account()
        msg = (
            f"*Balance*\n"
            f"Wallet: `{snap.total_wallet_balance:.4f}` USDT\n"
            f"Equity: `{snap.total_margin_balance:.4f}` USDT\n"
            f"Available: `{snap.available_balance:.4f}` USDT\n"
            f"uPnL: `{snap.total_unrealized_pnl:+.4f}` USDT"
        )
        await update.message.reply_markdown(msg)
    finally:
        await client.close()


async def cmd_positions(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    service, client = _build_service()
    try:
        await client.connect()
        snap = await service.account()
        if not snap.positions:
            await update.message.reply_text("No open positions.")
            return
        lines = ["*Open positions*"]
        for p in snap.positions:
            side = "LONG" if p.quantity > 0 else "SHORT"
            lines.append(
                f"`{p.symbol}` {side} qty=`{p.quantity}` "
                f"entry=`{p.entry_price}` mark=`{p.mark_price}` "
                f"uPnL=`{p.unrealized_pnl:+.4f}` lev=`{p.leverage}x`"
            )
        await update.message.reply_markdown("\n".join(lines))
    finally:
        await client.close()


async def cmd_risk(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    s = get_settings()
    msg = (
        "*Risk*\n"
        f"Per-trade: `{s.risk_per_trade_pct}%`\n"
        f"Max open positions: `{s.max_open_positions}`\n"
        f"Daily loss limit: `{s.max_daily_loss_pct}%`\n"
        f"Default leverage: `{s.default_leverage}x`\n"
        f"Require SL: `{s.require_stop_loss}`\n"
        f"Network: `{'testnet' if s.binance_testnet else 'MAINNET'}`"
    )
    await update.message.reply_markdown(msg)


async def _open_position(update: Update, args: list[str], side: OrderSide) -> None:
    if len(args) < 2:
        await update.message.reply_text(
            "Usage: /long SYMBOL STOP [TP]   or   /short SYMBOL STOP [TP]"
        )
        return
    symbol = args[0]
    try:
        stop = Decimal(args[1])
        tp = Decimal(args[2]) if len(args) >= 3 else None
    except (InvalidOperation, IndexError):
        await update.message.reply_text("Invalid numeric argument.")
        return

    service, client = _build_service()
    try:
        await client.connect()
        try:
            plan = await service.plan_trade(
                symbol=symbol, side=side, stop_loss_price=stop, take_profit_price=tp
            )
            entry, sl, tp_res = await service.execute_plan(plan, market_entry=True)
        except RiskViolation as e:
            await update.message.reply_text(f"Rejected: {e}")
            return

        msg = (
            f"*Opened* {plan.symbol} {plan.side.value}\n"
            f"qty=`{plan.quantity}`  ref=`{plan.entry_price}`\n"
            f"SL=`{plan.stop_loss_price}`"
        )
        if tp_res is not None:
            msg += f"  TP=`{plan.take_profit_price}`"
        msg += (
            f"\nrisk=`{plan.risk_amount:.4f}` USDT ({plan.risk_pct}%)"
            f"\nentry id=`{entry.order_id}`  sl id=`{sl.order_id}`"
        )
        await update.message.reply_markdown(msg)
    finally:
        await client.close()


async def cmd_long(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    await _open_position(update, context.args or [], OrderSide.BUY)


async def cmd_short(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    await _open_position(update, context.args or [], OrderSide.SELL)


async def cmd_close(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    args = context.args or []
    if not args:
        await update.message.reply_text("Usage: /close SYMBOL")
        return
    service, client = _build_service()
    try:
        await client.connect()
        res = await service.close(args[0])
        if res is None:
            await update.message.reply_text(f"No open position on {args[0].upper()}.")
        else:
            await update.message.reply_markdown(
                f"*Closed* `{res.symbol}` order_id=`{res.order_id}`"
            )
    finally:
        await client.close()


async def cmd_flatten(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    service, client = _build_service()
    try:
        await client.connect()
        results = await service.kill_switch()
        if not results:
            await update.message.reply_text("Nothing to close.")
            return
        lines = ["*Kill switch executed*"]
        for r in results:
            lines.append(f"closed `{r.symbol}` order_id=`{r.order_id}`")
        await update.message.reply_markdown("\n".join(lines))
    finally:
        await client.close()


# ---- Application -----------------------------------------------------


def build_application() -> Application:
    settings = get_settings()
    if not settings.has_telegram:
        raise RuntimeError(
            "Telegram is not configured. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID."
        )
    if not settings.has_credentials:
        raise RuntimeError(
            "Binance credentials missing. Set BINANCE_API_KEY and BINANCE_API_SECRET."
        )
    application = ApplicationBuilder().token(settings.telegram_bot_token).build()
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_start))
    application.add_handler(CommandHandler("balance", cmd_balance))
    application.add_handler(CommandHandler("positions", cmd_positions))
    application.add_handler(CommandHandler("risk", cmd_risk))
    application.add_handler(CommandHandler("long", cmd_long))
    application.add_handler(CommandHandler("short", cmd_short))
    application.add_handler(CommandHandler("close", cmd_close))
    application.add_handler(CommandHandler("flatten", cmd_flatten))
    return application


def run_bot() -> None:
    """Run the bot using long polling."""
    application = build_application()
    logger.info("Archangel Telegram bot starting (polling)")
    application.run_polling()
