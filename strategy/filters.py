"""
滤镜引擎 - 预计算市场状态和过滤条件
"""
import numpy as np


class FilterEngine:
    """
    每根K线预计算5个滤镜状态，触发时只查 价格突破+成交量+K线形态。
    Classic/Accelerated 路径自动切换。
    """
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.closes = []
        self.volumes = []
        self.bars = []
        self.session_high = 0
        self.session_low = 999999
        self.vwap_cum_tp_vol = 0.0
        self.vwap_cum_vol = 0
        self.vwap = 0.0
        self.ema12 = None
        self.ema26 = None
        self.macd_line = 0.0
        self.signal_line = 0.0
        self.macd_hist = 0.0
        self._macd_bars = 0
        self._macd_line_history = []
        self.state = {
            'sma20': {'ok': None, 'val': '--', 'detail': '--'},
            'sma50': {'ok': None, 'val': '--', 'detail': '--'},
            'price_pos': {'ok': None, 'val': '--', 'detail': '--'},
            'trend': {'ok': None, 'val': '--', 'detail': '--'},
            'vwap': {'ok': None, 'val': '--', 'detail': '--'},
            'macd': {'ok': None, 'val': '--', 'detail': '--'},
            'gap': {'ok': None, 'val': '--', 'detail': '--'},
            'dir': '', 'price': '--', 'all_ok': False,
        }

    def update(self, bar: dict) -> None:
        """每根K线完成后调用，预计算滤镜状态"""
        self.bars.append(bar)
        self.closes.append(bar['close'])
        self.volumes.append(bar['volume'])
        self.session_high = max(self.session_high, bar['high'])
        self.session_low = min(self.session_low, bar['low'])

        if len(self.closes) > 500:
            self.closes = self.closes[-500:]
            self.volumes = self.volumes[-500:]
            self.bars = self.bars[-500:]

        ch = self.closes
        price = bar['close']

        typical_price = (bar['high'] + bar['low'] + bar['close']) / 3.0
        self.vwap_cum_tp_vol += typical_price * bar['volume']
        self.vwap_cum_vol += bar['volume']
        self.vwap = self.vwap_cum_tp_vol / self.vwap_cum_vol if self.vwap_cum_vol > 0 else price

        self._macd_bars += 1
        ema12_mult = 2.0 / (12 + 1)
        ema26_mult = 2.0 / (26 + 1)
        signal_mult = 2.0 / (9 + 1)
        if self.ema12 is None:
            self.ema12 = np.mean(ch[-12:]) if len(ch) >= 12 else price
            self.ema26 = np.mean(ch[-26:]) if len(ch) >= 26 else price
        else:
            self.ema12 = self.ema12 + ema12_mult * (price - self.ema12)
            self.ema26 = self.ema26 + ema26_mult * (price - self.ema26)
        self.macd_line = self.ema12 - self.ema26
        self._macd_line_history.append(self.macd_line)
        if len(self._macd_line_history) > 50:
            self._macd_line_history = self._macd_line_history[-50:]
        if self._macd_bars < 9:
            self.signal_line = np.mean(self._macd_line_history) if self._macd_line_history else self.macd_line
        elif self._macd_bars == 9:
            self.signal_line = np.mean(self._macd_line_history[-9:])
        else:
            self.signal_line = self.signal_line + signal_mult * (self.macd_line - self.signal_line)
        self.macd_hist = 2.0 * (self.macd_line - self.signal_line)

        sma20 = np.mean(ch[-20:]) if len(ch) >= 20 else None
        sma20_prev = np.mean(ch[-21:-1]) if len(ch) >= 21 else None
        sma20_rising = sma20 > sma20_prev if sma20 is not None and sma20_prev is not None else None

        sma50 = np.mean(ch[-50:]) if len(ch) >= 50 else None

        price_pos = 0.5
        if self.session_high > self.session_low:
            price_pos = (price - self.session_low) / (self.session_high - self.session_low)

        trend_bull = 0
        trend_bear = 0
        if len(self.bars) >= 5:
            for b in self.bars[-5:]:
                if b['close'] >= b['open']:
                    trend_bull += 1
                else:
                    trend_bear += 1

        self.state['sma20'] = {
            'ok': None,
            'val': f'{sma20:.2f}' if sma20 else '--',
            'detail': f'SMA20={sma20:.2f}' if sma20 else '数据不足',
            'sma20': sma20,
            'rising': sma20_rising,
        }
        self.state['sma50'] = {
            'ok': None,
            'val': f'{sma50:.2f}' if sma50 else '--',
            'detail': f'SMA50={sma50:.2f}' if sma50 else '数据不足',
            'sma50': sma50,
        }
        self.state['price_pos'] = {
            'ok': None,
            'val': f'{price_pos*100:.0f}%',
            'detail': f'当日位置{price_pos*100:.0f}%',
            'pos': price_pos,
        }
        self.state['trend'] = {
            'ok': None,
            'val': f'{trend_bull}阳{trend_bear}阴' if len(self.bars) >= 5 else '--',
            'detail': f'最近5根{trend_bull}阳{trend_bear}阴' if len(self.bars) >= 5 else '数据不足',
            'bull': trend_bull,
            'bear': trend_bear,
        }
        self.state['vwap'] = {
            'ok': None,
            'val': f'${self.vwap:.2f}',
            'detail': f'VWAP=${self.vwap:.2f}',
            'vwap': self.vwap,
        }
        self.state['macd'] = {
            'ok': None,
            'val': f'{self.macd_hist:+.3f}',
            'detail': f'DIF={self.macd_line:+.3f} DEA={self.signal_line:+.3f} MACD={self.macd_hist:+.3f}',
            'macd_hist': self.macd_hist,
            'macd_line': self.macd_line,
            'signal_line': self.signal_line,
        }
        self.state['gap'] = {
            'ok': None,
            'val': '--',
            'detail': '--',
        }

    def check_filters(self, dir: str, entry_price: float, bar: dict, vol_avg_20: float) -> tuple:
        """核心过滤检查（仅 价格突破+成交量+K线形态）"""
        vol_ok = True
        cur_vol = bar['volume'] if bar else 0
        if vol_avg_20 > 0 and cur_vol < vol_avg_20 * self.cfg['vol_mult']:
            vol_ok = False

        mom_ok = bar['close'] >= bar['open'] if dir == 'call' else bar['close'] <= bar['open']
        cur_body = abs(bar['close'] - bar['open']) / bar['open'] if bar['open'] else 0
        body_ok = cur_body >= self.cfg['min_body']

        core_ok = vol_ok and mom_ok and body_ok

        return core_ok, {
            'volume': {'ok': vol_ok, 'val': f'{cur_vol:,}',
                        'detail': f'{cur_vol:,}>={vol_avg_20*self.cfg["vol_mult"]:,.0f}' if vol_avg_20 else '数据不足'},
            'momentum': {'ok': mom_ok, 'val': '阳' if bar['close'] >= bar['open'] else '阴',
                          'detail': f'{"阳线✓" if mom_ok else "非阳线✗"}' if dir == 'call' else f'{"阴线✓" if mom_ok else "非阴线✗"}'},
            'body': {'ok': body_ok, 'val': f'{cur_body*100:.3f}%',
                      'detail': f'{cur_body*100:.3f}%{"≥" if body_ok else "<"}{self.cfg["min_body"]*100:.2f}%'},
        }

    def check_preloaded(self, dir: str, regime: str = None) -> tuple:
        """检查预加载滤镜（SMA20, SMA50, price_pos, trend, VWAP, MACD）"""
        ch = self.closes
        cs = self.bars
        price = ch[-1] if ch else 0

        sma20 = np.mean(ch[-20:]) if len(ch) >= 20 else None
        sma20_prev = np.mean(ch[-21:-1]) if len(ch) >= 21 else None
        sma20_rising = sma20 > sma20_prev if sma20 is not None and sma20_prev is not None else True
        sma20_ok = True
        if sma20 is not None:
            if dir == 'call' and (price < sma20 or not sma20_rising):
                sma20_ok = False
            if dir == 'put' and (price > sma20 or sma20_rising):
                sma20_ok = False

        sma50 = np.mean(ch[-50:]) if len(ch) >= 50 else None
        sma50_ok = True
        if sma50 is not None:
            if dir == 'call' and price < sma50:
                sma50_ok = False
            if dir == 'put' and price > sma50:
                sma50_ok = False

        price_pos = 0.5
        if self.session_high > self.session_low:
            price_pos = (price - self.session_low) / (self.session_high - self.session_low)
        pos_ok = True
        if dir == 'call' and price_pos > 0.85:
            pos_ok = False
        if dir == 'put' and price_pos < 0.15:
            pos_ok = False

        trend_ok = True
        bull = bear = 0
        if len(cs) >= 5:
            for b in cs[-5:]:
                if b['close'] >= b['open']:
                    bull += 1
                else:
                    bear += 1
            if dir == 'call' and bull < 3:
                trend_ok = False
            if dir == 'put' and bear < 3:
                trend_ok = False

        vwap_ok = True
        if self.vwap > 0:
            if dir == 'call' and price < self.vwap:
                vwap_ok = False
            if dir == 'put' and price > self.vwap:
                vwap_ok = False

        macd_ok = True
        if self._macd_bars >= 9:
            if dir == 'call' and self.macd_hist <= 0:
                macd_ok = False
            if dir == 'put' and self.macd_hist >= 0:
                macd_ok = False

        if regime == 'trending':
            bonus_passed = sum([sma20_ok, sma50_ok, pos_ok, trend_ok, vwap_ok])
            all_ok = bonus_passed >= 4
        else:
            bonus_passed = sum([sma20_ok, sma50_ok, pos_ok, trend_ok, vwap_ok, macd_ok])
            all_ok = bonus_passed >= 4

        return all_ok, {
            'sma20': {'ok': sma20_ok, 'val': f'{sma20:.2f}' if sma20 else '--',
                       'detail': f'SMA20={sma20:.2f} {"↑" if sma20_rising else "↓"}' if sma20 else '数据不足'},
            'sma50': {'ok': sma50_ok, 'val': f'{sma50:.2f}' if sma50 else '--',
                       'detail': f'SMA50={sma50:.2f}' if sma50 else '数据不足'},
            'price_pos': {'ok': pos_ok, 'val': f'{price_pos*100:.0f}%',
                           'detail': f'当日位置{price_pos*100:.0f}%'},
            'trend': {'ok': trend_ok, 'val': f'{bull}阳{bear}阴' if len(cs) >= 5 else '--',
                       'detail': f'最近5根{bull}阳{bear}阴' if len(cs) >= 5 else '数据不足'},
            'vwap': {'ok': vwap_ok, 'val': f'${self.vwap:.2f}',
                      'detail': f'价格{"above" if price > self.vwap else "below"} VWAP${self.vwap:.2f}'},
            'macd': {'ok': macd_ok, 'val': f'{self.macd_hist:+.3f}',
                      'detail': f'MACD柱={self.macd_hist:+.3f} {"↑" if self.macd_hist > 0 else "↓"}'},
        }, bonus_passed

    def reset_day(self) -> None:
        """日初重置"""
        self.closes = []
        self.volumes = []
        self.bars = []
        self.session_high = 0
        self.session_low = 999999
        self.vwap_cum_tp_vol = 0.0
        self.vwap_cum_vol = 0
        self.vwap = 0.0
        self.ema12 = None
        self.ema26 = None
        self.macd_line = 0.0
        self.signal_line = 0.0
        self.macd_hist = 0.0
        self._macd_bars = 0
        self._macd_line_history = []

    def detect_regime(self) -> tuple:
        """检测市场状态：trending(趋势) / neutral(中性) / choppy(震荡)"""
        if len(self.bars) < 20:
            return 'neutral', '数据不足'

        recent = self.bars[-20:]

        recent_range = np.mean([b['high'] - b['low'] for b in recent[-5:]])
        older_range = np.mean([b['high'] - b['low'] for b in recent[:-5]])
        range_ratio = recent_range / older_range if older_range > 0 else 1.0

        bull = sum(1 for b in recent if b['close'] >= b['open'])
        bear = 20 - bull
        consistency = max(bull, bear) / 20.0

        highs = [b['high'] for b in recent]
        lows = [b['low'] for b in recent]
        new_highs = sum(1 for i in range(1, len(highs)) if highs[i] >= max(highs[:i]))
        new_lows = sum(1 for i in range(1, len(lows)) if lows[i] <= min(lows[:i]))
        trend_strength = (new_highs + new_lows) / 19.0

        all_highs = max(b['high'] for b in recent)
        all_lows = min(b['low'] for b in recent)
        price_range = all_highs - all_lows
        mid_price = self.closes[-1]
        price_pos = (mid_price - all_lows) / price_range if price_range > 0 else 0.5

        detail = f'波动比{range_ratio:.2f} 方向{consistency:.0%} 趋势{trend_strength:.0%}'

        if range_ratio < 0.85 and consistency < 0.65 and trend_strength < 0.4:
            return 'choppy', detail
        elif range_ratio > 1.2 or consistency > 0.70 or trend_strength > 0.5:
            return 'trending', detail
        else:
            return 'neutral', detail

    def get_regime_params(self) -> dict:
        """根据市场状态返回动态参数"""
        regime, detail = self.detect_regime()

        if regime == 'trending':
            return {
                'regime': 'trending',
                'detail': detail,
                'lookback': 3,
                'pullback': True,
                'vol_mult': 0.7,
                'min_body': 0.0002,
                'preloaded_pass': 3,
                'gap_mult': 1.5,
                'tp_partial_pct': 1.00,
                'sl_pct': 0.25,
                'timeout_bars': 9999,
                'pos_mult': 0.7,
            }
        elif regime == 'choppy':
            return {
                'regime': 'choppy',
                'detail': detail,
                'lookback': 2,
                'pullback': False,
                'vol_mult': 0.6,
                'min_body': 0.0001,
                'preloaded_pass': 2,
                'gap_mult': 2.0,
                'tp_partial_pct': 0.50,
                'sl_pct': 0.30,
                'timeout_bars': 8,
                'pos_mult': 0.8,
            }
        else:
            return {
                'regime': 'neutral',
                'detail': detail,
                'lookback': 3,
                'pullback': False,
                'vol_mult': 0.8,
                'min_body': 0.0003,
                'preloaded_pass': 3,
                'gap_mult': 1.0,
                'tp_partial_pct': 0.80,
                'sl_pct': 0.25,
                'timeout_bars': 4,
                'pos_mult': 0.4,
            }

    def status(self) -> dict:
        """返回当前滤镜状态（供Web读取）"""
        return dict(self.state)