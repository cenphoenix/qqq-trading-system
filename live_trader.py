#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
QQQ 0DTE 动态过滤突破策略 - 实盘交易系统 v7
Regime-adaptive 市场状态检测 (trending/neutral/choppy)
预加载滤镜(SMA20+SMA50+位置+趋势+VWAP+MACD) + 核心过滤(量能+动量+实体)
RSI方向确认 + ATR追高回撤 + 回踩确认(动量豁免)
动态参数: lookback/vol_mult/body/gap_mult/SL/timeout/pos_mult

功能：
1. 实时订阅QQQ 1分钟K线 → 信号检测
2. Regime自适应双向突破信号 (v6.3核心策略 + v6.5现代优化)
3. 自动下单（长桥API / OAuth2）
4. 风控：动态止损/半仓止盈/分阶段超时/日亏损熔断/冷却
5. Telegram推送交易信号 / Web仪表盘
"""
import os, sys, time, json, signal, math, re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import numpy as np

# 时区常量（自动处理夏令时/冬令时）
from zoneinfo import ZoneInfo
TZ_ET = ZoneInfo("America/New_York")    # 美东（自动EDT/EST切换）


# stdout兜底（打包后 console=False 时可能为 None）
if sys.stdout is None:
    sys.stdout = open(os.devnull, 'w', encoding='utf-8', errors='replace')
if sys.stderr is None:
    sys.stderr = open(os.devnull, 'w', encoding='utf-8', errors='replace')


def _json_default(obj):
    """JSON 序列化兜底：处理 numpy 类型和 datetime"""
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")

# ===== 策略模块 =====
from strategy import get_option_symbol, FilterEngine
from strategy.price_action import PriceActionFilter
from signal_names import display_signal_name
from v7_integration import V7Integration

# ===== 长桥SDK =====
from longbridge.openapi import (
    Config, QuoteContext, TradeContext,
    SubType, Period, AdjustType, OAuthBuilder,
    OrderSide, OrderType, TimeInForceType, OutsideRTH
)

# ===== 配置（从 config_manager 加载）=====
from config_manager import get_flat_config

def _load_config():
    """从 settings.json 加载配置，失败回退默认值"""
    try:
        cfg = get_flat_config()
        cfg['pos_pct'] = cfg.get('order_pct', 8)
        return cfg
    except Exception as e:
        print(f"[Config] settings.json 加载失败: {e}, 使用默认配置")
        return {
            'symbol': 'QQQ.US', 'sl': 0.25, 'tp': 0.30,
            'lookback': 3, 'lookback_accel': 2, 'pullback_confirm': False,
            'rsi_period': 14, 'rsi_overbought': 75, 'rsi_oversold': 25,
            'loss_cooldown': 3, 'tp_partial_pct': 1.00, 'tp_trail_drop': 0.30,
            'stock_trail_pct': 0.003, 'timeout_stage1_bars': 5, 'timeout_stage1_min': 0.30,
            'timeout_stage2_bars': 10, 'timeout_stage2_min': 0.60, 'timeout_stage3_bars': 15,
            'option_offset': 2.0, 'order_pct': 20, 'contract_multiplier': 100,
            'pos_pct': 20, 'max_trades': 999, 'daily_limit': 25,
            'start_time': '09:40', 'end_time': '14:30',
            'extended_end_time': '15:00',
            'extension_order_pct': 5,
            'trail_activate': 0.10, 'trail_drop': 0.05,
            'stock_exit_enabled': True, 'stock_sl_pct': 0.0025, 'stock_tp_pct': 0.0040,
            'stock_trail_activate': 0.0030, 'stock_trail_drop': 0.0015,
            'enable_put_entries': True, 'put_time_stop_bars': 5,
            'put_order_pct': 8.0,
            'put_allowed_signals': ['VWAP_Breakout', 'Kline_Pattern', 'Granville_Pullback'],
            'put_quality_filter': True, 'put_min_strength': 80,
            'put_min_price_pos': 0.20, 'put_min_vwap_dist': 0.0010,
            'put_min_macd_hist_abs': 0.0, 'put_min_sma20_slope_abs': 0.0,
            'price_action_filter': True, 'price_action_require_put_trend': True,
            'price_action_min_close_location': 0.65, 'price_action_min_body_ratio': 1.0,
            'price_action_min_direction_bars': 3,
            'price_action_tight_overlap': 0.62, 'price_action_tight_alternation': 0.43,
            'price_action_require_call_quality': True,
            'price_action_vwap_call_max_range_position': 0.40,
            'price_action_require_ema_call_strong_bar': True,
            'price_action_call_min_close_location': 0.55,
            'price_action_call_min_body_ratio': 0.80,
            'price_action_trend_extend_timeout_bars': 20,
            'brooks_priority_mode': True,
            'brooks_range_call_max_position': 0.40,
            'brooks_range_put_min_position': 0.60,
            'brooks_trend_skip_fixed_stock_tp': True,
            'shadow_signal_tracking': True,
            'shadow_signal_cooldown_bars': 5,
            'shadow_signal_max_per_day': 100,
            'shadow_signal_live_orders': True,
            'shadow_live_order_pos_mult': 0.80,
            'shadow_live_open_pos_mult': 0.50,
            'shadow_live_afternoon_allowed_signals': ['VWAP_Breakout'],
            'shadow_live_sl_pct': 0.22,
            'shadow_live_disable_open_stop_widen': True,
            'trend_day_filter_enabled': True,
            'trend_day_min_bars': 30,
            'trend_day_lookback_bars': 20,
            'trend_day_min_move_pct': 0.0018,
            'trend_day_min_vwap_dist': 0.0010,
            'trend_day_min_sma20_slope': 0.00015,
            'trend_day_countertrend_hard_block': False,
            'market_regime_enabled': True,
            'market_regime_soft_countertrend': True,
            'market_regime_hard_countertrend': False,
            'market_regime_countertrend_pos_mult': 0.15,
            'market_regime_countertrend_sl_pct': 0.20,
            'market_regime_range_breakout_pos_mult': 0.20,
            'market_regime_range_breakout_sl_pct': 0.22,
            'opening_range_filter_enabled': True,
            'opening_range_minutes': 30,
            'opening_range_call_block_pos': 0.90,
            'opening_range_inside_fade_start_min': 690,
            'opening_range_breakout_buffer_pct': 0.0010,
            'opening_range_breakout_min_vwap_dist': 0.0012,
            'opening_range_breakout_min_sma20_slope': 0.00008,
            'opening_range_breakout_min_recent_move_pct': 0.0012,
            'enable_momentum_death_entries': False,
            'momentum_death_pos_mult': 0.55,
            'momentum_death_sl_pct': 0.22,
            'momentum_death_tp_partial_pct': 0.20,
            'momentum_death_timeout_bars': 8,
            'momentum_death_relaxed_put_quality': True,
            'disabled_entry_signals': ['EMA_Cross', 'RSI_Reversal', 'RSI_Overbought', 'Chan_First_Buy', 'Momentum_Death'],
            'enable_countertrend_reversal_entries': False,
            'enable_kline_entries': True,
            'kline_quality_filter': True,
            'kline_call_live_patterns': ['ORB突破', 'BB挤压突破'],
            'kline_max_price_pos': 0.82,
            'kline_min_macd_hist': 0.0,
            'kline_min_sma20_slope': 0.0,
            'enable_granville_entries': True,
            'granville_quality_filter': True,
            'granville_max_price_pos': 0.85,
            'granville_min_macd_hist': 0.0,
            'granville_min_sma20_slope': 0.00005,
            'granville_min_vwap_dist': 0.0005,
            'granville_min_dist_pct': 0.20,
            'granville_require_day_direction': True,
            'trend_quick_trail_activate_pct': 20,
            'trend_quick_trail_drop_pct': 12,
            'trend_timeout_bonus_bars': 4,
            'afternoon_put_start_min': 810,
            'afternoon_put_min_vwap_dist': 0.006,
            'afternoon_put_min_sma20_slope_abs': 0.00005,
            'max_gap': 0.0020, 'vol_mult': 0.8, 'min_body': 0.0003,
            'reversal_drop': 0.002, 'reversal_bounce': 0.001,
            'check_interval': 20, 'capital': 100000,
        }

CONFIG = _load_config()

# 配置热重载机制
_settings_mtime = [0.0]  # 用列表包裹以便在函数内修改

def _app_dir():
    """获取应用根目录（打包后exe目录 / 开发时脚本目录）"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

def _maybe_reload_config():
    """检查 settings.json 是否更新，如果是则热重载"""
    settings_file = os.path.join(_app_dir(), "settings.json")
    if not os.path.exists(settings_file):
        return False
    try:
        mtime = os.path.getmtime(settings_file)
        if mtime <= _settings_mtime[0]:
            return False
        _settings_mtime[0] = mtime
        new_cfg = _load_config()
        CONFIG.clear()
        CONFIG.update(new_cfg)
        print(f"⚙️ 配置已热重载 ({datetime.now(TZ_ET).strftime('%H:%M:%S ET')})")
        return True
    except Exception as e:
        print(f"[Config] 热重载失败: {e}")
        return False


class QQQLiveTrader:
    def __init__(self, config=None):
        self.cfg = config or CONFIG
        self.running = False
        self.position = None
        self.trades_today = []
        self.daily_pnl = 0
        self.kline_buffer = []       # 1分钟K线缓冲
        self.one_min_candles = []    # 1分钟K线（直接用于信号检测）
        self.current_date = None
        self.daily_signals = 0       # 今日已触发信号数

        # 技术指标
        self.close_history = []      # 收盘价历史（计算SMA）
        self.volume_history = []     # 成交量历史（计算均量）
        self.consecutive_losses = 0  # 连续亏损次数
        self.consecutive_wins = 0    # 当前连胜计数
        self.max_consecutive_wins = 0  # 历史最长连胜
        self.max_consecutive_losses = 0  # 历史最长连亏
        self.largest_win_usd = 0     # 最大单笔盈利
        self.largest_loss_usd = 0    # 最大单笔亏损
        self.largest_win_pct = 0     # 最大单笔盈利%
        self.largest_loss_pct = 0    # 最大单笔亏损%
        self.call_trades = 0         # CALL方向交易数
        self.put_trades = 0          # PUT方向交易数
        self.call_wins = 0           # CALL方向盈利数
        self.put_wins = 0            # PUT方向盈利数
        self.call_pnl = 0.0          # CALL方向累计盈亏
        self.put_pnl = 0.0           # PUT方向累计盈亏
        self.loss_cooldown_until = None  # 时间戳冷却：下次允许同向交易的时间
        self.last_loss_dir = None    # 最近一次亏损的方向（'call'或'put'），冷却期间允许反向
        self.big_loss_cooldown = 0   # 大亏(>20%)后同方向冷却剩余K线数
        self._big_loss_dir = None    # 大亏冷却的方向
        self.current_price = 0       # 当前正股价格
        self.actual_capital = self.cfg['capital']  # 实际资金（_execute_trade中更新）
        self.start_time = datetime.now(TZ_ET)  # 启动时间（美东），用于计算运行时间
        self.account_info = {}  # 账户资金信息
        self.yesterday_pnl = 0.0   # 昨日盈亏（启动时加载）
        self.yesterday_trades = 0  # 昨日交易笔数
        self.yesterday_wr = 0.0    # 昨日胜率
        self._daily_summary_sent = False  # 日终总结是否已发送

        # 初始化其他变量
        self._init_vars()

        # v6.5 FilterEngine
        self.engine = FilterEngine(self.cfg)

        # v7 多引擎信号系统
        self.v7 = V7Integration(self.cfg)
        self.price_action = PriceActionFilter(self.cfg)
        self.price_action_state = {'state': 'warming_up', 'direction': '', 'reason': ''}
        self.day_market_regime = {
            'type': 'warming_up',
            'direction': '',
            'label': '预热',
            'reason': '',
        }
        self._last_entry_rejection = ''
        self._hard_skip_shadow_live = False

        # 初始化长桥连接
        self._init_api()

    def _calc_rsi(self, period=14):
        """计算RSI指标（Wilder平滑法）
        
        使用Wilder的指数移动平均替代简单平均，
        在短周期（1分钟线）下更稳定，减少假信号。
        """
        ch = self.close_history
        if len(ch) < period + 1:
            return 50  # 数据不足返回中性值
        
        # 计算价格变化
        deltas = [ch[i] - ch[i-1] for i in range(1, len(ch))]
        
        # 初始平均：用前period个值的简单平均
        gains = [max(d, 0) for d in deltas[:period]]
        losses = [max(-d, 0) for d in deltas[:period]]
        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period
        
        # Wilder平滑：后续用 EMA = (prev * (period-1) + current) / period
        for i in range(period, len(deltas)):
            gain = max(deltas[i], 0)
            loss = max(-deltas[i], 0)
            avg_gain = (avg_gain * (period - 1) + gain) / period
            avg_loss = (avg_loss * (period - 1) + loss) / period
        
        if avg_loss == 0:
            return 100
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    def _init_vars(self):
        """初始化实例变量（在_init_api之后调用）"""
        # 衰竭反转追踪
        self.session_high = 0        # 当日最高价
        self.session_low = 999999    # 当日最低价
        self.reversal_fired = False  # 今日是否已触发过反转信号

        # CSV文件目录（按日归档）
        script_dir = str(_app_dir())
        self.csv_dir = os.path.join(script_dir, 'data', 'candles')
        os.makedirs(self.csv_dir, exist_ok=True)
        self._last_position_verify = 0  # 上次持仓验证时间戳

        # 共享状态文件（供 Web 仪表盘读取）
        self.state_path = os.path.join(script_dir, 'state.json')

        # 信号过滤状态（实时同步给Web）
        self.filter_status = {
            'sma20': {'ok': None, 'val': '--', 'detail': '--'},
            'sma50': {'ok': None, 'val': '--', 'detail': '--'},
            'volume': {'ok': None, 'val': '--', 'detail': '--'},
            'momentum': {'ok': None, 'val': '--', 'detail': '--'},
            'body': {'ok': None, 'val': '--', 'detail': '--'},
            'price_pos': {'ok': None, 'val': '--', 'detail': '--'},
            'trend': {'ok': None, 'val': '--', 'detail': '--'},
            'vwap': {'ok': None, 'val': '--', 'detail': '--'},
            'macd': {'ok': None, 'val': '--', 'detail': '--'},
            'atr': {'ok': None, 'val': '--', 'detail': '--'},
            'dir': '', 'mode': '', 'price': '--', 'all_ok': False,
        }
        self.current_signal = None
        self._missing_position_count = 0  # 长桥持仓未找到计数器（连续3次才清空）
        self._lb_pos_cache = 0            # 长桥持仓缓存
        self._lb_pos_cache_time = 0       # 缓存时间戳
        self._trading_lock = False        # 开仓防重入锁
        self._position_check_lock = False # 报价推送触发风控时防重入
        self._subscribed_quote_symbols = set()
        self.latest_quote_prices = {}
        self._signal_cooldowns = {}

        # -- P0 #7 阶梯式日亏损熔断 --
        self._loss_circuit_warning_fired = False    # 警告级熔断是否已触发
        self._loss_circuit_conservative_fired = False  # 保守级熔断是否已触发

        # Web 显示用的实时账户/持仓
        self._account_state = {}
        self._broker_positions = []
        self.signal_probes = []
        self._signal_probe_seq = 0

        # 实时事件日志（供仪表盘读取）
        self.events = []  # [{'time': 'HH:MM:SS', 'msg': '...', 'tag': 'info/signal/trade/error'}]

    def _add_event(self, msg, tag='info'):
        """添加实时事件（写入state.json供仪表盘显示）"""
        ts = datetime.now(TZ_ET).strftime('%H:%M:%S')
        self.events.append({'time': ts, 'msg': msg, 'tag': tag})
        if len(self.events) > 100:
            self.events = self.events[-100:]

    def _write_csv(self, candle):
        """写入K线数据到 data/candles/{日期}.csv（按日归档）"""
        try:
            # 从K线时间戳获取日期（candle['time']已是ET）
            dt = candle['time']
            if isinstance(dt, datetime):
                date_str = dt.strftime('%Y-%m-%d')
            else:
                date_str = datetime.now(TZ_ET).strftime('%Y-%m-%d')
            filepath = os.path.join(self.csv_dir, f'{date_str}.csv')

            # 文件不存在则写表头
            if not os.path.exists(filepath):
                with open(filepath, 'w', newline='', encoding='utf-8') as f:
                    f.write('timestamp,open,high,low,close,volume,turnover\n')
            # 追加K线数据
            with open(filepath, 'a', newline='', encoding='utf-8') as f:
                ts = candle['time'].strftime('%Y-%m-%d %H:%M:%S')
                f.write(f"{ts},{candle['open']},{candle['high']},{candle['low']},"
                        f"{candle['close']},{candle['volume']},{candle.get('turnover', 0)}\n")
        except Exception as e:
            print(f"  ⚠️ CSV写入失败: {e}")

    def _is_valid_stock_entry_price(self, entry_price, current_stock=None):
        """Validate that entry_price is a QQQ stock price, not an option premium."""
        try:
            entry = float(entry_price or 0)
            current = float(current_stock or self.current_price or 0)
            if entry < 100:
                return False
            if current > 100 and abs(current - entry) / current > 0.10:
                return False
            return True
        except Exception:
            return False

    def _current_stock_price(self):
        price = self.latest_quote_prices.get(self.cfg['symbol']) or self.current_price or 0
        try:
            if float(price) > 100:
                return float(price)
        except Exception:
            pass
        try:
            stock_quotes = self.quote_ctx.quote([self.cfg['symbol']])
            if stock_quotes and float(stock_quotes[0].last_done) > 100:
                return float(stock_quotes[0].last_done)
        except Exception:
            pass
        return 0.0

    def _position_signal_name(self, pos):
        name = pos.get('display_engine') or pos.get('engine') or ''
        if name:
            return display_signal_name(name)
        reason = str(pos.get('reason', ''))
        for candidate in ('VWAP_Breakout', 'Granville_Pullback', 'EMA_Cross', 'Kline_Pattern', 'RSI_Reversal'):
            if candidate in reason:
                return candidate
        if pos.get('regime') == 'neutral':
            return 'Kline_Pattern'
        return display_signal_name(name)

    def _is_trend_aligned_position(self, pos):
        """Return True when a position direction matches the current/day trend."""
        if not pos:
            return False
        direction = pos.get('dir', '')
        day_direction = pos.get('day_market_direction', '')
        regime = pos.get('day_market_regime', '')
        if not day_direction and isinstance(self.day_market_regime, dict):
            day_direction = self.day_market_regime.get('direction', '')
            regime = regime or self.day_market_regime.get('type', '')
        return (
            direction in ('call', 'put')
            and day_direction == direction
            and regime in ('trend_up', 'trend_down')
        )

    def _timeout_profile(self, pos):
        signal = self._position_signal_name(pos)
        reason = str(pos.get('reason', ''))
        regime = pos.get('regime', '')
        if signal == 'VWAP_Breakout':
            profile = {'signal': signal, 'stage': 8, 'hard': 12, 'min_profit': 0.0}
        elif signal == 'EMA_Cross':
            profile = {'signal': signal, 'stage': 10, 'hard': 12, 'min_profit': 0.0}
        elif signal == 'Granville_Pullback':
            profile = {'signal': signal, 'stage': 7, 'hard': 10, 'min_profit': 2.0}
        elif signal == 'Kline_Pattern' or regime == 'neutral' or 'neutral' in reason:
            profile = {'signal': 'Kline_Pattern', 'stage': 10, 'hard': 12, 'min_profit': 0.0}
        else:
            base = int(pos.get('timeout_bars', 10) or 10)
            profile = {
                'signal': signal or 'default',
                'stage': max(base * 3 // 4, 7),
                'hard': base,
                'min_profit': 5.0,
            }
        if self._is_trend_aligned_position(pos):
            bonus = int(self.cfg.get('trend_timeout_bonus_bars', 4) or 0)
            profile = dict(profile)
            profile['stage'] += bonus
            profile['hard'] += bonus
        return profile

    def _entry_signal_name(self, sig):
        raw = sig.get('display_engine') or sig.get('engine') or sig.get('raw_engine') or ''
        if raw in (
            'VWAP_Breakout', 'EMA_Cross', 'Kline_Pattern', 'Granville_Pullback',
            'RSI_Reversal', 'RSI_Overbought', 'Chan_First_Buy', 'Momentum_Death',
        ):
            return raw
        return display_signal_name(raw)

    def _trend_day_bias(self, context=None):
        if not self.cfg.get('trend_day_filter_enabled', True):
            return {'direction': '', 'reason': ''}
        min_bars = int(self.cfg.get('trend_day_min_bars', 30) or 30)
        if len(self.close_history) < min_bars:
            return {'direction': '', 'reason': ''}

        lookback = int(self.cfg.get('trend_day_lookback_bars', 20) or 20)
        if len(self.close_history) <= lookback:
            return {'direction': '', 'reason': ''}

        current = float(self.close_history[-1])
        prior = float(self.close_history[-lookback])
        if prior <= 0:
            return {'direction': '', 'reason': ''}

        ctx = context or self._entry_quality_context(current)
        move = (current - prior) / prior
        vwap_dist = float(ctx.get('vwap_dist', 0) or 0)
        sma20_slope = float(ctx.get('sma20_slope', 0) or 0)
        min_move = float(self.cfg.get('trend_day_min_move_pct', 0.0018) or 0.0018)
        min_vwap = float(self.cfg.get('trend_day_min_vwap_dist', 0.0010) or 0.0010)
        min_slope = float(self.cfg.get('trend_day_min_sma20_slope', 0.00015) or 0.00015)

        if move >= min_move and vwap_dist >= min_vwap and sma20_slope >= min_slope:
            return {
                'direction': 'call',
                'reason': f'trend_day_long move={move*100:.2f}% vwap={vwap_dist*100:.2f}% slope={sma20_slope*100:.3f}%',
            }
        if move <= -min_move and vwap_dist <= -min_vwap and sma20_slope <= -min_slope:
            return {
                'direction': 'put',
                'reason': f'trend_day_short move={move*100:.2f}% vwap={vwap_dist*100:.2f}% slope={sma20_slope*100:.3f}%',
            }
        return {'direction': '', 'reason': ''}

    def _bar_et_minute(self, bar):
        value = bar.get('time') or bar.get('timestamp')
        try:
            if isinstance(value, datetime):
                dt = value
            elif isinstance(value, (int, float)):
                dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
            elif isinstance(value, str):
                dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
            else:
                return None
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=TZ_ET)
            dt = dt.astimezone(TZ_ET)
            return dt.hour * 60 + dt.minute
        except Exception:
            return None

    def _regular_session_bars(self):
        bars = []
        for bar in self.one_min_candles:
            minute = self._bar_et_minute(bar)
            if minute is not None and 570 <= minute <= 960:
                bars.append(bar)
        return bars

    def _opening_range_context(self, price, context=None):
        if not self.cfg.get('opening_range_filter_enabled', True):
            return {'enabled': False, 'ready': False}

        minutes = int(self.cfg.get('opening_range_minutes', 30) or 30)
        if minutes <= 0:
            return {'enabled': False, 'ready': False}

        regular_bars = self._regular_session_bars()
        if regular_bars:
            opening = [
                bar for bar in regular_bars
                if (self._bar_et_minute(bar) or 0) < 570 + minutes
            ]
            after_opening = [
                bar for bar in regular_bars
                if (self._bar_et_minute(bar) or 0) >= 570 + minutes
            ]
        else:
            opening = self.one_min_candles[:minutes]
            after_opening = self.one_min_candles[minutes:]

        if len(opening) < max(10, minutes // 2) or not after_opening:
            return {
                'enabled': True,
                'ready': False,
                'reason': f'opening_range_warming bars={len(opening)}/{minutes}',
            }

        current = float(price or opening[-1].get('close', 0) or 0)
        high = max(float(bar['high']) for bar in opening)
        low = min(float(bar['low']) for bar in opening)
        width = max(high - low, 1e-9)
        pos = (current - low) / width

        ctx = context or self._entry_quality_context(current)
        vwap_dist = float(ctx.get('vwap_dist', 0) or 0)
        sma20_slope = float(ctx.get('sma20_slope', 0) or 0)
        recent_move = 0.0
        recent_bars = regular_bars or self.one_min_candles
        if len(recent_bars) >= 6:
            prior = float(recent_bars[-6].get('close', 0) or 0)
            if prior > 0:
                recent_move = (current - prior) / prior
        current_minute = self._bar_et_minute(recent_bars[-1]) if recent_bars else None

        buffer_pct = float(self.cfg.get('opening_range_breakout_buffer_pct', 0.0010) or 0.0010)
        min_vwap = float(self.cfg.get('opening_range_breakout_min_vwap_dist', 0.0012) or 0.0012)
        min_slope = float(self.cfg.get('opening_range_breakout_min_sma20_slope', 0.00008) or 0.00008)
        min_recent = float(self.cfg.get('opening_range_breakout_min_recent_move_pct', 0.0012) or 0.0012)

        call_breakout = (
            current >= high * (1 + buffer_pct)
            and vwap_dist >= min_vwap
            and sma20_slope >= min_slope
            and recent_move >= min_recent
        )
        put_breakdown = (
            current <= low * (1 - buffer_pct)
            and vwap_dist <= -min_vwap
            and sma20_slope <= -min_slope
            and recent_move <= -min_recent
        )

        return {
            'enabled': True,
            'ready': True,
            'minutes': minutes,
            'high': high,
            'low': low,
            'width_pct': width / current if current else 0.0,
            'position': pos,
            'current_minute': current_minute,
            'above_high_pct': (current - high) / high if high else 0.0,
            'below_low_pct': (low - current) / low if low else 0.0,
            'call_breakout': call_breakout,
            'put_breakdown': put_breakdown,
            'vwap_dist': vwap_dist,
            'sma20_slope': sma20_slope,
            'recent_move': recent_move,
        }

    def _should_skip_opening_range_call(self, sig, signal, context):
        if sig.get('dir') != 'call':
            return False

        or_ctx = self._opening_range_context(float(sig.get('price') or 0), context)
        sig.setdefault('metadata', {})['opening_range'] = or_ctx
        sig['opening_range'] = or_ctx
        if not or_ctx.get('enabled') or not or_ctx.get('ready'):
            return False

        block_pos = float(self.cfg.get('opening_range_call_block_pos', 0.90) or 0.90)
        inside_fade_start = int(self.cfg.get('opening_range_inside_fade_start_min', 690) or 690)
        price = float(sig.get('price') or 0)
        high = float(or_ctx.get('high') or 0)
        above_opening_high = high > 0 and price > high
        weak_above_breakout = (
            above_opening_high
            and (
                or_ctx['sma20_slope'] < float(self.cfg.get('opening_range_breakout_min_sma20_slope', 0.00008) or 0.00008)
                or or_ctx['recent_move'] < float(self.cfg.get('opening_range_breakout_min_recent_move_pct', 0.0012) or 0.0012)
            )
        )
        inside_late_fade = (
            (or_ctx.get('current_minute') or 0) >= inside_fade_start
            and or_ctx.get('position', 0.0) >= block_pos
            and or_ctx['sma20_slope'] <= 0
            and or_ctx['recent_move'] <= 0
        )

        if weak_above_breakout or inside_late_fade:
            self._last_entry_rejection = (
                f"OpeningRange CALL chase block: pos={or_ctx['position']*100:.0f}% "
                f"high={high:.2f} vwap={or_ctx['vwap_dist']*100:.2f}% "
                f"slope={or_ctx['sma20_slope']*100:.3f}% recent={or_ctx['recent_move']*100:.2f}%"
            )
            self._hard_skip_shadow_live = True
            print(f"  OR CALL chase block: {signal} | {self._last_entry_rejection}")
            return True

        if above_opening_high:
            sig['reason'] = (
                f"[OR breakout accepted high={or_ctx['high']:.2f} pos={or_ctx['position']*100:.0f}%] "
                f"{sig.get('reason', '')}"
            )

        return False

    def _classify_day_market_regime(self, context=None):
        if not self.cfg.get('market_regime_enabled', True):
            return {'type': 'disabled', 'direction': '', 'label': '未启用', 'reason': ''}
        bars = self.one_min_candles
        if len(bars) < 15:
            return {
                'type': 'warming_up',
                'direction': '',
                'label': '预热',
                'reason': f'K线不足: {len(bars)}/15',
            }

        current = float(bars[-1]['close'])
        first_open = float(bars[0]['open'])
        session_high = max(float(bar['high']) for bar in bars)
        session_low = min(float(bar['low']) for bar in bars)
        session_range = max(session_high - session_low, 1e-9)
        session_pos = (current - session_low) / session_range
        day_move = (current - first_open) / first_open if first_open else 0.0

        lookback = min(20, len(bars) - 1)
        recent_prior = float(bars[-lookback]['close']) if lookback > 0 else current
        recent_move = (current - recent_prior) / recent_prior if recent_prior else 0.0
        recent = bars[-min(20, len(bars)):]
        up_bars = sum(1 for bar in recent if float(bar['close']) >= float(bar['open']))
        down_bars = len(recent) - up_bars

        ctx = context or self._entry_quality_context(current)
        vwap_dist = float(ctx.get('vwap_dist', 0) or 0)
        sma20_slope = float(ctx.get('sma20_slope', 0) or 0)
        pa_state = self.price_action_state if isinstance(self.price_action_state, dict) else {}
        pa_direction = pa_state.get('direction', '')
        pa_state_name = pa_state.get('state', '')

        opening = bars[:min(15, len(bars))]
        opening_high = max(float(bar['high']) for bar in opening)
        opening_low = min(float(bar['low']) for bar in opening)
        broke_open_high = current > opening_high
        broke_open_low = current < opening_low

        trend_up = (
            len(bars) >= int(self.cfg.get('trend_day_min_bars', 30) or 30)
            and vwap_dist >= 0.0006
            and sma20_slope >= 0.00005
            and session_pos >= 0.60
            and (day_move >= 0.0012 or recent_move >= 0.0010 or up_bars >= 12)
        )
        trend_down = (
            len(bars) >= int(self.cfg.get('trend_day_min_bars', 30) or 30)
            and vwap_dist <= -0.0006
            and sma20_slope <= -0.00005
            and session_pos <= 0.40
            and (day_move <= -0.0012 or recent_move <= -0.0010 or down_bars >= 12)
        )

        if trend_up or pa_direction == 'call' and session_pos >= 0.65 and vwap_dist > 0:
            return {
                'type': 'trend_up',
                'direction': 'call',
                'label': '趋势上涨',
                'reason': (
                    f'day={day_move*100:.2f}% recent={recent_move*100:.2f}% '
                    f'vwap={vwap_dist*100:.2f}% pos={session_pos*100:.0f}% '
                    f'slope={sma20_slope*100:.3f}%'
                ),
                'preferred_signals': ['Granville_Pullback', 'Kline_Pattern', 'VWAP_Breakout'],
            }
        if trend_down or pa_direction == 'put' and session_pos <= 0.35 and vwap_dist < 0:
            return {
                'type': 'trend_down',
                'direction': 'put',
                'label': '趋势下跌',
                'reason': (
                    f'day={day_move*100:.2f}% recent={recent_move*100:.2f}% '
                    f'vwap={vwap_dist*100:.2f}% pos={session_pos*100:.0f}% '
                    f'slope={sma20_slope*100:.3f}%'
                ),
                'preferred_signals': ['VWAP_Breakout', 'Kline_Pattern', 'Granville_Pullback'],
            }

        if len(bars) <= 75 and ((broke_open_high and day_move < 0) or (broke_open_low and day_move > 0)):
            direction = 'put' if broke_open_high else 'call'
            return {
                'type': 'opening_reversal',
                'direction': direction,
                'label': '开盘反转',
                'reason': f'open_range=({opening_low:.2f}-{opening_high:.2f}) day={day_move*100:.2f}%',
                'preferred_signals': ['Kline_Pattern', 'RSI_Reversal', 'RSI_Overbought'],
            }

        range_pct = session_range / current if current else 0.0
        if (
            len(bars) >= 30
            and (pa_state_name == 'trading_range' or abs(vwap_dist) <= 0.0015)
            and 0.25 <= session_pos <= 0.75
            and range_pct <= 0.0065
        ):
            return {
                'type': 'range',
                'direction': '',
                'label': '震荡市',
                'reason': f'range={range_pct*100:.2f}% vwap={vwap_dist*100:.2f}% pos={session_pos*100:.0f}%',
                'preferred_signals': ['Failed_Breakout', 'VWAP_Reversion', 'Kline_Pattern'],
            }

        return {
            'type': 'unclear',
            'direction': '',
            'label': '未明确',
            'reason': f'day={day_move*100:.2f}% vwap={vwap_dist*100:.2f}% pos={session_pos*100:.0f}%',
        }

    def _apply_market_regime_to_signal(self, sig, signal, direction, context):
        regime = self.day_market_regime or self._classify_day_market_regime(context)
        sig.setdefault('metadata', {})['day_market_regime'] = regime
        sig['day_market_regime'] = regime.get('type', '')
        sig['day_market_label'] = regime.get('label', '')
        sig['day_market_direction'] = regime.get('direction', '')

        regime_direction = regime.get('direction', '')
        if (
            (self.cfg.get('market_regime_soft_countertrend', True) or self.cfg.get('market_regime_hard_countertrend', False))
            and regime_direction
            and direction
            and direction != regime_direction
        ):
            if self.cfg.get('market_regime_hard_countertrend', False):
                self._last_entry_rejection = f'当日行情逆势禁入: {regime.get("label", "")}'
                self._hard_skip_shadow_live = True
                return True
            sig['pos_mult'] = min(
                float(sig.get('pos_mult', 1.0) or 1.0),
                float(self.cfg.get('market_regime_countertrend_pos_mult', 0.15) or 0.15),
            )
            sig['sl_pct'] = min(
                float(sig.get('sl_pct', self.cfg.get('sl', 0.25)) or self.cfg.get('sl', 0.25)),
                float(self.cfg.get('market_regime_countertrend_sl_pct', 0.20) or 0.20),
            )
            sig['reason'] = f"[{regime.get('label', '')}逆势降级] {sig.get('reason', '')}"

        if regime.get('type') == 'range' and signal == 'VWAP_Breakout':
            sig['pos_mult'] = min(
                float(sig.get('pos_mult', 1.0) or 1.0),
                float(self.cfg.get('market_regime_range_breakout_pos_mult', 0.20) or 0.20),
            )
            sig['sl_pct'] = min(
                float(sig.get('sl_pct', self.cfg.get('sl', 0.25)) or self.cfg.get('sl', 0.25)),
                float(self.cfg.get('market_regime_range_breakout_sl_pct', 0.22) or 0.22),
            )
            sig['reason'] = f"[震荡突破降级] {sig.get('reason', '')}"
        return False

    def _should_skip_entry_signal(self, sig):
        self._last_entry_rejection = ''
        self._hard_skip_shadow_live = False
        signal = self._entry_signal_name(sig)
        price = float(sig.get('price') or 0)
        direction = sig.get('dir', '')
        reason = str(sig.get('reason', ''))
        context = self._entry_quality_context(price)
        disabled_signals = set(self.cfg.get('disabled_entry_signals') or [])
        if signal in disabled_signals:
            self._last_entry_rejection = f'胜率优先禁用信号: {signal}'
            self._hard_skip_shadow_live = True
            print(f"  ⛔ 胜率优先禁用信号: {signal}")
            return True
        if self._apply_market_regime_to_signal(sig, signal, direction, context):
            return True
        if self._should_skip_opening_range_call(sig, signal, context):
            return True

        trend_bias = self._trend_day_bias(context)
        trend_direction = trend_bias.get('direction', '')
        if (
            self.cfg.get('trend_day_countertrend_hard_block', True)
            and trend_direction
            and direction
            and direction != trend_direction
        ):
            self._last_entry_rejection = f'趋势日逆势禁入: {trend_bias.get("reason", "")}'
            self._hard_skip_shadow_live = True
            print(f"  ⛔ 趋势日逆势禁入: {direction} vs {trend_direction} | {signal}")
            return True

        countertrend_signals = {'RSI_Reversal', 'Chan_First_Buy', 'RSI_Overbought', 'Momentum_Death'}
        allow_momentum_death = signal == 'Momentum_Death' and self.cfg.get('enable_momentum_death_entries', True)
        if signal in countertrend_signals and not allow_momentum_death and not self.cfg.get('enable_countertrend_reversal_entries', False):
            self._last_entry_rejection = f'逆势反转信号暂停: {signal}'
            print(f"  ⛔ 逆势反转信号已暂停: {signal}（等待更可靠的主要趋势反转结构）")
            return True

        if self.cfg.get('brooks_priority_mode', True):
            market_state = self.price_action.market_state(self.one_min_candles)
            state_direction = market_state.get('direction', '')
            if state_direction and direction and state_direction != direction:
                self._last_entry_rejection = f'Brooks方向冲突: {market_state.get("state")}'
                print(
                    f"  ⛔ Brooks方向冲突: {market_state.get('state')} "
                    f"优先于 {signal} {direction}"
                )
                return True
            if market_state.get('state') == 'trading_range':
                direction_setups = {
                    setup.get('name')
                    for setup in market_state.get('setups', [])
                    if setup.get('direction') == direction
                }
                price_action = market_state.get(direction, {})
                range_position = float(price_action.get('range_position', 0.5))
                edge_entry = (
                    direction == 'call'
                    and signal == 'VWAP_Breakout'
                    and range_position <= float(self.cfg.get('brooks_range_call_max_position', 0.40))
                ) or (
                    direction == 'put'
                    and range_position >= float(self.cfg.get('brooks_range_put_min_position', 0.60))
                    and 'Failed_Breakout' in direction_setups
                )
                if not edge_entry:
                    self._last_entry_rejection = 'Brooks震荡区间中部或缺少边缘失败突破'
                    print(f"  ⛔ Brooks震荡区间过滤: 区间中部或缺少边缘失败突破，跳过 {signal}")
                    return True
            sig.setdefault('metadata', {})['brooks_market_state'] = market_state

        if direction == 'put':
            context['vwap_dir_dist'] = -context['vwap_dist']
            if not self.cfg.get('enable_put_entries', False):
                self._last_entry_rejection = 'PUT信号暂停'
                print("  ⛔ PUT信号已暂停 (enable_put_entries=False)")
                return True
            if self.cfg.get('put_quality_filter', True) and self._should_skip_put_signal(sig, signal, context):
                return True
            skip_put_trend_requirement = (
                signal == 'Momentum_Death'
                and self.cfg.get('momentum_death_relaxed_put_quality', True)
            )
            if (
                not skip_put_trend_requirement
                and self.cfg.get('price_action_filter', True)
                and self.cfg.get('price_action_require_put_trend', True)
            ):
                price_action = self.price_action.evaluate(self.one_min_candles, direction)
                if not price_action.get('allow'):
                    self._last_entry_rejection = f'PUT价格行为过滤: {price_action.get("reason", "")}'
                    print(f"  ⛔ PUT价格行为过滤: {price_action.get('reason', '未确认空头趋势')}")
                    return True
                sig.setdefault('metadata', {})['price_action'] = price_action
        elif direction == 'call' and self.cfg.get('price_action_filter', True):
            if self.cfg.get('price_action_require_call_quality', True):
                price_action = self.price_action.evaluate(self.one_min_candles, direction)
                if not price_action.get('ready'):
                    self._last_entry_rejection = 'CALL价格行为K线不足'
                    print(f"  ⛔ CALL价格行为过滤: {price_action.get('reason', 'K线不足')}")
                    return True
                if signal == 'VWAP_Breakout':
                    max_range_position = float(
                        self.cfg.get('price_action_vwap_call_max_range_position', 0.40)
                    )
                    if price_action['range_position'] > max_range_position:
                        self._last_entry_rejection = (
                            f'VWAP CALL区间位置过高: {price_action["range_position"]*100:.0f}%'
                        )
                        print(
                            f"  ⛔ VWAP CALL追高过滤: 最近20根区间位置"
                            f"{price_action['range_position']*100:.0f}% > {max_range_position*100:.0f}%"
                        )
                        return True
                if signal == 'EMA_Cross' and self.cfg.get('price_action_require_ema_call_strong_bar', True):
                    if not price_action.get('strong_breakout'):
                        self._last_entry_rejection = 'EMA CALL缺少强多头信号K线'
                        print(f"  ⛔ EMA CALL价格行为过滤: {price_action.get('reason', '缺少强多头信号K线')}")
                        return True
                sig.setdefault('metadata', {})['price_action'] = price_action

        if signal == 'VWAP_Breakout':
            dist = context['vwap_dir_dist'] if direction else context['vwap_dist']
            max_dist = self.cfg.get('vwap_max_chase_pct', 0.0030)
            if dist > max_dist:
                self._last_entry_rejection = f'VWAP距离过远: {dist*100:.2f}%'
                print(f"  ⛔ VWAP追高过滤: 距VWAP {dist*100:.2f}% > {max_dist*100:.2f}%，跳过")
                return True

        if signal == 'EMA_Cross':
            meta = sig.get('metadata') or {}
            adx = float(meta.get('adx') or getattr(self.engine, 'adx', 0) or 0)
            min_adx = self.cfg.get('ema_min_adx_live', 35)
            if adx < min_adx:
                self._last_entry_rejection = f'EMA ADX不足: {adx:.1f}<{min_adx:.0f}'
                print(f"  ⛔ EMA质量过滤: ADX={adx:.1f} < {min_adx:.0f}，跳过")
                return True

        is_kline = signal == 'Kline_Pattern' or sig.get('regime') == 'neutral' or 'neutral' in reason
        if is_kline and not self.cfg.get('enable_kline_entries', False):
            print("  ⛔ Kline/neutral信号已暂停: 近期实盘和回测质量不足")
            return True
        if is_kline and self.cfg.get('kline_quality_filter', True):
            allowed_patterns = self.cfg.get('kline_call_live_patterns') or []
            if direction == 'call' and allowed_patterns and not any(pat in reason for pat in allowed_patterns):
                self._last_entry_rejection = 'Kline普通形态仅记录'
                self._hard_skip_shadow_live = True
                print(f"  ⛔ Kline普通CALL形态仅记录不下单: {reason}")
                return True
            max_pos = self.cfg.get('kline_max_price_pos', 0.82)
            min_macd = self.cfg.get('kline_min_macd_hist', 0.0)
            min_slope = self.cfg.get('kline_min_sma20_slope', 0.0)
            if context['price_pos'] > max_pos:
                self._last_entry_rejection = f'Kline高位追入过滤: {context["price_pos"]*100:.0f}%'
                self._hard_skip_shadow_live = direction == 'call'
                print(f"  ⛔ Kline高位追入过滤: 日内位置{context['price_pos']*100:.0f}% > {max_pos*100:.0f}%")
                return True
            if context['macd_hist'] <= min_macd:
                self._last_entry_rejection = f'Kline动量过滤: MACD_hist={context["macd_hist"]:.4f}'
                self._hard_skip_shadow_live = direction == 'call'
                print(f"  ⛔ Kline动量过滤: MACD_hist={context['macd_hist']:.4f} <= {min_macd:.4f}")
                return True
            if context['sma20_slope'] <= min_slope:
                self._last_entry_rejection = f'Kline趋势过滤: SMA20斜率={context["sma20_slope"]*100:.4f}%'
                self._hard_skip_shadow_live = direction == 'call'
                print(f"  ⛔ Kline趋势过滤: SMA20斜率={context['sma20_slope']*100:.4f}% <= {min_slope*100:.4f}%")
                return True

        if signal == 'Granville_Pullback' and not self.cfg.get('enable_granville_entries', False):
            print("  ⛔ Granville信号已暂停: 近期实盘和回测质量不足")
            return True
        if signal == 'Granville_Pullback' and self.cfg.get('granville_quality_filter', True):
            day_direction = ''
            if isinstance(self.day_market_regime, dict):
                day_direction = self.day_market_regime.get('direction', '') or ''
            if (
                direction == 'call'
                and self.cfg.get('granville_require_day_direction', True)
                and day_direction
                and day_direction != 'call'
            ):
                self._last_entry_rejection = f'Granville逆当日方向仅记录: {day_direction}'
                self._hard_skip_shadow_live = True
                print(f"  ⛔ Granville CALL逆当日方向仅记录: day={day_direction}")
                return True
            max_pos = self.cfg.get('granville_max_price_pos', 0.85)
            min_macd = self.cfg.get('granville_min_macd_hist', 0.0)
            min_slope = self.cfg.get('granville_min_sma20_slope', 0.0)
            min_vwap_dist = self.cfg.get('granville_min_vwap_dist', 0.0)
            meta = sig.get('metadata') or {}
            dist_pct = float(meta.get('dist_pct') or 0)
            min_dist_pct = self.cfg.get('granville_min_dist_pct', 0.03)
            if context['price_pos'] > max_pos:
                self._last_entry_rejection = f'Granville高位过滤: {context["price_pos"]*100:.0f}%'
                self._hard_skip_shadow_live = direction == 'call'
                print(f"  ⛔ Granville高位过滤: 日内位置{context['price_pos']*100:.0f}% > {max_pos*100:.0f}%")
                return True
            if context['macd_hist'] <= min_macd:
                self._last_entry_rejection = f'Granville动量过滤: MACD_hist={context["macd_hist"]:.4f}'
                self._hard_skip_shadow_live = direction == 'call'
                print(f"  ⛔ Granville动量过滤: MACD_hist={context['macd_hist']:.4f} <= {min_macd:.4f}")
                return True
            if context['sma20_slope'] <= min_slope:
                self._last_entry_rejection = f'Granville趋势过滤: SMA20斜率={context["sma20_slope"]*100:.4f}%'
                self._hard_skip_shadow_live = direction == 'call'
                print(f"  ⛔ Granville趋势过滤: SMA20斜率={context['sma20_slope']*100:.4f}% <= {min_slope*100:.4f}%")
                return True
            if context['vwap_dir_dist'] <= min_vwap_dist:
                self._last_entry_rejection = f'Granville VWAP过滤: {context["vwap_dir_dist"]*100:.3f}%'
                self._hard_skip_shadow_live = direction == 'call'
                print(f"  ⛔ Granville VWAP过滤: 距VWAP={context['vwap_dir_dist']*100:.3f}% <= {min_vwap_dist*100:.3f}%")
                return True
            if dist_pct < min_dist_pct:
                self._last_entry_rejection = f'Granville回踩确认不足: {dist_pct:.2f}%'
                self._hard_skip_shadow_live = direction == 'call'
                print(f"  ⛔ Granville回踩确认不足: dist={dist_pct:.2f}% < {min_dist_pct:.2f}%")
                return True

        return False

    def _should_skip_put_signal(self, sig, signal, context):
        allowed = self.cfg.get('put_allowed_signals') or ['VWAP_Breakout', 'EMA_Cross', 'RSI_Reversal']
        if signal not in allowed:
            print(f"  ⛔ PUT信号过滤: {signal} 不在允许列表 {allowed}")
            return True

        if signal == 'Momentum_Death' and self.cfg.get('momentum_death_relaxed_put_quality', True):
            meta = sig.get('metadata') or {}
            macd_prev = float(meta.get('macd_hist_prev') or 0)
            macd_now = float(meta.get('macd_hist') or 0)
            rsi_prev = float(meta.get('rsi_prev') or 50)
            rsi_now = float(meta.get('rsi') or 50)
            if not (macd_prev > macd_now and rsi_prev > rsi_now and context['price_pos'] >= 0.35):
                print(
                    f"  ⛔ Momentum_Death质量过滤: MACD {macd_prev:.3f}->{macd_now:.3f}, "
                    f"RSI {rsi_prev:.1f}->{rsi_now:.1f}, pos={context['price_pos']*100:.0f}%"
                )
                return True
            return False

        strength = float(sig.get('strength') or 0)
        min_strength = float(self.cfg.get('put_min_strength', 80) or 0)
        if strength and strength < min_strength:
            print(f"  ⛔ PUT强度过滤: {strength:.0f} < {min_strength:.0f}")
            return True

        min_price_pos = float(self.cfg.get('put_min_price_pos', 0.20) or 0)
        if context['price_pos'] < min_price_pos:
            print(f"  ⛔ PUT低位追空过滤: 日内位置{context['price_pos']*100:.0f}% < {min_price_pos*100:.0f}%")
            return True

        min_vwap_dist = float(self.cfg.get('put_min_vwap_dist', 0.0010) or 0)
        if context['vwap_dir_dist'] < min_vwap_dist:
            print(f"  ⛔ PUT VWAP过滤: 价格低于VWAP {context['vwap_dir_dist']*100:.2f}% < {min_vwap_dist*100:.2f}%")
            return True

        now_et = datetime.now(TZ_ET)
        cur_min_et = now_et.hour * 60 + now_et.minute
        afternoon_start = int(self.cfg.get('afternoon_put_start_min', 810) or 810)
        if cur_min_et >= afternoon_start:
            afternoon_vwap = float(self.cfg.get('afternoon_put_min_vwap_dist', 0.006) or 0.006)
            afternoon_slope = float(self.cfg.get('afternoon_put_min_sma20_slope_abs', 0.00005) or 0.00005)
            if context['vwap_dir_dist'] < afternoon_vwap:
                self._last_entry_rejection = f'下午PUT VWAP跌破不足: {context["vwap_dir_dist"]*100:.2f}%'
                self._hard_skip_shadow_live = True
                print(
                    f"  ⛔ 下午PUT假跌破过滤: 距VWAP {context['vwap_dir_dist']*100:.2f}% "
                    f"< {afternoon_vwap*100:.2f}%"
                )
                return True
            if context['sma20_slope'] > -afternoon_slope:
                self._last_entry_rejection = f'下午PUT趋势斜率不足: {context["sma20_slope"]*100:.4f}%'
                self._hard_skip_shadow_live = True
                print(
                    f"  ⛔ 下午PUT趋势过滤: SMA20斜率={context['sma20_slope']*100:.4f}% "
                    f"> -{afternoon_slope*100:.4f}%"
                )
                return True

        min_macd_abs = float(self.cfg.get('put_min_macd_hist_abs', 0.0) or 0)
        if context['macd_hist'] >= -min_macd_abs:
            print(f"  ⛔ PUT动量过滤: MACD_hist={context['macd_hist']:.4f} 未转空")
            return True

        min_slope_abs = float(self.cfg.get('put_min_sma20_slope_abs', 0.0) or 0)
        if context['sma20_slope'] >= -min_slope_abs:
            print(f"  ⛔ PUT趋势过滤: SMA20斜率={context['sma20_slope']*100:.4f}% 未向下")
            return True

        return False

    def _entry_quality_context(self, price):
        session_high = max(self.session_high or 0, price)
        session_low = min(self.session_low if self.session_low < 999999 else price, price)
        price_pos = (price - session_low) / (session_high - session_low) if session_high > session_low else 0.5
        vwap = getattr(self.engine, 'vwap', 0) or price
        vwap_dist = (price - vwap) / vwap if vwap else 0.0
        sma20_slope = 0.0
        if len(self.close_history) >= 25:
            sma20 = float(np.mean(self.close_history[-20:]))
            sma20_prev = float(np.mean(self.close_history[-25:-5]))
            sma20_slope = (sma20 - sma20_prev) / price if price else 0.0
        return {
            'price_pos': price_pos,
            'vwap_dist': vwap_dist,
            'vwap_dir_dist': vwap_dist,
            'macd_hist': float(getattr(self.engine, 'macd_hist', 0) or 0),
            'sma20_slope': sma20_slope,
        }

    def _allow_broker_exit_notifications_now(self):
        """Broker reconcile is only a fallback; avoid off-hours historical notification spam."""
        now = datetime.now(TZ_ET)
        cur_min = now.hour * 60 + now.minute
        return 570 <= cur_min <= 970  # 09:30-16:10 ET

    def _start_signal_probe(
        self, sig, opt_symbol, contracts, entry_price, entry_bar,
        source='live', rejection_reason='',
    ):
        """记录入场后5/10/20根K线的正股方向收益，用于分析信号质量。"""
        try:
            signal_name = self._entry_signal_name(sig)
            direction = sig.get('dir', '')
            if source == 'shadow':
                cooldown = int(self.cfg.get('shadow_signal_cooldown_bars', 5) or 5)
                recent_shadow = [
                    probe for probe in self.signal_probes
                    if probe.get('source') == 'shadow'
                    and probe.get('signal') == signal_name
                    and probe.get('dir') == direction
                    and int(entry_bar) - int(probe.get('entry_bar', -9999)) < cooldown
                ]
                if recent_shadow:
                    return False
                max_shadow = int(self.cfg.get('shadow_signal_max_per_day', 100) or 100)
                if sum(probe.get('source') == 'shadow' for probe in self.signal_probes) >= max_shadow:
                    return False

            self._signal_probe_seq += 1
            entry_time = datetime.now(TZ_ET).strftime('%Y-%m-%d %H:%M:%S')
            probe = {
                'id': self._signal_probe_seq,
                'entry_time': entry_time,
                'entry_bar': int(entry_bar),
                'signal': signal_name,
                'dir': sig.get('dir', ''),
                'entry_price': float(entry_price),
                'opt_symbol': opt_symbol,
                'contracts': int(contracts),
                'reason': sig.get('reason', ''),
                'regime': sig.get('regime', ''),
                'day_market_regime': sig.get('day_market_regime', ''),
                'day_market_label': sig.get('day_market_label', ''),
                'day_market_direction': sig.get('day_market_direction', ''),
                'opening_range': sig.get('opening_range', {}),
                'source': source,
                'rejection_reason': rejection_reason,
                'milestones': {5: None, 10: None, 20: None},
                'completed': False,
            }
            self.signal_probes.append(probe)
            if len(self.signal_probes) > 500:
                self.signal_probes = self.signal_probes[-500:]
            self._save_signal_probes_snapshot()
            return True
        except Exception as e:
            print(f"  ⚠️ 信号追踪初始化失败: {e}")
            return False

    def _should_skip_and_track(self, sig):
        if sig.get('_shadow_live_approved'):
            return False

        skipped = self._should_skip_entry_signal(sig)
        if skipped:
            rejection = self._last_entry_rejection or '质量过滤未通过'
            live_orders = self.cfg.get('shadow_signal_live_orders', False)
            if live_orders and not self._hard_skip_shadow_live:
                signal_name = self._entry_signal_name(sig)
                now_et = datetime.now(TZ_ET)
                cur_min_et = now_et.hour * 60 + now_et.minute
                afternoon_allowed = set(self.cfg.get('shadow_live_afternoon_allowed_signals') or [])
                if cur_min_et >= 780 and afternoon_allowed and signal_name not in afternoon_allowed:
                    print(f"  ⏭️ 下午影子真实单仅允许 {sorted(afternoon_allowed)}，{signal_name} 只记录")
                    live_orders = False
                if not live_orders:
                    pass
                else:
                    cooldown = int(self.cfg.get('shadow_signal_cooldown_bars', 5) or 5)
                    duplicate = any(
                        probe.get('source') in ('shadow', 'shadow_live')
                        and probe.get('signal') == signal_name
                        and probe.get('dir') == sig.get('dir', '')
                        and len(self.one_min_candles) - int(probe.get('entry_bar', -9999)) < cooldown
                        for probe in self.signal_probes
                    )
                    if not duplicate:
                        shadow_mult = float(self.cfg.get('shadow_live_order_pos_mult', 0.80) or 0.80)
                        if cur_min_et < 600:
                            shadow_mult *= float(self.cfg.get('shadow_live_open_pos_mult', 0.50) or 0.50)
                            rejection = f"{rejection}; 开盘影子仓位减半"
                        sig['_shadow_live_approved'] = True
                        sig['shadow_live_order'] = True
                        sig['shadow_rejection_reason'] = rejection
                        sig['sl_pct'] = min(
                            float(sig.get('sl_pct', self.cfg.get('sl', 0.25)) or self.cfg.get('sl', 0.25)),
                            float(self.cfg.get('shadow_live_sl_pct', 0.22) or 0.22),
                        )
                        sig['pos_mult'] = min(
                            float(sig.get('pos_mult', 1.0) or 1.0),
                            shadow_mult,
                        )
                        sig['reason'] = f"[影子测试单|原拒绝:{rejection}] {sig.get('reason', '')}"
                        print(f"  🧪 影子信号真实下单已开启: {signal_name} {sig.get('dir')} | {rejection}")
                        return False
            if self.cfg.get('shadow_signal_tracking', True):
                self._start_signal_probe(
                    sig=sig,
                    opt_symbol='',
                    contracts=0,
                    entry_price=float(sig.get('price') or self.current_price or 0),
                    entry_bar=len(self.one_min_candles),
                    source='shadow',
                    rejection_reason=rejection,
                )
        return skipped

    def _update_signal_probes(self, candle):
        """每根已完成K线更新未完成的信号追踪记录。"""
        if not self.signal_probes:
            return
        changed = False
        current_bar = len(self.one_min_candles)
        current_price = float(candle.get('close', 0) or 0)
        if current_price <= 0:
            return

        for probe in self.signal_probes:
            if probe.get('completed'):
                continue
            entry_price = float(probe.get('entry_price', 0) or 0)
            if entry_price <= 0:
                continue
            bars_elapsed = current_bar - int(probe.get('entry_bar', current_bar))
            milestones = probe.setdefault('milestones', {5: None, 10: None, 20: None})
            for target in (5, 10, 20):
                if bars_elapsed < target or milestones.get(target) is not None:
                    continue
                pct = (current_price - entry_price) / entry_price * 100
                if probe.get('dir') == 'put':
                    pct = -pct
                ts = candle.get('time')
                if isinstance(ts, datetime):
                    ts = ts.strftime('%Y-%m-%d %H:%M:%S')
                milestones[target] = {
                    'bars': target,
                    'time': ts,
                    'price': current_price,
                    'pct': round(pct, 4),
                }
                probe[f'm{target}_pct'] = round(pct, 4)
                probe[f'm{target}_price'] = current_price
                changed = True
            probe['completed'] = all(milestones.get(t) is not None for t in (5, 10, 20))

        if changed:
            self._save_signal_probes_snapshot()
            try:
                import dashboard_v7
                dashboard_v7.update_signal_probes(self._serialize_signal_probes())
            except Exception:
                pass

    def _save_signal_probes_snapshot(self):
        """把信号追踪单独落盘，避免依赖平仓记录。"""
        try:
            today_et = datetime.now(TZ_ET).strftime('%Y-%m-%d')
            records_dir = os.path.join(_app_dir(), 'records')
            os.makedirs(records_dir, exist_ok=True)
            filepath = os.path.join(records_dir, f'signal_probes_{today_et}.json')
            rows = self._serialize_signal_probes()
            tmp_path = filepath + '.tmp'
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump({
                    'date': today_et,
                    'updated': datetime.now(TZ_ET).strftime('%Y-%m-%d %H:%M:%S'),
                    'probes': rows,
                }, f, ensure_ascii=False, indent=2, default=_json_default)
            os.replace(tmp_path, filepath)
            try:
                import dashboard_v7
                dashboard_v7.update_signal_probes(rows)
            except Exception:
                pass
        except Exception as e:
            print(f"  ⚠️ 信号追踪保存失败: {e}")

    def _serialize_signal_probes(self):
        rows = []
        for p in self.signal_probes:
            rows.append({
                'id': p.get('id'),
                'entry_time': p.get('entry_time', ''),
                'entry_bar': p.get('entry_bar', 0),
                'signal': p.get('signal', ''),
                'dir': p.get('dir', ''),
                'entry_price': p.get('entry_price', 0),
                'opt_symbol': p.get('opt_symbol', ''),
                'contracts': p.get('contracts', 0),
                'reason': p.get('reason', ''),
                'regime': p.get('regime', ''),
                'source': p.get('source', 'live'),
                'rejection_reason': p.get('rejection_reason', ''),
                'm5_pct': p.get('m5_pct'),
                'm10_pct': p.get('m10_pct'),
                'm20_pct': p.get('m20_pct'),
                'm5_price': p.get('m5_price'),
                'm10_price': p.get('m10_price'),
                'm20_price': p.get('m20_price'),
                'completed': p.get('completed', False),
                'milestones': p.get('milestones', {}),
            })
        return rows

    def _trade_notify_key(self, trade, source='exit'):
        """生成交易通知指纹，避免同一笔平仓重复通知，也避免同合约多次交易被误去重。"""
        try:
            sym = str(trade.get('opt_symbol', ''))
            direction = str(trade.get('dir', ''))
            contracts = int(float(trade.get('closed_contracts') or trade.get('contracts') or 0))
            entry = round(float(trade.get('entry_opt_price') or trade.get('entry_price') or 0), 4)
            exit_price = round(float(trade.get('exit_opt_price') or trade.get('exit_price') or 0), 4)
            pnl = round(float(trade.get('pnl_usd') or 0), 2)
            reason = str(trade.get('exit_reason') or trade.get('reason') or '')[:80]
            return f"{source}|{sym}|{direction}|{contracts}|{entry}|{exit_price}|{pnl}|{reason}"
        except Exception:
            return f"{source}|{time.time()}"

    def _notification_log_path(self, date_str=None):
        today_et = date_str or datetime.now(TZ_ET).strftime('%Y-%m-%d')
        log_dir = os.path.join(_app_dir(), 'logs')
        os.makedirs(log_dir, exist_ok=True)
        return os.path.join(log_dir, f'notifications_{today_et}.json')

    def _load_notification_keys(self, include_recent_days=False):
        try:
            paths = [self._notification_log_path()]
            if include_recent_days:
                log_dir = os.path.join(_app_dir(), 'logs')
                if os.path.isdir(log_dir):
                    files = sorted(
                        [os.path.join(log_dir, name) for name in os.listdir(log_dir) if name.startswith('notifications_') and name.endswith('.json')],
                        reverse=True,
                    )
                    paths = list(dict.fromkeys(paths + files[:5]))
            keys = set()
            for path in paths:
                if not os.path.exists(path):
                    continue
                with open(path, encoding='utf-8') as f:
                    data = json.load(f)
                keys.update(str(item.get('key')) for item in data.get('items', []) if item.get('key'))
            return keys
        except Exception:
            return set()

    def _load_notification_items(self):
        try:
            path = self._notification_log_path()
            if not os.path.exists(path):
                return []
            with open(path, encoding='utf-8') as f:
                data = json.load(f)
            return data.get('items', []) if isinstance(data, dict) else []
        except Exception:
            return []

    def _recent_live_exit_notification_exists(self, opt_symbol, seconds=300):
        """Return True when live exit/partial already notified recently for the same contract."""
        if not opt_symbol:
            return False
        now = datetime.now(TZ_ET)
        for item in reversed(self._load_notification_items()):
            if item.get('type') not in ('exit', 'partial'):
                continue
            key = str(item.get('key', ''))
            title = str(item.get('title', ''))
            if title != opt_symbol and f'|{opt_symbol}|' not in key:
                continue
            try:
                sent_at = datetime.strptime(str(item.get('time', '')), '%Y-%m-%d %H:%M:%S')
                sent_at = sent_at.replace(tzinfo=TZ_ET)
            except Exception:
                return True
            if 0 <= (now - sent_at).total_seconds() <= seconds:
                return True
        return False

    def _mark_notification_sent(self, key, msg_type, title=''):
        try:
            path = self._notification_log_path()
            data = {'items': []}
            if os.path.exists(path):
                with open(path, encoding='utf-8') as f:
                    data = json.load(f)
            items = data.setdefault('items', [])
            if any(str(item.get('key')) == str(key) for item in items):
                return
            items.append({
                'key': key,
                'type': msg_type,
                'title': title,
                'time': datetime.now(TZ_ET).strftime('%Y-%m-%d %H:%M:%S'),
            })
            tmp_path = path + '.tmp'
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2, default=_json_default)
            os.replace(tmp_path, path)
        except Exception as e:
            print(f"  ⚠️ 通知记录写入失败: {e}")

    def _save_state(self):
        """保存状态到state.json（供 Web 仪表盘读取）"""
        try:
            import json
            uptime = int((datetime.now(TZ_ET) - self.start_time).total_seconds())
            hours, remainder = divmod(uptime, 3600)
            minutes, seconds = divmod(remainder, 60)
            uptime_str = f"{hours}h {minutes}m {seconds}s"

            state = {
                'connected': True,
                'running': self.running,
                'current_price': self.current_price,
                'position': None,
                'trades_today': [],
                'signal_probes': [],
                'daily_pnl': self.daily_pnl,
                'filter_status': self.filter_status,
                'price_action_state': self.price_action_state,
                'day_market_regime': self.day_market_regime,
                'current_signal': self.current_signal,
                'session_high': self.session_high,
                'session_low': self.session_low if self.session_low < 999999 else 0,
                'candle_count': len(self.one_min_candles),
                'updated': datetime.now(TZ_ET).strftime('%H:%M:%S'),
                'events': self.events[-30:],  # 最近30条事件
                # 运行时间
                'uptime': uptime_str,
                # 从长桥拉取的实时账户/持仓
                'account': self._account_state,
                'broker_positions': self._broker_positions,
                'equity': self.account_info.get('equity', self.cfg.get('capital', 100000)),
            }
            if self.position:
                state['position'] = {
                    'dir': self.position.get('dir', ''),
                    'entry_price': self.position.get('entry_price', 0),
                    'qty': self.position.get('qty', 0),
                    'contracts': self.position.get('contracts', 0),
                    'reason': self.position.get('reason', ''),
                    'stock_peak': self.position.get('stock_peak', 0),
                    'half_closed': self.position.get('half_closed', False),
                    'max_pnl_pct': self.position.get('max_pnl_pct', 0),
                }
            for t in self.trades_today:
                state['trades_today'].append({
                    'time': t.get('time', t.get('entry_time', '')),
                    'dir': t.get('dir', ''),
                    # 🔧 优先保存期权开仓价（entry_opt_price），否则 fallback 到 entry_price
                    'entry_price': t.get('entry_opt_price') or t.get('entry_price', 0),
                    'exit_price': t.get('exit_opt_price') or t.get('exit_price', 0),
                    'entry_time': t.get('entry_time', ''),
                    'exit_time': t.get('exit_time', ''),
                    'contracts': t.get('contracts', 0),
                    'pnl_pct': round(t.get('pnl_pct', 0), 2),
                    'pnl_usd': round(t.get('pnl_usd', 0), 2),
                    'reason': t.get('reason', ''),
                    'exit_reason': t.get('exit_reason', ''),
                    'result': t.get('result', '') or ('win' if t.get('pnl_pct', 0) > 0 else 'lose' if t.get('pnl_pct', 0) < 0 else ''),
                    'opt_symbol': t.get('opt_symbol', ''),
                    'regime': t.get('regime', 'neutral'),
                    'day_market_regime': t.get('day_market_regime', ''),
                    'day_market_label': t.get('day_market_label', ''),
                    'day_market_direction': t.get('day_market_direction', ''),
                })
            for p in self.signal_probes[-200:]:
                state['signal_probes'].append({
                    'id': p.get('id'),
                    'time': p.get('entry_time', ''),
                    'entry_bar': p.get('entry_bar', 0),
                    'signal': p.get('signal', ''),
                    'dir': p.get('dir', ''),
                    'entry_price': p.get('entry_price', 0),
                    'reason': p.get('reason', ''),
                    'regime': p.get('regime', ''),
                    'day_market_regime': p.get('day_market_regime', ''),
                    'day_market_label': p.get('day_market_label', ''),
                    'day_market_direction': p.get('day_market_direction', ''),
                    'source': p.get('source', 'live'),
                    'rejection_reason': p.get('rejection_reason', ''),
                    'm5_pct': p.get('m5_pct'),
                    'm10_pct': p.get('m10_pct'),
                    'm20_pct': p.get('m20_pct'),
                    'm5_price': p.get('m5_price'),
                    'm10_price': p.get('m10_price'),
                    'm20_price': p.get('m20_price'),
                    'completed': p.get('completed', False),
                })
            tmp_path = self.state_path + '.tmp'
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(state, f, ensure_ascii=False, indent=2, default=_json_default)
            # Windows文件锁定重试机制
            for attempt in range(5):
                try:
                    os.replace(tmp_path, self.state_path)  # 原子替换
                    break
                except OSError as e:
                    if attempt < 4:
                        time.sleep(0.2)  # 等待200ms后重试
                    else:
                        raise

            # 保存持仓快照
            if self.position:
                script_dir = str(_app_dir())
                pos_snapshot = [{
                    'sym': self.position.get('opt_symbol', 'QQQ'),
                    'qty': self.position.get('contracts', 0),
                    'cost': f"${self.position.get('entry_opt_price', 0):.2f}",
                    'cur': f"${self.current_price:.2f}",
                    'pnl': f"${self.position.get('pnl_usd', 0):.2f}",
                    'pct': f"{self.position.get('pnl_pct', 0):.1f}%"
                }]
                pos_path = os.path.join(script_dir, 'position_snapshot.json')
                with open(pos_path, 'w', encoding='utf-8') as f:
                    json.dump(pos_snapshot, f, ensure_ascii=False)
            else:
                # 无持仓时删除快照文件
                script_dir = str(_app_dir())
                pos_path = os.path.join(script_dir, 'position_snapshot.json')
                if os.path.exists(pos_path):
                    os.remove(pos_path)
        except Exception as e:
            print(f"  ⚠️ 状态保存失败: {e}")

    def _init_api(self):
        """初始化长桥API - 支持WSL和Windows"""
        self.quote_ctx = None
        self.trade_ctx = None
        import os
        try:
            script_dir = str(_app_dir())
            env_paths = [
                os.path.join(script_dir, '.env'),
                os.path.expanduser('~/.hermes/.env'),
                os.path.expanduser('~\\.hermes\\.env'),
            ]
            for env_file in env_paths:
                if os.path.exists(env_file):
                    with open(env_file, encoding='utf-8') as f:
                        for line in f:
                            line = line.strip()
                            if line and '=' in line and not line.startswith('#'):
                                k, v = line.split('=', 1)
                                v = v.strip('"').strip("'")
                                if 'LONGPORT' in k or 'LONGBRIDGE' in k or 'MINIMAX' in k:
                                    os.environ[k] = v
                    print(f"Loaded env from: {env_file}")
                    break

            # OAuth 2.0 认证（自动管理 token）
            import os
            client_id = os.environ.get('LONGBRIDGE_CLIENT_ID')
            if not client_id:
                raise RuntimeError("缺少 LONGBRIDGE_CLIENT_ID，请在 .env 中配置长桥 OAuth Client ID")
            
            # 首次运行会打印授权链接，完成后 token 自动缓存
            oauth = OAuthBuilder(client_id).build(
                lambda url: print(f"🔗 请授权: {url}")
            )
            self.config = Config.from_oauth(oauth)
            self.quote_ctx = QuoteContext(self.config)
            self.trade_ctx = TradeContext(self.config)
            self.quote_ctx.set_on_quote(self._on_quote)
            print("✅ 长桥API连接成功")
            self._add_event("✅ 长桥API连接成功", "engine")
        except Exception as e:
            print(f"❌ 长桥API连接失败: {e}")
            self._add_event(f"❌ API连接失败: {e}", "error")
            import traceback
            traceback.print_exc()

    def _extract_quote_price(self, event):
        """兼容不同SDK推送对象结构，提取最新价"""
        quote = getattr(event, 'quote', event)
        for attr in ('last_done', 'last_price', 'price'):
            value = getattr(quote, attr, None)
            if value is not None:
                try:
                    price = float(value)
                    if price > 0:
                        return price
                except (TypeError, ValueError):
                    pass
        return 0.0

    def _subscribe_quotes(self, symbols):
        """订阅报价推送，避免重复订阅"""
        symbols = [s for s in symbols if s and s not in self._subscribed_quote_symbols]
        if not symbols or not self.quote_ctx:
            return
        try:
            self.quote_ctx.subscribe(symbols, [SubType.Quote])
            self._subscribed_quote_symbols.update(symbols)
            print(f"📡 已订阅报价推送: {', '.join(symbols)}")
        except Exception as e:
            print(f"⚠️ 报价订阅失败 {symbols}: {e}")
            # 发送网络/API错误通知
            self._handle_error_with_notification(e, "订阅报价")

    def _unsubscribe_quotes(self, symbols):
        """取消不再需要的报价推送"""
        symbols = [s for s in symbols if s and s in self._subscribed_quote_symbols and s != self.cfg['symbol']]
        if not symbols or not self.quote_ctx:
            return
        try:
            self.quote_ctx.unsubscribe(symbols, [SubType.Quote])
            for symbol in symbols:
                self._subscribed_quote_symbols.discard(symbol)
                self.latest_quote_prices.pop(symbol, None)
            print(f"📴 已取消报价推送: {', '.join(symbols)}")
        except Exception as e:
            print(f"⚠️ 取消报价订阅失败 {symbols}: {e}")

    def _check_tiered_loss_circuit(self) -> tuple:
        """P0 #7 阶梯式日亏损熔断 — 返回 (level, msg)
        level=0: 正常交易
        level=1: 警告 — 仓位减半
        level=2: 保守 — 仅trending信号 + 仓位降至25%
        level=3: 熔断 — 停止所有新交易
        """
        loss_pct = abs(self.daily_pnl) / max(self.actual_capital, self.cfg['capital']) * 100 if self.daily_pnl < 0 else 0
        limit = self.cfg.get('daily_limit', 12)
        warn_pct = self.cfg.get('daily_limit_warning_pct', 5)
        cons_pct = self.cfg.get('daily_limit_conservative_pct', 8)

        if loss_pct >= limit:
            return 3, f'熔断({loss_pct:.1f}%>={limit:.0f}%)'
        if loss_pct >= cons_pct:
            if not self._loss_circuit_conservative_fired:
                self._loss_circuit_conservative_fired = True
                self._notify(f"日亏损保守级: {loss_pct:.1f}% (>{cons_pct:.0f}%) 仓位降至25%, 只做trending")
            return 2, f'保守({loss_pct:.1f}%>={cons_pct:.0f}%)'
        if loss_pct >= warn_pct:
            if not self._loss_circuit_warning_fired:
                self._loss_circuit_warning_fired = True
                self._notify(f"日亏损警告级: {loss_pct:.1f}% (>{warn_pct:.0f}%) 仓位减半")
            return 1, f'警告({loss_pct:.1f}%>={warn_pct:.0f}%)'
        return 0, ''

    def _subscribe_position_quote(self, opt_symbol):
        """持仓建立/恢复后订阅期权报价，价格变动即触发风控"""
        if not opt_symbol:
            return
        self._subscribe_quotes([opt_symbol])

    def _on_quote(self, symbol, event):
        """报价推送回调：更新缓存，并用持仓期权价格实时检查止盈止损"""
        price = self._extract_quote_price(event)
        if price <= 0:
            return

        self.latest_quote_prices[symbol] = price
        
        # VIX指数更新（v7波动率过滤）
        if symbol == 'VIX.US':
            self.v7.update_vix(price)
            return
        
        if symbol == self.cfg['symbol']:
            self.current_price = price
            return

        pos = self.position
        if not self.running or not pos or symbol != pos.get('opt_symbol'):
            return

        self._check_position(
            opt_price=price,
            current_stock=self.current_price or None,
            triggered_by_push=True,
        )

    def _preload_history(self):
        """启动时预加载今日全部K线，并回放信号检测"""
        try:
            from longbridge.openapi import AdjustType
            count = 500  # 足够覆盖全天（09:30-15:50 ≈ 375根）
            print(f"📥 加载今日K线（最多{count}根）...", end=" ")
            candles = self.quote_ctx.candlesticks(
                self.cfg['symbol'], Period.Min_1, count,
                AdjustType.NoAdjust
            )
            if not candles:
                print("无数据")
                return

            # 过滤出今天的K线（用美东时间判断交易日，c.timestamp是UTC需转ET）
            today_str = datetime.now(TZ_ET).strftime('%Y-%m-%d')
            today_candles = []
            for c in candles:
                # 将UTC时间戳转为美东时间再比较日期
                if hasattr(c.timestamp, 'astimezone'):
                    c_et = c.timestamp.astimezone(TZ_ET).strftime('%Y-%m-%d')
                else:
                    c_et = str(c.timestamp)[:10]  # fallback
                if c_et == today_str:
                    today_candles.append(c)

            if not today_candles:
                # 如果过滤后没有，用最后50根
                today_candles = candles[-50:]
                print(f"今日无数据，加载最近{len(today_candles)}根")
            else:
                print(f"今日{len(today_candles)}根")

            # 填充数据
            for c in today_candles:
                bar = {
                    'time': c.timestamp,
                    'open': float(c.open),
                    'high': float(c.high),
                    'low': float(c.low),
                    'close': float(c.close),
                    'volume': int(c.volume),
                }
                self.kline_buffer.append(bar)
                self.one_min_candles.append(bar)
                self.close_history.append(bar['close'])
                self.volume_history.append(bar['volume'])
                self.session_high = max(self.session_high, bar['high'])
                self.session_low = min(self.session_low, bar['low'])
                # v6.5 同步填充 FilterEngine
                bar_with_dir = dict(bar)
                bar_with_dir['body_pct'] = abs(bar['close'] - bar['open']) / bar['open'] * 100 if bar['open'] else 0
                bar_with_dir['dir'] = 1 if bar['close'] >= bar['open'] else -1
                self.engine.update(bar_with_dir)
                self._write_csv(bar)

            self.current_price = float(today_candles[-1].close)
            self.current_date = datetime.now(TZ_ET).strftime('%Y-%m-%d')

            sma = np.mean(self.close_history[-20:]) if len(self.close_history) >= 20 else 0
            vol_avg = np.mean(self.volume_history[-20:]) if len(self.volume_history) >= 20 else 0
            print(f"  📊 价格${self.current_price:.2f} | SMA20:{sma:.2f} | 均量:{vol_avg:,.0f}")

            # ===== 检查长桥现有持仓，避免重复下单 =====
            print(f"  🔍 检查长桥持仓...")
            try:
                stock_positions = self.trade_ctx.stock_positions()
                has_position = False
                if stock_positions and hasattr(stock_positions, 'channels'):
                    for channel in stock_positions.channels:
                        if hasattr(channel, 'positions'):
                            for pos in channel.positions:
                                if hasattr(pos, 'symbol') and 'QQQ' in str(pos.symbol) and 'US' in str(pos.symbol):
                                    qty = int(getattr(pos, 'quantity', 0) or 0)
                                    if qty > 0:
                                        has_position = True
                                        print(f"  ⚠️ 发现长桥持仓: {pos.symbol} x {qty}张")
                                        # 恢复内部持仓状态
                                        self.position = {
                                            'order_id': 'restored',
                                            'dir': 'call' if 'C' in str(pos.symbol) else 'put',
                                            'entry_price': float(getattr(pos, 'cost_price', 0) or 0),
                                            'opt_symbol': str(pos.symbol),
                                            'entry_opt_price': float(getattr(pos, 'cost_price', 0) or 0),
                                            'sl_pct': self.cfg['sl'],
                                            'tp_pct': self.cfg['tp'],
                                            'contracts': qty,
                                            'quantity': qty * self.cfg['contract_multiplier'],
                                            'entry_time': datetime.now(TZ_ET),
                                            'entry_bar': len(self.one_min_candles),
                                            'reason': '重启恢复持仓',
                                            'max_pnl_pct': 0,
                                            'half_closed': False,
                                            'half_closed_max_pct': 0.0,
                                            'order_status': 'restored',
                                        }
                                        print(f"  ✅ 已恢复内部持仓状态")
                                        self._subscribe_position_quote(self.position.get('opt_symbol'))
                                        break
                        if has_position:
                            break
            except Exception as e:
                print(f"  ⚠️ 检查持仓失败: {e}")

            # ===== 回放信号检测（仅在无持仓时）=====
            if len(self.one_min_candles) >= self.cfg['lookback'] + 1:
                last_bar = {
                    'time': today_candles[-1].timestamp,
                    'open': float(today_candles[-1].open),
                    'high': float(today_candles[-1].high),
                    'low': float(today_candles[-1].low),
                    'close': float(today_candles[-1].close),
                    'volume': int(today_candles[-1].volume),
                }
                # 用最后一根K线的时间算美东分钟数
                ts = today_candles[-1].timestamp
                if hasattr(ts, 'astimezone'):
                    et = ts.astimezone(TZ_ET)
                else:
                    et = datetime.now(TZ_ET).astimezone(TZ_ET)
                cur_min = et.hour * 60 + et.minute
                print(f"  🔍 回放信号检测（美东{et.strftime('%H:%M')}）...")
                
                # 只在无持仓时检测信号，有持仓则跳过
                if not self.position:
                    self._check_breakout(last_bar, cur_min)
                    if not self.position:
                        self._check_reversal(last_bar, cur_min)
                else:
                    print(f"  ⏭️ 已有持仓，跳过信号检测")
                
                print(f"  ✅ 回放完成 | 过滤状态已更新")
            else:
                print(f"  ⏳ K线不足{self.cfg['lookback']+1}根，跳过回放")

        except Exception as e:
            print(f"⚠️ 预加载失败: {e}（不影响正常运行）")

    def _recover_broker_position(self):
        """启动时从长桥同步实际持仓，接管被手动关闭后遗留的仓位"""
        if self.position: # 如果内存已有，跳过
            return

        try:
            res = self.trade_ctx.stock_positions()
            if not res or not getattr(res, 'channels', None):
                return

            now_et = datetime.now(TZ_ET).strftime('%y%m%d') # 260513

            for channel in res.channels:
                if not getattr(channel, 'positions', None):
                    continue
                for p in channel.positions:
                    symbol = str(p.symbol)
                    qty = int(p.quantity)
                    # 检查是否是今天的 0DTE 期权持仓
                    if qty > 0 and 'QQQ' in symbol and now_et in symbol:
                        cost = float(getattr(p, 'cost_price', 0))
                        direction = 'call' if 'C' in symbol.upper() else 'put'
                        stock_entry = self._current_stock_price()
                        stock_exit_enabled = stock_entry > 100

                        print(f"🔍 发现长桥持仓: {symbol} x{qty}")
                        print(f"📥 正在接管并同步到系统...")

                        # 恢复内存持仓对象
                        self.position = {
                            'dir': direction,
                            'contracts': qty,
                            'entry_opt_price': cost,          # 期权成本价（显式）
                            'opt_symbol': symbol,
                            'entry_price': stock_entry if stock_exit_enabled else 0,
                            # 设为当前时间，防止被误判超时直接平仓
                            'entry_time': datetime.now(TZ_ET),
                            'stock_peak': 0,       # 等待报价更新
                            'stock_valley': 0,
                            'stock_peak': stock_entry if stock_exit_enabled else 0,
                            'stock_exit_enabled': stock_exit_enabled,
                            'half_closed': False,  # 假设未半仓
                            'max_pnl_pct': 0,
                            'max_pnl_abs': 0,
                            'sl_pct': self.cfg.get('sl', 0.25),
                            'reason': '系统重启恢复',
                            '_is_recovered': True
                        }

                        # 同步到交易记录（防止 Web 端数据丢失）
                        self.trades_today.append({
                            'time': datetime.now(TZ_ET).strftime('%H:%M:%S'),
                            'entry_time': datetime.now(TZ_ET),
                            'dir': direction,
                            'contracts': qty,
                            'opt_symbol': symbol,
                            'entry_opt_price': cost,
                            'entry_price': stock_entry if stock_exit_enabled else 0,
                            'pnl_pct': 0,
                            'pnl_usd': 0,
                            'win': None,
                            'reason': '系统重启恢复',
                            'exit_reason': '',
                        })

                        # 启动报价订阅
                        self._subscribe_position_quote(symbol)
                        self._save_state()
                        self._add_event(f"✅ 接管成功: {symbol}", "info")
                        return # 只接管单腿（单边持仓）

        except Exception as e:
            print(f"⚠️ 自动恢复持仓失败: {e}")

    def start(self):
        """启动交易系统"""
        self.running = True
        print(f"🚀 QQQ 0DTE v7 多引擎策略启动")
        print(f"📊 市场状态自适应: 趋势(顺势)/中性(标准)/震荡(快进快出)")
        print(f"💰 资金: 实时查询 | 下单: {self.cfg['order_pct']}%资金/笔")
        print(f"📈 标的: {self.cfg['symbol']}")
        print(f"⏰ 交易窗口: {self.cfg['start_time']}-{self.cfg['end_time']} (美东)")
        print(f"🔄 交易次数: 不限制 | 日亏损熔断: {self.cfg['daily_limit']}%")
        print(f"📉 止损: 动态(趋势25%/震荡30%) | 止盈: 动态(趋势100%/震荡50%)")
        print(f"🛡 超时: 动态(趋势15min/中性10min/震荡5min)")
        print(f"🔍 过滤: 量能+动量+实体(动态) + 预加载4取N(动态)")
        print(f"🔄 冷却: 按方向冷却(反向不受影响) | 衰竭反转: 独立信号")
        print("=" * 60)

        # 检查API连接
        if not self.quote_ctx:
            print("❌ 无法启动: 长桥API未连接")
            return

        # 预加载历史K线（解决重启后数据不足问题）
        self._preload_history()

        # 订阅1分钟K线
        self.quote_ctx.subscribe_candlesticks(
            self.cfg['symbol'], Period.Min_1
        )
        self.quote_ctx.set_on_candlestick(self._on_candlestick)
        self._subscribe_quotes([self.cfg['symbol']])
        
        # 订阅VIX指数（用于v7波动率过滤）
        try:
            self._subscribe_quotes(['VIX.US'])
            print("📡 已订阅VIX指数")
        except Exception as e:
            print(f"⚠️ VIX订阅失败: {e}")
        
        # 初始化v7 Dashboard
        try:
            import dashboard_v7
            dashboard_v7.set_signal_manager(self.v7.signal_manager)
            
            if os.environ.get('QQQ_DASHBOARD_STARTED') == '1':
                print("📊 v7 Dashboard已由 run_web.py 启动，交易引擎仅接入状态")
            else:
                # 直接运行 live_trader.py 时才启动 Dashboard
                import threading
                def run_dashboard():
                    try:
                        os.environ['QQQ_DASHBOARD_STARTED'] = '1'
                        dashboard_v7.run_dashboard("0.0.0.0", 8080)
                    except Exception as e:
                        print(f"⚠️ Dashboard运行失败: {e}")

                dashboard_thread = threading.Thread(target=run_dashboard, daemon=True)
                dashboard_thread.start()
                print("📊 v7 Dashboard已启动: http://localhost:8080")
            self._add_event("📊 v7 Dashboard已连接", "engine")
            
            # 初始化Dashboard状态
            dashboard_v7.set_running(True)
            dashboard_v7.set_connected(True)
            dashboard_v7.add_event("🚀 系统启动", "info")
        except ImportError as e:
            print(f"⚠️ Dashboard导入失败(需要安装fastapi): {e}")
            self._add_event(f"⚠️ Dashboard导入失败: {e}", "error")
        except Exception as e:
            print(f"⚠️ Dashboard连接失败: {e}")
            self._add_event(f"⚠️ Dashboard连接失败: {e}", "error")

        # 🔍 从长桥同步实际持仓，接管被手动关闭后遗留的仓位
        self._recover_broker_position()

        # 💰 启动时拉取账户资金和真实持仓（供 Web 显示）
        self._sync_account_state()

        if self.position:
            self._subscribe_position_quote(self.position.get('opt_symbol'))

        print("📡 已订阅QQQ 1分钟K线推送")
        print("📡 已订阅QQQ报价推送，持仓期权将用推送实时风控")
        print("⏳ 每20秒检测一次信号（持仓止盈止损由报价推送实时触发）...")

        # 主循环 - 每20秒检测一次
        last_order_sync = 0

        # ⚠️ 启动时先保存昨天未写入的记录（进程被kill -9不会调stop()）
        try:
            self._save_pending_records()
        except Exception as e:
            print(f"⚠️ 保存历史记录失败: {e}")

        # 📥 恢复今日已平仓交易记录（从 records/ 文件，避免重启后数据丢失）
        self._load_today_records()
        self._load_today_signal_probes()

        # 📊 加载昨日交易记录（用于启动通知）
        try:
            from zoneinfo import ZoneInfo
            TZ_ET = ZoneInfo("America/New_York")
            yesterday_et = (datetime.now(TZ_ET) - timedelta(days=1)).strftime('%Y-%m-%d')
            y_file = os.path.join(_app_dir(), 'records', f'{yesterday_et}.json')
            if os.path.exists(y_file):
                with open(y_file, encoding='utf-8') as f:
                    y_data = json.load(f)
                self.yesterday_pnl = float(y_data.get('pnl', 0))
                self.yesterday_trades = int(y_data.get('total', 0))
                wins = int(y_data.get('wins', 0))
                total = self.yesterday_trades
                self.yesterday_wr = round(wins / total * 100, 1) if total > 0 else 0.0
        except Exception as e:
            print(f"  ⚠️ 加载昨日记录失败: {e}")

        # 启动立即同步一次长桥订单（覆盖为今天的）
        try:
            self._sync_longbridge_orders()
            print(f"📤 启动同步长桥订单完成")
        except Exception as e:
            print(f"⚠️ 启动同步订单失败: {e}")

        # 🚀 发送启动通知
        try:
            self._notify("🚀 系统启动", msg_type='startup')
        except Exception as e:
            print(f"⚠️ 启动通知发送失败: {e}")

        try:
            while self.running:
                # 配置热重载（检测 settings.json 变化）
                _maybe_reload_config()
                # 每20秒检测一次信号
                self._check_signal_20s()
                self._check_position(triggered_by_push=False)
                
                # 每60秒同步一次长桥订单 + 账户资金到文件
                now = time.time()
                if now - last_order_sync >= 60:
                    last_order_sync = now
                    self._sync_longbridge_orders()
                    self._sync_account_state(silent=True)
                
                # 检查是否需要发送周报（每周五收盘后）
                try:
                    self._check_and_send_weekly_summary()
                except Exception:
                    pass
                
                time.sleep(self.cfg['check_interval'])  # 20秒检测一次
        except KeyboardInterrupt:
            self.stop()

    def _is_today_expiry(self, opt_symbol: str) -> bool:
        """检查期权合约是否是当天到期"""
        try:
            # 期权代码格式: QQQ{YYMMDD}{C/P}{strike}.US
            # 例如: QQQ260528C721000.US
            if not opt_symbol or not opt_symbol.startswith('QQQ'):
                return False
            # 提取YYMMDD部分 (位置3-8)
            date_str = opt_symbol[3:9]
            # 转换为完整日期
            year = 2000 + int(date_str[:2])
            month = int(date_str[2:4])
            day = int(date_str[4:6])
            expiry_date = datetime(year, month, day).strftime('%Y-%m-%d')
            today = datetime.now(TZ_ET).strftime('%Y-%m-%d')
            return expiry_date == today
        except:
            return False

    def stop(self, close_on_exit=True):
        """停止交易系统"""
        self.running = False
        print("\n🛑 交易系统停止")
        if close_on_exit and self.position:
            # 只平掉当天到期的期权
            if self._is_today_expiry(self.position.get('opt_symbol', '')):
                self._close_position("系统停止")
            else:
                print(f"  ⏸ 非当天到期期权，保留持仓: {self.position['opt_symbol']}")
        elif self.position:
            self._sync_longbridge_orders()
            self._save_state()
            print(f"  ⏸ 保留当前持仓: {self.position['opt_symbol']} x{self.position['contracts']}张")
        self._print_summary()
        self._save_daily_records()

    def _on_candlestick(self, symbol, candle):
        """K线回调 - 核心策略逻辑"""
        # PushCandlestick 结构: candle.candlestick.open/high/low/close/volume
        cs = candle.candlestick
        
        # 只处理已完成的K线（跳过进行中的实时更新）
        if not candle.is_confirmed:
            return

        if not self.running:
            return

        now = datetime.now(TZ_ET)

        # 日初重置（检测新交易日，用美东时间）
        today_str = now.strftime('%Y-%m-%d')
        if self.current_date != today_str:
            if self.current_date is not None:
                print(f"\n📅 新交易日: {today_str} | 重置日内状态")
            self.current_date = today_str
            self.session_high = 0
            self.session_low = 999999
            self.reversal_fired = False
            self.daily_signals = 0
            self.trades_today = []
            self.daily_pnl = 0
            old_symbol = self.position.get('opt_symbol') if self.position else None
            self.position = None
            self._unsubscribe_quotes([old_symbol])
            self.consecutive_losses = 0  # 新交易日重置亏损计数
            self.consecutive_wins = 0
            self.max_consecutive_wins = 0
            self.max_consecutive_losses = 0
            self.largest_win_usd = 0
            self.largest_loss_usd = 0
            self.largest_win_pct = 0
            self.largest_loss_pct = 0
            self.call_trades = 0
            self.put_trades = 0
            self.call_wins = 0
            self.put_wins = 0
            self.call_pnl = 0.0
            self.put_pnl = 0.0
            self.signal_probes = []
            self._signal_probe_seq = 0
            self.loss_cooldown_until = None  # 新交易日重置冷却
            self.last_loss_dir = None    # 新交易日重置亏损方向
            self.big_loss_cooldown = 0   # 新交易日重置大亏冷却
            self._signal_cooldowns = {}
            self.price_action_state = {'state': 'warming_up', 'direction': '', 'reason': ''}
            self.day_market_regime = {
                'type': 'warming_up',
                'direction': '',
                'label': '预热',
                'reason': '',
            }
            self._loss_circuit_warning_fired = False      # 新交易日重置熔断通知
            self._loss_circuit_conservative_fired = False  # 新交易日重置熔断通知
            self._daily_summary_sent = False  # 新交易日重置日终总结标记
            self.engine.reset_day()  # v6.5 重置 FilterEngine
            # 只在非预加载情况下清空K线数据
            if not self.one_min_candles:
                self.kline_buffer = []
                self.close_history = []
                self.volume_history = []

        # 解析1分钟K线
        bar = {
            'time': now,
            'open': float(cs.open),
            'high': float(cs.high),
            'low': float(cs.low),
            'close': float(cs.close),
            'volume': int(cs.volume),
            'turnover': float(cs.turnover),
        }

        # 更新当日高低点
        self.session_high = max(self.session_high, bar['high'])
        self.session_low = min(self.session_low, bar['low'])

        self.kline_buffer.append(bar)

        # ===== 直接存储1分钟K线用于信号检测 =====
        one_min = {
            'time': bar['time'],
            'open': bar['open'],
            'high': bar['high'],
            'low': bar['low'],
            'close': bar['close'],
            'volume': bar['volume'],
        }
        one_min['body_pct'] = abs(one_min['close'] - one_min['open']) / one_min['open'] * 100
        one_min['dir'] = 1 if one_min['close'] >= one_min['open'] else -1

        self.one_min_candles.append(one_min)
        self._update_signal_probes(one_min)
        self.price_action_state = self.price_action.market_state(self.one_min_candles)

        # 写入today.csv（供cron监控读取）
        self._write_csv(one_min)
        self.current_price = one_min['close']
        
        # 更新Dashboard
        try:
            import dashboard_v7
            dashboard_v7.update_price(self.current_price)
            dashboard_v7.update_candle_count(len(self.one_min_candles))
        except Exception:
            pass

        # 更新指标历史
        self.close_history.append(one_min['close'])
        self.volume_history.append(one_min['volume'])
        if len(self.close_history) > 1000:
            self.close_history = self.close_history[-1000:]
            self.volume_history = self.volume_history[-1000:]

        # FilterEngine 计算 ATR/VWAP/SMA 等技术指标（供信号检测用）
        self.engine.update(one_min)
        self.day_market_regime = self._classify_day_market_regime()
        try:
            import dashboard_v7
            dashboard_v7.update_day_market_regime(self.day_market_regime)
        except Exception:
            pass
        
        # v7 多引擎更新
        et_now = now.astimezone(TZ_ET)
        cur_min_et = et_now.hour * 60 + et_now.minute
        self.v7.update(one_min, cur_min_et)
        
        self._save_state()

        # 打印1分钟K线
        d = "🟢" if one_min['dir'] > 0 else "🔴"
        sma = np.mean(self.close_history[-20:]) if len(self.close_history) >= 20 else 0
        sma_str = f" SMA20:{sma:.2f}" if sma > 0 else ""
        print(f"  {d} 1min {now.astimezone(TZ_ET).strftime('%H:%M ET')} "
              f"O:{one_min['open']:.2f} H:{one_min['high']:.2f} "
              f"L:{one_min['low']:.2f} C:{one_min['close']:.2f} "
              f"Vol:{one_min['volume']:,}{sma_str}")

        # ===== 每根1分钟K线都检测信号 =====
        if len(self.one_min_candles) >= self.cfg['lookback'] + 1:
            # 时间转换：长桥返回HKT(UTC+8)，需转美东(UTC-4夏令时)
            et_now = now.astimezone(TZ_ET)
            cur_min_et = et_now.hour * 60 + et_now.minute

            # v6.5 信号检测
            self._check_breakout(one_min, cur_min_et)

            # 衰竭反转检测（突破未触发时检查反转）
            if not self.position:
                self._check_reversal(one_min, cur_min_et)
            
            # v7 多引擎信号检测（如果v6.5未触发）
            if not self.position:
                self._check_v7_signal(cur_min_et)

    def _check_v7_signal(self, cur_min_et: int):
        """v7 多引擎信号检测"""
        # 时间窗口检查
        s_h, s_m = map(int, self.cfg['start_time'].split(':'))
        dyn_end = self._effective_end_time()
        e_h, e_m = map(int, dyn_end.split(':'))
        if not (s_h*60+s_m <= cur_min_et <= e_h*60+e_m):
            return
            
        # 午盘软断路器
        if 720 <= cur_min_et < 780:
            return
            
        if self.position:
            return
            
        # 双重检查：查长桥实际持仓
        try:
            if self._check_longbridge_position() > 0:
                return
        except:
            return
            
        # 冷却检查
        if self.loss_cooldown_until is not None:
            now = datetime.now(TZ_ET)
            if now < self.loss_cooldown_until:
                return
                
        # 检查v7信号
        sig = self.v7.check_signal()
        if sig is None:
            return
        cooldown_key = f"{sig.get('display_engine') or sig.get('engine')}:{sig.get('dir')}"
        cooldown_until = self._signal_cooldowns.get(cooldown_key)
        if cooldown_until and datetime.now(TZ_ET) < cooldown_until:
            remaining = int((cooldown_until - datetime.now(TZ_ET)).total_seconds() / 60) + 1
            print(f"  ⏳ 信号冷却中: {cooldown_key} 剩余{remaining}分钟")
            return

        # ===== PUT信号拦截：真实数据显示系统PUT信号1W/20L，禁用可多赚$52K =====
        if sig.get('dir') == 'put' and self.cfg.get('disable_put_signals', True) and not self.cfg.get('enable_put_entries', False):
            print(f"  ⛔ v7 PUT信号已禁用 (disable_put_signals=True)")
            return

        # 执行交易
        print(f"  🎯 v7信号: {sig['engine']} {sig['dir']} @ ${sig['price']:.2f} (强度:{sig['strength']:.0f})")
        if self._should_skip_and_track(sig):
            return
        self._execute_trade(sig)
        
    def _check_signal_20s(self):
        """每20秒主动检测信号（不依赖K线回调）"""
        if not self.running:
            return
        if self.position:
            return
        # 双重检查：查长桥实际持仓
        try:
            if self._check_longbridge_position() > 0:
                return
        except:
            return
        # v6.3: 取消每日交易次数限制

        now = datetime.now(TZ_ET)

        # 时间转换：直接使用美东时间
        et_now = now
        cur_min_et = et_now.hour * 60 + et_now.minute

        # 检查时间窗口（动态：盈利守护/亏损反攻）
        s_h, s_m = map(int, self.cfg['start_time'].split(':'))
        dyn_end = self._effective_end_time()
        e_h, e_m = map(int, dyn_end.split(':'))
        if not (s_h*60+s_m <= cur_min_et <= e_h*60+e_m):
            return

        # 获取当前正股价格
        try:
            quotes = self.quote_ctx.quote([self.cfg['symbol']])
            if not quotes:
                return
            current_price = float(quotes[0].last_done)
        except:
            return

        # 构建模拟K线（用于信号检测）
        if len(self.one_min_candles) < 2:
            return

        # 用最近两根K线构建信号检测数据
        prev_bar = self.one_min_candles[-1]
        fake_bar = {
            'time': now,
            'open': prev_bar['close'],  # 开盘=前一收盘
            'high': max(prev_bar['high'], current_price),
            'low': min(prev_bar['low'], current_price),
            'close': current_price,
            'volume': prev_bar['volume'],
            'dir': 1 if current_price >= prev_bar['close'] else -1,
            'body_pct': abs(current_price - prev_bar['close']) / prev_bar['close'] * 100,
        }

        # 更新当日高低点
        self.session_high = max(self.session_high, current_price)
        self.session_low = min(self.session_low, current_price)

        # v6.5 同步 FilterEngine（用于20秒轮询的滤镜状态）
        self.engine.session_high = self.session_high
        self.engine.session_low = self.session_low
        # 注意：不调用 engine.update(fake_bar)，避免假K线污染SMA/趋势计算

        # 检测突破信号
        self._check_breakout(fake_bar, cur_min_et)

        # 检测衰竭反转
        if not self.position:
            self._check_reversal(fake_bar, cur_min_et)
            
        # v7 多引擎信号检测（如果v6.5未触发）
        if not self.position:
            self._check_v7_signal(cur_min_et)

    def _update_filters_current(self, bar):
        """更新过滤器状态供 Web 显示（简化版）"""
        entry_price = bar['close']
        ref_dir = 'call' if bar['close'] >= bar['open'] else 'put'
        vh = self.volume_history
        vol_avg = np.mean(vh[-20:]) if len(vh) >= 20 else 0
        self.filter_status = {
            'dir': '做多' if ref_dir == 'call' else '做空',
            'price': f'${entry_price:.2f}',
            'all_ok': False,
        }
        self._save_state()

    def _check_breakout(self, bar, cur_min):
        """v6.3 动态过滤突破信号（根据市场状态自适应）"""
        # 时间窗口（动态：盈利守护/亏损反攻）
        s_h, s_m = map(int, self.cfg['start_time'].split(':'))
        dyn_end = self._effective_end_time()
        e_h, e_m = map(int, dyn_end.split(':'))
        if not (s_h*60+s_m <= cur_min <= e_h*60+e_m):
            return
        # ===== P1 #8 午盘软断路器：12:00-13:00 ET 禁止开新仓 =====
        # 只禁止新开仓信号，已有持仓的退出逻辑不受影响
        if 720 <= cur_min < 780:
            return
        if self.position:
            return
        # 双重检查：查长桥实际持仓
        try:
            if self._check_longbridge_position() > 0:
                return
        except:
            return
        # v6.3: 取消每日交易次数限制
        if self.daily_pnl <= -self.actual_capital * self.cfg['daily_limit'] / 100:
            self._update_filters_current(bar)
            return
        # RSI预过滤：仅过滤极端值（<20或>80），方向确认移到信号检测后
        rsi = self._calc_rsi(self.cfg['rsi_period'])
        if rsi > 80:
            self._update_filters_current(bar)
            return
        if rsi < 20:
            self._update_filters_current(bar)
            return

        # ===== v6.3 市场状态检测 =====
        regime_params = self.engine.get_regime_params()
        regime = regime_params['regime']

        cs = self.one_min_candles
        # 动态lookback：趋势市3根，震荡市2根，中性市8根
        lb = regime_params['lookback']
        if len(cs) < lb + 1:
            self._update_filters_current(bar)
            return

        entry_price = bar['close']

        # ===== v6.3 动态突破检测 =====
        # 只用一个lookback（动态），不再双路径
        upper = max(c['high'] for c in cs[-lb-1:-1])
        lower = min(c['low'] for c in cs[-lb-1:-1])

        # 突破检测
        gap_up = (entry_price - upper) / upper if upper > 0 else 999
        gap_dn = (lower - entry_price) / lower if lower > 0 else 999
        max_gap = self.cfg['max_gap'] * regime_params['gap_mult']

        sig_dir = None
        if entry_price > upper and gap_up < max_gap:
            sig_dir = 'call'
        elif entry_price < lower and gap_dn < max_gap:
            sig_dir = 'put'

        # ===== PUT信号拦截：真实数据显示系统PUT信号1W/20L，禁用可多赚$52K =====
        if sig_dir == 'put' and self.cfg.get('disable_put_signals', True) and not self.cfg.get('enable_put_entries', False):
            print(f"  ⛔ PUT信号已禁用 (disable_put_signals=True)")
            return

        if not sig_dir:
            return
        ch = self.close_history

        # ===== P1 #8 Neutral状态空间安全垫(Buffer) =====
        # 横盘市需要超越突破线至少0.01%才入场
        if regime == 'neutral':
            buffer_pct = self.cfg.get('neutral_breakout_buffer_pct', 0.0003)
            if sig_dir == 'call' and entry_price <= upper * (1 + buffer_pct):
                if self.cfg.get('debug', False):
                    print(f"  ⛔ Neutral Buffer: 价格${entry_price:.2f}未达8根高点${upper:.2f}+{buffer_pct*100:.2f}%(${upper*(1+buffer_pct):.2f})")
                return
            if sig_dir == 'put' and entry_price >= lower * (1 - buffer_pct):
                if self.cfg.get('debug', False):
                    print(f"  ⛔ Neutral Buffer: 价格${entry_price:.2f}未达8根低点${lower:.2f}-{buffer_pct*100:.2f}%(${lower*(1-buffer_pct):.2f})")
                return
            macd_hist = getattr(self.engine, 'macd_hist', 0)
            if sig_dir == 'call' and macd_hist <= self.cfg.get('neutral_min_macd_hist', 0):
                print(f"  ⛔ Neutral MACD确认不足: MACD_hist={macd_hist:.4f}，跳过做多")
                return
            if sig_dir == 'put' and macd_hist >= -self.cfg.get('neutral_min_macd_hist', 0):
                print(f"  ⛔ Neutral MACD确认不足: MACD_hist={macd_hist:.4f}，跳过做空")
                return
            if len(ch) >= 5:
                sma5 = np.mean(ch[-5:])
                if sig_dir == 'call' and entry_price <= sma5:
                    print(f"  ⛔ Neutral SMA5确认不足: 价格${entry_price:.2f} <= SMA5 ${sma5:.2f}")
                    return
                if sig_dir == 'put' and entry_price >= sma5:
                    print(f"  ⛔ Neutral SMA5确认不足: 价格${entry_price:.2f} >= SMA5 ${sma5:.2f}")
                    return

        # ===== B. 趋势方向过滤：禁止逆势交易 =====
        ch = self.close_history
        if len(ch) >= 50:
            sma20 = np.mean(ch[-20:])
            sma50 = np.mean(ch[-50:])
            # 下降趋势（SMA20 < SMA50 且价格在SMA20下方）→ 禁止做多
            if sma20 < sma50 and entry_price < sma20 and sig_dir == 'call':
                print(f"  ⛔ 趋势过滤: SMA20({sma20:.2f})<SMA50({sma50:.2f}) 价格在均线下方，禁止做多")
                return
            # 上升趋势（SMA20 > SMA50 且价格在SMA20上方）→ 禁止做空
            if sma20 > sma50 and entry_price > sma20 and sig_dir == 'put':
                print(f"  ⛔ 趋势过滤: SMA20({sma20:.2f})>SMA50({sma50:.2f}) 价格在均线上方，禁止做空")
                return

        # ===== C. RSI 方向确认 =====
        rsi_val = self._calc_rsi(self.cfg['rsi_period'])
        if sig_dir == 'call' and (rsi_val < 25 or rsi_val > 75):
            print(f"  ⛔ RSI过滤: RSI={rsi_val:.1f}，做多要求25-75")
            return
        if sig_dir == 'put' and (rsi_val < 25 or rsi_val > 75):
            print(f"  ⛔ RSI过滤: RSI={rsi_val:.1f}，做空要求25-75")
            return

        # ===== 动量确认：当前K线同向 =====
        mom_ok = (bar['close'] >= bar['open']) if sig_dir == 'call' else (bar['close'] <= bar['open'])
        if not mom_ok:
            return

        # ===== 量能确认（动态阈值）=====
        vh = self.volume_history
        vol_avg = np.mean(vh[-20:]) if len(vh) >= 20 else 0
        cur_vol = bar['volume']
        vol_ok = cur_vol >= vol_avg * regime_params['vol_mult'] if vol_avg > 0 else True
        if not vol_ok:
            return

        # ===== 实体确认（动态阈值）====
        cur_body = abs(bar['close'] - bar['open']) / bar['open'] if bar['open'] else 0
        body_ok = cur_body >= regime_params['min_body']
        if not body_ok:
            return

        # ===== D. VWAP 硬过滤：价格必须在VWAP正确一侧 =====
        vwap = self.engine.vwap
        if vwap > 0:
            if sig_dir == 'call' and entry_price < vwap:
                print(f"  ⛔ VWAP过滤: 价格${entry_price:.2f} < VWAP${vwap:.2f}，禁止做多")
                return
            if sig_dir == 'put' and entry_price > vwap:
                print(f"  ⛔ VWAP过滤: 价格${entry_price:.2f} > VWAP${vwap:.2f}，禁止做空")
                return

        # ===== A. ATR 动态追高/追低（替代固定0.15/0.85）=====
        atr = self.engine.atr
        if atr > 0 and self.session_high > self.session_low:
            # 趋势市放宽到 2.0*ATR，中性/震荡用 1.5*ATR
            atr_mult = 2.0 if regime == 'trending' else 1.5
            atr_threshold = atr * atr_mult
            if sig_dir == 'call' and entry_price > self.session_high - atr_threshold:
                print(f"  ⛔ ATR追高: 价格${entry_price:.2f} 距当日高点${self.session_high:.2f}仅${self.session_high - entry_price:.2f} < {atr_mult}×ATR(${atr_threshold:.2f})，禁止追高")
                return
            if sig_dir == 'put' and entry_price < self.session_low + atr_threshold:
                print(f"  ⛔ ATR追低: 价格${entry_price:.2f} 距当日低点${self.session_low:.2f}仅${entry_price - self.session_low:.2f} < {atr_mult}×ATR(${atr_threshold:.2f})，禁止追低")
                return

        # ===== 回踩确认（趋势市+动量豁免）=====
        if regime_params['pullback']:
            # 动量豁免：连续3根同向K线 → 跳过回踩，直接追入
            recent_3 = cs[-3:] if len(cs) >= 3 else cs
            if sig_dir == 'call':
                consecutive_bull = sum(1 for b in recent_3 if b['close'] >= b['open'])
                if consecutive_bull >= 3:
                    print(f"  ⚡ 动量直入: 连续{consecutive_bull}根阳线，跳过回踩")
                else:
                    prev = cs[-2] if len(cs) >= 2 else None
                    if prev and prev['close'] > prev['open']:
                        return  # 做多要求前1根是阴线
            elif sig_dir == 'put':
                consecutive_bear = sum(1 for b in recent_3 if b['close'] <= b['open'])
                if consecutive_bear >= 3:
                    print(f"  ⚡ 动量直入: 连续{consecutive_bear}根阴线，跳过回踩")
                else:
                    prev = cs[-2] if len(cs) >= 2 else None
                    if prev and prev['close'] < prev['open']:
                        return  # 做空要求前1根是阳线

        # ===== 预加载滤镜（动态阈值）=====
        pre_ok, pre_filters, bonus_count = self.engine.check_preloaded(sig_dir, regime=regime)
        preloaded_pass = regime_params['preloaded_pass']
        # P1 #7 下午交易量少，提高滤镜要求
        if cur_min >= 720:  # 12:00 ET
            preloaded_pass += 1
        if bonus_count < preloaded_pass:
            fails = [k for k, v in pre_filters.items() if not v.get('ok')]
            print(f"  🔍 {sig_dir}(regime={regime}) 预加载滤镜不足({bonus_count}/{preloaded_pass}): {', '.join(fails)}")
            return

        # ===== 核心过滤状态（供Web显示）=====
        core_ok, core_filters = self.engine.check_filters(sig_dir, entry_price, bar, vol_avg)

        direction = '做多' if sig_dir == 'call' else '做空'
        mode_tag = f'{regime}({lb}根)'
        self.filter_status = {
            'sma20': pre_filters.get('sma20', {}),
            'sma50': pre_filters.get('sma50', {}),
            'volume': core_filters.get('volume', {}),
            'momentum': core_filters.get('momentum', {}),
            'body': core_filters.get('body', {}),
            'price_pos': pre_filters.get('price_pos', {}),
            'trend': pre_filters.get('trend', {}),
            'vwap': pre_filters.get('vwap', {}),
            'macd': pre_filters.get('macd', {}),
            'atr': self.engine.state.get('atr', {}),
            'dir': direction,
            'mode': mode_tag,
            'regime_detail': regime_params['detail'],
            'price': f'${entry_price:.2f}',
            'all_ok': True,
        }

        # ===== 冷却检查（时间戳方式）=====
        if self.loss_cooldown_until is not None:
            now = datetime.now(TZ_ET)
            if now < self.loss_cooldown_until:
                if sig_dir == self.last_loss_dir:
                    remaining = int((self.loss_cooldown_until - now).total_seconds() / 60)
                    print(f"  ⏳ 冷却中({self.last_loss_dir}方向)，跳过有效信号，剩余{remaining}分钟")
                    return
                else:
                    print(f"  ⏳ 冷却中但方向相反({sig_dir}≠{self.last_loss_dir})，允许交易，冷却到{self.loss_cooldown_until.strftime('%H:%M')}")
            else:
                self.loss_cooldown_until = None  # 过期自动清除

        # ===== 构建信号（使用regime动态参数）=====
        gap_pct = gap_up if sig_dir == 'call' else gap_dn
        sl_pct = regime_params['sl_pct']
        tp_partial = regime_params['tp_partial_pct']
        sig = {
            'dir': sig_dir,
            'reason': f'{regime}突破{(upper if sig_dir=="call" else lower):.2f}{direction}(跳空{gap_pct*100:.2f}%,LB{lb})',
            'price': entry_price,
            'sl': entry_price * (1 - sl_pct) if sig_dir == 'call' else entry_price * (1 + sl_pct),
            'tp': entry_price * (1 + self.cfg['tp']) if sig_dir == 'call' else entry_price * (1 - self.cfg['tp']),
            'sl_pct': sl_pct,
            'tp_partial_pct': tp_partial,
            'timeout_bars': regime_params['timeout_bars'],
            'pos_mult': regime_params['pos_mult'],
            'regime': regime,
            'engine': 'breakout',
            'display_engine': 'Kline_Pattern',
        }

        self._save_state()

        # 打印过滤日志
        vol_t = core_filters['volume']['detail']
        mom_v = core_filters['momentum']['val']
        body_v = core_filters['body']['val']
        vwap_v = '✓' if pre_filters.get('vwap', {}).get('ok') else '✗'
        macd_v = '✓' if pre_filters.get('macd', {}).get('ok') else '✗'
        filters_str = (
            f"regime={regime} | "
            f"LB{lb} | "
            f"量能({vol_t}) | "
            f"动量({mom_v}) | "
            f"实体({body_v}) | "
            f"VWAP({vwap_v}) | "
            f"MACD({macd_v}) | "
            f"滤镜({bonus_count}/{preloaded_pass})"
        )
        print(f"  🎯 {direction}[{regime}]突破@${entry_price:.2f} | {filters_str}")

        # ===== 执行交易 =====
        filters_passed = [f"regime={regime}", f"LB{lb}", f"量能✓", f"动量✓", f"实体✓", f"滤镜{bonus_count}/{preloaded_pass}"]
        sig['reason'] += f" [{', '.join(filters_passed)}]"

        if self._should_skip_and_track(sig):
            return
        self.daily_signals += 1
        self.current_signal = sig
        self._add_event(f"🎯 {direction}突破@${entry_price:.2f} | {filters_str}", "signal")
        self._execute_trade(sig)

    def _calc_rsi_from_closes(self, closes, period=14):
        """从价格数组计算RSI（Wilder平滑法）"""
        if len(closes) < period + 1:
            return 50
        deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
        gains = [max(d, 0) for d in deltas[:period]]
        losses = [max(-d, 0) for d in deltas[:period]]
        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period
        for d in deltas[period:]:
            avg_gain = (avg_gain * (period - 1) + max(d, 0)) / period
            avg_loss = (avg_loss * (period - 1) + max(-d, 0)) / period
        if avg_loss == 0:
            return 100
        rs = avg_gain / avg_loss
        return 100 - 100 / (1 + rs)

    def _check_reversal(self, bar, cur_min_et):
        """衰竭反转信号检测 - 抓超跌反弹/超涨回调"""
        # 时间窗口（动态：盈利守护/亏损反攻）
        s_h, s_m = map(int, self.cfg['start_time'].split(':'))
        dyn_end = self._effective_end_time()
        e_h, e_m = map(int, dyn_end.split(':'))
        if not (s_h*60+s_m <= cur_min_et <= e_h*60+e_m):
            return
        # ===== P1 #8 午盘软断路器：12:00-13:00 ET 禁止开新仓 =====
        if 720 <= cur_min_et < 780:
            return
        if self.position:
            return
        # 双重检查：查长桥实际持仓
        try:
            if self._check_longbridge_position() > 0:
                return
        except:
            return
        # v6.3: 取消每日交易次数限制
        if self.daily_pnl <= -self.actual_capital * self.cfg['daily_limit'] / 100:
            return
        if self.reversal_fired:  # 每天只抓一次反转
            return

        cs = self.one_min_candles
        if len(cs) < 3:
            return

        prev = cs[-2] if len(cs) >= 2 else cs[-1]  # 前一根K线（用于确认反弹，不是当前K线）
        entry = bar['close']

        # ===== 超跌反弹（做多）=====
        if self.session_high > 0:
            drop_from_high = (self.session_high - entry) / self.session_high
            if drop_from_high >= self.cfg['reversal_drop']:
                # 开盘40分钟内禁止逆势反转信号
                if cur_min_et < 615:  # 09:35-10:15 ET
                    return
                # 三乘三硬锁 (1) 多周期RSI共振
                rsi_1m = self._calc_rsi(self.cfg['rsi_period'])
                if rsi_1m >= 18:  # 1-min RSI < 18 极端超卖
                    return
                rsi_5m = 50
                closes_5m = self.close_history[-(5*14+1)::5] if len(self.close_history) >= 5*14+5 else []
                if len(closes_5m) >= 15:
                    rsi_5m = self._calc_rsi_from_closes(closes_5m, 14)
                    if rsi_5m >= 25:  # 5-min RSI < 25
                        return
                # 三乘三硬锁 (2) 结构确认：突破前3根高点
                if len(cs) < 4:
                    return
                high_3 = max(b['high'] for b in cs[-4:-1])
                if entry <= high_3:
                    return
                # 三乘三硬锁 (3) 量能确认
                bar_vol = bar.get('volume', 0)
                vol_avg = sum(self.volume_history[-5:]) / 5 if len(self.volume_history) >= 5 else 0
                if vol_avg > 0 and bar_vol < vol_avg * 1.5:
                    return
                # 确认反弹：前一根K线收阳 + 实体足够大
                bounce_body = abs(prev['close'] - prev['open']) / prev['open'] if prev['open'] else 0
                if prev['close'] >= prev['open'] and bounce_body >= self.cfg['reversal_bounce']:
                    # 如果有持仓且是PUT，先平仓再反向开仓
                    if self.position and self.position.get('dir') == 'put':
                        print(f"  🔄 反转信号：平掉PUT仓位，反向开CALL")
                        self._close_position("反转信号-平PUT")
                        self.position = None
                    
                    sig = {
                        'dir': 'call',
                        'reason': f'超跌反弹|从{self.session_high:.2f}跌{drop_from_high*100:.1f}% RSI1m{rsi_1m:.0f} RSI5m{rsi_5m:.0f}',
                        'price': entry,
                        'sl': entry * (1 - self.cfg['sl']),
                        'tp': entry * (1 + self.cfg['tp']),
                        'engine': 'reversal',
                        'display_engine': 'RSI_Reversal',
                    }
                    # 冷却检查（时间戳方式）
                    if self.loss_cooldown_until is not None:
                        now = datetime.now(TZ_ET)
                        if now < self.loss_cooldown_until:
                            if self.last_loss_dir == 'call':
                                remaining = int((self.loss_cooldown_until - now).total_seconds() / 60)
                                print(f"  ⏳ 冷却中(call方向)，剩余{remaining}分钟，跳过超跌反弹信号")
                                return
                        else:
                            self.loss_cooldown_until = None
                    if self._should_skip_and_track(sig):
                        return
                    self.reversal_fired = True
                    self.daily_signals += 1
                    print(f"  🔄 衰竭反转做多! 从高点跌{drop_from_high*100:.1f}%")
                    self.current_signal = sig
                    self._add_event(f"🔄 衰竭反转做多! 跌{drop_from_high*100:.1f}%", "signal")
                    self._execute_trade(sig)
                    return

        # ===== 超涨回调（做空）=====
        if self.session_low < 999999:
            rise_from_low = (entry - self.session_low) / self.session_low
            if rise_from_low >= self.cfg['reversal_drop']:
                # 开盘40分钟内禁止逆势反转信号
                if cur_min_et < 615:
                    return
                # 三乘三硬锁 (1) 多周期RSI共振
                rsi_1m = self._calc_rsi(self.cfg['rsi_period'])
                if rsi_1m <= 82:  # 1-min RSI > 82 极端超买
                    return
                rsi_5m = 50
                closes_5m = self.close_history[-(5*14+1)::5] if len(self.close_history) >= 5*14+5 else []
                if len(closes_5m) >= 15:
                    rsi_5m = self._calc_rsi_from_closes(closes_5m, 14)
                    if rsi_5m <= 75:  # 5-min RSI > 75
                        return
                # 三乘三硬锁 (2) 结构确认：跌破前3根低点
                if len(cs) < 4:
                    return
                low_3 = min(b['low'] for b in cs[-4:-1])
                if entry >= low_3:
                    return
                # 三乘三硬锁 (3) 量能确认
                bar_vol = bar.get('volume', 0)
                vol_avg = sum(self.volume_history[-5:]) / 5 if len(self.volume_history) >= 5 else 0
                if vol_avg > 0 and bar_vol < vol_avg * 1.5:
                    return
                # 确认回调：前一根K线收阴 + 实体足够大
                drop_body = abs(prev['close'] - prev['open']) / prev['open'] if prev['open'] else 0
                if prev['close'] <= prev['open'] and drop_body >= self.cfg['reversal_bounce']:
                    # 如果有持仓且是CALL，先平仓再反向开仓
                    if self.position and self.position.get('dir') == 'call':
                        print(f"  🔄 反转信号：平掉CALL仓位，反向开PUT")
                        self._close_position("反转信号-平CALL")
                        self.position = None
                    
                    # ===== PUT信号拦截：真实数据显示系统PUT信号1W/20L，禁用可多赚$52K =====
                    if self.cfg.get('disable_put_signals', True) and not self.cfg.get('enable_put_entries', False):
                        print(f"  ⛔ 反转PUT信号已禁用 (disable_put_signals=True)")
                        return

                    sig = {
                        'dir': 'put',
                        'reason': f'超涨回调|从{self.session_low:.2f}涨{rise_from_low*100:.1f}% RSI1m{rsi_1m:.0f} RSI5m{rsi_5m:.0f}',
                        'price': entry,
                        'sl_pct': 0.18,  # PUT止损18%
                        'sl': entry * (1 + 0.18),
                        'tp': entry * (1 - self.cfg['tp']),
                        'engine': 'reversal',
                        'display_engine': 'RSI_Reversal',
                    }
                    # 冷却检查（时间戳方式）
                    if self.loss_cooldown_until is not None:
                        now = datetime.now(TZ_ET)
                        if now < self.loss_cooldown_until:
                            if self.last_loss_dir == 'put':
                                remaining = int((self.loss_cooldown_until - now).total_seconds() / 60)
                                print(f"  ⏳ 冷却中(put方向)，剩余{remaining}分钟，跳过超涨回调信号")
                                return
                        else:
                            self.loss_cooldown_until = None
                    if self._should_skip_and_track(sig):
                        return
                    self.reversal_fired = True
                    self.daily_signals += 1
                    print(f"  🔄 衰竭反转做空! 从低点涨{rise_from_low*100:.1f}%")
                    self.current_signal = sig
                    self._add_event(f"🔄 衰竭反转做空! 涨{rise_from_low*100:.1f}%", "signal")
                    self._execute_trade(sig)

    def _check_longbridge_position(self):
        """检查长桥实际期权持仓张数（仅统计今日0DTE合约）- 带30秒缓存防限频"""
        now = time.time()
        # 缓存30秒，避免API限频(429002)
        if now - self._lb_pos_cache_time < 30:
            return self._lb_pos_cache
        try:
            stock_positions = self.trade_ctx.stock_positions()
            if not stock_positions or not hasattr(stock_positions, 'channels'):
                self._lb_pos_cache = 0
                self._lb_pos_cache_time = now
                return 0
            
            today_str = datetime.now(TZ_ET).strftime('%y%m%d')  # 今日到期日
            total_contracts = 0
            for channel in stock_positions.channels:
                if not hasattr(channel, 'positions'):
                    continue
                for pos in channel.positions:
                    symbol = str(getattr(pos, 'symbol', ''))
                    qty = int(getattr(pos, 'quantity', 0) or 0)
                    # 只统计今日到期的QQQ期权（精确匹配0DTE）
                    if qty > 0 and 'QQQ' in symbol and '.US' in symbol and today_str in symbol:
                        total_contracts += qty
                        print(f"  📊 长桥持仓: {symbol} x {qty}张")
            self._lb_pos_cache = total_contracts
            self._lb_pos_cache_time = now
            return total_contracts
        except Exception as e:
            print(f"  ⚠️ 检查长桥持仓异常: {e}")
            # 发送网络/API错误通知
            self._handle_error_with_notification(e, "查询持仓")
            return self._lb_pos_cache  # 异常时返回缓存值

    def _execute_trade(self, sig):
        """执行期权交易 - 增强版订单验证"""
        # ===== PUT每日上限3笔 =====
        if sig.get('dir') == 'put' and self.put_trades >= 3:
            print(f"  ⛔ PUT已达每日上限({self.put_trades}/3)，跳过")
            return
        # ===== 防重入锁：防止下单等待期间重复开仓 =====
        if self._should_skip_and_track(sig):
            return
        if self._trading_lock:
            print(f"  ⛔ 开仓锁定中，跳过")
            return
        self._trading_lock = True
        try:
            self._execute_trade_inner(sig)
        finally:
            self._trading_lock = False

    def _execute_trade_inner(self, sig):
        """执行期权交易（内部实现）"""
        # ===== 开仓前检查：已有持仓禁止重复开仓 =====
        try:
            existing = self._check_longbridge_position()
            if existing > 0:
                print(f"  ⛔ 已有持仓 {existing}张，禁止重复开仓")
                return
        except Exception as e:
            print(f"  ⚠️ 检查持仓失败: {e}，跳过本次开仓")
            return

        price = Decimal(str(sig['price']))  # 正股入场价

        # ===== 获取实际账户余额（自动识别货币，统一转USD）=====
        try:
            assets = self.trade_ctx.account_balance()
            total_cash = 0
            equity = 0
            buying_power = 0
            acct_currency = 'USD'  # 默认美元
            if assets:
                for asset in assets:
                    if hasattr(asset, 'total_cash') and asset.total_cash:
                        total_cash += float(asset.total_cash)
                    if hasattr(asset, 'cash') and asset.cash:
                        cash = float(asset.cash)
                    else:
                        cash = 0
                    if hasattr(asset, 'buy_power') and asset.buy_power:
                        buying_power += float(asset.buy_power)
                    if hasattr(asset, 'net_assets') and asset.net_assets:
                        equity += float(asset.net_assets)
                    if hasattr(asset, 'currency') and asset.currency:
                        acct_currency = str(asset.currency)
            # 根据实际货币决定是否转换
            if acct_currency == 'HKD':
                capital = total_cash / 7.8
                equity = equity / 7.8 if equity else capital
                buying_power = buying_power / 7.8 if buying_power else capital
                print(f"  💰 账户余额: HKD {total_cash:,.2f} → USD {capital:,.2f}")
            else:
                capital = total_cash
                equity = equity if equity else total_cash
                print(f"  💰 账户余额: USD {capital:,.2f}")
            self.actual_capital = capital if capital > 0 else self.cfg['capital']
            # 货币统一为 USD
            if acct_currency == 'HKD':
                cash_usd = total_cash / 7.8
            else:
                cash_usd = total_cash
            
            self.account_info = {
                'equity': equity,           # 已转 USD
                'cash': buying_power,       # 实际可用现金
                'buying_power': cash_usd,   # 总购买力（含杠杆）
            }
        except Exception as e:
            print(f"  ⚠️ 获取余额失败: {e}，使用默认资金: ${self.cfg['capital']:,}")
            capital = self.cfg['capital']
            buying_power = capital  # 默认购买力等于资金
            self.account_info = {'equity': capital, 'cash': capital, 'buying_power': capital}

        # ===== 生成期权合约代码 =====
        opt_symbol = get_option_symbol(float(price), sig['dir'], self.cfg['option_offset'])

        # ===== 获取期权当前价格 =====
        opt_price = None
        try:
            opt_quotes = self.quote_ctx.quote([opt_symbol])
            if opt_quotes and hasattr(opt_quotes[0], 'last_done') and opt_quotes[0].last_done > 0:
                opt_price = float(opt_quotes[0].last_done)
                print(f"  📊 期权价格: ${opt_price:.2f}")
        except Exception as e:
            print(f"  ⚠️ 获取期权价格失败: {e}")

        if opt_price is None or opt_price <= 0:
            print(f"  ⛔ 无法获取期权价格，放弃下单")
            return

        # ===== P0 #6 波动率调整仓位 =====
        # 先计算 volatility-based sizing multiplier
        vol_mult_factor = 1.0
        if self.cfg.get('vol_adjusted_sizing', True) and self.engine.atr > 0:
            base_atr = self.cfg.get('base_atr', 0.35) if 'base_atr' in self.cfg else 0.35
            vol_mult_factor = base_atr / self.engine.atr  # baseATR / currentATR
            vol_mult_factor = max(0.4, min(vol_mult_factor, 1.6))  # 封顶 0.4x~1.6x
            print(f"  📈 波动率调整: ATR=${self.engine.atr:.2f} vs base=${base_atr} → coef={vol_mult_factor:.2f}")

        # ===== P0 #9 时间风控（gamma risk tapering）=====
        now_et = datetime.now(TZ_ET)
        cur_min_et = now_et.hour * 60 + now_et.minute
        time_risk_mult = 1.0
        gamma_warning = False

        # 延长时间窗口(14:30-15:00,仅亏损时)仓位缩减
        if self._is_extension_window(cur_min_et) and self.daily_pnl <= 0:
            extension_pct = self.cfg.get('extension_order_pct', 5)
            normal_pct = self.cfg.get('order_pct', 8)
            ext_mult = extension_pct / normal_pct
            time_risk_mult *= ext_mult
            print(f"  🕐 反攻窗口(14:30-15:00): 仓位缩减({extension_pct}%, {ext_mult:.0%}倍)")

        if cur_min_et >= 945:  # 15:45 ET
            gamma_warning = True
            time_risk_mult = 0.0  # 禁止开新仓
            print(f"  🕐 尾端风控(15:45+ ET): gamma极高，禁止新仓")
        elif cur_min_et >= 930:  # 15:30 ET
            time_risk_mult = 0.5   # 仓位减半
            print(f"  🕐 尾端风控(15:30 ET): 仓位减半(剩余<30min)")

        # ===== 按资金百分比计算张数（动态仓位倍数）=====
        pos_mult = sig.get('pos_mult', 1.0)  # 震荡市0.4/中性0.5/趋势0.7

        # P0 #7 阶梯式熔断 → 进一步调整仓位
        circuit_level = sig.get('circuit_level', 0)
        if circuit_level == 1:   # warning: 仓位减半
            pos_mult *= 0.5
            print(f"  🛡️ 警告级熔断生效: 仓位减半")
        elif circuit_level == 2:  # conservative: 仓位降至25%
            pos_mult *= 0.25
            print(f"  🛡️ 保守级熔断生效: 仓位降至25%")

        # P1 #4 连亏强制降仓（连亏3笔以上仓位降至80%）
        if self.consecutive_losses >= 3:
            loss_penalty = 0.8
            print(f"  🛡 连亏{self.consecutive_losses}笔 → 仓位降至80%")
        else:
            loss_penalty = 1.0

        combined_mult = pos_mult * vol_mult_factor * time_risk_mult * loss_penalty
        min_option_price = self.cfg.get('min_full_size_option_price', 0.75)
        if opt_price < min_option_price:
            low_price_mult = self.cfg.get('low_option_price_mult', 0.35)
            combined_mult *= low_price_mult
            print(f"  🛡️ 低权利金风控: ${opt_price:.2f} < ${min_option_price:.2f}，仓位系数降至{low_price_mult:.0%}")
        # PUT uses a stricter standalone position cap; afternoon trades use half size.
        if sig['dir'] == 'put':
            effective_pct = min(self.cfg['order_pct'], self.cfg.get('put_order_pct', 3.0))
        else:
            effective_pct = self.cfg['order_pct']
        if cur_min_et >= 720:
            effective_pct *= 0.5
        order_amount = capital * effective_pct / 100 * combined_mult
        contracts = max(1, int(order_amount / (opt_price * self.cfg['contract_multiplier'])))
        max_contracts = int(self.cfg.get('max_contracts_per_trade', 400) or 400)
        if opt_price < min_option_price:
            max_contracts = min(max_contracts, int(self.cfg.get('max_low_price_contracts', 300) or 300))
        if cur_min_et >= 720:
            max_contracts = min(max_contracts, int(self.cfg.get('max_afternoon_contracts', 300) or 300))
        if contracts > max_contracts:
            print(f"  🛡️ 单笔张数上限: {contracts}张 → {max_contracts}张")
            contracts = max_contracts
        if gamma_warning:  # 15:45+ 直接禁止
            contracts = 0
        qty = contracts * self.cfg['contract_multiplier']
        print(f"  📊 下单: {contracts}张 × ${opt_price:.2f} × {self.cfg['contract_multiplier']}股 = ${order_amount:,.2f} (pos_mult={pos_mult:.2f}, vol_coef={vol_mult_factor:.2f}, time_coef={time_risk_mult:.2f})")

        if contracts <= 0:
            print(f"  ⛔ 尾端风控禁止开仓，放弃交易")
            return

        side = OrderSide.Buy  # 买入期权（Call看多/ Put看空，都是Buy开仓）

        try:
            resp = self.trade_ctx.submit_order(
                symbol=opt_symbol,
                order_type=OrderType.MO,
                side=side,
                submitted_quantity=Decimal(str(contracts)),  # 下单张数
                time_in_force=TimeInForceType.Day,
                outside_rth=OutsideRTH.AnyTime,
                remark=f"v6_opt_{sig['dir']}",
            )

            order_id = resp.order_id
            print(f"  📋 订单已提交: {order_id}")
            print(f"  📊 期权: {opt_symbol} | 张数: {contracts} | 方向: {sig['dir']}")
            
            # ===== 记录所有提交的订单（用于追踪）=====
            self._log_order(order_id, opt_symbol, sig['dir'], contracts, 'submitted')

            # ===== 增强版订单检测机制 =====
            order_filled = False
            order_status = None
            max_retries = 5  # 增加到5次重试
            retry_interval = 3  # 增加到3秒间隔
            executed_qty = 0
            executed_price = 0

            for attempt in range(max_retries):
                time.sleep(retry_interval)
                try:
                    # 查询订单状态 - 使用多种方式
                    order_info = None
                    
                    # 方式1: 查询所有今日订单，遍历查找
                    try:
                        all_orders = self.trade_ctx.today_orders()
                        print(f"  🔍 查询到 {len(all_orders)} 个今日订单")
                        for o in all_orders:
                            # 打印每个订单的ID用于调试
                            o_id = getattr(o, 'order_id', None)
                            if o_id:
                                print(f"    订单ID: {o_id} (类型: {type(o_id).__name__})")
                            # 比较时转换类型
                            if str(o_id) == str(order_id):
                                order_info = o
                                print(f"  ✅ 找到匹配订单!")
                                break
                    except Exception as e1:
                        print(f"  ⚠️ 查询今日订单失败: {e1}")
                    
                    if order_info:
                        # 获取订单状态
                        order_status = getattr(order_info, 'status', None)
                        executed_qty = float(getattr(order_info, 'executed_quantity', 0) or 0)
                        executed_price = float(getattr(order_info, 'executed_price', 0) or 0)
                        
                        # 🔧 调试：打印订单所有关键字段
                        print(f"  📋 订单详情: ID={order_id}, status={order_status}, exec_qty={executed_qty}, exec_price={executed_price}")
                        print(f"     订单字段: {[a for a in dir(order_info) if not a.startswith('_') and 'price' in a.lower() or 'done' in a.lower() or 'exec' in a.lower()]}")
                        
                        # 🔧 如果 executed_price 为 0 但订单已成交/部分成交，用 last_done 作为补充
                        if executed_price <= 0 and executed_qty > 0:
                            last_done = getattr(order_info, 'last_done', 0)
                            if last_done and float(last_done) > 0:
                                executed_price = float(last_done)
                                print(f"  ℹ️  使用 last_done 作为成交价: ${executed_price}")
                        
                        # 🔧 如果还是没有，尝试用订单的 price 字段（限价单价格）
                        if executed_price <= 0 and executed_qty > 0:
                            order_price = getattr(order_info, 'price', 0)
                            if order_price and float(order_price) > 0:
                                executed_price = float(order_price)
                                print(f"  ℹ️  使用订单 price 作为成交价: ${executed_price}")
                        
                        print(f"  📊 订单状态: {order_status} | 已成交: {executed_qty}张 @ ${executed_price}")
                        
                        # 检查是否已成交
                        if executed_qty >= contracts:
                            order_filled = True
                            self._log_order(order_id, opt_symbol, sig['dir'], contracts, 'filled', executed_qty, executed_price)
                            print(f"  ✅ 订单完全成交!")
                            break
                        elif executed_qty > 0:
                            # 部分成交
                            print(f"  ⚠️ 部分成交: {executed_qty}/{contracts}张")
                            if attempt == max_retries - 1:
                                # 最后一次重试，取消剩余订单
                                print(f"  ❌ 部分成交超时，取消剩余订单")
                                try:
                                    self.trade_ctx.cancel_order(order_id)
                                    self._log_order(order_id, opt_symbol, sig['dir'], contracts, 'cancelled_partial')
                                    print(f"  🚫 已取消剩余订单")
                                except Exception as cancel_err:
                                    print(f"  ⚠️ 取消订单失败: {cancel_err}")
                                    self._log_order(order_id, opt_symbol, sig['dir'], contracts, 'cancel_failed')
                                order_filled = executed_qty > 0  # 部分成交也算成功
                                if order_filled and executed_qty < contracts:
                                    original_contracts = contracts
                                    contracts = int(executed_qty)  # 修正：用实际成交数
                                    qty = contracts * self.cfg['contract_multiplier']
                                    print(f"  📝 部分成交修正: 合约数 {contracts}/{original_contracts}")
                                break
                        else:
                            # 未成交
                            print(f"  ⏳ 等待成交... ({attempt + 1}/{max_retries})")
                            if attempt == max_retries - 1:
                                # 最后一次重试仍未成交，取消订单
                                print(f"  ❌ 订单超时未成交，取消订单")
                                try:
                                    self.trade_ctx.cancel_order(order_id)
                                    self._log_order(order_id, opt_symbol, sig['dir'], contracts, 'cancelled_timeout')
                                    print(f"  🚫 已取消订单")
                                except Exception as cancel_err:
                                    print(f"  ⚠️ 取消订单失败: {cancel_err}")
                                    self._log_order(order_id, opt_symbol, sig['dir'], contracts, 'cancel_failed')
                                self._notify(
                                    f"⏰ 订单超时取消 {opt_symbol}",
                                    'system', event_type='cancel', symbol=opt_symbol,
                                )
                        if attempt == max_retries - 1:
                            print(f"  ❌ 无法查询订单状态，放弃交易")
                            self._log_order(order_id, opt_symbol, sig['dir'], contracts, 'query_failed')
                            return

                except Exception as query_err:
                    print(f"  ⚠️ 查询订单状态失败: {query_err}")
                    if attempt == max_retries - 1:
                        print(f"  ❌ 查询失败次数过多，放弃交易")
                        self._log_order(order_id, opt_symbol, sig['dir'], contracts, 'query_error')
                        return

            if not order_filled:
                print(f"  ❌ 订单未成交，放弃交易")
                return

            # ===== 订单成交，建立持仓 =====
            self.position = {
                'order_id': order_id,
                'dir': sig['dir'],
                'entry_price': float(price),       # 正股入场价
                'opt_symbol': opt_symbol,           # 期权合约代码
                'entry_opt_price': float(executed_price) if executed_price > 0 else None,  # 期权入场价
                'sl_pct': sig.get('sl_pct', self.cfg['sl']),  # 动态止损百分比
                'tp_pct': self.cfg['tp'],           # 止盈百分比（旧逻辑保留）
                'contracts': contracts,             # 张数
                'quantity': qty,                    # 总股数
                'entry_time': datetime.now(TZ_ET),
                'entry_bar': len(self.one_min_candles),
                'reason': sig['reason'],
                'engine': sig.get('engine', ''),
                'display_engine': sig.get('display_engine') or display_signal_name(sig.get('engine', '')),
                'shadow_live_order': bool(sig.get('shadow_live_order', False)),
                'shadow_rejection_reason': sig.get('shadow_rejection_reason', ''),
                'max_pnl_pct': 0,
                'half_closed': False,  # 动态止盈：是否已平仓一半
                'half_closed_max_pct': 0.0,  # 半仓后的峰值（用于跟踪止盈）
                'order_status': 'filled',  # 订单状态
                # v6.3 动态参数（供_check_position使用）
                'tp_partial_pct': sig.get('tp_partial_pct', 1.00),  # 动态止盈阈值
                'timeout_bars': sig.get('timeout_bars', 10),        # 动态超时
                'regime': sig.get('regime', 'neutral'),             # 市场状态
                'day_market_regime': sig.get('day_market_regime', ''),
                'day_market_label': sig.get('day_market_label', ''),
                'day_market_direction': sig.get('day_market_direction', ''),
                # 正股跟踪止损
                'stock_peak': float(price),  # 正股最高价(Call)/最低价(Put)
                'peak_opt_pnl': 0,           # 期权峰值盈利(用于半仓跟踪)
                # 市场上下文（复盘用）
                'atr_at_entry': round(self.engine.atr, 4) if hasattr(self.engine, 'atr') else 0,
                'macd_hist_entry': round(self.engine.macd_hist, 4) if hasattr(self.engine, 'macd_hist') else 0,
                'vwap_entry': round(self.engine.vwap, 2) if hasattr(self.engine, 'vwap') else 0,
                'sma20_entry': round(self.engine.state.get('sma20', {}).get('sma20', 0), 2) if isinstance(self.engine.state.get('sma20', {}).get('sma20'), (int, float)) else 0,
            }
            self._subscribe_position_quote(opt_symbol)
            self.trades_today.append(self.position.copy())
            probe_source = 'shadow_live' if sig.get('shadow_live_order') else 'live'
            self._start_signal_probe(
                sig, opt_symbol, contracts, float(price), len(self.one_min_candles),
                source=probe_source,
                rejection_reason=sig.get('shadow_rejection_reason', ''),
            )
            self._add_event(f"📈 开仓: {opt_symbol} x{contracts}张 @${executed_price:.2f}", "trade")
            self.current_signal = None  # 已开仓，清除信号
            
            # 更新v7 Dashboard
            try:
                import dashboard_v7
                dashboard_v7.add_trade({
                    'timestamp': datetime.now(TZ_ET).strftime('%H:%M:%S'),
                    'engine': sig.get('display_engine') or display_signal_name(sig.get('engine', 'v6.5')),
                    'direction': sig['dir'],
                    'strength': sig.get('strength', 0),
                    'entry_price': float(price),
                    'reason': sig['reason'],
                    'day_market_regime': sig.get('day_market_regime', ''),
                    'day_market_label': sig.get('day_market_label', ''),
                    'day_market_direction': sig.get('day_market_direction', ''),
                })
                dashboard_v7.update_position(self.position)
                dashboard_v7.update_trades(self.trades_today)
                dashboard_v7.add_event(f"📈 开仓: {opt_symbol} x{contracts}张", "trade")
            except Exception:
                pass
            # 如果入场价未获取，尝试获取
            if self.position['entry_opt_price'] is None:
                time.sleep(1)
                try:
                    opt_q = self.quote_ctx.quote([opt_symbol])
                    if opt_q and opt_q[0].last_done > 0:
                        self.position['entry_opt_price'] = float(opt_q[0].last_done)
                        # 同步更新 trades_today 中的记录
                        self.trades_today[-1]['entry_opt_price'] = self.position['entry_opt_price']
                        print(f"  💹 期权入场价: ${self.position['entry_opt_price']:.2f}")
                except Exception as e:
                    print(f"  ⚠️ 获取期权入场价失败: {e}，将用BS估算")

            d = "🟢做多" if sig['dir'] == 'call' else "🔴做空"
            print(f"\n  {'='*50}")
            print(f"  🎯 {d}信号! (第{self.daily_signals}个)")
            print(f"  📍 原因: {sig['reason']}")
            print(f"  📈 期权: {opt_symbol}")
            print(f"  💰 正股入场: ${float(price):.2f}")
            print(f"  💹 期权入场: ${self.position.get('entry_opt_price', 0):.2f}")
            print(f"  📊 数量: {contracts}张 ({qty}股)")
            print(f"  📋 订单: {order_id}")
            print(f"  ✅ 状态: 已成交")
            print(f"  {'='*50}\n")

            self._save_state()
            self._notify(
                f"🎯 开仓 {opt_symbol}",
                'entry',
                sig=sig,
                opt_symbol=opt_symbol,
                price=float(price),
                contracts=int(contracts),
                qty=int(qty),
                order_id=order_id,
            )

            # ===== 同步验证长桥持仓 =====
            self._verify_position(opt_symbol, contracts)

        except Exception as e:
            print(f"  ❌ 下单失败: {e}")
            import traceback
            traceback.print_exc()
            # 发送网络/API错误通知
            self._handle_error_with_notification(e, "提交订单")

    def _log_order(self, order_id, opt_symbol, direction, contracts, status, executed_qty=0, executed_price=0):
        """记录订单日志（用于追踪所有提交的订单）"""
        try:
            log_dir = os.path.join(_app_dir(), 'logs')
            os.makedirs(log_dir, exist_ok=True)
            today = datetime.now(TZ_ET).strftime('%Y-%m-%d')
            log_file = os.path.join(log_dir, f'orders_{today}.log')
            
            timestamp = datetime.now(TZ_ET).strftime('%Y-%m-%d %H:%M:%S')
            log_entry = f"{timestamp} | {order_id} | {opt_symbol} | {direction} | {contracts}张 | {status}"
            if executed_qty > 0:
                log_entry += f" | 成交:{executed_qty}张 @{executed_price}"
            
            with open(log_file, 'a', encoding='utf-8') as f:
                f.write(log_entry + '\n')
        except Exception as e:
            print(f"  ⚠️ 订单日志写入失败: {e}")

    def _verify_position(self, opt_symbol, expected_qty):
        """同步验证长桥账户实际持仓"""
        print(f"\n  🔍 验证长桥持仓...")
        try:
            time.sleep(2)  # 等待持仓更新
            
            # 查询股票持仓
            stock_positions = self.trade_ctx.stock_positions()
            if stock_positions and hasattr(stock_positions, 'channels'):
                for channel in stock_positions.channels:
                    if hasattr(channel, 'positions'):
                        for pos in channel.positions:
                            if hasattr(pos, 'symbol') and pos.symbol == opt_symbol:
                                actual_qty = int(getattr(pos, 'quantity', 0) or 0)
                                done_qty = actual_qty
                                print(f"  📊 找到期权持仓: {pos.symbol}")
                                print(f"  📊 持仓数量: {actual_qty} | 已成交: {done_qty}")
                                
                                if done_qty >= expected_qty:
                                    print(f"  ✅ 持仓验证通过!")
                                    return True
                                else:
                                    print(f"  ⚠️ 持仓不足: 期望{expected_qty}, 实际{done_qty}")
                                    # 更新实际持仓数量
                                    if self.position:
                                        self.position['contracts'] = done_qty
                                        self.position['quantity'] = done_qty * self.cfg['contract_multiplier']
                                        print(f"  📝 已更新持仓数量为: {done_qty}张")
                                    # 发送持仓不一致通知
                                    self._notify(
                                        "⚠️ 持仓数量不一致",
                                        'position_anomaly',
                                        anomaly_type='mismatch',
                                        details=f"期权 <code>{opt_symbol}</code>\n期望 <b>{expected_qty}</b>张\n实际 <b>{done_qty}</b>张",
                                    )
                                    return True
            
            # 如果没找到，尝试查询所有持仓
            print(f"  ⚠️ 未在持仓中找到 {opt_symbol}，查询所有持仓...")
            if stock_positions and hasattr(stock_positions, 'channels'):
                for channel in stock_positions.channels:
                    if hasattr(channel, 'positions'):
                        for pos in channel.positions:
                            if hasattr(pos, 'symbol'):
                                print(f"  📊 持仓: {pos.symbol} x {getattr(pos, 'quantity', 0)}")
            
            print(f"  ⚠️ 持仓验证未找到匹配项，但订单已确认成交")
            # 发送持仓丢失通知
            self._notify(
                "❌ 持仓验证失败",
                'position_anomaly',
                anomaly_type='verify_failed',
                details=f"期权 <code>{opt_symbol}</code>\n期望 <b>{expected_qty}</b>张\n长桥未找到匹配持仓",
            )
            return False
            
        except Exception as e:
            print(f"  ⚠️ 持仓验证失败: {e}")
            print(f"  📝 继续执行（订单已确认成交）")
            # 发送持仓验证失败通知
            self._notify(
                "❗ 持仓验证异常",
                'position_anomaly',
                anomaly_type='verify_failed',
                details=f"期权 <code>{opt_symbol}</code>\n错误 <code>{str(e)[:80]}</code>",
            )
            return False

    def _sync_position_from_longbridge(self):
        """从长桥同步持仓到内部状态"""
        try:
            stock_positions = self.trade_ctx.stock_positions()
            if not stock_positions or not hasattr(stock_positions, 'channels'):
                return
            
            for channel in stock_positions.channels:
                if not hasattr(channel, 'positions'):
                    continue
                for pos in channel.positions:
                    symbol = getattr(pos, 'symbol', '')
                    qty = int(getattr(pos, 'quantity', 0) or 0)
                    cost = float(getattr(pos, 'cost_price', 0) or 0)
                    
                    # 只处理QQQ期权持仓
                    if qty > 0 and 'QQQ' in str(symbol) and '.US' in str(symbol) and ('C' in str(symbol) or 'P' in str(symbol)):
                        # 获取当前价格
                        try:
                            opt_quotes = self.quote_ctx.quote([symbol])
                            if opt_quotes and opt_quotes[0].last_done > 0:
                                current_price = float(opt_quotes[0].last_done)
                            else:
                                current_price = cost
                        except:
                            current_price = cost
                        
                        # 计算盈亏
                        pnl_pct = (current_price - cost) / cost * 100 if cost > 0 else 0
                        stock_entry = self._current_stock_price()
                        stock_exit_enabled = stock_entry > 100
                        
                        # 恢复内部持仓状态
                        self.position = {
                            'order_id': 'synced',
                            'dir': 'call' if 'C' in str(symbol) else 'put',
                            'entry_opt_price': cost,          # 期权成本
                            'opt_symbol': symbol,
                            'entry_price': stock_entry if stock_exit_enabled else 0,
                            'sl_pct': self.cfg['sl'],
                            'tp_pct': self.cfg['tp'],
                            'contracts': qty,
                            'quantity': qty * self.cfg['contract_multiplier'],
                'entry_time': datetime.now(TZ_ET),
                            'entry_bar': len(self.one_min_candles),
                            'reason': '长桥持仓同步',
                            'max_pnl_pct': pnl_pct,
                            'half_closed': False,
                            'half_closed_max_pct': 0.0,
                            'stock_peak': stock_entry if stock_exit_enabled else 0,
                            'stock_exit_enabled': stock_exit_enabled,
                            'order_status': 'synced',
                        }
                        print(f"  🔄 从长桥同步持仓: {symbol} x {qty}张, 成本${cost:.2f}, 盈亏{pnl_pct:+.1f}%")
                        self._subscribe_position_quote(symbol)
                        self._save_state()
                        return
        except Exception as e:
            print(f"  ⚠️ 长桥持仓同步失败: {e}")

    def _check_position(self, opt_price=None, current_stock=None, triggered_by_push=False):
        """检查持仓状态；持仓期权报价推送时会实时调用，主循环仅作兜底"""
        if self._position_check_lock:
            return
        self._position_check_lock = True
        try:
            return self._check_position_impl(opt_price, current_stock, triggered_by_push)
        finally:
            self._position_check_lock = False

    def _check_position_impl(self, opt_price=None, current_stock=None, triggered_by_push=False):
        """持仓风控实现"""
        # ===== 0DTE 强制收盘平仓（16:00 ET）=====
        now_et = datetime.now(TZ_ET)
        if now_et.hour >= 16:
            if self.position:
                self._close_position("⏰ 16:00 ET 强制收盘平仓")
            # 发送日终总结（只发送一次）
            if not self._daily_summary_sent:
                self._daily_summary_sent = True
                try:
                    self._notify("📊 日终总结", msg_type='daily_summary')
                except Exception as e:
                    print(f"⚠️ 日终总结发送失败: {e}")
            return

        # 如果没有内部持仓，尝试从长桥同步
        if not self.position:
            self._sync_position_from_longbridge()
            if not self.position:
                # 即使没有持仓，也要检查长桥是否有持仓（防止手动平仓后系统不知道）
                # 使用缓存避免API限频
                lb_count = self._check_longbridge_position()
                if lb_count == 0:
                    self._unsubscribe_quotes([None])
                    self.position = None
                return

        pos = self.position
        entry_stock = pos['entry_price']  # 正股入场价
        stock_entry_valid = pos.get('stock_exit_enabled', True) and self._is_valid_stock_entry_price(entry_stock, current_stock)

        # ===== 定期验证长桥实际持仓（每60秒一次）=====
        current_time = time.time()
        if current_time - self._last_position_verify >= 60:
            self._last_position_verify = current_time
            self._sync_verify_position()

        # 获取正股当前价格
        if current_stock is None:
            current_stock = self.latest_quote_prices.get(self.cfg['symbol']) or self.current_price or None
        if current_stock is None:
            try:
                stock_quotes = self.quote_ctx.quote([self.cfg['symbol']])
                if not stock_quotes:
                    return
                current_stock = float(stock_quotes[0].last_done)
            except:
                return
        stock_entry_valid = pos.get('stock_exit_enabled', True) and self._is_valid_stock_entry_price(entry_stock, current_stock)

        # 获取期权当前价格（尝试获取实时价）
        if opt_price is None:
            opt_price = self.latest_quote_prices.get(pos['opt_symbol'])
        if opt_price is None and not triggered_by_push:
            try:
                opt_quotes = self.quote_ctx.quote([pos['opt_symbol']])
                if opt_quotes and hasattr(opt_quotes[0], 'last_done') and opt_quotes[0].last_done > 0:
                    opt_price = float(opt_quotes[0].last_done)
            except:
                pass

        if opt_price is None and not stock_entry_valid:
            opt_price = pos.get('entry_opt_price') or 1.0

        # 如果获取不到期权价格，用BS估算
        if opt_price is None:
            try:
                from scipy.stats import norm
                import numpy as np
                # 用实际剩余交易时间（美东9:30-16:00 = 6.5小时）
                now_et = datetime.now(TZ_ET)
                close_et = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
                remaining_seconds = max((close_et - now_et).total_seconds(), 60)
                T = remaining_seconds / (6.5 * 3600 * 252)  # 年化剩余时间
                r = 0.05
                sigma = 0.25  # 隐含波动率估算
                K = entry_stock + (2.0 if pos['dir'] == 'call' else -2.0)
                if pos['dir'] == 'call':
                    d1 = (np.log(current_stock/K) + (r + sigma**2/2)*T) / (sigma*np.sqrt(T))
                    d2 = d1 - sigma*np.sqrt(T)
                    opt_price = current_stock * norm.cdf(d1) - K * np.exp(-r*T) * norm.cdf(d2)
                else:
                    d1 = (np.log(current_stock/K) + (r + sigma**2/2)*T) / (sigma*np.sqrt(T))
                    d2 = d1 - sigma*np.sqrt(T)
                    opt_price = K * np.exp(-r*T) * norm.cdf(-d2) - current_stock * norm.cdf(-d1)
            except:
                # 备用：简单杠杆估算
                opt_price = abs(current_stock - entry_stock) * 10 + 0.5

        entry_opt = pos.get('entry_opt_price') or opt_price or 1.0
        if entry_opt <= 0:
            entry_opt = 1.0

        # 计算期权盈亏百分比
        pnl_pct = (opt_price - entry_opt) / entry_opt * 100
        pos['max_pnl_pct'] = max(pos['max_pnl_pct'], pnl_pct)
        # 半仓后峰值跟踪
        if pos['half_closed']:
            pos['half_closed_max_pct'] = max(pos.get('half_closed_max_pct', 0), pnl_pct)

        # ===== v6.5 正股跟踪止损：更新正股峰值 =====
        if stock_entry_valid and pos['dir'] == 'call':
            pos['stock_peak'] = max(pos.get('stock_peak', entry_stock), current_stock)
        elif stock_entry_valid:  # put
            pos['stock_peak'] = min(pos.get('stock_peak', entry_stock), current_stock)

        stock_pnl = (current_stock - entry_stock) / entry_stock if stock_entry_valid else 0
        if pos['dir'] == 'put':
            stock_pnl = -stock_pnl
        stock_pnl_pct = stock_pnl * 100
        pos['max_stock_pnl_pct'] = max(pos.get('max_stock_pnl_pct', 0), stock_pnl_pct)

        # 持仓K线数
        bars_held = len(self.one_min_candles) - pos['entry_bar']
        signal_name_for_exit = self._position_signal_name(pos)

        # ===== v6.3 动态退出条件（使用regime参数）=====
        ex = None
        sl_pct = pos.get('sl_pct', self.cfg['sl']) * 100    # 动态止损（震荡30%/趋势25%）
        tp_partial = pos.get('tp_partial_pct', 1.00) * 100  # 动态止盈（震荡50%/趋势100%）
        tp_trail_drop = self.cfg['tp_trail_drop'] * 100  # 30%
        
        # 优化6: 开盘入场的仓位，止损放宽到35%（开盘波动大）
        entry_bar = pos.get('entry_bar', 0)
        if entry_bar > 0 and len(self.one_min_candles) > 0:
            # 计算入场时的ET分钟数
            entry_candle_idx = min(entry_bar, len(self.one_min_candles) - 1)
            # 简化：如果持仓在开盘30分钟内，放宽止损
            if (
                bars_held <= 30
                and sl_pct < 35
                and not (
                    pos.get('shadow_live_order')
                    and self.cfg.get('shadow_live_disable_open_stop_widen', True)
                )
            ):
                sl_pct = 35.0

        # --- 1. 止损（期权价格，最高优先）---
        if pnl_pct <= -sl_pct:
            ex = f"止损({pnl_pct:.1f}%≤-{sl_pct:.0f}%)"

        # --- 1.2 正股风控（回测v6.3口径，降低期权报价噪声影响）---
        if not ex and not pos.get('half_closed'):
            fast_bars = int(self.cfg.get('fast_fail_bars', 5) or 5)
            if bars_held >= fast_bars and stock_entry_valid:
                if signal_name_for_exit == 'VWAP_Breakout':
                    stock_stop = self.cfg.get('vwap_fast_stock_stop_pct', 0.0006) * 100
                    opt_stop = self.cfg.get('vwap_fast_option_stop_pct', 18)
                    if stock_pnl_pct <= -stock_stop or pnl_pct <= -opt_stop:
                        ex = f"VWAP快退({bars_held}min, 正股{stock_pnl_pct:.2f}%, 期权{pnl_pct:.1f}%)"
                elif signal_name_for_exit == 'Kline_Pattern':
                    stock_stop = self.cfg.get('kline_fast_stock_stop_pct', 0.0005) * 100
                    opt_stop = self.cfg.get('kline_fast_option_stop_pct', 12)
                    if stock_pnl_pct <= -stock_stop and pnl_pct <= -opt_stop:
                        ex = f"Kline快退({bars_held}min, 正股{stock_pnl_pct:.2f}%, 期权{pnl_pct:.1f}%)"

        if not ex and not pos.get('half_closed'):
            quick_activate = self.cfg.get('quick_trail_activate_pct', 15)
            quick_drop = self.cfg.get('quick_trail_drop_pct', 8)
            if self._is_trend_aligned_position(pos):
                quick_activate = self.cfg.get('trend_quick_trail_activate_pct', quick_activate)
                quick_drop = self.cfg.get('trend_quick_trail_drop_pct', quick_drop)
            floor_activate = self.cfg.get('profit_floor_activate_pct', 20)
            floor_pct = self.cfg.get('profit_floor_pct', 8)
            if pos.get('max_pnl_pct', 0) >= floor_activate and pnl_pct <= floor_pct:
                ex = f"盈利保护({pos.get('max_pnl_pct', 0):.1f}%→{pnl_pct:.1f}%)"
            elif pos.get('max_pnl_pct', 0) >= quick_activate:
                drawdown = pos.get('max_pnl_pct', 0) - pnl_pct
                if drawdown >= quick_drop:
                    ex = f"快速移动止盈({pos.get('max_pnl_pct', 0):.1f}%→{pnl_pct:.1f}%)"

        if not ex and self.cfg.get('stock_exit_enabled', True) and stock_entry_valid:
            stock_sl = self.cfg.get('stock_sl_pct', 0.0025)
            stock_tp = self.cfg.get('stock_tp_pct', 0.0040)
            stock_trail_activate = self.cfg.get('stock_trail_activate', 0.0030)
            stock_trail_drop = self.cfg.get('stock_trail_drop', 0.0015)
            pa_state = self.price_action.market_state(self.one_min_candles)
            brooks_trend_active = (
                self.cfg.get('brooks_priority_mode', True)
                and self.cfg.get('brooks_trend_skip_fixed_stock_tp', True)
                and pa_state.get('direction') == pos['dir']
                and stock_pnl > 0
            )

            if stock_pnl <= -stock_sl:
                ex = f"正股止损({stock_pnl_pct:.2f}%≤-{stock_sl*100:.2f}%)"
            elif stock_pnl >= stock_tp and not brooks_trend_active:
                ex = f"正股止盈({stock_pnl_pct:.2f}%≥{stock_tp*100:.2f}%)"
            elif pos.get('max_stock_pnl_pct', 0) >= stock_trail_activate * 100:
                stock_pullback = (pos.get('max_stock_pnl_pct', 0) - stock_pnl_pct) / 100
                if stock_pullback >= stock_trail_drop:
                    ex = f"正股跟踪止盈(峰值{pos.get('max_stock_pnl_pct', 0):.2f}%→{stock_pnl_pct:.2f}%,回撤{stock_pullback*100:.2f}%)"

        # --- 1.5 PUT时间止损：3分钟不盈利就平 ---
        put_time_stop_bars = int(self.cfg.get('put_time_stop_bars', 0) or 0)
        if not ex and put_time_stop_bars > 0 and pos['dir'] == 'put' and not pos.get('half_closed'):
            if bars_held >= put_time_stop_bars and pnl_pct <= 0:
                ex = f"PUT时间止损({bars_held}min无盈利)"

        # --- 2. 分阶段超时（v6.3: 使用动态timeout_bars）---
        if not ex and not pos['half_closed']:
            timeout_profile = self._timeout_profile(pos)
            signal_name = timeout_profile['signal']
            s2_bars = timeout_profile['stage']
            s2_min = timeout_profile['min_profit']
            s3_bars = timeout_profile['hard']

            # P1 #3 SMA5动量延长超时：盈利+正股未破SMA5 → 趋势信号放宽到15分钟
            if pnl_pct > 0 and len(self.close_history) >= 5:
                sma5 = sum(self.close_history[-5:]) / 5
                if (pos['dir'] == 'call' and current_stock > sma5) or \
                   (pos['dir'] == 'put' and current_stock < sma5):
                    s3_bars = max(s3_bars, self.cfg.get('trend_extend_timeout_bars', 15))

            pa_state = self.price_action.market_state(self.one_min_candles)
            if pnl_pct > 0 and pa_state.get('direction') == pos['dir']:
                pa_extend = int(self.cfg.get('price_action_trend_extend_timeout_bars', 20) or 20)
                if pa_extend > s3_bars:
                    s3_bars = pa_extend
                    if self.cfg.get('debug', False):
                        print(f"  📈 Always-In {pos['dir']} 延长持仓至 {s3_bars} 分钟")

            if bars_held >= s3_bars:
                ex = f"硬超时({signal_name},{s3_bars}分钟)"
            elif bars_held >= s2_bars and pnl_pct < s2_min:
                ex = f"阶段超时({signal_name},{s2_bars}min盈利{pnl_pct:.1f}%<{s2_min:.0f}%)"

        # --- 3. 动态止盈：盈利≥150%平仓一半 ---
        if not ex and not pos['half_closed'] and pnl_pct >= tp_partial:
            self._close_partial(f"盈利{tp_partial:.0f}%平仓一半")
            return

        # --- 4. 半仓后：正股跟踪止损（替代期权峰值回撤，更稳定）---
        if not ex and pos['half_closed'] and stock_entry_valid:
            stock_trail = self.cfg.get('stock_trail_pct', 0.003)
            peak = pos.get('stock_peak', entry_stock)
            if pos['dir'] == 'call' and peak > entry_stock:
                pullback = (peak - current_stock) / peak
                if pullback >= stock_trail:
                    ex = f"正股跟踪止损(高点${peak:.2f}→${current_stock:.2f},回撤{pullback*100:.2f}%)"
            elif pos['dir'] == 'put' and peak < entry_stock:
                pullback = (current_stock - peak) / peak if peak > 0 else 0
                if pullback >= stock_trail:
                    ex = f"正股跟踪止损(低点${peak:.2f}→${current_stock:.2f},回撤{pullback*100:.2f}%)"

        # --- 5. 半仓后：期权峰值回撤30%全平（备用，正股跟踪优先）---
        if not ex and pos['half_closed']:
            peak_pnl = pos.get('half_closed_max_pct', 0)
            if peak_pnl >= tp_partial:
                drawdown = peak_pnl - pnl_pct
                if drawdown >= tp_trail_drop:
                    ex = f"半仓跟踪止盈(峰值{peak_pnl:.0f}%→{pnl_pct:.1f}%,回撤{drawdown:.0f}%)"

        # --- 6. 半仓后超时（动态timeout_bars）---
        if not ex and pos['half_closed']:
            timeout_profile = self._timeout_profile(pos)
            s3_bars = timeout_profile['hard']
            signal_name = timeout_profile['signal']
            if len(self.close_history) >= 5:
                sma5 = sum(self.close_history[-5:]) / 5
                if (pos['dir'] == 'call' and current_stock > sma5) or \
                   (pos['dir'] == 'put' and current_stock < sma5):
                    s3_bars = max(s3_bars, self.cfg.get('trend_extend_timeout_bars', 15))
            if bars_held >= s3_bars:
                ex = f"硬超时({signal_name},{s3_bars}分钟)"

        if ex:
            self._close_position(ex)
            return

        # 每5根K线打印一次持仓状态
        if bars_held > 0 and bars_held % 5 == 0 and pos.get('_last_status_bar') != bars_held:
            pos['_last_status_bar'] = bars_held
            d = "🟢" if pnl_pct > 0 else "🔴" if pnl_pct < 0 else "⚪"
            peak = pos.get('stock_peak', entry_stock)
            trail_dist = abs(current_stock - peak) / peak * 100 if peak > 0 else 0
            print(f"  {d} 期权持仓 | 正股${current_stock:.2f}(峰${peak:.2f}距{trail_dist:.2f}%) | 期权${opt_price:.2f} | "
                  f"盈亏: {pnl_pct:+.1f}% | 最大: {pos['max_pnl_pct']:.1f}% | 持仓: {bars_held}min")

    def _sync_verify_position(self):
        """同步验证长桥实际持仓与内部持仓是否一致"""
        if not self.position:
            return
        
        pos = self.position
        opt_symbol = pos['opt_symbol']
        expected_qty = pos['contracts']
        
        try:
            stock_positions = self.trade_ctx.stock_positions()
            if stock_positions and hasattr(stock_positions, 'channels'):
                for channel in stock_positions.channels:
                    if hasattr(channel, 'positions'):
                        for p in channel.positions:
                            if hasattr(p, 'symbol') and p.symbol == opt_symbol:
                                actual_qty = int(getattr(p, 'quantity', 0) or 0)
                                if actual_qty != expected_qty:
                                    print(f"  ⚠️ 持仓不一致! 内部:{expected_qty}张, 长桥:{actual_qty}张")
                                    # 更新为实际数量
                                    pos['contracts'] = actual_qty
                                    pos['quantity'] = actual_qty * self.cfg['contract_multiplier']
                                    print(f"  📝 已同步持仓数量为: {actual_qty}张")
                                    
                                    # 如果实际持仓为0，说明被强平或出错
                                    if actual_qty == 0:
                                        print(f"  ❌ 持仓已清空! 清除内部持仓")
                                        self._unsubscribe_quotes([pos.get('opt_symbol')])
                                        self.position = None
                                        self._save_state()
                                        # 发送持仓被清空通知
                                        self._notify(
                                            "🔴 持仓被清空",
                                            'position_anomaly',
                                            anomaly_type='cleared',
                                            details=f"期权 <code>{opt_symbol}</code>\n系统持仓已被清空\n可能是被强平或异常操作",
                                        )
                                    else:
                                        # 发送持仓不一致通知
                                        self._notify(
                                            "⚠️ 持仓数量不一致",
                                            'position_anomaly',
                                            anomaly_type='mismatch',
                                            details=f"期权 <code>{opt_symbol}</code>\n内部 <b>{expected_qty}</b>张\n长桥 <b>{actual_qty}</b>张",
                                        )
                                    return
                                else:
                                    # 数量一致，持仓正常
                                    self._missing_position_count = 0  # 重置未找到计数
                                    return
                # 如果遍历完没找到
                # ⚠️ 不要立即清空持仓！可能是网络延迟或持仓尚未更新
                # 记录警告，下次验证时再检查
                print(f"  ⚠️ 长桥未找到 {opt_symbol} 持仓（可能是网络延迟，等待下次验证）")
                if not hasattr(self, '_missing_position_count'):
                    self._missing_position_count = 0
                self._missing_position_count += 1
                # 连续3次（3分钟）都找不到才清空持仓
                if self._missing_position_count >= 3:
                    print(f"  ❌ 连续{self._missing_position_count}次未找到持仓，清除内部持仓")
                    self._unsubscribe_quotes([pos.get('opt_symbol')])
                    self.position = None
                    self._save_state()
                    self._missing_position_count = 0
                    print(f"  📝 已清除内部持仓")
                    # 发送持仓丢失通知
                    self._notify(
                        "❌ 持仓丢失",
                        'position_anomaly',
                        anomaly_type='missing',
                        details=f"期权 <code>{opt_symbol}</code>\n连续{3}次未找到持仓\n已自动清除内部持仓",
                    )
                else:
                    print(f"  ⏳ 第{self._missing_position_count}/3次未找到，继续保留持仓")
        except Exception as e:
            print(f"  ⚠️ 持仓同步验证失败: {e}")

    def _close_partial(self, reason):
        """平仓一半仓位（动态止盈用）"""
        pos = self.position
        if not pos or pos['contracts'] <= 1:
            return

        half = pos['contracts'] // 2
        if half <= 0:
            return

        side = OrderSide.Sell  # 卖出平仓

        try:
            resp = self.trade_ctx.submit_order(
                symbol=pos['opt_symbol'],
                order_type=OrderType.MO,
                side=side,
                submitted_quantity=Decimal(str(half)),
                time_in_force=TimeInForceType.Day,
                outside_rth=OutsideRTH.AnyTime,
                remark=f"v6_partial_close",
            )

            order_id = resp.order_id
            print(f"  📋 半仓平仓订单已提交: {order_id}")

            partial_filled = False
            executed_qty = 0
            executed_price = 0
            max_retries = 5
            retry_interval = 3

            for attempt in range(max_retries):
                time.sleep(retry_interval)
                try:
                    order_info = None
                    try:
                        orders = self.trade_ctx.today_orders(order_id=order_id)
                        if orders:
                            order_info = orders[0]
                    except Exception as e1:
                        print(f"  ⚠️ 半仓查询失败(方式1): {e1}")

                    if not order_info:
                        try:
                            all_orders = self.trade_ctx.today_orders()
                            for o in all_orders:
                                if str(getattr(o, 'order_id', None)) == str(order_id):
                                    order_info = o
                                    break
                        except Exception as e2:
                            print(f"  ⚠️ 半仓查询失败(方式2): {e2}")

                    if not order_info:
                        print(f"  ⚠️ 未找到半仓订单: {order_id} ({attempt + 1}/{max_retries})")
                        continue

                    order_status = getattr(order_info, 'status', None)
                    executed_qty = float(getattr(order_info, 'executed_quantity', 0) or 0)
                    executed_price = float(getattr(order_info, 'executed_price', 0) or 0)
                    print(f"  📊 半仓状态: {order_status} | 已成交: {executed_qty}张 @ ${executed_price}")

                    if str(order_status) == 'OrderStatus.Rejected':
                        print(f"  ❌ 半仓平仓订单被拒，保留原持仓")
                        self._notify(
                            f"❌ 半仓平仓被拒 {pos['opt_symbol']}",
                            'position_anomaly',
                            anomaly_type='partial_rejected',
                            details=f"订单 <code>{order_id}</code>\n持仓保留，等待下次风控重试",
                        )
                        self._save_state()
                        return

                    if executed_qty > 0:
                        partial_filled = True
                        break

                    if attempt == max_retries - 1:
                        try:
                            self.trade_ctx.cancel_order(order_id)
                            print(f"  🚫 半仓订单超时未成交，已尝试取消")
                        except Exception as cancel_err:
                            print(f"  ⚠️ 半仓订单取消失败: {cancel_err}")
                except Exception as query_err:
                    print(f"  ⚠️ 半仓查询异常: {query_err}")

            if not partial_filled:
                print(f"  ⏳ 半仓平仓未成交，保留原持仓")
                self._save_state()
                return

            half = min(int(executed_qty), half)

            # 获取平仓时的期权价格
            exit_opt = float(executed_price) if executed_price > 0 else 0
            if exit_opt <= 0:
                try:
                    opt_quotes = self.quote_ctx.quote([pos['opt_symbol']])
                    if opt_quotes and hasattr(opt_quotes[0], 'last_done') and opt_quotes[0].last_done > 0:
                        exit_opt = float(opt_quotes[0].last_done)
                    else:
                        exit_opt = pos.get('entry_opt_price') or 1.0
                except:
                    exit_opt = pos.get('entry_opt_price') or 1.0

            entry_opt = pos.get('entry_opt_price') or exit_opt
            if entry_opt <= 0:
                entry_opt = 1.0

            pnl_pct = (exit_opt - entry_opt) / entry_opt * 100
            pnl_usd = half * self.cfg['contract_multiplier'] * (exit_opt - entry_opt)
            
            # 扣除半仓手续费（长桥美股期权：平台费$0.65/张 + 监管费$0.02/张）
            # 半仓平仓只收平仓手续费，不开仓手续费
            option_fee_per_contract = 0.67  # $0.65平台费 + $0.02监管费
            half_fee = half * option_fee_per_contract  # 只收平仓手续费
            pnl_usd -= half_fee
            pos['option_fee'] = pos.get('option_fee', 0) + half_fee  # 累计手续费
            
            self.daily_pnl += pnl_usd
            
            # 更新Dashboard
            try:
                import dashboard_v7
                dashboard_v7.update_pnl(self.daily_pnl, len(self.trades_today))
                dashboard_v7.add_event(f"📉 半仓平仓: {pnl_usd:+.2f}", "trade")
            except Exception:
                pass
            
            # 方向累计盈亏（半仓也要算）
            if pos['dir'] == 'call':
                self.call_pnl += pnl_usd
            else:
                self.put_pnl += pnl_usd

            # 计算整体持仓在平仓时的盈亏（用于设置半仓后的跟踪止损基准）
            try:
                opt_quotes_all = self.quote_ctx.quote([pos['opt_symbol']])
                if opt_quotes_all and hasattr(opt_quotes_all[0], 'last_done') and opt_quotes_all[0].last_done > 0:
                    overall_opt_price = float(opt_quotes_all[0].last_done)
                else:
                    overall_opt_price = exit_opt
            except:
                overall_opt_price = exit_opt
            overall_pnl_pct = (overall_opt_price - entry_opt) / entry_opt * 100

            # 更新持仓：减少张数
            pos['contracts'] -= half
            # 标记半仓状态，并重置峰值起点（剩余仓位的跟踪从当前价格开始）
            pos['half_closed'] = True
            pos['half_closed_max_pct'] = overall_pnl_pct   # 用整体盈亏作基准，而非已平仓半张的盈亏
            # 重置正股峰值：剩余仓位的跟踪止损从当前正股价格开始计算
            try:
                stock_quotes = self.quote_ctx.quote([self.cfg['symbol']])
                if stock_quotes:
                    current_stock = float(stock_quotes[0].last_done)
                    pos['stock_peak'] = current_stock
            except:
                pass  # 如果获取失败，保持原peak不变，不阻塞流程

            print(f"\n  {'='*50}")
            print(f"  ✂️ 部分平仓: {reason}")
            print(f"  📈 期权: {pos['opt_symbol']}")
            print(f"  💰 入场: ${entry_opt:.2f} → 平仓: ${exit_opt:.2f}")
            print(f"  📊 平仓: {half}张 | 剩余: {pos['contracts']}张")
            print(f"  💵 本次盈亏: {pnl_pct:+.2f}% (${pnl_usd:+,.2f})")
            print(f"  📋 订单: {order_id}")
            print(f"  {'='*50}\n")

            notified = self._notify(
                f"✂️ 部分平仓 {pos['opt_symbol']}",
                'partial',
                pos=pos,
                reason=reason,
                entry_opt=entry_opt,
                exit_opt=exit_opt,
                half=int(half),
                remaining=int(pos['contracts']),
                pnl_pct=pnl_pct,
                pnl_usd=pnl_usd,
            )
            partial_key_trade = {
                **pos,
                'closed_contracts': half,
                'exit_opt_price': exit_opt,
                'pnl_usd': pnl_usd,
                'exit_reason': reason,
            }
            if notified:
                self._mark_notification_sent(self._trade_notify_key(partial_key_trade, 'partial'), 'partial', pos.get('opt_symbol', ''))

            self._save_state()
            self._sync_gist()  # 实时同步到小程序

        except Exception as e:
            print(f"  ❌ 部分平仓失败: {e}")

    def _close_position(self, reason):
        """平仓（期权）- 增强版订单验证"""
        pos = self.position
        if not pos:
            return

        side = OrderSide.Sell  # 卖出平仓（不管Call还是Put，都是Sell平仓）

        try:
            resp = self.trade_ctx.submit_order(
                symbol=pos['opt_symbol'],  # 使用期权代码平仓
                order_type=OrderType.MO,
                side=side,
                submitted_quantity=Decimal(str(pos['contracts'])),  # 平几张
                time_in_force=TimeInForceType.Day,
                outside_rth=OutsideRTH.AnyTime,
                remark=f"v6_opt_close",
            )

            order_id = resp.order_id
            print(f"  📋 平仓订单已提交: {order_id}")

            # ===== 增强版平仓订单检测 =====
            close_filled = False
            max_retries = 5
            retry_interval = 3
            exit_opt = 0
            close_qty = 0
            requested_qty = int(pos['contracts'])

            for attempt in range(max_retries):
                time.sleep(retry_interval)
                try:
                    # 查询订单状态
                    order_info = None
                    
                    # 方式1: 通过order_id查询
                    try:
                        orders = self.trade_ctx.today_orders(order_id=order_id)
                        if orders:
                            order_info = orders[0]
                    except Exception as e1:
                        print(f"  ⚠️ 平仓查询失败(方式1): {e1}")
                    
                    # 方式2: 查询所有今日订单
                    if not order_info:
                        try:
                            all_orders = self.trade_ctx.today_orders()
                            for o in all_orders:
                                if hasattr(o, 'order_id') and o.order_id == order_id:
                                    order_info = o
                                    break
                        except Exception as e2:
                            print(f"  ⚠️ 平仓查询失败(方式2): {e2}")
                    
                    if order_info:
                        order_status = getattr(order_info, 'status', None)
                        executed_qty = float(getattr(order_info, 'executed_quantity', 0) or 0)
                        executed_price = float(getattr(order_info, 'executed_price', 0) or 0)
                        
                        print(f"  📊 平仓状态: {order_status} | 已成交: {executed_qty}张 @ ${executed_price}")
                        
                        if str(order_status) == 'OrderStatus.Rejected':
                            print(f"  ❌ 平仓订单被拒，保留持仓等待下次重试")
                            self._notify(
                                f"❌ 平仓被拒 {pos['opt_symbol']}",
                                'position_anomaly',
                                anomaly_type='close_rejected',
                                details=f"订单 <code>{order_id}</code>\n持仓保留，等待下次风控重试",
                            )
                            self._save_state()
                            return
                         
                        if executed_qty >= requested_qty:
                            close_filled = True
                            close_qty = requested_qty
                            exit_opt = float(executed_price) if executed_price > 0 else 0
                            print(f"  ✅ 平仓完全成交!")
                            break
                        elif executed_qty > 0:
                            # 部分成交
                            print(f"  ⚠️ 平仓部分成交: {executed_qty}/{requested_qty}张")
                            if attempt == max_retries - 1:
                                close_filled = True
                                close_qty = int(executed_qty)
                                exit_opt = float(executed_price) if executed_price > 0 else 0
                                break
                        else:
                            print(f"  ⏳ 等待平仓成交... ({attempt + 1}/{max_retries})")
                            if attempt == max_retries - 1:
                                print(f"  ❌ 平仓超时，尝试取消")
                                try:
                                    self.trade_ctx.cancel_order(order_id)
                                except:
                                    pass
                    else:
                        print(f"  ⚠️ 未找到平仓订单: {order_id}")
                        if attempt == max_retries - 1:
                            print(f"  ❌ 无法查询平仓订单状态")
                            
                except Exception as query_err:
                    print(f"  ⚠️ 平仓查询异常: {query_err}")
                    if attempt == max_retries - 1:
                        print(f"  ❌ 平仓查询失败次数过多")

            # 如果平仓订单未成交，尝试获取当前期权价格
            if not close_filled:
                print(f"  ⚠️ 平仓订单未成交（可能超时/取消），尝试获取当前价格")
                try:
                    opt_quotes = self.quote_ctx.quote([pos['opt_symbol']])
                    if opt_quotes and hasattr(opt_quotes[0], 'last_done') and opt_quotes[0].last_done > 0:
                        exit_opt = float(opt_quotes[0].last_done)
                        print(f"  📈 获取到期权当前价: ${exit_opt:.2f}")
                except:
                    pass
                
                # 如果还是获取不到，用入场价（保守处理）
                if exit_opt <= 0:
                    exit_opt = pos.get('entry_opt_price') or 1.0
                    print(f"  ⚠️ 无法获取当前价，使用入场价: ${exit_opt:.2f}")
                
                # ⚠️ 平仓订单未成交 → 不清空持仓！保留以便下次重试
                print(f"  ⏳ 保留持仓，等待下次平仓重试")
                self._save_state()
                return
            if close_qty <= 0:
                print(f"  ⚠️ 平仓成交数量异常，保留持仓")
                self._save_state()
                return

            entry_opt = pos.get('entry_opt_price') or exit_opt
            if entry_opt <= 0:
                entry_opt = 1.0

            pnl_pct = (exit_opt - entry_opt) / entry_opt * 100

            # 计算盈亏金额（张数 × 100股 × 权利金变动）
            pnl_usd = close_qty * self.cfg['contract_multiplier'] * (exit_opt - entry_opt)
            
            # 扣除期权手续费（长桥美股期权：平台费$0.65/张 + 监管费$0.02/张）
            # 开仓和平仓各收一次，总手续费 = 张数 × $0.67 × 2
            option_fee_per_contract = 0.67  # $0.65平台费 + $0.02监管费
            total_fee = close_qty * option_fee_per_contract * 2  # 开仓+平仓
            pnl_usd -= total_fee
            pos['option_fee'] = pos.get('option_fee', 0) + total_fee  # 记录手续费

            self.daily_pnl += pnl_usd
            closing_full = close_qty >= requested_qty
            closed_pos = pos.copy()
            closed_pos.update({
                'win': pnl_pct > 0,
                'exit_opt_price': exit_opt,
                'exit_time': datetime.now(TZ_ET),
                'pnl_pct': pnl_pct,
                'pnl_usd': pnl_usd,
                'exit_reason': reason,
                'closed_contracts': close_qty,
            })
            
            # 更新Dashboard
            try:
                import dashboard_v7
                dashboard_v7.update_pnl(self.daily_pnl, len(self.trades_today))
                dashboard_v7.update_trades(self.trades_today)
                dashboard_v7.update_position(None if closing_full else pos)
                dashboard_v7.add_event(f"{'🟢' if pnl_pct > 0 else '🔴'} 平仓: {pnl_usd:+.2f} ({pnl_pct:+.1f}%)", "trade")
            except Exception:
                pass

            if closing_full:
                # 标记盈亏
                pos.update(closed_pos)

                # 同步更新 trades_today 中的记录
                for t in self.trades_today:
                    if t.get('opt_symbol') == pos['opt_symbol'] and t.get('exit_opt_price') is None:
                        t.update({
                            'win': closed_pos['win'],
                            'exit_opt_price': exit_opt,
                            'exit_time': closed_pos['exit_time'],
                            'pnl_pct': pnl_pct,
                            'pnl_usd': pnl_usd,
                            'exit_reason': reason,
                            # 市场上下文（从入场快照继承，已存在position中）
                            'atr_at_entry': pos.get('atr_at_entry', 0),
                            'macd_hist_entry': pos.get('macd_hist_entry', 0),
                            'vwap_entry': pos.get('vwap_entry', 0),
                            'sma20_entry': pos.get('sma20_entry', 0),
                            'regime': pos.get('regime', 'neutral'),
                        })
                        break

            d = "✅盈利" if pnl_pct > 0 else "❌亏损"
            print(f"\n  {'='*50}")
            print(f"  🏁 平仓: {reason}")
            print(f"  📈 期权: {pos['opt_symbol']}")
            print(f"  💰 入场: ${entry_opt:.2f} → 平仓: ${exit_opt:.2f}")
            print(f"  {d}: {pnl_pct:+.2f}% (${pnl_usd:+,.2f})")
            print(f"  📋 订单: {order_id}")
            print(f"  {'='*50}\n")

            notified = self._notify(
                f"🏁 平仓 {pos['opt_symbol']}",
                'exit',
                pos=closed_pos,
                reason=reason,
                entry_opt=entry_opt,
                exit_opt=exit_opt,
                pnl_pct=pnl_pct,
                pnl_usd=pnl_usd,
                order_id=order_id,
            )
            if notified:
                self._mark_notification_sent(self._trade_notify_key(closed_pos, 'exit'), 'exit', pos.get('opt_symbol', ''))

            closed_symbol = pos.get('opt_symbol')
            if closing_full:
                self.position = None  # 全部成交后才清空
                self._unsubscribe_quotes([closed_symbol])
            else:
                pos['contracts'] = requested_qty - close_qty
                pos['quantity'] = pos['contracts'] * self.cfg['contract_multiplier']
                pos['half_closed'] = True
                pos['half_closed_max_pct'] = max(pos.get('half_closed_max_pct', 0), pnl_pct)
                print(f"  ⚠️ 平仓只成交 {close_qty}/{requested_qty}张，剩余 {pos['contracts']}张继续风控")
            
            # 更新方向累计盈亏（所有单，不分盈亏）
            if pos['dir'] == 'call':
                self.call_pnl += pnl_usd
                if pnl_pct > 0:
                    self.call_wins += 1
            else:
                self.put_pnl += pnl_usd
                if pnl_pct > 0:
                    self.put_wins += 1

            # 更新连续统计
            if pnl_pct > 0:
                self.consecutive_wins += 1
                self.consecutive_losses = 0
                self.max_consecutive_wins = max(self.max_consecutive_wins, self.consecutive_wins)
                if pnl_usd > self.largest_win_usd:
                    self.largest_win_usd = pnl_usd
                    self.largest_win_pct = pnl_pct
            else:
                self.consecutive_losses += 1
                self.consecutive_wins = 0
                self.max_consecutive_losses = max(self.max_consecutive_losses, self.consecutive_losses)
                if pnl_usd < self.largest_loss_usd:
                    self.largest_loss_usd = pnl_usd
                    self.largest_loss_pct = pnl_pct

            # P1 #10 冷却：时间戳方式（替代 tick 计数，防止20s循环烧完冷却）
            if pnl_pct < 0:
                self.last_loss_dir = pos['dir']
                signal_name = display_signal_name(pos.get('display_engine') or pos.get('engine') or '')
                if 'Granville_Pullback' in str(pos.get('reason', '')) or signal_name == 'Granville_Pullback':
                    key = f"Granville_Pullback:{pos['dir']}"
                    self._signal_cooldowns[key] = datetime.now(TZ_ET) + timedelta(minutes=self.cfg.get('granville_loss_cooldown_min', 10))
                    print(f"  ⏳ Granville亏损冷却: {key} 10分钟")

                abs_loss = abs(pnl_pct)
                cons_limit = self.cfg.get('loss_consecutive_limit', 3)

                if abs_loss >= 15 or self.consecutive_losses >= cons_limit:
                    # 大亏(>=15%) 或连亏>=3笔：冷却5分钟
                    cd_minutes = 5
                else:
                    # 小亏：冷却2分钟
                    cd_minutes = 2
                self.loss_cooldown_until = datetime.now(TZ_ET) + timedelta(minutes=cd_minutes)
                print(f"  ⏳ 亏损{abs_loss:.1f}%，连亏{self.consecutive_losses}次 -> 冷却{cd_minutes}分钟({self.last_loss_dir}) 到{self.loss_cooldown_until.strftime('%H:%M')}")

                # 半仓后连续亏损：止损收紧
                if pos['half_closed']:
                    tighten = self.cfg.get('half_close_sl_tighten', 0.15)
                    print(f"  🛡 半仓后亏损，下次止损收紧至{tighten*100:.0f}%")

            # 每次平仓后实时写入 records 文件（独立于 Gist 的安全备份）
            self._save_records_snapshot()

            self._save_state()
            self._sync_gist()  # 实时同步到小程序

        except Exception as e:
            print(f"  ❌ 平仓失败: {e}（持仓保留，下次重试）")
            import traceback
            traceback.print_exc()

    def _notify_feishu(self, msg):
        """飞书通知 - 发送消息到用户"""
        try:
            import requests
            # 读取飞书凭据
            env_path = os.path.expanduser('~/.hermes/.env')
            app_id = app_secret = None
            if os.path.exists(env_path):
                for line in open(env_path, encoding='utf-8'):
                    if line.strip().startswith('FEISHU_APP_ID'):
                        app_id = line.split('=', 1)[1].strip()
                    elif line.strip().startswith('FEISHU_APP_SECRET'):
                        app_secret = line.split('=', 1)[1].strip()
            if not app_id or not app_secret:
                print(f"  ⚠️ 飞书凭据未配置，写日志: {msg}")
                log_path = os.path.join(_app_dir(), 'logs', 'trade_log.txt')
                os.makedirs(os.path.dirname(log_path), exist_ok=True)
                with open(log_path, 'a', encoding='utf-8') as f:
                    f.write(f'[{datetime.now(TZ_ET):%H:%M}] {msg}\n')
                return

            # 获取 token
            token_resp = requests.post(
                'https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal',
                json={'app_id': app_id, 'app_secret': app_secret},
                timeout=10
            )
            token_data = token_resp.json()
            if token_data.get('code') != 0:
                print(f"  ⚠️ 飞书token获取失败: {token_data}")
                return
            token = token_data['tenant_access_token']

            # 发送消息给用户
            user_open_id = self.cfg.get('feishu', {}).get('open_id', '')
            payload = {
                'receive_id': user_open_id,
                'msg_type': 'text',
                'content': json.dumps({'text': f"[QQQ Trader]\n{msg}"})
            }
            resp = requests.post(
                'https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id',
                headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
                json=payload,
                timeout=10
            )
            result = resp.json()
            if result.get('code') == 0:
                print(f"  ✅ 飞书推送成功")
            else:
                print(f"  ⚠️ 飞书推送失败: {result}")
        except Exception as e:
            import traceback
            print(f"  ⚠️ 飞书通知异常: {e}")
            traceback.print_exc()
            log_path = os.path.join(_app_dir(), 'logs', 'trade_log.txt')
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            with open(log_path, 'a', encoding='utf-8') as f:
                f.write(f'[{datetime.now(TZ_ET):%H:%M}] {msg}\n')

    def _fmt_day_market(self, source=None):
        regime = {}
        if isinstance(source, dict):
            raw = source.get('metadata', {}).get('day_market_regime') if isinstance(source.get('metadata'), dict) else None
            if isinstance(raw, dict):
                regime = raw
            else:
                regime = {
                    'type': source.get('day_market_regime', ''),
                    'label': source.get('day_market_label', ''),
                }
        if not regime:
            regime = self.day_market_regime if isinstance(self.day_market_regime, dict) else {}
        label = regime.get('label') or regime.get('type') or '--'
        direction = regime.get('direction') or ''
        reason = regime.get('reason') or ''
        direction_text = {'call': '偏多', 'put': '偏空'}.get(direction, '中性')
        if reason:
            return f"当日行情 <b>{label}</b> ({direction_text})\n<code>{reason}</code>\n"
        return f"当日行情 <b>{label}</b> ({direction_text})\n"

    def _fmt_entry(self, sig, opt_symbol, price, contracts, qty, order_id):
        """格式化开仓通知"""
        dir_emoji = '🟢' if sig['dir'] == 'call' else '🔴'
        dir_text = '做多 CALL' if sig['dir'] == 'call' else '做空 PUT'
        regime = sig.get('regime', '--')
        reason = sig.get('reason', '--')
        entry_opt = self.position.get('entry_opt_price', 0) if self.position else 0
        closed_trades = [t for t in self.trades_today if t.get('win') is not None]
        total = len(closed_trades)
        wins = sum(1 for t in closed_trades if t.get('win'))
        wr = wins/total*100 if total > 0 else 0
        return (
            f"<b>🎯 开仓 #{len(self.trades_today)}</b>\n"
            f"───────────\n"
            f"{dir_emoji} <b>{dir_text}</b>\n"
            f"<code>{opt_symbol}</code>\n"
            f"───────────\n"
            f"正股 <b>${price:.2f}</b> | 期权 <b>${entry_opt:.2f}</b>\n"
            f"数量 <b>{contracts}</b>张 ({qty}股)\n"
            f"策略行情 <b>{regime}</b>\n"
            f"{self._fmt_day_market(sig)}"
            f"理由 {reason}\n"
            f"订单 {order_id}\n"
            f"───────────\n"
            f"📈 今日统计\n"
            f"交易 <b>{total}</b>笔 | 胜率<b>{wr:.0f}%</b> | 盈亏<b>${self.daily_pnl:+,.2f}</b>\n"
            f"🔥 连胜{self.max_consecutive_wins} | ❄️ 连亏{self.max_consecutive_losses}"
        )

    def _fmt_exit(self, pos, reason, entry_opt, exit_opt, pnl_pct, pnl_usd, order_id='--'):
        """格式化平仓通知"""
        emoji = '✅' if pnl_pct > 0 else '❌'
        dir_emoji = '🟢' if pos.get('dir') == 'call' else '🔴'
        dir_text = 'CALL' if pos.get('dir') == 'call' else 'PUT'
        label = '盈利' if pnl_pct > 0 else '亏损'
        closed_trades = [t for t in self.trades_today if t.get('win') is not None]
        total = len(closed_trades)
        wins = sum(1 for t in closed_trades if t.get('win'))
        wr = wins/total*100 if total > 0 else 0
        return (
            f"<b>🏁 平仓 #{len(self.trades_today)}</b>\n"
            f"───────────\n"
            f"{dir_emoji} <b>{dir_text}</b> <code>{pos.get('opt_symbol','')}</code>\n"
            f"原因 <b>{reason}</b>\n"
            f"───────────\n"
            f"入场 ${entry_opt:.2f} → 平仓 ${exit_opt:.2f}\n"
            f"{emoji} {label} <b>{pnl_pct:+.2f}%</b> (${pnl_usd:+,.2f})\n"
            f"{self._fmt_day_market(pos)}"
            f"订单 {order_id}\n"
            f"───────────\n"
            f"📈 今日统计\n"
            f"交易 <b>{total}</b>笔 | 胜率<b>{wr:.0f}%</b> | 盈亏<b>${self.daily_pnl:+,.2f}</b>\n"
            f"🔥 连胜{self.max_consecutive_wins} | ❄️ 连亏{self.max_consecutive_losses}"
        )

    def _fmt_partial(self, pos, reason, entry_opt, exit_opt, half, remaining, pnl_pct, pnl_usd):
        """格式化部分平仓通知"""
        emoji = '✅' if pnl_pct > 0 else '❌'
        dir_emoji = '🟢' if pos.get('dir') == 'call' else '🔴'
        dir_text = 'CALL' if pos.get('dir') == 'call' else 'PUT'
        return (
            f"<b>✂️ 部分平仓</b>\n"
            f"───────────\n"
            f"{dir_emoji} <b>{dir_text}</b> <code>{pos.get('opt_symbol','')}</code>\n"
            f"原因 <b>{reason}</b>\n"
            f"───────────\n"
            f"入场 ${entry_opt:.2f} → 平仓 ${exit_opt:.2f}\n"
            f"{emoji} <b>{pnl_pct:+.2f}%</b> (${pnl_usd:+,.2f})\n"
            f"{self._fmt_day_market(pos)}"
            f"平掉 <b>{half}</b>张 | 剩余 <b>{remaining}</b>张"
        )

    def _fmt_alert(self, level, loss_pct, threshold):
        """格式化熔断/警告通知"""
        icons = {1: '⚠️', 2: '🔶', 3: '🔴'}
        labels = {1: '警告', 2: '保守', 3: '熔断'}
        icon = icons.get(level, '⚠️')
        desc = ''
        if level >= 1: desc += ' 仓位减半'
        if level >= 2: desc += ' | 只做trending'
        if level >= 3: desc += ' | 停止所有交易'
        return (
            f"<b>{icon} 亏损{labels.get(level,'通知')}</b>\n"
            f"───────────\n"
            f"当前亏损 <b>{loss_pct:.1f}%</b> (阈值 {threshold:.0f}%)\n"
            f"{desc}"
        )

    def _fmt_system(self, event_type, **kw):
        """格式化系统事件"""
        if event_type == 'exit':
            return (
                f"<b>⚠️ 系统退出</b>\n"
                f"───────────\n"
                f"原因 <b>{kw.get('sig_name','')}</b>\n"
                f"时间 {kw.get('time','')}\n"
                f"今日交易 <b>{kw.get('trades',0)}</b>笔\n"
                f"盈亏 <b>{kw.get('pnl',0):+,.2f}</b>"
            )
        elif event_type == 'crash':
            return (
                f"<b>❌ 系统异常</b>\n"
                f"───────────\n"
                f"时间 {kw.get('time','')}\n"
                f"错误 <code>{kw.get('error','')}</code>"
            )
        elif event_type == 'cancel':
            return (
                f"<b>⏰ 订单超时取消</b>\n"
                f"───────────\n"
                f"期权 <code>{kw.get('symbol','')}</code>"
            )
        return ''

    def _fmt_startup(self):
        """格式化启动通知"""
        return (
            f"<b>🚀 系统启动</b>\n"
            f"───────────\n"
            f"版本 <code>v7 Multi-Engine</code>\n"
            f"时间 <code>{datetime.now(TZ_ET).strftime('%Y-%m-%d %H:%M ET')}</code>\n"
            f"账户 <b>${self.actual_capital:,.2f}</b>\n"
            f"昨日盈亏 <b>${self.yesterday_pnl:+,.2f}</b> ({self.yesterday_trades}笔, 胜率{self.yesterday_wr:.0f}%)"
        )

    def _fmt_shutdown(self, reason='未知'):
        """格式化停止通知"""
        runtime = datetime.now(TZ_ET) - self.start_time
        hours = int(runtime.total_seconds() // 3600)
        mins = int((runtime.total_seconds() % 3600) // 60)
        total = len(self.trades_today)
        wins = sum(1 for t in self.trades_today if t.get('win'))
        return (
            f"<b>⏹️ 系统停止</b>\n"
            f"───────────\n"
            f"原因 <b>{reason}</b>\n"
            f"运行时长 <b>{hours}h {mins}m</b>\n"
            f"今日交易 <b>{total}</b>笔 | 盈利<b>{wins}</b> | 亏损<b>{total-wins}</b>\n"
            f"盈亏 <b>${self.daily_pnl:+,.2f}</b>"
        )

    def _fmt_daily_summary(self):
        """格式化日终总结通知"""
        try:
            self._save_daily_records()
            from review_summary import build_review_summary
            return build_review_summary('day', datetime.now(TZ_ET).strftime('%Y-%m-%d')).get('telegram_html', '')
        except Exception as e:
            print(f"⚠️ 新版日终复盘生成失败，回退旧版: {e}")
        closed_trades = [t for t in self.trades_today if t.get('win') is not None]
        total = len(closed_trades)
        wins = sum(1 for t in closed_trades if t.get('win'))
        wr = wins/total*100 if total > 0 else 0
        
        call_cnt = sum(1 for t in closed_trades if t.get('dir')=='call')
        put_cnt = total - call_cnt
        call_win = sum(1 for t in closed_trades if t.get('dir')=='call' and t.get('win'))
        put_win = sum(1 for t in closed_trades if t.get('dir')=='put' and t.get('win'))
        call_wr = call_win/call_cnt*100 if call_cnt > 0 else 0
        put_wr = put_win/put_cnt*100 if put_cnt > 0 else 0
        
        return (
            f"<b>📊 日终总结 {datetime.now(TZ_ET).strftime('%Y-%m-%d')}</b>\n"
            f"───────────\n"
            f"总交易 <b>{total}</b>笔 | 盈利<b>{wins}</b> | 亏损<b>{total-wins}</b>\n"
            f"胜率 <b>{wr:.1f}%</b>\n"
            f"盈亏 <b>${self.daily_pnl:+,.2f}</b>\n"
            f"───────────\n"
            f"方向分布\n"
            f"🟢 CALL <b>{call_cnt}</b>笔 (胜率{call_wr:.0f}%, ${self.call_pnl:+,.2f})\n"
            f"🔴 PUT  <b>{put_cnt}</b>笔 (胜率{put_wr:.0f}%, ${self.put_pnl:+,.2f})\n"
            f"───────────\n"
            f"连续统计\n"
            f"🔥 最长连胜 <b>{self.max_consecutive_wins}</b>笔\n"
            f"❄️ 最长连亏 <b>{self.max_consecutive_losses}</b>笔\n"
            f"───────────\n"
            f"单笔最佳\n"
            f"✅ 最大盈利 <b>+${self.largest_win_usd:,.2f}</b> ({self.largest_win_pct:+.1f}%)\n"
            f"❌ 最大亏损 <b>-${abs(self.largest_loss_usd):,.2f}</b> ({self.largest_loss_pct:+.1f}%)"
        )

    def _get_today_stats_html(self):
        """获取今日统计HTML片段（用于开仓/平仓通知底部）"""
        closed_trades = [t for t in self.trades_today if t.get('win') is not None]
        total = len(closed_trades)
        wins = sum(1 for t in closed_trades if t.get('win'))
        wr = wins/total*100 if total > 0 else 0
        
        return (
            f"📈 今日统计\n"
            f"交易 <b>{total}</b>笔 | 胜率<b>{wr:.0f}%</b> | 盈亏<b>${self.daily_pnl:+,.2f}</b>\n"
            f"🔥 连胜{self.max_consecutive_wins} | ❄️ 连亏{self.max_consecutive_losses}"
        )

    def _fmt_weekly_summary(self):
        """格式化周度收益汇总通知"""
        try:
            from review_summary import build_review_summary
            return build_review_summary('week', datetime.now(TZ_ET).strftime('%Y-%m-%d')).get('telegram_html', '')
        except Exception as e:
            print(f"⚠️ 新版周度复盘生成失败，回退旧版: {e}")
        from datetime import timedelta
        import json
        
        today = datetime.now(TZ_ET).date()
        # 计算本周一
        monday = today - timedelta(days=today.weekday())
        
        weekly_trades = []
        weekly_pnl = 0
        weekly_wins = 0
        weekly_total = 0
        daily_summaries = []
        
        # 读取本周所有交易记录
        records_dir = os.path.join(_app_dir(), 'records')
        for i in range(5):  # 周一到周五
            day = monday + timedelta(days=i)
            filepath = os.path.join(records_dir, f'{day.strftime("%Y-%m-%d")}.json')
            if os.path.exists(filepath):
                try:
                    with open(filepath, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    day_trades = data.get('trades', [])
                    day_pnl = data.get('pnl', 0)
                    day_wins = data.get('wins', 0)
                    day_total = data.get('total', 0)
                    
                    weekly_trades.extend(day_trades)
                    weekly_pnl += day_pnl
                    weekly_wins += day_wins
                    weekly_total += day_total
                    
                    daily_summaries.append({
                        'date': day.strftime('%m/%d'),
                        'trades': day_total,
                        'wins': day_wins,
                        'pnl': day_pnl,
                    })
                except Exception:
                    pass
        
        if weekly_total == 0:
            return "<b>📊 周度总结</b>\n───────────\n本周无交易记录"
        
        weekly_wr = weekly_wins / weekly_total * 100 if weekly_total > 0 else 0
        
        # 计算方向分布
        call_cnt = sum(1 for t in weekly_trades if t.get('dir') == 'call')
        put_cnt = weekly_total - call_cnt
        call_win = sum(1 for t in weekly_trades if t.get('dir') == 'call' and t.get('result') == 'win')
        put_win = sum(1 for t in weekly_trades if t.get('dir') == 'put' and t.get('result') == 'win')
        call_wr = call_win / call_cnt * 100 if call_cnt > 0 else 0
        put_wr = put_win / put_cnt * 100 if put_cnt > 0 else 0
        call_pnl = sum(t.get('pnl_usd', 0) for t in weekly_trades if t.get('dir') == 'call')
        put_pnl = sum(t.get('pnl_usd', 0) for t in weekly_trades if t.get('dir') == 'put')
        
        # 找出最佳/最差交易
        best_trade = max(weekly_trades, key=lambda t: t.get('pnl_usd', 0))
        worst_trade = min(weekly_trades, key=lambda t: t.get('pnl_usd', 0))
        
        # 每日明细
        daily_detail = ""
        for d in daily_summaries:
            emoji = "✅" if d['pnl'] > 0 else "❌" if d['pnl'] < 0 else "➖"
            daily_detail += f"{emoji} {d['date']} {d['trades']}笔 {d['wins']}胜 ${d['pnl']:+,.2f}\n"
        
        return (
            f"<b>📊 周度总结 {monday.strftime('%m/%d')}~{today.strftime('%m/%d')}</b>\n"
            f"───────────\n"
            f"总交易 <b>{weekly_total}</b>笔 | 盈利<b>{weekly_wins}</b> | 亏损<b>{weekly_total - weekly_wins}</b>\n"
            f"胜率 <b>{weekly_wr:.1f}%</b>\n"
            f"盈亏 <b>${weekly_pnl:+,.2f}</b>\n"
            f"───────────\n"
            f"方向分布\n"
            f"🟢 CALL <b>{call_cnt}</b>笔 (胜率{call_wr:.0f}%, ${call_pnl:+,.2f})\n"
            f"🔴 PUT  <b>{put_cnt}</b>笔 (胜率{put_wr:.0f}%, ${put_pnl:+,.2f})\n"
            f"───────────\n"
            f"每日明细\n"
            f"{daily_detail}"
            f"───────────\n"
            f"最佳交易\n"
            f"✅ <code>{best_trade.get('opt_symbol', '--')}</code> ${best_trade.get('pnl_usd', 0):+,.2f}\n"
            f"最差交易\n"
            f"❌ <code>{worst_trade.get('opt_symbol', '--')}</code> ${worst_trade.get('pnl_usd', 0):+,.2f}"
        )

    def _fmt_network_alert(self, error_msg, retry_count=0):
        """格式化网络断连告警"""
        return (
            f"<b>🌐 网络异常</b>\n"
            f"───────────\n"
            f"错误 <code>{error_msg[:100]}</code>\n"
            f"重试次数 <b>{retry_count}</b>\n"
            f"时间 {datetime.now(TZ_ET).strftime('%H:%M:%S ET')}\n"
            f"───────────\n"
            f"系统将自动重连，请关注后续通知"
        )

    def _fmt_api_rate_limit(self, api_name, wait_seconds):
        """格式化API限流告警"""
        return (
            f"<b>⏱️ API限流</b>\n"
            f"───────────\n"
            f"接口 <b>{api_name}</b>\n"
            f"等待 <b>{wait_seconds}</b>秒后重试\n"
            f"时间 {datetime.now(TZ_ET).strftime('%H:%M:%S ET')}\n"
            f"───────────\n"
            f"交易暂停，等待限流解除"
        )

    def _fmt_position_anomaly(self, anomaly_type, details):
        """格式化持仓异常告警"""
        icons = {
            'mismatch': '⚠️',
            'missing': '❌',
            'cleared': '🔴',
            'verify_failed': '❗',
        }
        labels = {
            'mismatch': '持仓数量不一致',
            'missing': '持仓丢失',
            'cleared': '持仓被清空',
            'verify_failed': '持仓验证失败',
        }
        icon = icons.get(anomaly_type, '⚠️')
        label = labels.get(anomaly_type, '持仓异常')
        
        return (
            f"<b>{icon} {label}</b>\n"
            f"───────────\n"
            f"{details}\n"
            f"时间 {datetime.now(TZ_ET).strftime('%H:%M:%S ET')}\n"
            f"───────────\n"
            f"请检查账户状态"
        )

    def _fmt_monthly_summary(self):
        """格式化月度复盘通知"""
        try:
            from review_summary import build_review_summary
            return build_review_summary('month', datetime.now(TZ_ET).strftime('%Y-%m-%d')).get('telegram_html', '')
        except Exception as e:
            return f"<b>月度复盘生成失败</b>\n<code>{str(e)[:180]}</code>"

    def _summary_flags_path(self):
        records_dir = os.path.join(_app_dir(), 'records')
        os.makedirs(records_dir, exist_ok=True)
        return os.path.join(records_dir, 'review_summary_sent.json')

    def _load_summary_flags(self):
        try:
            path = self._summary_flags_path()
            if os.path.exists(path):
                with open(path, 'r', encoding='utf-8-sig') as f:
                    data = json.load(f)
                return data if isinstance(data, dict) else {}
        except Exception:
            pass
        return {}

    def _save_summary_flags(self, flags):
        try:
            path = self._summary_flags_path()
            tmp = path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(flags, f, ensure_ascii=False, indent=2, default=_json_default)
            os.replace(tmp, path)
        except Exception as e:
            print(f"⚠️ 复盘推送标记保存失败: {e}")

    def _send_period_summary_once(self, period, msg_type):
        try:
            from review_summary import build_review_summary
            summary = build_review_summary(period, datetime.now(TZ_ET).strftime('%Y-%m-%d'))
            key = f"{period}:{summary.get('start_date')}:{summary.get('end_date')}"
            flags = self._load_summary_flags()
            if flags.get(key):
                return False
            sent = self._notify(f"📊 {summary.get('title', '复盘摘要')}", msg_type=msg_type)
            if sent:
                flags[key] = datetime.now(TZ_ET).strftime('%Y-%m-%d %H:%M:%S')
                self._save_summary_flags(flags)
            return sent
        except Exception as e:
            print(f"⚠️ {period}复盘推送失败: {e}")
            return False

    def _check_and_send_weekly_summary(self):
        """检查是否需要发送周报/月报（收盘后）"""
        now = datetime.now(TZ_ET)
        if not (16 <= now.hour < 17 and now.minute >= 5):
            return
        if now.weekday() == 4:
            self._send_period_summary_once('week', 'weekly_summary')
        try:
            from review_summary import is_last_weekday_of_month
            if now.weekday() < 5 and is_last_weekday_of_month(now.date()):
                self._send_period_summary_once('month', 'monthly_summary')
        except Exception as e:
            print(f"⚠️ 月报日期检查失败: {e}")

    def _notify_telegram(self, msg, msg_type='info', **kw):
        """Telegram通知 - 支持HTML格式的消息"""
        try:
            import requests
            tg_cfg = self.cfg.get('telegram', {})
            bot_token = tg_cfg.get('bot_token', '')
            chat_id = tg_cfg.get('chat_id', '')
            if not bot_token or not chat_id:
                print(f"  ⚠️ Telegram凭据未配置，写日志: {msg}")
                log_path = os.path.join(_app_dir(), 'logs', 'trade_log.txt')
                os.makedirs(os.path.dirname(log_path), exist_ok=True)
                with open(log_path, 'a', encoding='utf-8') as f:
                    f.write(f'[{datetime.now(TZ_ET):%H:%M}] {msg}\n')
                return False

            # 根据消息类型生成格式化文本
            if msg_type == 'entry' and kw:
                text = self._fmt_entry(**kw)
            elif msg_type == 'exit' and kw:
                text = self._fmt_exit(**kw)
            elif msg_type == 'partial' and kw:
                text = self._fmt_partial(**kw)
            elif msg_type == 'alert' and kw:
                text = self._fmt_alert(**kw)
            elif msg_type == 'startup':
                text = self._fmt_startup()
            elif msg_type == 'shutdown':
                text = self._fmt_shutdown(**kw)
            elif msg_type == 'daily_summary':
                text = self._fmt_daily_summary()
            elif msg_type == 'weekly_summary':
                text = self._fmt_weekly_summary()
            elif msg_type == 'monthly_summary':
                text = self._fmt_monthly_summary()
            elif msg_type == 'network' and kw:
                text = self._fmt_network_alert(**kw)
            elif msg_type == 'rate_limit' and kw:
                text = self._fmt_api_rate_limit(**kw)
            elif msg_type == 'position_anomaly' and kw:
                text = self._fmt_position_anomaly(**kw)
            elif msg_type == 'system' and kw:
                text = self._fmt_system(**kw)
            else:
                # 通用格式：用分隔线和HTML加粗标题
                lines = msg.split('\n')
                first = lines[0] if lines else msg
                rest = '\n'.join(lines[1:]) if len(lines) > 1 else ''
                if 'cancel' in first.lower():
                    text = f"<b>{first}</b>\n───────────\n{rest}"
                else:
                    text = f"<b>{first}</b>\n───────────\n{rest}" if rest else f"<b>{first}</b>"

            # 添加页脚
            footer = f"\n───────────\n<code>QQQ 0DTE v7</code>"
            full_text = text + footer

            # 代理配置
            proxies = {}
            proxy_url = tg_cfg.get('proxy', '')
            if proxy_url:
                proxies = {'https': proxy_url, 'http': proxy_url}

            api_url = f'https://api.telegram.org/bot{bot_token}/sendMessage'

            def _post_telegram(payload, label):
                last_error = None
                for attempt in range(3):
                    try:
                        return requests.post(
                            api_url,
                            json=payload,
                            timeout=15,
                            proxies=proxies,
                        )
                    except requests.RequestException as err:
                        last_error = err
                        safe_err = str(err).replace(bot_token, '***TOKEN***')
                        print(f"  ⚠️ Telegram{label}网络异常({attempt + 1}/3): {safe_err[:220]}")
                        if attempt < 2:
                            time.sleep(2 * (attempt + 1))
                raise last_error

            resp = _post_telegram(
                {'chat_id': chat_id, 'text': full_text, 'parse_mode': 'HTML'},
                '推送',
            )
            result = resp.json()
            if result.get('ok'):
                print(f"  ✅ Telegram推送成功")
                return True
            else:
                print(f"  ⚠️ Telegram推送失败: {result}")
                # 如果HTML解析失败，回退纯文本
                if 'can\'t parse' in str(result):
                    resp2 = _post_telegram(
                        {'chat_id': chat_id, 'text': f"[QQQ Trader]\n{msg}"},
                        '纯文本回退',
                    )
                    try:
                        fallback = resp2.json()
                        if fallback.get('ok'):
                            print(f"  ✅ Telegram纯文本回退成功")
                            return True
                        print(f"  ⚠️ Telegram纯文本回退失败: {fallback}")
                    except Exception:
                        print(f"  ⚠️ Telegram纯文本回退响应异常: {resp2.text[:200]}")
                return False
        except Exception as e:
            safe_err = str(e)
            try:
                bot_token = self.cfg.get('telegram', {}).get('bot_token', '')
                if bot_token:
                    safe_err = safe_err.replace(bot_token, '***TOKEN***')
            except Exception:
                pass
            print(f"  ⚠️ Telegram通知失败，已记录到本地，后续会重试: {safe_err[:260]}")
            log_path = os.path.join(_app_dir(), 'logs', 'trade_log.txt')
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            with open(log_path, 'a', encoding='utf-8') as f:
                f.write(f'[{datetime.now(TZ_ET):%H:%M}] {msg}\n')
            return False

    def _notify(self, msg, msg_type='info', **kw):
        """统一通知 - 同时发送飞书和Telegram"""
        tg_cfg = self.cfg.get('telegram', {})
        print(f"  📨 Telegram推送: enabled={tg_cfg.get('enabled')}, type={msg_type}")
        sent = False
        if self.cfg.get('feishu', {}).get('enabled', True):
            self._notify_feishu(msg)
        if tg_cfg.get('enabled', False):
            sent = bool(self._notify_telegram(msg, msg_type=msg_type, **kw))
        return sent

    def _handle_error_with_notification(self, error, context="", notify_type=None):
        """处理错误并发送通知（避免重复通知）"""
        error_str = str(error).lower()
        now = time.time()
        
        # 网络错误检测
        network_keywords = ['connection', 'timeout', 'network', 'socket', 'http', 'ssl', 'dns']
        is_network_error = any(kw in error_str for kw in network_keywords)
        
        # API限流检测
        rate_limit_keywords = ['429', 'rate limit', 'too many', 'throttle', 'limit']
        is_rate_limit = any(kw in error_str for kw in rate_limit_keywords)
        
        # 避免重复通知（5分钟内相同错误只通知一次）
        error_key = f"{context}_{type(error).__name__}"
        if hasattr(self, '_last_error_notify'):
            if error_key in self._last_error_notify:
                if now - self._last_error_notify[error_key] < 300:
                    return  # 5分钟内已通知过
        else:
            self._last_error_notify = {}
        
        # 发送通知
        if is_network_error:
            self._notify(
                "🌐 网络异常",
                'network',
                error_msg=str(error)[:100],
                retry_count=getattr(self, '_network_retry_count', 0),
            )
            self._last_error_notify[error_key] = now
        elif is_rate_limit:
            # 从错误信息中提取等待时间
            wait_seconds = 60  # 默认60秒
            import re
            wait_match = re.search(r'(\d+)\s*(?:second|sec|s)', error_str)
            if wait_match:
                wait_seconds = int(wait_match.group(1))
            
            self._notify(
                "⏱️ API限流",
                'rate_limit',
                api_name=context or "Longbridge API",
                wait_seconds=wait_seconds,
            )
            self._last_error_notify[error_key] = now
        elif notify_type:
            # 其他指定类型的通知
            self._notify(str(error)[:200], notify_type)
            self._last_error_notify[error_key] = now

    def _sync_gist(self):
        """实时同步交易记录到Gist（供小程序读取）"""
        try:
            # 先保存当日记录
            self._save_daily_records()
            # 同步到Gist（打包后直接import调用，避免subprocess无法启动子进程）
            from update_gist import main as sync_gist_main
            sync_gist_main()
            print("  📤 Gist同步完成")
        except Exception as e:
            print(f"  ⚠️ Gist同步失败: {e}")

    def _effective_end_time(self):
        """根据当日盈亏返回动态交易结束时间"""
        dyn_end = self.cfg['end_time']
        if self.daily_pnl <= 0:
            dyn_end = self.cfg.get('extended_end_time', '15:00')
        return dyn_end

    def _is_extension_window(self, cur_min):
        """判断是否在延长时间窗口内(14:30-15:00)"""
        e_h, e_m = map(int, self.cfg['end_time'].split(':'))
        ee_h, ee_m = map(int, self.cfg.get('extended_end_time', '15:00').split(':'))
        return e_h * 60 + e_m <= cur_min < ee_h * 60 + ee_m

    def _print_summary(self):
        """打印今日总结"""
        wins = len([t for t in self.trades_today if t.get('win')])
        total = len(self.trades_today)
        print("\n" + "=" * 60)
        print("📊 今日交易总结")
        print("=" * 60)
        print(f"  策略版本: v7 Multi-Engine | 09:35-15:50美东")
        print(f"  交易次数: {total} (做多: {sum(1 for t in self.trades_today if t.get('dir')=='call')}, "
              f"做空: {sum(1 for t in self.trades_today if t.get('dir')=='put')})")
        print(f"  胜率: {wins}/{total} ({wins/total*100:.0f}%)" if total > 0 else "  胜率: N/A")
        print(f"  累计盈亏: ${self.daily_pnl:+,.2f}")
        print("=" * 60)

    def _sync_account_state(self, silent=False):
        """从长桥实时拉取账户资金和期权持仓，写入 state.json 供 Web 显示"""
        try:
            account_info = {}
            positions = []

            # 1. 拉取账户资金
            try:
                balance = self.trade_ctx.account_balance()
                if balance:
                    # balance 本身就是一个 list
                    currencies_list = list(balance) if isinstance(balance, (list, tuple)) else (getattr(balance, 'currencies', None) or [])
                    # 累加器：合并所有币种，统一为 USD
                    total_net_assets_usd = 0.0
                    total_cash_usd = 0.0
                    total_buying_power_usd = 0.0
                    
                    for cur in currencies_list:
                        currency = str(getattr(cur, 'currency', 'unknown') or 'unknown')
                        net_assets = float(getattr(cur, 'net_assets', 0) or 0)
                        total_cash_val = float(getattr(cur, 'total_cash', 0) or 0)
                        # cash 字段在 SDK 4.x 中不存在，直接用 total_cash 更可靠
                        cash = 0.0
                        cash_attr = getattr(cur, 'cash', None)
                        if cash_attr is not None:
                            cash = float(cash_attr or 0)
                        else:
                            cash = total_cash_val
                        market_value = float(getattr(cur, 'market_value', 0) or 0)
                        # buy_power 在 SDK 4.x 中叫 buy_power，旧版叫 buying_power
                        buying_power = 0.0
                        for bp_attr in ['buy_power', 'buying_power', 'max_power', 'power', 'available_funds', 'max_power_long']:
                            bp_val = getattr(cur, bp_attr, None)
                            if bp_val is not None:
                                buying_power = float(bp_val or 0)
                                if buying_power > 0:
                                    break
                        if buying_power == 0:
                            buying_power = cash
                        
                        # 转换为 USD
                        if currency == 'HKD':
                            rate = 7.8
                            net_assets_usd = net_assets / rate
                            cash_usd = cash / rate
                            total_cash_usd += total_cash_val / rate
                            buying_power_usd = buying_power / rate
                        elif currency == 'CNY':
                            rate = 7.2  # 人民币汇率，可根据需要调整
                            net_assets_usd = net_assets / rate
                            cash_usd = cash / rate
                            total_cash_usd += total_cash_val / rate
                            buying_power_usd = buying_power / rate
                        else:
                            # USD 或其他货币（如 SGD、EUR 等），暂按 1:1
                            net_assets_usd = net_assets
                            cash_usd = cash
                            total_cash_usd += total_cash_val
                            buying_power_usd = buying_power
                        
                        total_net_assets_usd += net_assets_usd
                        total_cash_usd += cash_usd
                        total_buying_power_usd += buying_power_usd
                    
                    # 保存合并后的扁平结构（Web 用）
                    account_info = {
                        'net_assets': total_net_assets_usd,
                        'cash': total_buying_power_usd,
                        'buying_power': total_cash_usd,
                    }
                    
                    # 打印日志（仅首次启动时）
                    if not silent:
                        print(f"💰 账户资金 (USD): 净值=${total_net_assets_usd:,.0f} 现金=${total_buying_power_usd:,.0f} 购买力=${total_cash_usd:,.0f}")
                    # 兜底
                    if not currencies_list:
                        for attr in ['total_assets', 'net_assets', 'cash', 'market_value']:
                            val = getattr(balance, attr, None)
                            if val is not None and val != 0:
                                account_info['_direct'] = account_info.get('_direct', {})
                                account_info['_direct'][attr] = float(val or 0)

                    if account_info:
                        # 更新实际资金（用于通知/风控计算）
                        if total_net_assets_usd > 0:
                            self.actual_capital = total_net_assets_usd
                        # 修复: account_info 值可能是 float (net_assets/cash) 或 dict (_direct)
                        if not silent:
                            summary_parts = []
                            for k, v in account_info.items():
                                if isinstance(v, dict):
                                    summary_parts.append(f"{k}=${v.get('net_assets',0):,.0f}")
                                else:
                                    summary_parts.append(f"{k}=${v:,.0f}")
                            summary = ', '.join(summary_parts)
                            print(f"💰 账户资金: {summary}")
                    else:
                        if not silent:
                            print(f"  ⚠️ account_balance 返回空: {type(balance).__name__} {dir(balance)}")
            except Exception as e:
                if not silent:
                    print(f"  ⚠️ 拉取账户资金失败: {e}")

            # 2. 拉取实际持仓（含期权+正股）
            try:
                lb_pos = self.trade_ctx.stock_positions()
                if lb_pos:
                    for ch in getattr(lb_pos, 'channels', []) or []:
                        for p in getattr(ch, 'positions', []) or []:
                            symbol = str(getattr(p, 'symbol', '') or '')
                            if not symbol:
                                continue
                            qty = float(getattr(p, 'quantity', 0) or 0)
                            if qty <= 0:
                                continue
                            available = float(getattr(p, 'available_quantity', qty) or qty)
                            cost = float(getattr(p, 'cost_price', 0) or 0)
                            channel = str(getattr(ch, 'name', '') or str(getattr(ch, 'channel', '')))
                            opt_match = re.search(r'\d{6}([CP])', symbol.upper())
                            direction = ''
                            if opt_match:
                                direction = 'call' if opt_match.group(1) == 'C' else 'put'
                            positions.append({
                                'symbol': symbol,
                                'opt_symbol': symbol,
                                'dir': direction,
                                'qty': int(qty),
                                'contracts': int(qty),
                                'available': int(available),
                                'cost': cost,
                                'entry_opt_price': cost,
                                'channel': channel,
                            })
            except Exception as e:
                print(f"  ⚠️ 拉取持仓失败: {e}")

            # 3. 写入 state.json
            self._account_state = account_info
            self._broker_positions = positions
            self._save_state()  # _save_state 会把这些字段打包进 state
            
            # 更新v7 Dashboard
            try:
                import dashboard_v7
                dashboard_v7.update_account(account_info)
                dashboard_v7.update_position(self.position)
                dashboard_v7.update_broker_positions(positions)
                dashboard_v7.update_pnl(self.daily_pnl, len(self.trades_today))
                dashboard_v7.update_trades(self.trades_today)
                dashboard_v7.update_vix(self.v7.get_vix_state())
                dashboard_v7.update_price(self.current_price)
                dashboard_v7.update_candle_count(len(self.one_min_candles))
                dashboard_v7.set_connected(True)
                dashboard_v7.set_running(self.running)
                dashboard_v7.update_filter_status(self.filter_status)
            except Exception:
                pass

        except Exception as e:
            print(f"  ⚠️ 同步账户状态失败: {e}")

    def _sync_longbridge_orders(self):
        """从长桥同步今日所有订单信息，保存到本地文件供web端读取"""
        try:
            all_orders = self.trade_ctx.today_orders()
            if not all_orders:
                if not getattr(self, '_warned_empty_orders', False):
                    print(f"  ⚠️ 长桥返回空订单列表（仅提示一次）")
                    self._warned_empty_orders = True
                return
            self._warned_empty_orders = False
            
            print(f"  📥 长桥返回 {len(all_orders)} 笔订单")
            
            orders = []
            for o in all_orders:
                try:
                    exec_qty = float(getattr(o, 'executed_quantity', 0) or 0)
                    exec_price = float(getattr(o, 'executed_price', 0) or 0)
                    side = '买入' if str(o.side) == 'OrderSide.Buy' else '卖出'
                    orders.append({
                        'symbol': str(o.symbol),
                        'side': side,
                        'quantity': int(o.quantity),
                        'executed_qty': exec_qty,
                        'executed_price': exec_price,
                        'status': str(o.status).replace('OrderStatus.', ''),
                    })
                except Exception as e:
                    print(f"  ⚠️ 解析订单失败: {e}")
            
            # 保存到本地文件（原子写入，防止截断）
            script_dir = str(_app_dir())
            filepath = os.path.join(script_dir, 'longbridge_orders.json')
            tmp_path = filepath + '.tmp'
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump({
                    'orders': orders,
                    'total': len(orders),
                    'buy_count': sum(1 for o in orders if o['side'] == '买入'),
                    'sell_count': sum(1 for o in orders if o['side'] == '卖出'),
                    'updated': datetime.now(TZ_ET).strftime('%Y-%m-%d %H:%M:%S'),
                }, f, ensure_ascii=False, indent=2, default=_json_default)
            # Windows文件锁定重试机制
            for attempt in range(5):
                try:
                    os.replace(tmp_path, filepath)  # 原子替换
                    break
                except OSError as e:
                    if attempt < 4:
                        time.sleep(0.2)
                    else:
                        raise
            
            print(f"  📤 长桥订单已同步: {len(orders)}笔 (买入:{sum(1 for o in orders if o['side']=='买入')}, 卖出:{sum(1 for o in orders if o['side']=='卖出')})")
            
        except Exception as e:
            import traceback
            print(f"  ❌ 同步长桥订单失败: {e}")
            print(f"  {traceback.format_exc()}")

    def _count_broker_trades(self, lb_data):
        """计算broker数据中可配对的交易数（只有买有卖的才算）"""
        from collections import defaultdict
        orders = lb_data.get('orders', [])
        filled = [o for o in orders if o.get('status') == 'Filled']
        symbol_data = defaultdict(lambda: {'buys': 0, 'sells': 0})
        for o in filled:
            sym = o['symbol']
            if o.get('side') == '买入':
                symbol_data[sym]['buys'] += 1
            elif o.get('side') == '卖出':
                symbol_data[sym]['sells'] += 1
        return sum(1 for d in symbol_data.values() if d['buys'] > 0 and d['sells'] > 0)

    def _load_today_records(self):
        """恢复今日交易记录（从 records/ 目录，避免重启后数据丢失）"""
        from zoneinfo import ZoneInfo
        TZ_ET = ZoneInfo("America/New_York")
        today_et = datetime.now(TZ_ET).strftime('%Y-%m-%d')

        script_dir = str(_app_dir())
        records_dir = os.path.join(script_dir, 'records')
        if not os.path.isdir(records_dir):
            print("📥 无 records/ 目录，跳过恢复")
            return

        # 记录文件统一用美东时间命名
        record_file = os.path.join(records_dir, f'{today_et}.json')

        if not os.path.exists(record_file):
            print(f"📥 今日({today_et}) 无交易记录文件，跳过恢复")
            return

        try:
            with open(record_file, encoding='utf-8') as f:
                data = json.load(f)

            trades = data.get('trades', [])
            if not trades:
                print(f"📥 今日记录文件存在但无交易，跳过恢复")
                return

            # 过滤：只加载 date 字段匹配今天的交易（防止跨天错误写入）
            trades = [t for t in trades if t.get('date', today_et) == today_et]
            if not trades:
                print(f"📥 今日记录文件中无今日交易，跳过恢复")
                return

            # 恢复 trades_today 和 daily_pnl
            for t in trades:
                entry_time_str = t.get('entry_time', '00:00:00')
                exit_time_str = t.get('exit_time', entry_time_str)
                try:
                    entry_time = datetime.strptime(entry_time_str, '%H:%M:%S').replace(
                        year=datetime.now(TZ_ET).year, month=datetime.now(TZ_ET).month, day=datetime.now(TZ_ET).day,
                        tzinfo=TZ_ET
                    )
                    exit_time = datetime.strptime(exit_time_str, '%H:%M:%S').replace(
                        year=datetime.now(TZ_ET).year, month=datetime.now(TZ_ET).month, day=datetime.now(TZ_ET).day,
                        tzinfo=TZ_ET
                    )
                except Exception:
                    entry_time = datetime.now(TZ_ET)
                    exit_time = datetime.now(TZ_ET)

                restored = {
                    'time': entry_time_str,  # web 用的 time 字段
                    'dir': t.get('dir', ''),
                    # 🔧 优先保存期权开仓价（entry_opt_price），否则 fallback 到 entry_price
                    'entry_price': t.get('entry_opt_price') or t.get('entry_price', 0),
                    'exit_opt_price': t.get('exit_price', 0),  # 和 position 里的一致
                    'exit_price': t.get('exit_price', 0),
                    'contracts': t.get('contracts', 0),
                    'pnl_pct': t.get('pnl_pct', 0),
                    'pnl_usd': t.get('pnl_usd', 0),
                    'reason': t.get('reason', ''),
                    'exit_reason': t.get('exit_reason', ''),
                    'result': t.get('result', ''),
                    'opt_symbol': t.get('opt_symbol', ''),
                    'win': t.get('result') == 'win',
                    'entry_time': entry_time,
                    'exit_time': exit_time,
                    'regime': t.get('regime', 'neutral'),
                    'atr_at_entry': t.get('atr_at_entry', 0),
                    'macd_hist_entry': t.get('macd_hist_entry', 0),
                    'vwap_entry': t.get('vwap_entry', 0),
                    'half_closed': t.get('half_closed', False),
                }
                self.trades_today.append(restored)
                self.daily_pnl += t.get('pnl_usd', 0)
                # 恢复方向累计盈亏
                if t.get('dir') == 'call':
                    self.call_pnl += t.get('pnl_usd', 0)
                elif t.get('dir') == 'put':
                    self.put_pnl += t.get('pnl_usd', 0)

            wins = sum(1 for t in trades if t.get('result') == 'win')
            total = len(trades)
            
            # 恢复最大盈亏统计
            for t in trades:
                pnl_usd = t.get('pnl_usd', 0)
                pnl_pct = t.get('pnl_pct', 0)
                if pnl_usd > self.largest_win_usd:
                    self.largest_win_usd = pnl_usd
                    self.largest_win_pct = pnl_pct
                if pnl_usd < self.largest_loss_usd:
                    self.largest_loss_usd = pnl_usd
                    self.largest_loss_pct = pnl_pct
            
            print(f"📥 恢复今日交易记录: {total}笔 (胜{wins}/负{total-wins}) 盈亏${self.daily_pnl:+,.2f}")

        except Exception as e:
            print(f"⚠️ 恢复今日记录失败: {e}")
            import traceback
            traceback.print_exc()

    def _load_today_signal_probes(self):
        """恢复今日信号追踪记录，重启后继续补齐5/10/20根K线。"""
        try:
            today_et = datetime.now(TZ_ET).strftime('%Y-%m-%d')
            filepath = os.path.join(_app_dir(), 'records', f'signal_probes_{today_et}.json')
            if not os.path.exists(filepath):
                return
            with open(filepath, encoding='utf-8') as f:
                data = json.load(f)
            probes = data.get('probes', [])
            if not probes:
                return
            self.signal_probes = []
            for p in probes:
                milestones = p.get('milestones') or {5: None, 10: None, 20: None}
                norm_milestones = {}
                for k, v in milestones.items():
                    try:
                        norm_milestones[int(k)] = v
                    except Exception:
                        pass
                restored = {
                    'id': int(p.get('id', len(self.signal_probes) + 1)),
                    'entry_time': p.get('entry_time', ''),
                    'entry_bar': int(p.get('entry_bar', len(self.one_min_candles))),
                    'signal': p.get('signal', ''),
                    'dir': p.get('dir', ''),
                    'entry_price': float(p.get('entry_price', 0) or 0),
                    'opt_symbol': p.get('opt_symbol', ''),
                    'contracts': int(p.get('contracts', 0) or 0),
                    'reason': p.get('reason', ''),
                    'regime': p.get('regime', ''),
                    'source': p.get('source', 'live'),
                    'rejection_reason': p.get('rejection_reason', ''),
                    'm5_pct': p.get('m5_pct'),
                    'm10_pct': p.get('m10_pct'),
                    'm20_pct': p.get('m20_pct'),
                    'm5_price': p.get('m5_price'),
                    'm10_price': p.get('m10_price'),
                    'm20_price': p.get('m20_price'),
                    'milestones': norm_milestones or {5: None, 10: None, 20: None},
                    'completed': bool(p.get('completed', False)),
                }
                self.signal_probes.append(restored)
            self._signal_probe_seq = max((p.get('id', 0) for p in self.signal_probes), default=0)
            print(f"📥 恢复今日信号追踪: {len(self.signal_probes)}条")
            try:
                import dashboard_v7
                dashboard_v7.update_signal_probes(self._serialize_signal_probes())
            except Exception:
                pass
        except Exception as e:
            print(f"⚠️ 恢复信号追踪失败: {e}")

    def _save_pending_records(self):
        """启动时保存上次未写入的交易记录（进程被kill -9不会调stop()）"""
        import json
        from zoneinfo import ZoneInfo
        TZ_ET = ZoneInfo("America/New_York")

        script_dir = str(_app_dir())
        lb_file = os.path.join(script_dir, 'longbridge_orders.json')
        if not os.path.exists(lb_file):
            return

        try:
            with open(lb_file, encoding='utf-8') as f:
                lb_data = json.load(f)
        except:
            return

        orders = lb_data.get('orders', [])
        filled = [o for o in orders if o.get('status') == 'Filled']
        if not filled:
            return

        # 从期权代码推断交易日期（到期日=交易日，0DTE）
        from collections import Counter
        dates = []
        for o in filled:
            sym = o['symbol'].replace('.US', '')
            date_part = sym[3:9]  # QQQ260429 → 260429
            try:
                y = 2000 + int(date_part[:2])
                m = int(date_part[2:4])
                d = int(date_part[4:6])
                dates.append(f"{y}-{m:02d}-{d:02d}")
            except:
                pass

        if not dates:
            return

        most_common_date = Counter(dates).most_common(1)[0][0]
        today_et = datetime.now(TZ_ET).strftime('%Y-%m-%d')

        # 如果broker数据的日期是今天或更新，说明是当前交易日，不用保存
        if most_common_date > today_et:
            return
        if most_common_date == today_et:
            # 今天的交易在收盘后由stop()保存，启动时不需要
            # 但如果records文件不存在，可能是被kill后重启，需要保存
            pass

# records文件统一用美东日期
        records_dir = os.path.join(script_dir, 'records')
        record_file = os.path.join(records_dir, f'{most_common_date}.json')

        # 先用broker数据计算期望的交易数
        expected_trades = self._count_broker_trades(lb_data)

        if os.path.exists(record_file):
            try:
                with open(record_file, encoding='utf-8') as f:
                    existing = json.load(f)
                # 如果已有文件且交易数>=broker对账数，跳过（已完整保存）
                existing_count = len(existing.get('trades', []))
                if existing_count >= expected_trades:
                    print(f"📋 {most_common_date}记录已存在({existing_count}笔,期望{expected_trades}笔)，跳过")
                    return
                else:
                    print(f"⚠️ {most_common_date}记录不完整({existing_count}/{expected_trades}笔)，将用broker数据覆盖")
            except:
                pass
        print(f"🔄 发现未保存的{most_common_date}(ET)交易记录，正在对账...")
        # 用对账逻辑重建并保存
        self._reconcile_and_save(lb_data, most_common_date)

    def _reconcile_and_save(self, lb_data, trade_date):
        """从broker数据对账并保存到指定日期的records文件"""
        from collections import defaultdict
        from zoneinfo import ZoneInfo
        TZ_ET = ZoneInfo("America/New_York")

        orders = lb_data.get('orders', [])
        filled = [o for o in orders if o.get('status') == 'Filled']

        symbol_data = defaultdict(lambda: {'buys': [], 'sells': []})
        for o in filled:
            sym = o['symbol']
            qty = float(o.get('executed_qty', 0) or o.get('quantity', 0))
            price = float(o.get('executed_price', 0) or 0)
            if o.get('side') == '买入':
                symbol_data[sym]['buys'].append({'qty': qty, 'price': price})
            elif o.get('side') == '卖出':
                symbol_data[sym]['sells'].append({'qty': qty, 'price': price})

        formatted_trades = []
        total_pnl = 0

        for sym in sorted(symbol_data.keys()):
            d = symbol_data[sym]
            buys = list(d['buys'])
            sells = list(d['sells'])
            if not sells:
                continue

            total_buy_qty = sum(b['qty'] for b in buys)
            total_sell_qty = sum(s['qty'] for s in sells)
            if total_buy_qty <= 0 or total_sell_qty <= 0:
                continue

            unmatched_buys = list(buys)
            sym_pnl = 0
            matched_qty = 0
            avg_buy = sum(b['qty'] * b['price'] for b in buys) / total_buy_qty
            avg_sell = sum(s['qty'] * s['price'] for s in sells) / total_sell_qty

            for sell in sells:
                sq = sell['qty']
                sp = sell['price']
                while sq > 0 and unmatched_buys:
                    buy = unmatched_buys[0]
                    match = min(sq, buy['qty'])
                    pnl = match * (sp - buy['price']) * 100
                    sym_pnl += pnl
                    matched_qty += match
                    buy['qty'] -= match
                    sq -= match
                    if buy['qty'] <= 0:
                        unmatched_buys.pop(0)

            opt_code = sym.replace('.US', '')
            rest = opt_code[9:]
            opt_type = rest[0]
            strike = float(rest[1:]) / 1000
            direction = 'call' if opt_type == 'C' else 'put'
            pnl_pct = (avg_sell - avg_buy) / avg_buy * 100 if avg_buy > 0 else 0
            matched_contracts = int(matched_qty)
            total_pnl += sym_pnl

            formatted_trades.append({
                'date': trade_date,
                'time': 'reconcile',
                'dir': direction,
                'entry_price': strike,
                'exit_price': avg_sell,
                'qty': matched_contracts * 100,
                'contracts': matched_contracts,
                'pnl_pct': round(pnl_pct, 2),
                'pnl_usd': round(sym_pnl, 2),
                'result': 'win' if sym_pnl > 0 else 'lose' if sym_pnl < 0 else '',
                'reason': f'启动对账({len(buys)}买/{len(sells)}卖)',
                'exit_reason': '启动对账',
                'opt_symbol': sym,
                'entry_opt_price': round(avg_buy, 2),
                '_source': 'startup_reconcile',
            })

        if not formatted_trades:
            return

        # 保存
        script_dir = str(_app_dir())
        records_dir = os.path.join(script_dir, 'records')
        os.makedirs(records_dir, exist_ok=True)
        filepath = os.path.join(records_dir, f'{trade_date}.json')
        tmp_path = filepath + '.tmp'
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump({
                'date': trade_date,
                'trades': formatted_trades,
                'total': len(formatted_trades),
                'wins': sum(1 for t in formatted_trades if t.get('result') == 'win'),
                'pnl': round(total_pnl, 2),
            }, f, ensure_ascii=False, indent=2, default=_json_default)
        # Windows文件锁定重试机制
        for attempt in range(5):
            try:
                os.replace(tmp_path, filepath)
                break
            except OSError as e:
                if attempt < 4:
                    time.sleep(0.2)
                else:
                    raise
        print(f"💾 启动对账完成: {filepath} ({len(formatted_trades)}笔, PnL=${total_pnl:+,.2f})")

    def _reconcile_trades_from_broker(self):
        """从 longbridge_orders.json 对账重建今日交易记录（FIFO配对）"""
        from collections import defaultdict
        from zoneinfo import ZoneInfo
        TZ_ET = ZoneInfo("America/New_York")

        script_dir = str(_app_dir())
        lb_file = os.path.join(script_dir, 'longbridge_orders.json')
        if not os.path.exists(lb_file):
            return []

        try:
            with open(lb_file, encoding='utf-8') as f:
                lb_data = json.load(f)
        except:
            return []

        orders = lb_data.get('orders', [])
        # 只处理 Filled 的订单
        filled = [o for o in orders if o.get('status') == 'Filled']
        if not filled:
            return []

        # 按合约分组，FIFO配对买卖
        symbol_data = defaultdict(lambda: {'buys': [], 'sells': []})
        for o in filled:
            sym = o['symbol']
            qty = float(o.get('executed_qty', 0) or o.get('quantity', 0))
            price = float(o.get('executed_price', 0) or 0)
            if o.get('side') == '买入':
                symbol_data[sym]['buys'].append({'qty': qty, 'price': price})
            elif o.get('side') == '卖出':
                symbol_data[sym]['sells'].append({'qty': qty, 'price': price})

        # 获取美东时间的今天日期
        today_et = datetime.now(TZ_ET).strftime('%Y-%m-%d')

        reconciled = []
        for sym in sorted(symbol_data.keys()):
            d = symbol_data[sym]
            buys = list(d['buys'])
            sells = list(d['sells'])

            # 只处理有买有卖的（已平仓）
            if not sells:
                continue

            total_buy_qty = sum(b['qty'] for b in buys)
            total_sell_qty = sum(s['qty'] for s in sells)
            if total_buy_qty <= 0 or total_sell_qty <= 0:
                continue

            # FIFO配对计算盈亏
            unmatched_buys = list(buys)
            total_pnl = 0
            matched_qty = 0
            avg_buy = sum(b['qty'] * b['price'] for b in buys) / total_buy_qty
            avg_sell = sum(s['qty'] * s['price'] for s in sells) / total_sell_qty

            for sell in sells:
                sq = sell['qty']
                sp = sell['price']
                while sq > 0 and unmatched_buys:
                    buy = unmatched_buys[0]
                    match = min(sq, buy['qty'])
                    pnl = match * (sp - buy['price']) * 100  # 期权乘数100
                    total_pnl += pnl
                    matched_qty += match
                    buy['qty'] -= match
                    sq -= match
                    if buy['qty'] <= 0:
                        unmatched_buys.pop(0)

            # 判断方向：从期权代码提取
            # QQQ260429C662000.US → C = Call, P = Put
            opt_code = sym.replace('.US', '')
            date_part = opt_code[3:9]  # 260429
            rest = opt_code[9:]  # C662000 or P655000
            opt_type = rest[0]  # C or P
            strike = float(rest[1:]) / 1000  # 662000 → 662.0
            direction = 'call' if opt_type == 'C' else 'put'

            # 提取到期日 → 入场时间估算
            # 260429 = 2026-04-29 到期，说明是4月29日的交易
            try:
                exp_year = 2000 + int(date_part[:2])
                exp_month = int(date_part[2:4])
                exp_day = int(date_part[4:6])
                trade_date = f"{exp_year}-{exp_month:02d}-{exp_day:02d}"
            except:
                trade_date = today_et

            # 计算盈亏百分比
            pnl_pct = (avg_sell - avg_buy) / avg_buy * 100 if avg_buy > 0 else 0
            matched_contracts = int(matched_qty)

            reconciled.append({
                'date': trade_date,
                'entry_time': today_et,
                'exit_time': today_et,
                'dir': direction,
                'entry_price': round(avg_buy, 2),
                'exit_price': avg_sell,
                'qty': matched_contracts * 100,
                'contracts': matched_contracts,
                'pnl_pct': round(pnl_pct, 2),
                'pnl_usd': round(total_pnl, 2),
                'result': 'win' if total_pnl > 0 else 'lose' if total_pnl < 0 else '',
                'reason': f'broker对账({len(buys)}买/{len(sells)}卖,配对{matched_contracts}张)',
                'exit_reason': 'broker对账',
                'opt_symbol': sym,
                'entry_opt_price': round(avg_buy, 2),
                '_source': 'broker_reconcile',
            })

        return reconciled

    def _save_records_snapshot(self):
        """每次平仓后实时保存当日记录到 records/（不依赖 Gist 同步）"""
        from zoneinfo import ZoneInfo
        TZ_ET = ZoneInfo("America/New_York")

        if not self.trades_today:
            return

        try:
            today_et = datetime.now(TZ_ET).strftime('%Y-%m-%d')
            script_dir = str(_app_dir())
            records_dir = os.path.join(script_dir, 'records')
            os.makedirs(records_dir, exist_ok=True)
            filepath = os.path.join(records_dir, f'{today_et}.json')

            trades = []
            for t in self.trades_today:
                if t.get('exit_time') is None:
                    continue  # 只保存已平仓的交易
                if not t.get('opt_symbol') or int(t.get('contracts') or 0) <= 0:
                    continue
                entry_time = t.get('entry_time')
                exit_time = t.get('exit_time')
                if isinstance(entry_time, datetime):
                    entry_time_str = entry_time.strftime('%H:%M:%S')
                else:
                    entry_time_str = str(entry_time)
                if isinstance(exit_time, datetime):
                    exit_time_str = exit_time.strftime('%H:%M:%S')
                else:
                    exit_time_str = str(exit_time)

                trades.append({
                    'entry_time': entry_time_str,
                    'exit_time': exit_time_str,
                    'dir': t.get('dir', ''),
                    # 🔧 优先保存期权开仓价（entry_opt_price），否则 fallback 到 entry_price
                    'entry_price': t.get('entry_opt_price') or t.get('entry_price', 0),
                    'exit_price': t.get('exit_opt_price') or t.get('exit_price', 0),
                    'contracts': t.get('contracts', 0),
                    'pnl_pct': round(t.get('pnl_pct', 0), 2),
                    'pnl_usd': round(t.get('pnl_usd', 0), 2),
                    'result': 'win' if t.get('win') else ('lose' if t.get('win') is False else ''),
                    'reason': t.get('reason', ''),
                    'exit_reason': t.get('exit_reason', ''),
                    'opt_symbol': t.get('opt_symbol', ''),
                    'regime': t.get('regime', 'neutral'),
                    'atr_at_entry': t.get('atr_at_entry', 0),
                    'macd_hist_entry': t.get('macd_hist_entry', 0),
                    'vwap_entry': t.get('vwap_entry', 0),
                    'sma20_entry': t.get('sma20_entry', 0),
                    'half_closed': t.get('half_closed', False),
                    '_source': 'live',
                })

            if not trades:
                return

            wins = sum(1 for t in trades if t['result'] == 'win')
            total_pnl = sum(t['pnl_usd'] for t in trades)

            import json
            tmp_path = filepath + '.tmp'
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump({
                    'date': today_et,
                    'trades': trades,
                    'total': len(trades),
                    'wins': wins,
                    'win_rate': round(wins / len(trades) * 100, 1) if trades else 0,
                    'pnl': round(total_pnl, 2),
                    'signal_probes': self._serialize_signal_probes(),
                    'updated': datetime.now(TZ_ET).strftime('%Y-%m-%d %H:%M:%S'),
                }, f, ensure_ascii=False, indent=2, default=_json_default)
            # Windows文件锁定重试机制
            for attempt in range(5):
                try:
                    os.replace(tmp_path, filepath)
                    break
                except OSError as e:
                    if attempt < 4:
                        time.sleep(0.2)
                    else:
                        raise
            print(f"📋 实时记录已覆盖: {today_et} ({len(trades)}笔, 胜率{wins}/{len(trades)}, ${total_pnl:+,.2f})")
        except Exception as e:
            print(f"  ⚠️ 实时记录保存失败: {e}")

    def _save_daily_records(self):
        """保存今日交易记录到 JSON 文件"""
        from zoneinfo import ZoneInfo
        TZ_ET = ZoneInfo("America/New_York")
        today_et = datetime.now(TZ_ET).strftime('%Y-%m-%d')

        # 先同步长桥订单信息
        self._sync_longbridge_orders()

        # 从broker对账重建交易记录（即使trades_today为空也能记录）
        broker_trades = self._reconcile_trades_from_broker()

        # 检查是否有新发现的broker对账平仓，发送通知。
        # 不能只按 opt_symbol 去重：同一天同一合约会反复开平，按合约去重会漏发前几单。
        notified_keys = self._load_notification_keys(include_recent_days=True)
        allow_broker_notify = self._allow_broker_exit_notifications_now()
        if not allow_broker_notify:
            print("  ⏭️ 非交易时间，跳过broker对账平仓补漏通知")
        for bt in broker_trades:
            if not allow_broker_notify:
                continue
            if bt.get('date') != today_et:
                continue
            sym = bt.get('opt_symbol', '')
            pnl = bt.get('pnl_usd', 0)
            notify_key = self._trade_notify_key(bt, 'broker_exit')
            if sym and pnl != 0 and notify_key not in notified_keys:
                if self._recent_live_exit_notification_exists(sym):
                    self._mark_notification_sent(notify_key, 'broker_exit_suppressed', sym)
                    notified_keys.add(notify_key)
                    print(f"  ⏭️ broker对账平仓通知已抑制，实盘路径刚发过: {sym}")
                    continue
                direction = bt.get('dir', '').upper()
                entry_price = bt.get('entry_price', 0)
                exit_price = bt.get('exit_price', 0)
                contracts = bt.get('contracts', 0)
                pnl_pct = bt.get('pnl_pct', 0)
                emoji = '🟢' if pnl > 0 else '🔴'
                notified = self._notify(
                    f"🏁 平仓 {sym}",
                    'exit',
                    pos={
                        'opt_symbol': sym,
                        'dir': bt.get('dir', ''),
                        'entry_opt_price': entry_price,
                        'exit_opt_price': exit_price,
                        'contracts': contracts,
                        'pnl_pct': pnl_pct,
                        'pnl_usd': pnl,
                        'day_market_regime': self.day_market_regime.get('type', '') if isinstance(self.day_market_regime, dict) else '',
                        'day_market_label': self.day_market_regime.get('label', '') if isinstance(self.day_market_regime, dict) else '',
                        'day_market_direction': self.day_market_regime.get('direction', '') if isinstance(self.day_market_regime, dict) else '',
                    },
                    reason='broker对账',
                    entry_opt=entry_price,
                    exit_opt=exit_price,
                    pnl_pct=pnl_pct,
                    pnl_usd=pnl,
                )
                if notified:
                    self._mark_notification_sent(notify_key, 'broker_exit', sym)
                    notified_keys.add(notify_key)
                    print(f"  {emoji} broker对账平仓通知: {sym} {direction} {contracts}张 ${pnl:+,.2f}")
                else:
                    print(f"  ⚠️ broker对账平仓通知发送失败，稍后会重试: {sym} {direction} {contracts}张 ${pnl:+,.2f}")

        # 合并：broker对账数据 + 内部trades_today（去重）
        # broker数据更可靠，作为主源；trades_today补充未平仓的
        seen_symbols = set()
        all_trades = []

        # 先放broker对账数据（完整准确）
        for bt in broker_trades:
            key = f"{bt['opt_symbol']}_{bt['contracts']}"
            if key not in seen_symbols:
                seen_symbols.add(key)
                all_trades.append(bt)

        # 再补internal trades_today中没有被broker覆盖的
        for t in (self.trades_today or []):
            opt_sym = t.get('opt_symbol', '')
            contracts = t.get('contracts', 0)
            if not opt_sym or int(contracts or 0) <= 0:
                continue
            key = f"{opt_sym}_{contracts}"
            if key not in seen_symbols:
                # 这笔交易broker没有对账记录，用internal数据
                entry_time = t.get('entry_time', '')
                exit_time = t.get('exit_time', '')
                if isinstance(entry_time, datetime):
                    time_str = entry_time.strftime('%H:%M:%S')
                else:
                    time_str = str(entry_time)[:8]
                if isinstance(exit_time, datetime):
                    exit_time_str = exit_time.strftime('%H:%M:%S')
                else:
                    exit_time_str = str(exit_time)[:8]
                pnl = t.get('pnl_pct', t.get('max_pnl_pct', 0))
                all_trades.append({
                    'date': datetime.now(TZ_ET).strftime('%Y-%m-%d'),
                    'entry_time': time_str,
                    'exit_time': exit_time_str,
                    'dir': t.get('dir', ''),
                    # 🔧 优先保存期权开仓价（entry_opt_price），否则 fallback 到 entry_price
                    'entry_price': t.get('entry_opt_price') or t.get('entry_price', 0),
                    'exit_price': t.get('exit_opt_price', 0),
                    'qty': t.get('quantity', 0),
                    'contracts': t.get('contracts', 0),
                    'pnl_pct': round(pnl, 2) if pnl else 0,
                    'pnl_usd': round(t.get('pnl_usd', 0), 2),
                    'result': 'win' if t.get('win') else ('lose' if t.get('win') is False else ''),
                    'reason': t.get('reason', ''),
                    'exit_reason': t.get('exit_reason', ''),
                    'opt_symbol': opt_sym,
                    'regime': t.get('regime', 'neutral'),
                    'day_market_regime': t.get('day_market_regime', ''),
                    'day_market_label': t.get('day_market_label', ''),
                    'day_market_direction': t.get('day_market_direction', ''),
                    'atr_at_entry': t.get('atr_at_entry', 0),
                    'macd_hist_entry': t.get('macd_hist_entry', 0),
                    'vwap_entry': t.get('vwap_entry', 0),
                    '_source': 'internal',
                })
                seen_symbols.add(key)
        
        if not all_trades:
            print("📋 今日无交易记录，跳过保存")
            return

        try:
            # 从交易数据推断日期（broker对账的用期权到期日，internal的用当前ET）
            from collections import Counter
            dates_from_trades = [t.get('date', '') for t in all_trades if t.get('date')]
            today = Counter(dates_from_trades).most_common(1)[0][0] if dates_from_trades else datetime.now(TZ_ET).strftime('%Y-%m-%d')
            script_dir = str(_app_dir())
            records_dir = os.path.join(script_dir, 'records')
            os.makedirs(records_dir, exist_ok=True)

            # all_trades 已经是格式化好的（来自broker对账 + internal补充）
            formatted_trades = all_trades

            # 从broker对账数据计算总PnL（比self.daily_pnl更准确）
            broker_pnl = sum(t.get('pnl_usd', 0) for t in formatted_trades if t.get('_source') == 'broker_reconcile')
            total_pnl = broker_pnl if broker_pnl != 0 else self.daily_pnl

            # 保存当日文件（用美东日期）
            filepath = os.path.join(records_dir, f'{today}.json')
            tmp_path = filepath + '.tmp'
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump({
                    'date': today,
                    'trades': formatted_trades,
                    'total': len(formatted_trades),
                    'wins': sum(1 for t in formatted_trades if t.get('result') == 'win'),
                    'pnl': round(total_pnl, 2),
                    'signal_probes': self._serialize_signal_probes(),
                }, f, ensure_ascii=False, indent=2, default=_json_default)
            # Windows文件锁定重试机制
            for attempt in range(5):
                try:
                    os.replace(tmp_path, filepath)  # 原子替换
                    break
                except OSError as e:
                    if attempt < 4:
                        time.sleep(0.2)
                    else:
                        raise

            broker_count = sum(1 for t in formatted_trades if t.get('_source') == 'broker_reconcile')
            internal_count = sum(1 for t in formatted_trades if t.get('_source') == 'internal')
            print(f"💾 交易记录已保存: {filepath} ({len(formatted_trades)}笔: broker={broker_count}, internal={internal_count})")
            print(f"📊 总盈亏: ${total_pnl:+,.2f}")
            print(f"📤 正在同步到 Gist...")

            # 自动调用 update_gist（打包后直接import，避免subprocess无法启动子进程）
            try:
                from update_gist import main as sync_gist_main
                sync_gist_main()
            except Exception as gist_err:
                print(f"⚠️ Gist同步失败: {gist_err}")

        except Exception as e:
            print(f"❌ 保存记录失败: {e}")


def main():
    trader = QQQLiveTrader(CONFIG)

    def signal_handler(sig, frame):
        sig_name = 'Ctrl+C' if sig == signal.SIGINT else 'SIGTERM'
        trader.stop()
        try:
            trader._notify(f"⏹️ 系统停止", msg_type='shutdown', reason=sig_name)
        except:
            pass
        sys.exit(0)

    try:
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
    except (ValueError, OSError):
        pass  # Windows console=False 时 signal 可能不可用

    # 异常兜底：任何未捕获的异常都会发通知
    try:
        trader.start()
    except Exception as e:
        try:
            trader._notify(f"❌ 系统异常崩溃\n时间: {datetime.now(TZ_ET).strftime('%Y-%m-%d %H:%M:%S ET')}\n错误: {e}")
        except:
            pass
        raise


if __name__ == '__main__':
    main()
