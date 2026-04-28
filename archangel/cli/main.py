"""Archangel CLI built with Typer.

Designed for manual, risk-disciplined futures trading. Every command that
opens a position requires an explicit stop-loss; sizing is computed from the
configured per-trade risk budget unless overridden.
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal

import typer
from rich.console import Console
from rich.table import Table

from archangel.config import get_settings
from archangel.exchange import BinanceFuturesClient, OrderSide
from archangel.risk import RiskGuard, RiskViolation
from archangel.trading import TradingService

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Archangel — risk-first crypto futures trading on Binance USDT-M.",
)
console = Console()


def _service() -> tuple[TradingService, BinanceFuturesClient]:
    settings = get_settings()
    if not settings.has_credentials:
        console.print(
            "[red]Missing Binance credentials.[/] Copy .env.example to .env and fill "
            "BINANCE_API_KEY / BINANCE_API_SECRET."
        )
        raise typer.Exit(code=2)
    client = BinanceFuturesClient(
        api_key=settings.binance_api_key,
        api_secret=settings.binance_api_secret,
        testnet=settings.binance_testnet,
    )
    guard = RiskGuard(settings)
    return TradingService(client, guard, settings), client


def _run(coro):
    return asyncio.run(coro)


@app.command()
def balance() -> None:
    """Show the futures wallet balance and equity."""

    async def _go() -> None:
        service, client = _service()
        try:
            await client.connect()
            snap = await service.account()
            table = Table(title="Account", show_header=False)
            table.add_row("Wallet balance", f"{snap.total_wallet_balance:.4f} USDT")
            table.add_row("Margin balance", f"{snap.total_margin_balance:.4f} USDT")
            table.add_row("Available", f"{snap.available_balance:.4f} USDT")
            table.add_row("Unrealized PnL", f"{snap.total_unrealized_pnl:+.4f} USDT")
            console.print(table)
        finally:
            await client.close()

    _run(_go())


@app.command()
def positions() -> None:
    """List currently open positions."""

    async def _go() -> None:
        service, client = _service()
        try:
            await client.connect()
            snap = await service.account()
            if not snap.positions:
                console.print("[green]No open positions.[/]")
                return
            table = Table(title="Open positions")
            table.add_column("Symbol")
            table.add_column("Side")
            table.add_column("Qty", justify="right")
            table.add_column("Entry", justify="right")
            table.add_column("Mark", justify="right")
            table.add_column("uPnL", justify="right")
            table.add_column("Lev", justify="right")
            for p in snap.positions:
                side = "LONG" if p.quantity > 0 else "SHORT"
                table.add_row(
                    p.symbol,
                    side,
                    f"{p.quantity}",
                    f"{p.entry_price}",
                    f"{p.mark_price}",
                    f"{p.unrealized_pnl:+.4f}",
                    f"{p.leverage}x",
                )
            console.print(table)
        finally:
            await client.close()

    _run(_go())


@app.command()
def trade(
    symbol: str = typer.Argument(..., help="Symbol, e.g. BTCUSDT"),
    side: str = typer.Argument(..., help="long | short"),
    stop: str = typer.Option(..., "--stop", "-s", help="Stop-loss price"),
    take_profit: str | None = typer.Option(None, "--tp", help="Optional take-profit price"),
    entry: str | None = typer.Option(
        None, "--entry", help="Limit entry price (default: market entry at mark price)"
    ),
    risk_pct: str | None = typer.Option(
        None, "--risk", help="Override per-trade risk percent (e.g. 0.5)"
    ),
    leverage: int | None = typer.Option(None, "--leverage", "-l", help="Leverage override"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Open a bracketed position (entry + stop-loss + optional take-profit)."""
    side_lower = side.lower()
    if side_lower not in ("long", "short", "buy", "sell"):
        console.print("[red]side must be 'long' or 'short'[/]")
        raise typer.Exit(code=2)
    order_side = OrderSide.BUY if side_lower in ("long", "buy") else OrderSide.SELL

    try:
        stop_d = Decimal(stop)
        tp_d = Decimal(take_profit) if take_profit is not None else None
        entry_d = Decimal(entry) if entry is not None else None
        risk_d = Decimal(risk_pct) if risk_pct is not None else None
    except Exception as e:  # noqa: BLE001
        console.print(f"[red]Invalid numeric value:[/] {e}")
        raise typer.Exit(code=2) from e

    async def _go() -> None:
        service, client = _service()
        try:
            await client.connect()
            try:
                plan = await service.plan_trade(
                    symbol=symbol,
                    side=order_side,
                    stop_loss_price=stop_d,
                    take_profit_price=tp_d,
                    entry_price=entry_d,
                    risk_pct=risk_d,
                    leverage=leverage,
                )
            except RiskViolation as e:
                console.print(f"[red]Rejected by risk guard:[/] {e}")
                raise typer.Exit(code=1) from e

            table = Table(title="Trade plan")
            table.add_row("Symbol", plan.symbol)
            table.add_row("Side", plan.side.value)
            table.add_row("Quantity", f"{plan.quantity}")
            table.add_row("Entry (ref)", f"{plan.entry_price}")
            table.add_row("Stop-loss", f"{plan.stop_loss_price}")
            table.add_row(
                "Take-profit", f"{plan.take_profit_price}" if plan.take_profit_price else "—"
            )
            table.add_row("Risk", f"{plan.risk_amount:.4f} USDT ({plan.risk_pct}% of equity)")
            table.add_row("Leverage", f"{plan.leverage}x")
            console.print(table)

            if not yes and not typer.confirm("Place this bracket order?", default=False):
                console.print("[yellow]Aborted.[/]")
                return

            try:
                entry_res, sl_res, tp_res = await service.execute_plan(
                    plan, market_entry=entry_d is None
                )
            except RiskViolation as e:
                console.print(f"[red]Rejected by risk guard:[/] {e}")
                raise typer.Exit(code=1) from e

            console.print(
                f"[green]Entry placed[/] id={entry_res.order_id} status={entry_res.status}"
            )
            console.print(f"[green]Stop-loss placed[/] id={sl_res.order_id}")
            if tp_res is not None:
                console.print(f"[green]Take-profit placed[/] id={tp_res.order_id}")
        finally:
            await client.close()

    _run(_go())


@app.command()
def close(symbol: str = typer.Argument(..., help="Symbol to flatten")) -> None:
    """Close any open position on a symbol with a reduce-only market order."""

    async def _go() -> None:
        service, client = _service()
        try:
            await client.connect()
            res = await service.close(symbol)
            if res is None:
                console.print(f"[yellow]No open position on {symbol.upper()}.[/]")
                return
            console.print(f"[green]Closed {res.symbol}[/] order_id={res.order_id}")
        finally:
            await client.close()

    _run(_go())


@app.command(name="flatten")
def flatten(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Kill switch — close every position and cancel every working order."""
    if not yes and not typer.confirm(
        "This will close ALL positions and cancel ALL orders. Continue?", default=False
    ):
        console.print("[yellow]Aborted.[/]")
        return

    async def _go() -> None:
        service, client = _service()
        try:
            await client.connect()
            results = await service.kill_switch()
            if not results:
                console.print("[green]Nothing to close.[/]")
                return
            for r in results:
                console.print(f"[green]Closed[/] {r.symbol} order_id={r.order_id}")
        finally:
            await client.close()

    _run(_go())


@app.command()
def risk() -> None:
    """Show the active risk configuration."""
    s = get_settings()
    table = Table(title="Risk configuration", show_header=False)
    table.add_row("Risk per trade", f"{s.risk_per_trade_pct}%")
    table.add_row("Max open positions", str(s.max_open_positions))
    table.add_row("Daily loss limit", f"{s.max_daily_loss_pct}%")
    table.add_row("Default leverage", f"{s.default_leverage}x")
    table.add_row("Require stop-loss", "yes" if s.require_stop_loss else "no")
    table.add_row("Network", "testnet" if s.binance_testnet else "MAINNET (real money)")
    console.print(table)


@app.command(name="telegram")
def telegram_bot() -> None:
    """Run the Telegram control bot until interrupted."""
    from archangel.telegram.bot import run_bot

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    run_bot()


# ---- journal sub-app ---------------------------------------------------

journal_app = typer.Typer(
    no_args_is_help=True,
    help="Persistent trade journal (Binance income history → SQLite).",
)
app.add_typer(journal_app, name="journal")


def _open_store(db_path: str | None):
    from archangel.journal import JournalStore

    return JournalStore(db_path) if db_path else JournalStore()


@journal_app.command("sync")
def journal_sync(
    db: str = typer.Option("", "--db", help="SQLite path (default ~/.archangel/journal.db)"),
    days: int = typer.Option(
        0, "--days", help="Force re-pull last N days (default: incremental from latest)"
    ),
) -> None:
    """Pull income history from Binance into the local journal store."""
    import time as _time

    from archangel.journal import sync_incomes

    settings = get_settings()
    if not settings.has_credentials:
        console.print("[red]Missing Binance credentials.[/]")
        raise typer.Exit(code=2)

    store = _open_store(db or None)

    async def _go() -> None:
        client = BinanceFuturesClient(
            api_key=settings.binance_api_key,
            api_secret=settings.binance_api_secret,
            testnet=settings.binance_testnet,
        )
        try:
            await client.connect()
            start_ms: int | None = None
            if days > 0:
                start_ms = int(_time.time() * 1000) - days * 86_400_000
            inserted = await sync_incomes(client, store, start_ms=start_ms)
            console.print(
                f"[green]Sync complete[/]: {inserted} new rows. Total in store: {store.count()}."
            )
        finally:
            await client.close()

    _run(_go())


@journal_app.command("daily")
def journal_daily(
    days: int = typer.Option(30, "--days", help="Show last N days"),
    db: str = typer.Option("", "--db"),
) -> None:
    """Show daily realized PnL, funding, and commission."""
    from archangel.journal import daily_pnl

    store = _open_store(db or None)
    rows = store.all_rows()
    days_data = daily_pnl(rows)
    if days > 0:
        days_data = days_data[-days:]
    if not days_data:
        console.print("[yellow]No journal data. Run 'archangel journal sync' first.[/]")
        return

    table = Table(title=f"Daily PnL (last {len(days_data)} days, UTC)")
    table.add_column("Day", style="cyan")
    table.add_column("Realized", justify="right")
    table.add_column("Funding", justify="right")
    table.add_column("Commission", justify="right")
    table.add_column("Net", justify="right", style="bold")

    for d in days_data:
        net_str = f"{d.net:+.4f}"
        net_style = "green" if d.net > 0 else "red" if d.net < 0 else ""
        table.add_row(
            d.day.isoformat(),
            f"{d.realized:+.4f}",
            f"{d.funding:+.4f}",
            f"{d.commission:+.4f}",
            f"[{net_style}]{net_str}[/]" if net_style else net_str,
        )
    console.print(table)


@journal_app.command("summary")
def journal_summary(
    symbol: str = typer.Option("", "--symbol", "-s", help="Filter by symbol"),
    db: str = typer.Option("", "--db"),
) -> None:
    """Show aggregate PnL, win rate, and fee/funding totals."""
    from archangel.journal import summary_for_symbol, total_summary

    store = _open_store(db or None)
    summary = summary_for_symbol(store, symbol) if symbol else total_summary(store)

    table = Table(title=f"Summary: {summary.symbol}", show_header=False)
    table.add_row("Trades (decided)", str(summary.win_count + summary.loss_count))
    table.add_row("Wins", str(summary.win_count))
    table.add_row("Losses", str(summary.loss_count))
    table.add_row("Win rate", f"{summary.win_rate}%")
    table.add_row("Realized PnL", f"{summary.realized:+.4f}")
    table.add_row("Funding", f"{summary.funding:+.4f}")
    table.add_row("Commission", f"{summary.commission:+.4f}")
    net_color = "green" if summary.net > 0 else "red" if summary.net < 0 else "white"
    table.add_row("[bold]Net[/]", f"[{net_color}]{summary.net:+.4f}[/]")
    console.print(table)


if __name__ == "__main__":
    app()
