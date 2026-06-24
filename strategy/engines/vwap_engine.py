"""VWAP retest signal engine.

The old engine bought the first VWAP break.  Recent live results showed that
this often turns into chasing.  The new default behavior arms a setup on the
first VWAP break and only emits a signal after a pullback/retest confirms.
"""
from typing import Dict, Optional

from .base import BaseEngine, Signal, SignalDirection


class VWAPEngine(BaseEngine):
    """VWAP breakout plus pullback/retest confirmation."""

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.name = "vwap"
        self.priority = 2

        self.vol_mult = float(cfg.get("vwap_vol_mult", 1.3) or 1.3)
        self.breakout_pct = float(cfg.get("vwap_breakout_pct", 0.001) or 0.001)

        self.retest_enabled = cfg.get("vwap_retest_enabled", True)
        self.retest_max_wait_bars = int(cfg.get("vwap_retest_max_wait_bars", 8) or 8)
        self.retest_min_wait_bars = int(cfg.get("vwap_retest_min_wait_bars", 1) or 1)
        self.retest_band_pct = float(cfg.get("vwap_retest_pullback_band_pct", 0.0015) or 0.0015)
        self.retest_min_vol_mult = float(cfg.get("vwap_retest_min_volume_mult", 0.8) or 0.8)
        self.retest_min_sma20_slope = float(cfg.get("vwap_retest_min_sma20_slope", 0.00003) or 0.00003)
        self.retest_min_vwap_slope = float(cfg.get("vwap_retest_min_vwap_slope", 0.0) or 0.0)
        self.call_max_range_position = float(cfg.get("vwap_retest_call_max_range_position", 0.70) or 0.70)
        self.put_min_range_position = float(cfg.get("vwap_retest_put_min_range_position", 0.30) or 0.30)

        self.vwap_cum_tp_vol = 0.0
        self.vwap_cum_vol = 0
        self.vwap = 0.0
        self.vwaps = []
        self.setup = None
        self.last_signal_bar = -1

    def update(self, bar: dict, et_minute: int = 0) -> None:
        super().update(bar, et_minute)

        typical_price = (bar["high"] + bar["low"] + bar["close"]) / 3.0
        self.vwap_cum_tp_vol += typical_price * bar["volume"]
        self.vwap_cum_vol += bar["volume"]
        self.vwap = self.vwap_cum_tp_vol / self.vwap_cum_vol if self.vwap_cum_vol > 0 else bar["close"]
        self.vwaps.append(self.vwap)
        if len(self.vwaps) > 500:
            self.vwaps = self.vwaps[-500:]

    def check(self) -> Optional[Signal]:
        if not self._initialized or len(self.closes) < 20 or self.vwap <= 0:
            return None

        price = self.closes[-1]
        bar = self.bars[-1]
        vol_avg = sum(self.volumes[-20:]) / 20
        if vol_avg <= 0:
            return None

        vol_ratio = bar["volume"] / vol_avg
        threshold = max(self.vwap * self.breakout_pct, 0.05)

        if self.retest_enabled:
            signal = self._check_retest_signal(price, bar, vol_ratio, threshold)
            if signal:
                return signal
            self._arm_retest_setup(price, vol_ratio, threshold)
            return None

        return self._first_cross_signal(price, vol_ratio, threshold)

    def _first_cross_signal(self, price: float, vol_ratio: float, threshold: float) -> Optional[Signal]:
        if price > self.vwap + threshold and vol_ratio >= self.vol_mult:
            breakout_pct = (price - self.vwap) / self.vwap * 100
            strength = min(100, 60 + breakout_pct * 300 + (vol_ratio - 1) * 15)
            return Signal(
                engine=self.name,
                direction=SignalDirection.CALL,
                strength=strength,
                entry_price=price,
                reason=f"VWAP breakout ${self.vwap:.2f} +{breakout_pct:.3f}%",
                metadata={"vwap": self.vwap, "vol_ratio": vol_ratio, "breakout_pct": breakout_pct},
            )

        if price < self.vwap - threshold and vol_ratio >= self.vol_mult:
            breakout_pct = (self.vwap - price) / self.vwap * 100
            strength = min(100, 60 + breakout_pct * 300 + (vol_ratio - 1) * 15)
            return Signal(
                engine=self.name,
                direction=SignalDirection.PUT,
                strength=strength,
                entry_price=price,
                reason=f"VWAP breakdown ${self.vwap:.2f} -{breakout_pct:.3f}%",
                metadata={"vwap": self.vwap, "vol_ratio": vol_ratio, "breakout_pct": breakout_pct},
            )

        return None

    def _arm_retest_setup(self, price: float, vol_ratio: float, threshold: float) -> None:
        if len(self.closes) < 2 or len(self.vwaps) < 2:
            return

        idx = len(self.closes) - 1
        prev_price = self.closes[-2]
        prev_vwap = self.vwaps[-2]
        call_cross = prev_price <= prev_vwap + threshold and price > self.vwap + threshold
        put_cross = prev_price >= prev_vwap - threshold and price < self.vwap - threshold

        if vol_ratio < self.vol_mult:
            return
        if call_cross:
            self.setup = {
                "dir": "call",
                "bar_index": idx,
                "cross_price": price,
                "cross_vwap": self.vwap,
                "vol_ratio": vol_ratio,
            }
        elif put_cross:
            self.setup = {
                "dir": "put",
                "bar_index": idx,
                "cross_price": price,
                "cross_vwap": self.vwap,
                "vol_ratio": vol_ratio,
            }

    def _check_retest_signal(
        self, price: float, bar: dict, vol_ratio: float, threshold: float
    ) -> Optional[Signal]:
        if not self.setup:
            return None

        idx = len(self.closes) - 1
        bars_since = idx - int(self.setup.get("bar_index", idx))
        direction = self.setup.get("dir")

        if bars_since > self.retest_max_wait_bars:
            self.setup = None
            return None
        if bars_since < self.retest_min_wait_bars or self.last_signal_bar == idx:
            return None

        ema9 = self._ema(9)
        if ema9 is None:
            return None

        prev_close = self.closes[-2] if len(self.closes) >= 2 else price
        sma20_slope = self._sma20_slope()
        vwap_slope = self._vwap_slope()
        range_pos = self._range_position(price)
        volume_ok = vol_ratio >= self.retest_min_vol_mult

        if direction == "call":
            near_vwap = bar["low"] <= self.vwap * (1 + self.retest_band_pct)
            near_ema = bar["low"] <= ema9 * (1 + self.retest_band_pct)
            confirm = (
                price > self.vwap
                and price >= ema9
                and price > bar["open"]
                and price >= prev_close
                and sma20_slope >= self.retest_min_sma20_slope
                and vwap_slope >= self.retest_min_vwap_slope
                and range_pos <= self.call_max_range_position
                and volume_ok
                and (near_vwap or near_ema)
            )
            if confirm:
                return self._emit_retest_signal(
                    SignalDirection.CALL, price, ema9, vol_ratio, bars_since,
                    range_pos, sma20_slope, vwap_slope,
                )
            if price < self.vwap - threshold:
                self.setup = None
            return None

        if direction == "put":
            near_vwap = bar["high"] >= self.vwap * (1 - self.retest_band_pct)
            near_ema = bar["high"] >= ema9 * (1 - self.retest_band_pct)
            confirm = (
                price < self.vwap
                and price <= ema9
                and price < bar["open"]
                and price <= prev_close
                and sma20_slope <= -self.retest_min_sma20_slope
                and vwap_slope <= -self.retest_min_vwap_slope
                and range_pos >= self.put_min_range_position
                and volume_ok
                and (near_vwap or near_ema)
            )
            if confirm:
                return self._emit_retest_signal(
                    SignalDirection.PUT, price, ema9, vol_ratio, bars_since,
                    range_pos, sma20_slope, vwap_slope,
                )
            if price > self.vwap + threshold:
                self.setup = None

        return None

    def _emit_retest_signal(
        self, direction: SignalDirection, price: float, ema9: float, vol_ratio: float,
        bars_since: int, range_pos: float, sma20_slope: float, vwap_slope: float,
    ) -> Signal:
        if direction == SignalDirection.CALL:
            dist_pct = (price - self.vwap) / self.vwap * 100
            reason = f"VWAP retest confirmed ${self.vwap:.2f} +{dist_pct:.3f}% wait={bars_since}"
            slope_bonus = max(0.0, sma20_slope)
        else:
            dist_pct = (self.vwap - price) / self.vwap * 100
            reason = f"VWAP retest failed ${self.vwap:.2f} -{dist_pct:.3f}% wait={bars_since}"
            slope_bonus = max(0.0, -sma20_slope)

        strength = min(100, 62 + dist_pct * 220 + (vol_ratio - 1) * 14 + slope_bonus * 50000)
        self.last_signal_bar = len(self.closes) - 1
        self.setup = None
        return Signal(
            engine=self.name,
            direction=direction,
            strength=strength,
            entry_price=price,
            reason=reason,
            metadata={
                "vwap": self.vwap,
                "ema9": ema9,
                "vol_ratio": vol_ratio,
                "breakout_pct": dist_pct,
                "setup_type": "retest",
                "bars_since_cross": bars_since,
                "range_position": range_pos,
                "sma20_slope": sma20_slope,
                "vwap_slope": vwap_slope,
            },
        )

    def _ema(self, period: int) -> Optional[float]:
        if len(self.closes) < period:
            return None
        mult = 2.0 / (period + 1)
        ema = self.closes[-period]
        for value in self.closes[-period + 1:]:
            ema = value * mult + ema * (1 - mult)
        return ema

    def _sma20_slope(self) -> float:
        if len(self.closes) < 25:
            return 0.0
        sma_now = sum(self.closes[-20:]) / 20
        sma_prev = sum(self.closes[-25:-5]) / 20
        price = self.closes[-1] or 1
        return (sma_now - sma_prev) / price

    def _vwap_slope(self) -> float:
        if len(self.vwaps) < 6:
            return 0.0
        price = self.closes[-1] or 1
        return (self.vwaps[-1] - self.vwaps[-6]) / price

    def _range_position(self, price: float) -> float:
        if self.session_high <= self.session_low:
            return 0.5
        return (price - self.session_low) / (self.session_high - self.session_low)

    def get_state(self) -> Dict:
        state = super().get_state()
        state.update({
            "vwap": self.vwap,
            "vwap_cum_vol": self.vwap_cum_vol,
            "setup": self.setup,
        })
        return state

    def reset(self) -> None:
        super().reset()
        self.vwap_cum_tp_vol = 0.0
        self.vwap_cum_vol = 0
        self.vwap = 0.0
        self.vwaps = []
        self.setup = None
        self.last_signal_bar = -1
