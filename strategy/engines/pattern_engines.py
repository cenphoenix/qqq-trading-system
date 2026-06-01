"""Additional v7 signal engines matching the analysis dashboard signal names."""
from typing import Dict, Optional

from .base import BaseEngine, Signal, SignalDirection


def _sma(values, period):
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def _rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    changes = [closes[i] - closes[i - 1] for i in range(len(closes) - period, len(closes))]
    gains = [c for c in changes if c > 0]
    losses = [-c for c in changes if c < 0]
    avg_gain = sum(gains) / period if gains else 0
    avg_loss = sum(losses) / period if losses else 0
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


class KlinePatternEngine(BaseEngine):
    """Engulfing / strong continuation candle pattern."""

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.name = 'kline_pattern'
        self.priority = 3
        self.min_body_pct = cfg.get('kline_min_body_pct', 0.0007)
        self.vol_mult = cfg.get('kline_vol_mult', 1.1)

    def check(self) -> Optional[Signal]:
        if not self._initialized or len(self.bars) < 21:
            return None
        cur = self.bars[-1]
        prev = self.bars[-2]
        price = cur['close']
        body = abs(cur['close'] - cur['open']) / cur['open'] if cur['open'] else 0
        vol_avg = sum(self.volumes[-21:-1]) / 20
        vol_ratio = cur['volume'] / vol_avg if vol_avg > 0 else 0
        if body < self.min_body_pct or vol_ratio < self.vol_mult:
            return None

        bullish_engulf = (
            prev['close'] < prev['open'] and cur['close'] > cur['open'] and
            cur['close'] > prev['open'] and cur['open'] <= prev['close']
        )
        bearish_engulf = (
            prev['close'] > prev['open'] and cur['close'] < cur['open'] and
            cur['close'] < prev['open'] and cur['open'] >= prev['close']
        )
        recent_high = max(self.highs[-6:-1])
        recent_low = min(self.lows[-6:-1])
        strong_up = cur['close'] > recent_high and cur['close'] > cur['open']
        strong_down = cur['close'] < recent_low and cur['close'] < cur['open']

        if bullish_engulf or strong_up:
            strength = min(100, 55 + body * 20000 + (vol_ratio - 1) * 20)
            return Signal(self.name, SignalDirection.CALL, strength, price,
                          f"K线多头形态 body={body*100:.2f}% vol={vol_ratio:.1f}x",
                          {'body_pct': body, 'vol_ratio': vol_ratio})
        if bearish_engulf or strong_down:
            strength = min(100, 55 + body * 20000 + (vol_ratio - 1) * 20)
            return Signal(self.name, SignalDirection.PUT, strength, price,
                          f"K线空头形态 body={body*100:.2f}% vol={vol_ratio:.1f}x",
                          {'body_pct': body, 'vol_ratio': vol_ratio})
        return None


class GranvillePullbackEngine(BaseEngine):
    """MA pullback continuation based on Granville-style rules."""

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.name = 'granville_pullback'
        self.priority = 3
        self.ma_period = cfg.get('granville_ma_period', 20)
        self.trend_period = cfg.get('granville_trend_period', 50)
        self.touch_pct = cfg.get('granville_touch_pct', 0.0018)

    def check(self) -> Optional[Signal]:
        if not self._initialized or len(self.closes) < self.trend_period:
            return None
        ma = _sma(self.closes, self.ma_period)
        trend_ma = _sma(self.closes, self.trend_period)
        if not ma or not trend_ma:
            return None
        cur = self.bars[-1]
        prev = self.bars[-2]
        price = cur['close']
        ma_slope = ma - _sma(self.closes[:-1], self.ma_period)

        touched_from_above = prev['low'] <= ma * (1 + self.touch_pct) and prev['close'] >= ma * (1 - self.touch_pct)
        touched_from_below = prev['high'] >= ma * (1 - self.touch_pct) and prev['close'] <= ma * (1 + self.touch_pct)

        if price > trend_ma and ma_slope > 0 and touched_from_above and cur['close'] > cur['open'] and cur['close'] > ma:
            dist = (price - ma) / ma * 100
            strength = min(100, 58 + dist * 120)
            return Signal(self.name, SignalDirection.CALL, strength, price,
                          f"Granville回踩MA{self.ma_period}后上行 dist={dist:.2f}%",
                          {'ma': ma, 'trend_ma': trend_ma, 'dist_pct': dist})
        if price < trend_ma and ma_slope < 0 and touched_from_below and cur['close'] < cur['open'] and cur['close'] < ma:
            dist = (ma - price) / ma * 100
            strength = min(100, 58 + dist * 120)
            return Signal(self.name, SignalDirection.PUT, strength, price,
                          f"Granville反抽MA{self.ma_period}后下行 dist={dist:.2f}%",
                          {'ma': ma, 'trend_ma': trend_ma, 'dist_pct': dist})
        return None


class ChanFirstBuyEngine(BaseEngine):
    """Simplified Chan first-buy reversal: new low + RSI recovery + bullish confirmation."""

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.name = 'chan_first_buy'
        self.priority = 5
        self.lookback = cfg.get('chan_first_buy_lookback', 30)

    def check(self) -> Optional[Signal]:
        if not self._initialized or len(self.closes) < self.lookback + 5:
            return None
        cur = self.bars[-1]
        prev = self.bars[-2]
        price = cur['close']
        recent_low = min(self.lows[-self.lookback:])
        prev_low = min(self.lows[-self.lookback:-1])
        rsi_now = _rsi(self.closes, 14)
        rsi_prev = _rsi(self.closes[:-1], 14)
        sma20 = _sma(self.closes, 20)
        if not sma20:
            return None
        new_low_then_reclaim = prev['low'] <= prev_low and price > prev['high']
        bullish_confirm = cur['close'] > cur['open'] and price > sma20 * 0.998
        rsi_recover = rsi_prev <= 35 and rsi_now > rsi_prev + 3
        if new_low_then_reclaim and bullish_confirm and rsi_recover:
            rebound = (price - recent_low) / recent_low * 100 if recent_low else 0
            strength = min(100, 55 + rebound * 80 + (35 - min(rsi_prev, 35)))
            return Signal(self.name, SignalDirection.CALL, strength, price,
                          f"缠论一买简化: 新低后收复 RSI {rsi_prev:.0f}->{rsi_now:.0f}",
                          {'recent_low': recent_low, 'rsi': rsi_now, 'rebound_pct': rebound})
        return None


class RSIOverboughtEngine(BaseEngine):
    """RSI overbought reversal signal."""

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.name = 'rsi_overbought'
        self.priority = 4
        self.threshold = cfg.get('rsi_overbought_signal', cfg.get('rsi_overbought', 75))

    def check(self) -> Optional[Signal]:
        if not self._initialized or len(self.closes) < 20:
            return None
        cur = self.bars[-1]
        prev = self.bars[-2]
        price = cur['close']
        rsi_now = _rsi(self.closes, 14)
        rsi_prev = _rsi(self.closes[:-1], 14)
        bearish_turn = cur['close'] < cur['open'] and cur['close'] < prev['close']
        near_high = price >= max(self.highs[-20:]) * 0.998
        if rsi_prev >= self.threshold and rsi_now < rsi_prev and bearish_turn and near_high:
            strength = min(100, 55 + (rsi_prev - self.threshold) * 2 + (rsi_prev - rsi_now) * 4)
            return Signal(self.name, SignalDirection.PUT, strength, price,
                          f"RSI超买回落 {rsi_prev:.0f}->{rsi_now:.0f}",
                          {'rsi': rsi_now, 'rsi_prev': rsi_prev})
        return None


class MomentumDeathEngine(BaseEngine):
    """Bearish momentum failure after an extended move."""

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.name = 'momentum_death'
        self.priority = 4

    def check(self) -> Optional[Signal]:
        if not self._initialized or len(self.closes) < 35:
            return None
        cur = self.bars[-1]
        price = cur['close']
        sma8 = _sma(self.closes, 8)
        sma21 = _sma(self.closes, 21)
        if not sma8 or not sma21:
            return None
        rsi_now = _rsi(self.closes, 14)
        rsi_prev = _rsi(self.closes[:-1], 14)
        extension = (max(self.highs[-20:]) - min(self.lows[-20:])) / price * 100 if price else 0
        lower_high = self.highs[-1] < self.highs[-2] <= self.highs[-3]
        bearish_close = cur['close'] < cur['open'] and cur['close'] < sma8
        trend_roll = sma8 < _sma(self.closes[:-1], 8) and sma8 > sma21
        rsi_roll = rsi_prev >= 60 and rsi_now < rsi_prev - 3
        if extension >= 0.35 and lower_high and bearish_close and trend_roll and rsi_roll:
            strength = min(100, 58 + extension * 40 + (rsi_prev - rsi_now) * 3)
            return Signal(self.name, SignalDirection.PUT, strength, price,
                          f"动量衰竭转弱 RSI {rsi_prev:.0f}->{rsi_now:.0f}",
                          {'extension_pct': extension, 'rsi': rsi_now})
        return None
