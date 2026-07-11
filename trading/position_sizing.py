"""Deterministic option position sizing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class PositionSize:
    contracts: int
    quantity: int
    order_amount: float
    effective_pct: float
    max_contracts: int


class PositionSizer:
    @staticmethod
    def calculate(
        capital: float,
        option_price: float,
        direction: str,
        combined_multiplier: float,
        config: Mapping[str, Any],
        afternoon: bool = False,
        blocked: bool = False,
    ) -> PositionSize:
        order_pct = float(config.get("order_pct", 0) or 0)
        effective_pct = min(order_pct, float(config.get("put_order_pct", 3.0) or 3.0)) if direction == "put" else order_pct
        if afternoon:
            effective_pct *= 0.5
        order_amount = float(capital) * effective_pct / 100 * max(0.0, float(combined_multiplier))
        contract_multiplier = int(config.get("contract_multiplier", 100) or 100)
        contracts = max(1, int(order_amount / (option_price * contract_multiplier))) if option_price > 0 else 0
        max_contracts = int(config.get("max_contracts_per_trade", 400) or 400)
        min_option_price = float(config.get("min_full_size_option_price", 0.75) or 0.75)
        if option_price < min_option_price:
            max_contracts = min(max_contracts, int(config.get("max_low_price_contracts", 300) or 300))
        if afternoon:
            max_contracts = min(max_contracts, int(config.get("max_afternoon_contracts", 300) or 300))
        contracts = 0 if blocked else min(contracts, max_contracts)
        return PositionSize(
            contracts, contracts * contract_multiplier, order_amount,
            effective_pct, max_contracts,
        )
