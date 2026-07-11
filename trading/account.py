"""Normalize Longbridge account balances for the trader and dashboard."""

from __future__ import annotations

from typing import Any, Iterable


class AccountSnapshotService:
    FX_TO_USD = {"USD": 1.0, "HKD": 7.8, "CNY": 7.2}
    BUYING_POWER_FIELDS = (
        "buy_power", "buying_power", "max_power", "power",
        "available_funds", "max_power_long",
    )

    @classmethod
    def _currencies(cls, balance: Any) -> Iterable[Any]:
        if isinstance(balance, (list, tuple)):
            return balance
        return getattr(balance, "currencies", None) or []

    @classmethod
    def normalize(cls, balance: Any) -> dict[str, float]:
        currencies = list(cls._currencies(balance))
        net_assets = cash = buying_power = 0.0
        for row in currencies:
            currency = str(getattr(row, "currency", "USD") or "USD").upper()
            divisor = cls.FX_TO_USD.get(currency, 1.0)
            row_net = float(getattr(row, "net_assets", 0) or 0)
            row_cash = float(getattr(row, "total_cash", 0) or 0)
            explicit_cash = getattr(row, "cash", None)
            if explicit_cash is not None:
                row_cash = float(explicit_cash or 0)
            row_power = 0.0
            for field in cls.BUYING_POWER_FIELDS:
                value = getattr(row, field, None)
                if value is not None and float(value or 0) > 0:
                    row_power = float(value)
                    break
            if row_power <= 0:
                row_power = row_cash
            net_assets += row_net / divisor
            cash += row_cash / divisor
            buying_power += row_power / divisor
        if currencies:
            return {
                "net_assets": net_assets,
                "cash": cash,
                "buying_power": buying_power,
            }
        return {
            key: float(getattr(balance, key, 0) or 0)
            for key in ("net_assets", "cash", "buying_power")
            if getattr(balance, key, None) is not None
        }
