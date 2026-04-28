"""Funding rate data + signals for USDT-M perpetuals."""

from archangel.funding.signals import (
    FundingQuote,
    FundingSignal,
    SignalKind,
    annualized_yield_pct,
    classify_signals,
    parse_premium_index,
)

__all__ = [
    "FundingQuote",
    "FundingSignal",
    "SignalKind",
    "annualized_yield_pct",
    "classify_signals",
    "parse_premium_index",
]
