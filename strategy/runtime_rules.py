"""Execution and exit rules shared by live trading and historical replay."""

from __future__ import annotations

from typing import Any, Mapping

from trading.position_risk import PositionRiskPolicy
from trading.position_sizing import PositionSizer


class StrategyRuntimeRules:
    @staticmethod
    def position_size(
        capital: float,
        option_price: float,
        direction: str,
        combined_multiplier: float,
        config: Mapping[str, Any],
        afternoon: bool = False,
        blocked: bool = False,
    ):
        return PositionSizer.calculate(
            capital, option_price, direction, combined_multiplier,
            config, afternoon=afternoon, blocked=blocked,
        )

    @staticmethod
    def risk_snapshot(position, option_price, current_stock, current_bar, stock_entry_valid):
        return PositionRiskPolicy.measure(
            position, option_price, current_stock, current_bar, stock_entry_valid,
        )

    @staticmethod
    def timeout_profile(position, config, signal, is_v62, trend_aligned):
        return PositionRiskPolicy.timeout_profile(
            position, config, signal, is_v62, trend_aligned,
        )

    @staticmethod
    def profit_tiers(config):
        return PositionRiskPolicy.profit_tiers(config)

    @classmethod
    def v62_lock_bars(cls, config: Mapping[str, Any]) -> int:
        profile = cls.timeout_profile(
            {"reason": "v6.2 CALL", "timeout_bars": config.get("timeout_stage3_bars", 20)},
            dict(config), "", True, False,
        )
        return int(profile["hard"])
