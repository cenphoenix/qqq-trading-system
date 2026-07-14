"""Persistent broker-order state and restart reconciliation."""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, tzinfo
from enum import Enum
from pathlib import Path
from typing import Any, Iterable


class OrderState(str, Enum):
    SUBMITTED = "submitted"
    PARTIAL = "partial"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


class OrderStateStore:
    ACTIVE_TOKENS = ("new", "wait", "pending", "partial")

    def __init__(self, app_dir: str | os.PathLike[str], timezone: tzinfo) -> None:
        self.path = Path(app_dir) / "runtime_orders.json"
        self._timezone = timezone
        self._orders: dict[str, dict[str, Any]] = {}
        self._load()

    @classmethod
    def normalize(cls, status: Any, executed: float = 0, requested: float = 0) -> OrderState:
        token = str(status or "").replace("OrderStatus.", "").lower()
        if "reject" in token:
            return OrderState.REJECTED
        if "cancel" in token or "withdraw" in token or "expire" in token:
            return OrderState.CANCELED
        if "fill" in token and "partial" not in token:
            return OrderState.FILLED
        if executed > 0 and (requested <= 0 or executed < requested):
            return OrderState.PARTIAL
        if any(part in token for part in cls.ACTIVE_TOKENS):
            return OrderState.SUBMITTED
        return OrderState.UNKNOWN

    @staticmethod
    def is_option_for(symbol: Any, underlying: str) -> bool:
        """Return whether a symbol is an option for the requested underlying."""
        token = str(symbol or "").upper()
        root = re.escape(str(underlying or "").upper())
        return bool(re.fullmatch(rf"{root}\d{{6}}[CP]\d+\.US", token))

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with self.path.open(encoding="utf-8") as stream:
                payload = json.load(stream)
            self._orders = {
                str(row["order_id"]): row for row in payload.get("orders", [])
                if isinstance(row, dict) and row.get("order_id")
            }
        except (OSError, json.JSONDecodeError, TypeError):
            self._orders = {}

    def _save(self) -> None:
        payload = {
            "updated": datetime.now(self._timezone).isoformat(),
            "orders": list(self._orders.values()),
        }
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp_path.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
        for attempt in range(5):
            try:
                os.replace(tmp_path, self.path)
                return
            except OSError:
                if attempt == 4:
                    raise
                time.sleep(0.1)

    def record(
        self,
        order_id: Any,
        symbol: str,
        side: Any,
        requested: float,
        status: Any,
        executed: float = 0,
        price: float = 0,
    ) -> dict[str, Any]:
        state = self.normalize(status, executed, requested)
        row = {
            "order_id": str(order_id), "symbol": str(symbol), "side": str(side),
            "requested_quantity": float(requested), "executed_quantity": float(executed),
            "executed_price": float(price), "broker_status": str(status or ""),
            "state": state.value, "updated": datetime.now(self._timezone).isoformat(),
        }
        self._orders[str(order_id)] = row
        self._save()
        return row

    def sync(self, orders: Iterable[Any]) -> list[dict[str, Any]]:
        for order in orders or []:
            self.record(
                getattr(order, "order_id", ""),
                getattr(order, "symbol", ""),
                getattr(order, "side", ""),
                float(getattr(order, "quantity", 0) or 0),
                getattr(order, "status", ""),
                float(getattr(order, "executed_quantity", 0) or 0),
                float(getattr(order, "executed_price", 0) or 0),
            )
        return self.active()

    def active(
        self,
        symbol: str | None = None,
        buy_only: bool = False,
        option_underlying: str | None = None,
    ) -> list[dict[str, Any]]:
        active_states = {OrderState.SUBMITTED.value, OrderState.PARTIAL.value}
        today = datetime.now(self._timezone).date()
        rows = []
        for row in self._orders.values():
            if row.get("state") not in active_states:
                continue
            try:
                updated = datetime.fromisoformat(str(row.get("updated", "")))
                if updated.date() != today:
                    continue
            except (TypeError, ValueError):
                continue
            rows.append(row)
        if symbol:
            rows = [row for row in rows if row.get("symbol") == symbol]
        if option_underlying:
            rows = [
                row for row in rows
                if self.is_option_for(row.get("symbol"), option_underlying)
            ]
        if buy_only:
            rows = [row for row in rows if "buy" in str(row.get("side", "")).lower()]
        return rows
