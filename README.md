# Archangel

Risk-first crypto futures trading toolkit for **Binance USDT-M perpetuals**.
Manual execution via CLI and Telegram, with a non-negotiable pre-trade risk
guard: every order must declare a stop-loss, position size is computed from a
configured risk budget, and a daily-loss circuit breaker pauses trading when
the account is drawn down beyond the configured limit.

> **Status:** MVP — manual trading + risk guard. No automated strategy
> execution, no backtesting (yet). Designed for people who already trade
> manually and want a tool that won't let them forget to set a stop.

## Features

- **Bracket orders** — entry + stop-loss (+ optional take-profit) placed in
  one command. If the stop-loss leg is rejected after the entry fills, the
  entry is automatically flattened so you never end up with a naked position.
- **Position sizing from risk** — given equity, a stop price and a per-trade
  risk percent, Archangel computes the largest quantity whose maximum loss
  stays within the budget, rounded down to the symbol's lot step.
- **Pre-trade risk guard** — rejects orders without a stop, with stops on the
  wrong side of entry, with sizing that exceeds the per-trade budget, or
  when the daily loss limit is hit or the max-open-positions cap would be
  breached.
- **Kill switch** — `archangel flatten` (or `/flatten` in Telegram) closes
  every open position and cancels every working order.
- **Modify-in-place + trailing stop** — `archangel modify` edits a resting
  LIMIT order's price/qty without cancel+replace; `archangel trail` attaches
  a native exchange-managed `TRAILING_STOP_MARKET` (reduce-only) to an open
  position. Side is auto-flipped from the position direction.
- **Two surfaces** — a `typer`-based CLI for desk use and a `python-telegram-bot`
  bot for mobile control.

## Install

Requires Python 3.11+.

```bash
git clone https://github.com/anziukafeinz/archangel.git
cd archangel
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pre-commit install
cp .env.example .env
# Fill BINANCE_API_KEY / BINANCE_API_SECRET (testnet recommended)
```

Get a **free testnet API key** at <https://testnet.binancefuture.com/> (login
with Google → "API Key" tab at the bottom). Keep `BINANCE_TESTNET=true` until
you're confident the tooling does what you expect.

## CLI usage

```bash
archangel risk                         # show active risk config
archangel balance                      # wallet balance & equity
archangel positions                    # list open positions

# Open a long with auto-sizing. Risk = configured % of equity.
archangel trade BTCUSDT long --stop 60000 --tp 65000

# Override risk for a single trade
archangel trade ETHUSDT short --stop 3500 --risk 0.5

# Limit entry instead of market
archangel trade SOLUSDT long --entry 140 --stop 135

archangel close BTCUSDT                # close one symbol
archangel flatten                      # kill switch

archangel modify 12345 -s BTCUSDT --side BUY --price 64500 --qty 0.5
archangel trail BTCUSDT -r 1.0         # 1% native trailing stop on the whole position
archangel trail ETHUSDT -r 0.8 -a 3500 # trail activates at 3500

archangel telegram                     # run the Telegram bot (foreground)
```

Every `trade` command prints a plan first and asks for confirmation
(`--yes` to skip).

## Telegram usage

Once `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set, run:

```bash
archangel telegram
```

Then talk to your bot:

```
/balance
/positions
/risk
/long  BTCUSDT 60000 65000      # long with SL=60000, TP=65000
/short ETHUSDT 3500             # short with SL=3500, no TP
/close BTCUSDT
/flatten
```

The bot ignores any chat that isn't `TELEGRAM_CHAT_ID`.

## Configuration

All settings live in `.env` (see `.env.example`). The risk-management knobs
are:

| Key | Default | Meaning |
|---|---|---|
| `RISK_PER_TRADE_PCT` | `1.0` | Percent of equity to risk per trade |
| `MAX_OPEN_POSITIONS` | `5` | Hard cap on simultaneous symbols |
| `MAX_DAILY_LOSS_PCT` | `5.0` | Pause trading if drawdown breaches this |
| `DEFAULT_LEVERAGE` | `5` | Leverage applied when opening a new symbol |
| `REQUIRE_STOP_LOSS` | `true` | Reject any order without an explicit SL |

## Architecture

```
archangel/
├── config.py            # Settings (pydantic-settings)
├── exchange/
│   ├── models.py        # Domain types: OrderSide, Position, BracketOrderRequest, ...
│   └── binance.py       # Async Binance Futures wrapper (python-binance)
├── risk/
│   ├── sizing.py        # Position-size-from-risk math
│   └── guards.py        # Pre-trade RiskGuard + DailyLossTracker
├── trading/
│   └── service.py       # Plan → validate → execute orchestration
├── cli/main.py          # Typer CLI
└── telegram/bot.py      # python-telegram-bot handlers
```

The dependency direction is one-way: `cli` and `telegram` depend on
`trading`, which depends on `exchange` and `risk`. `exchange` and `risk` are
independent of each other.

## Development

```bash
pip install -e ".[dev]"
pre-commit install
ruff check .
pytest -q
```

The unit tests cover position sizing and the risk guard end-to-end with
synthetic account snapshots — no Binance credentials required.

## Roadmap

- ~~Modify-order support for trailing stops~~ ✓ shipped (`archangel modify` / `archangel trail`)
- Multi-exchange via CCXT
- Strategy framework + backtesting
- Persistent trade journal & daily PnL report
- WebSocket push of fills/liquidations to Telegram
- Funding-rate / liquidation feed display

## Disclaimer

This is software for trading leveraged derivatives. You can lose more than
your initial deposit. **Always test on `BINANCE_TESTNET=true` first.** The
authors take no responsibility for any losses incurred from using this tool.

## License

MIT
