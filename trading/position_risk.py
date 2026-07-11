"""Normalized live position-risk measurements."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PositionRiskSnapshot:
    option_pnl_pct: float
    stock_pnl: float
    stock_pnl_pct: float
    bars_held: int


class PositionRiskPolicy:
    @staticmethod
    def timeout_profile(
        position: dict[str, Any],
        config: dict[str, Any],
        signal: str,
        is_v62: bool,
        trend_aligned: bool,
    ) -> dict[str, Any]:
        reason = str(position.get("reason", ""))
        regime = position.get("regime", "")
        if signal == "VWAP_Breakout":
            profile = {"signal": signal, "stage": 8, "hard": 12, "min_profit": 0.0}
        elif signal == "EMA_Cross":
            profile = {"signal": signal, "stage": 10, "hard": 12, "min_profit": 0.0}
        elif signal == "Granville_Pullback":
            profile = {"signal": signal, "stage": 7, "hard": 10, "min_profit": 2.0}
        elif is_v62:
            profile = {
                "signal": "v62_call_pool",
                "stage": int(config.get("v62_call_pool_timeout_stage_bars", 12) or 12),
                "hard": int(config.get("v62_call_pool_timeout_bars", 20) or 20),
                "min_profit": 0.0,
            }
            if trend_aligned:
                profile["hard"] = max(
                    profile["hard"], int(config.get("v62_call_pool_trend_timeout_bars", 28) or 28),
                )
        elif signal == "Kline_Pattern" or regime == "neutral" or "neutral" in reason:
            profile = {"signal": "Kline_Pattern", "stage": 10, "hard": 12, "min_profit": 0.0}
        else:
            base = int(position.get("timeout_bars", 10) or 10)
            profile = {
                "signal": signal or "default", "stage": max(base * 3 // 4, 7),
                "hard": base, "min_profit": 5.0,
            }
        if trend_aligned:
            bonus = int(config.get("trend_timeout_bonus_bars", 4) or 0)
            profile = dict(profile)
            profile["stage"] += bonus
            profile["hard"] += bonus
        return profile

    @staticmethod
    def profit_tiers(config: dict[str, Any]) -> list[dict[str, Any]]:
        normalized = []
        for index, tier in enumerate(config.get("profit_take_tiers") or []):
            if not isinstance(tier, dict):
                continue
            profit_pct = float(tier.get("profit_pct") or 0)
            close_pct = float(tier.get("close_pct") or 0)
            close_remaining = bool(tier.get("close_remaining", False))
            if profit_pct <= 0 or (close_pct <= 0 and not close_remaining):
                continue
            normalized.append({
                "key": str(tier.get("key") or f"tier_{index + 1}_{profit_pct:g}"),
                "profit_pct": profit_pct,
                "close_pct": min(max(close_pct, 0.01), 0.95),
                "close_remaining": close_remaining,
            })
        return sorted(normalized, key=lambda item: item["profit_pct"])

    @staticmethod
    def measure(
        position: dict[str, Any],
        option_price: float,
        current_stock: float,
        current_bar: int,
        stock_entry_valid: bool,
    ) -> PositionRiskSnapshot:
        entry_option = float(position.get("entry_opt_price") or option_price or 1.0)
        if entry_option <= 0:
            entry_option = 1.0
        option_pnl_pct = (float(option_price) - entry_option) / entry_option * 100
        position["max_pnl_pct"] = max(float(position.get("max_pnl_pct", 0) or 0), option_pnl_pct)
        if position.get("half_closed"):
            position["half_closed_max_pct"] = max(
                float(position.get("half_closed_max_pct", 0) or 0), option_pnl_pct,
            )

        entry_stock = float(position.get("entry_price", 0) or 0)
        if stock_entry_valid and position.get("dir") == "call":
            position["stock_peak"] = max(float(position.get("stock_peak", entry_stock) or entry_stock), current_stock)
        elif stock_entry_valid:
            position["stock_peak"] = min(float(position.get("stock_peak", entry_stock) or entry_stock), current_stock)
        stock_pnl = (current_stock - entry_stock) / entry_stock if stock_entry_valid and entry_stock > 0 else 0.0
        if position.get("dir") == "put":
            stock_pnl = -stock_pnl
        stock_pnl_pct = stock_pnl * 100
        position["max_stock_pnl_pct"] = max(
            float(position.get("max_stock_pnl_pct", 0) or 0), stock_pnl_pct,
        )
        bars_held = max(0, int(current_bar) - int(position.get("entry_bar", current_bar)))
        return PositionRiskSnapshot(option_pnl_pct, stock_pnl, stock_pnl_pct, bars_held)
