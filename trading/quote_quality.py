"""Quote freshness, spread, and execution-quality checks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class QuoteQuality:
    allowed: bool
    reason: str
    price: float
    bid: float
    ask: float
    mid: float
    spread_pct: float
    age_seconds: float | None


class QuoteQualityPolicy:
    @staticmethod
    def _number(quote: Any, *names: str) -> float:
        for name in names:
            value = getattr(quote, name, None)
            try:
                number = float(value or 0)
            except (TypeError, ValueError):
                continue
            if number > 0:
                return number
        return 0.0

    @staticmethod
    def _timestamp(value: Any) -> datetime | None:
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        if isinstance(value, (int, float)) and value > 0:
            scale = 1000 if value > 10_000_000_000 else 1
            return datetime.fromtimestamp(value / scale, tz=timezone.utc)
        if isinstance(value, str) and value:
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
            except ValueError:
                return None
        return None

    @classmethod
    def evaluate(
        cls,
        quote: Any,
        depth: Any = None,
        max_age_seconds: float = 30,
        max_spread_pct: float = 0.30,
        require_timestamp: bool = False,
        now: datetime | None = None,
    ) -> QuoteQuality:
        price = cls._number(quote, "last_done", "price", "last_price")
        bid = cls._number(quote, "bid_price", "bid", "best_bid")
        ask = cls._number(quote, "ask_price", "ask", "best_ask")
        if depth is not None:
            bids = getattr(depth, "bids", None) or []
            asks = getattr(depth, "asks", None) or []
            if bid <= 0 and bids:
                bid = cls._number(bids[0], "price", "bid_price")
            if ask <= 0 and asks:
                ask = cls._number(asks[0], "price", "ask_price")
        mid = (bid + ask) / 2 if bid > 0 and ask >= bid else price
        spread_pct = (ask - bid) / mid if bid > 0 and ask >= bid and mid > 0 else 0.0
        timestamp = cls._timestamp(
            getattr(quote, "timestamp", None)
            or getattr(quote, "updated_at", None)
            or getattr(quote, "time", None)
        )
        now = now or datetime.now(timezone.utc)
        age = max(0.0, (now - timestamp.astimezone(timezone.utc)).total_seconds()) if timestamp else None
        if price <= 0:
            return QuoteQuality(False, "missing option price", price, bid, ask, mid, spread_pct, age)
        if require_timestamp and timestamp is None:
            return QuoteQuality(False, "missing quote timestamp", price, bid, ask, mid, spread_pct, age)
        if age is not None and max_age_seconds > 0 and age > max_age_seconds:
            return QuoteQuality(False, f"stale quote {age:.1f}s", price, bid, ask, mid, spread_pct, age)
        if bid > 0 and ask > 0 and ask < bid:
            return QuoteQuality(False, "crossed option quote", price, bid, ask, mid, spread_pct, age)
        if spread_pct > max_spread_pct:
            return QuoteQuality(False, f"wide spread {spread_pct:.1%}", price, bid, ask, mid, spread_pct, age)
        return QuoteQuality(True, "", price, bid, ask, mid, spread_pct, age)
