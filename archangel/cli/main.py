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
strategy_app = typer.Typer(
    no_args_is_help=True,
    help="Strategy framework (list, inspect built-in strategies).",
)
app.add_typer(strategy_app, name="strategy")
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


@strategy_app.command("list")
def strategy_list() -> None:
    """List built-in strategies."""
    from archangel.strategy import STRATEGIES

    table = Table(title="Built-in strategies")
    table.add_column("Name")
    table.add_column("Class")
    table.add_column("Docstring", overflow="fold")
    for name, factory in sorted(STRATEGIES.items()):
        doc = (factory.__doc__ or "").strip().split("\n")[0]
        table.add_row(name, factory.__name__, doc)
    console.print(table)


@app.command()
def backtest(
    symbol: str = typer.Argument(..., help="Symbol, e.g. BTCUSDT"),
    strategy: str = typer.Argument(..., help="Strategy name (see `archangel strategy list`)"),
    interval: str = typer.Option("1h", "--tf", "-t", help="Kline interval (1m..1d)"),
    days: int = typer.Option(30, "--days", "-d", help="Lookback window in days"),
    initial_equity: float = typer.Option(10000.0, "--equity", help="Starting equity USDT"),
    risk_pct: float = typer.Option(1.0, "--risk", help="Risk % per trade"),
    fee_bps: float = typer.Option(4.0, "--fee", help="Taker fee in basis points"),
    slippage_bps: float = typer.Option(2.0, "--slippage", help="Slippage in basis points"),
) -> None:
    """Backtest a strategy against recent Binance history."""
    from archangel.backtest import Backtester
    from archangel.strategy import get_strategy, parse_kline_row

    try:
        strat = get_strategy(strategy)
    except KeyError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(code=2) from exc

    settings = get_settings()
    if not settings.has_credentials:
        console.print("[red]Missing Binance credentials. See .env.example.[/]")
        raise typer.Exit(code=2)

    async def _go() -> None:
        import time

        client = BinanceFuturesClient(
            api_key=settings.binance_api_key,
            api_secret=settings.binance_api_secret,
            testnet=settings.binance_testnet,
        )
        await client.connect()
        try:
            end_ms = int(time.time() * 1000)
            start_ms = end_ms - days * 24 * 60 * 60 * 1000
            console.print(f"[cyan]Fetching {symbol.upper()} {interval} klines for last {days}d…[/]")
            rows = await client.get_klines(
                symbol, interval, start_ms=start_ms, end_ms=end_ms, limit=1500
            )
            console.print(f"Loaded {len(rows)} bars")
            bars = [parse_kline_row(r) for r in rows]
        finally:
            await client.close()

        bt = Backtester(
            initial_equity=Decimal(str(initial_equity)),
            risk_per_trade_pct=Decimal(str(risk_pct)),
            fee_bps=Decimal(str(fee_bps)),
            slippage_bps=Decimal(str(slippage_bps)),
        )
        result = bt.run(strat, bars, symbol=symbol.upper())
        _print_backtest(result)

    _run(_go())


def _print_backtest(result) -> None:
    m = result.metrics
    header = Table(title=f"Backtest · {result.symbol} · {result.strategy}", show_header=False)
    header.add_row("Initial equity", f"{result.initial_equity:.2f}")
    header.add_row("Final equity", f"{result.final_equity:.2f}")
    color = "green" if m.total_return_pct >= 0 else "red"
    header.add_row("Total return", f"[{color}]{m.total_return_pct:+.2f}%[/]")
    header.add_row("Max drawdown", f"{m.max_drawdown_pct:.2f}%")
    header.add_row("Trades", f"{m.trade_count} ({m.win_count}W / {m.loss_count}L)")
    header.add_row("Win rate", f"{m.win_rate:.2f}%")
    header.add_row("Profit factor", f"{m.profit_factor}")
    header.add_row("Expectancy/trade", f"{m.expectancy:.2f}")
    header.add_row("Sharpe", f"{m.sharpe:.2f}")
    header.add_row("Sortino", f"{m.sortino:.2f}")
    console.print(header)

    if result.trades:
        t = Table(title="Trades (last 20)")
        t.add_column("Side")
        t.add_column("Entry", justify="right")
        t.add_column("Exit", justify="right")
        t.add_column("Qty", justify="right")
        t.add_column("PnL", justify="right")
        t.add_column("Reason")
        for tr in result.trades[-20:]:
            pnl_color = "green" if tr.pnl >= 0 else "red"
            t.add_row(
                tr.side,
                f"{tr.entry_price}",
                f"{tr.exit_price}",
                f"{tr.quantity}",
                f"[{pnl_color}]{tr.pnl:+.2f}[/]",
                tr.reason_out,
            )
        console.print(t)


@app.command()
def paper(
    symbol: str = typer.Argument(..., help="Symbol, e.g. BTCUSDT"),
    strategy: str = typer.Argument(..., help="Strategy name (see `archangel strategy list`)"),
    interval: str = typer.Option("1h", "--tf", "-t", help="Kline interval (1m..1d)"),
    initial_equity: float = typer.Option(10000.0, "--equity", help="Starting equity USDT"),
    risk_pct: float = typer.Option(1.0, "--risk", help="Risk % per trade"),
    telegram: bool = typer.Option(
        False, "--telegram/--no-telegram", help="Push fills to Telegram (optional)"
    ),
) -> None:
    """Run a strategy against live bars without touching the exchange."""
    from archangel.backtest.paper import PaperTrader
    from archangel.strategy import get_strategy

    try:
        strat = get_strategy(strategy)
    except KeyError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(code=2) from exc

    settings = get_settings()
    if not settings.has_credentials:
        console.print("[red]Missing Binance credentials. See .env.example.[/]")
        raise typer.Exit(code=2)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    def _on_trade(trade, equity) -> None:
        pnl_color = "green" if trade.pnl >= 0 else "red"
        console.print(
            f"[{pnl_color}]{trade.side} closed[/] entry={trade.entry_price} "
            f"exit={trade.exit_price} qty={trade.quantity} "
            f"pnl={trade.pnl:+.2f} equity={equity:.2f} ({trade.reason_out})"
        )

    async def _go() -> None:
        client = BinanceFuturesClient(
            api_key=settings.binance_api_key,
            api_secret=settings.binance_api_secret,
            testnet=settings.binance_testnet,
        )
        await client.connect()
        try:
            trader = PaperTrader(
                client,
                symbol=symbol,
                interval=interval,
                strategy=strat,
                initial_equity=Decimal(str(initial_equity)),
                risk_per_trade_pct=Decimal(str(risk_pct)),
                on_trade=_on_trade,
            )
            console.print(
                f"[cyan]Paper trading {symbol.upper()} {interval} strategy={strategy}[/] "
                f"(telegram={telegram})"
            )
            try:
                await trader.run()
            except KeyboardInterrupt:
                summary = trader.summary()
                _print_backtest(summary)
        finally:
            await client.close()

    _run(_go())


@app.command(name="telegram")
def telegram_bot() -> None:
    """Run the Telegram control bot until interrupted."""
    from archangel.telegram.bot import run_bot

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    run_bot()


if __name__ == "__main__":
    app()
