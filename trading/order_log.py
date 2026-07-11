"""Append-only audit log for submitted broker orders."""

from __future__ import annotations

from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any


class OrderAuditLog:
    def __init__(self, app_dir: str | Path, timezone: tzinfo) -> None:
        self._log_dir = Path(app_dir) / "logs"
        self._timezone = timezone

    def append(
        self,
        order_id: Any,
        symbol: str,
        direction: str,
        contracts: int,
        status: str,
        executed_quantity: float = 0,
        executed_price: float = 0,
    ) -> Path:
        self._log_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now(self._timezone)
        path = self._log_dir / f"orders_{now:%Y-%m-%d}.log"
        line = f"{now:%Y-%m-%d %H:%M:%S} | {order_id} | {symbol} | {direction} | {contracts}张 | {status}"
        if executed_quantity > 0:
            line += f" | 成交:{executed_quantity}张 @{executed_price}"
        with path.open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")
        return path
