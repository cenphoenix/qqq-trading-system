"""Pure trading-session time rules."""

from __future__ import annotations

from typing import Any, Mapping


class TradingSessionPolicy:
    @staticmethod
    def effective_end_time(config: Mapping[str, Any], daily_pnl: float) -> str:
        if daily_pnl <= 0:
            return str(config.get("extended_end_time", "15:00"))
        return str(config["end_time"])

    @staticmethod
    def is_extension_window(config: Mapping[str, Any], current_minute: int) -> bool:
        end_hour, end_minute = map(int, str(config["end_time"]).split(":"))
        extended_hour, extended_minute = map(
            int, str(config.get("extended_end_time", "15:00")).split(":"),
        )
        start = end_hour * 60 + end_minute
        end = extended_hour * 60 + extended_minute
        return start <= current_minute < end
