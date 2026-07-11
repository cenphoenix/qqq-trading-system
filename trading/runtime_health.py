"""Live-trading health telemetry with stale-data and failure diagnostics."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, tzinfo
from typing import Any


class RuntimeHealth:
    def __init__(self, timezone: tzinfo) -> None:
        self.timezone = timezone
        self.started_at = datetime.now(timezone)
        self.last_loop_at: datetime | None = None
        self.last_quote_at: datetime | None = None
        self.last_candle_at: datetime | None = None
        self.last_order_sync_at: datetime | None = None
        self.last_order_sync_error = ""
        self.last_notification_at: datetime | None = None
        self.last_notification_ok: bool | None = None
        self.last_notification_type = ""
        self.signal_checks = 0
        self.entry_attempts = 0
        self.orders_submitted = 0
        self.errors = 0
        self.rejections: Counter[str] = Counter()

    def beat(self, component: str, *, ok: bool = True, detail: str = "") -> None:
        now = datetime.now(self.timezone)
        if component == "loop":
            self.last_loop_at = now
        elif component == "quote":
            self.last_quote_at = now
        elif component == "candle":
            self.last_candle_at = now
        elif component == "order_sync":
            self.last_order_sync_at = now
            self.last_order_sync_error = "" if ok else str(detail)[:200]
        elif component == "notification":
            self.last_notification_at = now
            self.last_notification_ok = ok
            self.last_notification_type = str(detail)
        if not ok:
            self.errors += 1

    def reject(self, reason: str) -> None:
        normalized = " ".join(str(reason or "unknown").split())[:160]
        self.rejections[normalized] += 1

    @staticmethod
    def _age(now: datetime, value: datetime | None) -> float | None:
        return round((now - value).total_seconds(), 1) if value else None

    def snapshot(self, *, running: bool, market_open: bool = False) -> dict[str, Any]:
        now = datetime.now(self.timezone)
        ages = {
            "loop_seconds": self._age(now, self.last_loop_at),
            "quote_seconds": self._age(now, self.last_quote_at),
            "candle_seconds": self._age(now, self.last_candle_at),
            "order_sync_seconds": self._age(now, self.last_order_sync_at),
        }
        uptime_seconds = (now - self.started_at).total_seconds()
        issues: list[str] = []
        if running and ages["loop_seconds"] is not None and ages["loop_seconds"] > 90:
            issues.append("main_loop_stale")
        if running and market_open and uptime_seconds > 120 and (
            ages["quote_seconds"] is None or ages["quote_seconds"] > 90
        ):
            issues.append("market_quote_stale")
        if running and market_open and uptime_seconds > 180 and (
            ages["candle_seconds"] is None or ages["candle_seconds"] > 180
        ):
            issues.append("market_candle_stale")
        if self.last_order_sync_error:
            issues.append("order_sync_failed")
        if self.last_notification_ok is False:
            issues.append("notification_failed")
        status = "stopped" if not running else "degraded" if issues else "healthy"
        return {
            "status": status,
            "issues": issues,
            "ages": ages,
            "started_at": self.started_at.isoformat(),
            "last_order_sync_error": self.last_order_sync_error,
            "notification": {
                "last_at": self.last_notification_at.isoformat() if self.last_notification_at else "",
                "ok": self.last_notification_ok,
                "type": self.last_notification_type,
            },
            "counters": {
                "signal_checks": self.signal_checks,
                "entry_attempts": self.entry_attempts,
                "orders_submitted": self.orders_submitted,
                "errors": self.errors,
            },
            "top_rejections": [
                {"reason": reason, "count": count}
                for reason, count in self.rejections.most_common(10)
            ],
        }
