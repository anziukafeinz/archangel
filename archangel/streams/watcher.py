"""High-level watcher: subscribes to the user-data stream and pushes
human-readable summaries to Telegram (and optionally to a console handler).

This is the glue layer between :class:`UserDataStream`, the typed events and
:class:`TelegramNotifier`. It applies a small amount of policy:

- Order updates that aren't fills (e.g. plain ``NEW``) are suppressed by
  default to avoid notification spam — only ``FILLED``, ``PARTIALLY_FILLED``,
  ``CANCELED`` (with reason), and ``EXPIRED`` events are pushed.
- ``ACCOUNT_UPDATE`` events are pushed only when a position quantity changed
  (i.e. new exposure or a flat) to avoid noise from funding ticks.
- ``MARGIN_CALL`` is **always** pushed and prefixed with a klaxon emoji.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from archangel.notify.telegram import TelegramNotifier
from archangel.streams.user_data import (
    AccountUpdate,
    EventKind,
    MarginCallEvent,
    OrderUpdate,
    UserDataEvent,
)

logger = logging.getLogger(__name__)


def format_order_update(event: OrderUpdate) -> str | None:
    """Return a Telegram-Markdown summary, or ``None`` to suppress the push."""
    interesting = {"FILLED", "PARTIALLY_FILLED", "CANCELED", "EXPIRED"}
    if event.order_status not in interesting:
        return None

    icon = {
        "FILLED": "✅",
        "PARTIALLY_FILLED": "🟡",
        "CANCELED": "🛑",
        "EXPIRED": "⌛",
    }.get(event.order_status, "•")

    lines = [f"{icon} *{event.order_status}* `{event.symbol}` {event.side} {event.order_type}"]
    if event.last_filled_quantity > 0:
        lines.append(f"qty=`{event.last_filled_quantity}`  px=`{event.last_filled_price}`")
    if event.realized_pnl != 0:
        lines.append(f"realized PnL: `{event.realized_pnl:+.4f}`")
    if event.is_reduce_only:
        lines.append("_reduce-only_")
    if event.is_close_position:
        lines.append("_close-position_")
    if event.commission > 0:
        lines.append(f"fee `{event.commission} {event.commission_asset}`")
    return "\n".join(lines)


def format_account_update(event: AccountUpdate) -> str | None:
    moved = [p for p in event.positions if p.quantity != 0 or p.unrealized_pnl != 0]
    if not moved and event.reason != "ORDER":
        return None
    lines = [f"📊 *Account update* ({event.reason or 'unspecified'})"]
    for b in event.balances:
        if b.balance_change != 0:
            lines.append(f"{b.asset}: `{b.wallet_balance}` (Δ `{b.balance_change:+}`)")
    for p in moved:
        side = "LONG" if p.quantity > 0 else "SHORT" if p.quantity < 0 else "FLAT"
        lines.append(
            f"`{p.symbol}` {side} qty=`{p.quantity}` "
            f"entry=`{p.entry_price}` uPnL=`{p.unrealized_pnl:+.4f}`"
        )
    return "\n".join(lines) if len(lines) > 1 else None


def format_margin_call(event: MarginCallEvent) -> str:
    lines = [
        "🚨 *MARGIN CALL* 🚨",
        f"Cross wallet balance: `{event.cross_wallet_balance}`",
    ]
    for p in event.positions:
        lines.append(
            f"`{p.symbol}` {p.position_side} qty=`{p.quantity}` "
            f"mark=`{p.mark_price}` uPnL=`{p.unrealized_pnl:+.4f}` "
            f"maint=`{p.maintenance_margin_required}`"
        )
    return "\n".join(lines)


def format_event(event: UserDataEvent) -> str | None:
    if isinstance(event, MarginCallEvent):
        return format_margin_call(event)
    if isinstance(event, OrderUpdate):
        return format_order_update(event)
    if isinstance(event, AccountUpdate):
        return format_account_update(event)
    return None


def make_handler(
    notifier: TelegramNotifier | None = None,
    *,
    console: Callable[[str], None] | None = None,
) -> Callable[[UserDataEvent], Awaitable[None]]:
    """Build an async handler that fans events out to console + Telegram."""

    async def handler(event: UserDataEvent) -> None:
        if event.kind is EventKind.OTHER:
            logger.debug("Ignoring unknown event: %r", event.raw)
            return
        text = format_event(event)
        if text is None:
            return
        if console is not None:
            console(text)
        if notifier is not None:
            await notifier.send(text)

    return handler
