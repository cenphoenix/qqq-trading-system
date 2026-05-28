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
        # ATR (Average True Range) for dynamic chase filter
        self._atr_tr = []       # True Range history
        self.atr = 0.0          # current ATR value
        # -- P1 #1 Regime Detection v2 extras --
        self._price_atr_ratio_history = []  # ATR/price ratio
        self._open_period_bars = 0          # bars in opening-range period
        self._opening_range_done = False    # whether opening range has ended
        self._current_et_minute = 0         # current ET minute (1030=17:10 etc)
        self._base_atr = cfg.get('base_atr', 0.35)  # baseline ATR for normalization
        self.state = {
            'sma20': {'ok': None, 'val': '--', 'detail': '--'},
            'sma50': {'ok': None, 'val': '--', 'detail': '--'},
            'price_pos': {'ok': None, 'val': '--', 'detail': '--'},
            'trend': {'ok': None, 'val': '--', 'detail': '--'},
            'vwap': {'ok': None, 'val': '--', 'detail': '--'},
            'macd': {'ok': None, 'val': '--', 'detail': '--'},
            'atr': {'ok': None, 'val': '--', 'detail': '--'},
            'gap': {'ok': None, 'val': '--', 'detail': '--'},
            'dir': '', 'price': '--', 'all_ok': False,
        }

    def update(self, bar: dict, et_minute: int = 0) -> None:
        """每根K线完成后调用，预计算滤镜状态"""
        self._current_et_minute = et_minute
        # Track opening range period: first 30 min of trading (09:35-10:05 ET ≈ min 575-605)
        if not self._opening_range_done:
            if et_minute >= 605:  # 10:05 ET
                self._opening_range_done = True
            else:
                self._open_period_bars += 1

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

        # ATR calculation (14-period Wilder smoothing)
        if len(self.bars) >= 2:
            prev_bar = self.bars[-2]
            tr = max(
                bar['high'] - bar['low'],
                abs(bar['high'] - prev_bar['close']),
                abs(bar['low'] - prev_bar['close'])
            )
        else:
            tr = bar['high'] - bar['low']
        self._atr_tr.append(tr)
        if len(self._atr_tr) > 200:
            self._atr_tr = self._atr_tr[-200:]
        atr_period = 14
        if len(self._atr_tr) < atr_period:
            self.atr = np.mean(self._atr_tr)
        elif len(self._atr_tr) == atr_period:
            self.atr = np.mean(self._atr_tr[-atr_period:])
        else:
        # Wilder smoothing
            self.atr = (self.atr * (atr_period - 1) + tr) / atr_period

        # -- P1 #1: Track ATR/price ratio for normalized volatility --
        price = ch[-1] if ch else 1
        atr_pct = (self.atr / price * 100) if price > 0 else 0
        self._price_atr_ratio_history.append(atr_pct)
        if len(self._price_atr_ratio_history) > 200:
            self._price_atr_ratio_history = self._price_atr_ratio_history[-200:]

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
        self.state['atr'] = {
            'ok': None,
            'val': f'${self.atr:.2f}',
            'detail': f'ATR14=${self.atr:.2f}',
            'atr': self.atr,
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
        """检查预加载滤镜（SMA50, price_pos, trend, VWAP缓冲, MACD）"""
        ch = self.closes
        cs = self.bars
        price = ch[-1] if ch else 0

        # v7: SMA20不再作为过滤条件（1分钟噪音太大）
        sma20 = np.mean(ch[-20:]) if len(ch) >= 20 else None
        sma20_prev = np.mean(ch[-21:-1]) if len(ch) >= 21 else None
        sma20_rising = sma20 > sma20_prev if sma20 is not None and sma20_prev is not None else True
        sma20_ok = True  # v7: 始终为True，不参与过滤

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

        # v7: VWAP增加±0.1%缓冲区
        vwap_ok = True
        if self.vwap > 0:
            vwap_buffer = self.vwap * 0.001  # 0.1%缓冲
            if dir == 'call' and price < (self.vwap - vwap_buffer):
                vwap_ok = False
            if dir == 'put' and price > (self.vwap + vwap_buffer):
                vwap_ok = False

        macd_ok = True
        if self._macd_bars >= 9:
            if dir == 'call' and self.macd_hist <= 0:
                macd_ok = False
            if dir == 'put' and self.macd_hist >= 0:
                macd_ok = False

        # v7: 只计算SMA50+pos+trend+VWAP+MACD（SMA20不参与）
        if regime == 'trending':
            bonus_passed = sum([sma50_ok, pos_ok, trend_ok, vwap_ok])
        else:
            bonus_passed = sum([sma50_ok, pos_ok, trend_ok, vwap_ok, macd_ok])
        
        # v7: preloaded_pass已在get_regime_params中设为2
        all_ok = bonus_passed >= 2

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
        self._atr_tr = []
        self.atr = 0.0
        # P1 #1 重置新变量
        self._price_atr_ratio_history = []
        self._open_period_bars = 0
        self._opening_range_done = False
        self._current_et_minute = 0

    def detect_regime(self) -> tuple:
        """P1 #1 市场状态检测 v2 — 升级：ATR归一化 + 开盘区间 + 时间衰减"""
        if len(self.bars) < 10:
            return 'neutral', '数据不足'

        recent = self.bars[-10:]

        recent_range = np.mean([b['high'] - b['low'] for b in recent[-5:]])
        older_range = np.mean([b['high'] - b['low'] for b in recent[:-5]])
        range_ratio = recent_range / older_range if older_range > 0 else 1.0

        bull = sum(1 for b in recent if b['close'] >= b['open'])
        bear = 20 - bull
        consistency = max(bull, bear) / 10.0

        highs = [b['high'] for b in recent]
        lows = [b['low'] for b in recent]
        new_highs = sum(1 for i in range(1, len(highs)) if highs[i] >= max(highs[:i]))
        new_lows = sum(1 for i in range(1, len(lows)) if lows[i] <= min(lows[:i]))
        trend_strength = (new_highs + new_lows) / 19.0

        # -- P1 #1 (a): ATR归一化 --
        price = self.closes[-1] if self.closes else 1
        atr_pct = (self.atr / price * 100) if price > 0 else 0
        base_atr_pct = self._base_atr / price * 100
        atr_normalized = atr_pct / base_atr_pct if base_atr_pct > 0 else 1.0

        # 近期ATR加速
        atr_accel = 1.0
        if len(self._price_atr_ratio_history) >= 10:
            recent_atr = np.mean(self._price_atr_ratio_history[-10:])
            older_atr = np.mean(self._price_atr_ratio_history[-20:-10])
            if older_atr > 0:
                atr_accel = recent_atr / older_atr

        detail = f'波动比{range_ratio:.2f} 方向{consistency:.0%} 趋势{trend_strength:.0%} ATR%{atr_pct:.2f}'

        # -- P1 #1 (b): 开盘区间（前30min，降低灵敏度）--
        if not self._opening_range_done and self._open_period_bars > 5:
            return 'neutral', f'开盘探索期({self._open_period_bars}根) ATR%{atr_pct:.2f}'

        # -- P1 #1 (c): 时间衰减（14:00 ET后流动性下降，倾向震荡）--
        time_decay_factor = 1.0
        if self._current_et_minute >= 840:  # 14:00 ET
            time_decay_factor = 0.85  # 趋势阈值收紧15%
            detail += f' 午后衰减×{time_decay_factor:.2f}'

        # 综合判断
        adjusted_trend_threshold = 0.5 * time_decay_factor
        adjusted_range_threshold = 1.2 * (1 / time_decay_factor) if time_decay_factor < 1 else 1.2

        if range_ratio < 0.85 and consistency < 0.65 and trend_strength < 0.4 * time_decay_factor:
            return 'choppy', detail
        elif range_ratio > adjusted_range_threshold or consistency > 0.65 or trend_strength > adjusted_trend_threshold or atr_accel > 1.3:
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
                'lookback': 2,          # v7: 3→2, 快速捕捉
                'pullback': True,
                'vol_mult': 0.5,        # v7: 0.7→0.5, 放宽量能
                'min_body': 0.0001,     # v7: 0.0002→0.0001, 接受小实体
                'preloaded_pass': 2,    # v7: 3→2, 降低共识要求
                'gap_mult': 1.5,
                'tp_partial_pct': 1.00,
                'sl_pct': 0.25,
                'timeout_bars': 9999,
                'pos_mult': 0.7,
                'is_opening_period': False,
            }
        elif regime == 'choppy':
            return {
                'regime': 'choppy',
                'detail': detail,
                'lookback': 2,
                'pullback': False,
                'vol_mult': 0.5,        # v7: 0.6→0.5
                'min_body': 0.0001,
                'preloaded_pass': 2,    # v7: 2 不变
                'gap_mult': 2.0,
                'tp_partial_pct': 0.50,
                'sl_pct': 0.30,
                'timeout_bars': 8,
                'pos_mult': 0.8,
                'is_opening_period': False,
            }
        else:
            is_open = not self._opening_range_done and self._open_period_bars > 5
            return {
                'regime': 'neutral',
                'detail': detail,
                'lookback': 2,          # v7: 5→2, 快速捕捉
                'pullback': False,
                'vol_mult': 0.5,        # v7: 0.8→0.5
                'min_body': 0.0001,     # v7: 0.0003→0.0001
                'preloaded_pass': 2,    # v7: 3→2
                'gap_mult': 1.0,
                'tp_partial_pct': 0.80,
                'sl_pct': 0.25,
                'timeout_bars': 8,
                'pos_mult': 0.4,
                'is_opening_period': is_open,
            }

    def status(self) -> dict:
        """返回当前滤镜状态（供Web读取）"""
        return dict(self.state)