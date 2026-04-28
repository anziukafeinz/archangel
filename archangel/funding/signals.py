"""Pure helpers for parsing and classifying funding-rate data.

These functions operate on raw Binance payloads (or already-parsed
:class:`FundingQuote` / history rows) and never hit the network. The CLI
layer wraps them around live data; tests pump synthetic payloads through.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

# Binance funding cadence: 3 settlements per day (every 8h).
SETTLEMENTS_PER_YEAR = Decimal("365") * Decimal("3")


class SignalKind(StrEnum):
    EXTREME_POSITIVE = "EXTREME_POSITIVE"
    EXTREME_NEGATIVE = "EXTREME_NEGATIVE"
    SIGN_FLIP = "SIGN_FLIP"


@dataclass(frozen=True)
class FundingQuote:
    """Snapshot of a single perpetual's current funding state."""

    symbol: str
    mark_price: Decimal
    last_funding_rate: Decimal
    next_funding_time_ms: int

    @property
    def annualized_yield_pct(self) -> Decimal:
        return annualized_yield_pct(self.last_funding_rate)


@dataclass(frozen=True)
class FundingSignal:
    symbol: str
    kind: SignalKind
    last_funding_rate: Decimal
    annualized_yield_pct: Decimal
    note: str = ""


def annualized_yield_pct(rate_per_8h: Decimal) -> Decimal:
    """Convert a per-8h funding rate (e.g. 0.0001 = 0.01%) into annual %.

    A rate of ``0.0001`` paid 3x/day = 0.0003/day = ~10.95% annualized.
    """
    return (rate_per_8h * SETTLEMENTS_PER_YEAR * Decimal("100")).quantize(Decimal("0.0001"))


def parse_premium_index(payloads: list[dict]) -> list[FundingQuote]:
    """Parse a ``/fapi/v1/premiumIndex`` response into typed quotes."""
    out: list[FundingQuote] = []
    for p in payloads:
        symbol = str(p.get("symbol", ""))
        if not symbol or not symbol.endswith("USDT"):
            # Skip non-USDT-M / coin-margined entries that may sneak in
            continue
        rate_raw = p.get("lastFundingRate", "0")
        # Some entries (delivery contracts) come with empty funding rate
        if rate_raw in ("", None):
            continue
        out.append(
            FundingQuote(
                symbol=symbol,
                mark_price=Decimal(str(p.get("markPrice", "0") or "0")),
                last_funding_rate=Decimal(str(rate_raw)),
                next_funding_time_ms=int(p.get("nextFundingTime", 0) or 0),
            )
        )
    return out


def classify_signals(
    quotes: list[FundingQuote],
    *,
    extreme_threshold: Decimal = Decimal("0.0005"),  # 0.05% per 8h ≈ ~55% APR
    history_by_symbol: dict[str, list[Decimal]] | None = None,
) -> list[FundingSignal]:
    """Surface noteworthy quotes.

    - **EXTREME_POSITIVE / NEGATIVE**: ``|rate| >= extreme_threshold``. These
      are the cases where the perp is paying so much that it's worth a
      contrarian or arbitrage look.
    - **SIGN_FLIP**: only emitted when ``history_by_symbol`` is supplied.
      A flip means the most recent historical rate had a different sign
      from the current one; trend-following funding strategies use this
      as a regime change marker.

    Returns the signals sorted by ``|annualized_yield_pct|`` descending.
    """
    signals: list[FundingSignal] = []
    history_by_symbol = history_by_symbol or {}

    for quote in quotes:
        rate = quote.last_funding_rate
        annualized = quote.annualized_yield_pct
        if rate >= extreme_threshold:
            signals.append(
                FundingSignal(
                    symbol=quote.symbol,
                    kind=SignalKind.EXTREME_POSITIVE,
                    last_funding_rate=rate,
                    annualized_yield_pct=annualized,
                    note=f"Longs paying {annualized}% APR",
                )
            )
        elif rate <= -extreme_threshold:
            signals.append(
                FundingSignal(
                    symbol=quote.symbol,
                    kind=SignalKind.EXTREME_NEGATIVE,
                    last_funding_rate=rate,
                    annualized_yield_pct=annualized,
                    note=f"Shorts paying {annualized}% APR",
                )
            )

        history = history_by_symbol.get(quote.symbol)
        if history:
            previous = history[-1]
            if previous != 0 and rate != 0 and (previous > 0) != (rate > 0):
                signals.append(
                    FundingSignal(
                        symbol=quote.symbol,
                        kind=SignalKind.SIGN_FLIP,
                        last_funding_rate=rate,
                        annualized_yield_pct=annualized,
                        note=f"Flipped from {previous} to {rate}",
                    )
                )

    signals.sort(key=lambda s: abs(s.annualized_yield_pct), reverse=True)
    return signals
