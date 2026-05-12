#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
QQQ 0DTE 双路径突破策略 - 实盘交易系统 v6.2
Classic(LB5/0.8x量/回踩确认) + Accelerated(LB3/1.2x量/近3根回踩) 自动切换
预加载滤镜(SMA20+SMA50+位置+趋势 4取3) + 核心过滤(量能+动量+实体)
按方向冷却 | 衰竭反转独立信号 | engine.status() 实时状态

功能：
1. 实时订阅QQQ 1分钟K线 → 聚合5分钟
2. v6.2 双路径突破信号检测（Classic/Accelerated 自动切换）
3. 自动下单（长桥API）
4. 风控：止损/止盈/跟踪止损/日亏损熔断/最大交易次数
5. 飞书推送交易信号
"""
import os, sys, time, json, signal, math
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import numpy as np

# 时区常量（自动处理夏令时/冬令时）
from zoneinfo import ZoneInfo
TZ_ET = ZoneInfo("America/New_York")    # 美东（自动EDT/EST切换）
TZ_HKT = timezone(timedelta(hours=8))   # 北京/香港时间

# stdout兜底（打包后console=False时为None，由入口main_app.py统一处理）
if sys.stdout is None:
    sys.stdout = open(os.devnull, 'w', encoding='utf-8', errors='replace')
if sys.stderr is None:
    sys.stderr = open(os.devnull, 'w', encoding='utf-8', errors='replace')


def _json_default(obj):
    """JSON序列化兜底：处理numpy bool/int/float"""
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")

# ===== 策略模块 =====
from strategy import get_option_symbol, FilterEngine

# ===== 长桥SDK =====
from longbridge.openapi import (
    Config, QuoteContext, TradeContext,
    SubType, Period, AdjustType, TradeSessions,
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
            'option_offset': 2.0, 'order_pct': 8, 'contract_multiplier': 100,
            'pos_pct': 8, 'max_trades': 999, 'daily_limit': 25,
            'start_time': '09:35', 'end_time': '15:50',
            'trail_activate': 0.10, 'trail_drop': 0.05,
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
        print(f"⚙️ 配置已热重载 ({datetime.now().strftime('%H:%M:%S')})")
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
        self.cooldown_remaining = 0  # 冷却剩余次数
        self.last_loss_dir = None    # 最近一次亏损的方向（'call'或'put'），冷却期间允许反向
        self.big_loss_cooldown = 0   # 大亏(>20%)后同方向冷却剩余K线数
        self._big_loss_dir = None    # 大亏冷却的方向
        self.current_price = 0       # 当前正股价格
        self.actual_capital = self.cfg['capital']  # 实际资金（_execute_trade中更新）

        # 初始化其他变量
        self._init_vars()

        # v6.2 FilterEngine
        self.engine = FilterEngine(self.cfg)

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

        # CSV文件路径（与脚本同目录）
        script_dir = str(_app_dir())
        self.csv_path = os.path.join(script_dir, 'today.csv')
        self.csv_initialized = False
        self._last_position_verify = 0  # 上次持仓验证时间戳

        # 共享状态文件（供trader_web.py读取）
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

        # 实时事件日志（供仪表盘读取）
        self.events = []  # [{'time': 'HH:MM:SS', 'msg': '...', 'tag': 'info/signal/trade/error'}]

    def _add_event(self, msg, tag='info'):
        """添加实时事件（写入state.json供仪表盘显示）"""
        ts = datetime.now().strftime('%H:%M:%S')
        self.events.append({'time': ts, 'msg': msg, 'tag': tag})
        if len(self.events) > 100:
            self.events = self.events[-100:]

    def _write_csv(self, candle):
        """写入K线数据到today.csv（供cron监控读取）"""
        try:
            if not self.csv_initialized:
                # 新的一天，写表头
                with open(self.csv_path, 'w', newline='', encoding='utf-8') as f:
                    f.write('timestamp,open,high,low,close,volume,turnover\n')
                self.csv_initialized = True
            # 追加K线数据
            with open(self.csv_path, 'a', newline='', encoding='utf-8') as f:
                ts = candle['time'].strftime('%Y-%m-%d %H:%M:%S')
                f.write(f"{ts},{candle['open']},{candle['high']},{candle['low']},"
                        f"{candle['close']},{candle['volume']},{candle.get('turnover', 0)}\n")
        except Exception as e:
            print(f"  ⚠️ CSV写入失败: {e}")

    def _save_state(self):
        """保存状态到state.json（供trader_web.py读取）"""
        try:
            import json
            state = {
                'connected': True,
                'running': self.running,
                'current_price': self.current_price,
                'position': None,
                'trades_today': [],
                'daily_pnl': self.daily_pnl,
                'filter_status': self.filter_status,
                'current_signal': self.current_signal,
                'session_high': self.session_high,
                'session_low': self.session_low if self.session_low < 999999 else 0,
                'candle_count': len(self.one_min_candles),
                'updated': datetime.now().strftime('%H:%M:%S'),
                'events': self.events[-30:],  # 最近30条事件
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
                    'time': t.get('time', ''),
                    'dir': t.get('dir', ''),
                    'entry_price': t.get('entry_price', 0),
                    'exit_price': t.get('exit_opt_price') or t.get('exit_price', 0),
                    'contracts': t.get('contracts', 0),
                    'pnl_pct': t.get('pnl_pct', 0),
                    'pnl_usd': t.get('pnl_usd', 0),
                    'reason': t.get('reason', ''),
                    'exit_reason': t.get('exit_reason', ''),
                    'result': t.get('result', '') or ('win' if t.get('pnl_pct', 0) > 0 else 'lose' if t.get('pnl_pct', 0) < 0 else ''),
                    'opt_symbol': t.get('opt_symbol', ''),
                })
            tmp_path = self.state_path + '.tmp'
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(state, f, ensure_ascii=False, indent=2, default=_json_default)
            os.replace(tmp_path, self.state_path)  # 原子替换
        except Exception as e:
            print(f"  ⚠️ 状态保存失败: {e}")

    def _init_api(self):
        """初始化长桥API - 支持WSL和Windows"""
        self.quote_ctx = None
        self.trade_ctx = None
        try:
            script_dir = str(_app_dir())
            env_paths = [
                os.path.join(script_dir, '.env'),
                os.path.expanduser('~/.hermes/.env'),
                os.path.expanduser('~\\.hermes\\.env'),
                r'C:\Users\Admin\.hermes\.env',
            ]
            for env_file in env_paths:
                if os.path.exists(env_file):
                    with open(env_file, encoding='utf-8') as f:
                        for line in f:
                            line = line.strip()
                            if line and '=' in line and not line.startswith('#'):
                                k, v = line.split('=', 1)
                                v = v.strip('"').strip("'")
                                if 'LONGPORT' in k or 'MINIMAX' in k:
                                    os.environ[k] = v
                    print(f"Loaded env from: {env_file}")
                    break

            self.config = Config.from_apikey_env()
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
                AdjustType.NoAdjust, TradeSessions.All
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
                # v6.2 同步填充 FilterEngine
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
                                            'entry_time': datetime.now(),
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
                    et = datetime.now().astimezone(TZ_ET)
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

    def start(self):
        """启动交易系统"""
        self.running = True
        print(f"🚀 QQQ 0DTE v6.3 动态过滤策略启动")
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
            self.cfg['symbol'], Period.Min_1, TradeSessions.All
        )
        self.quote_ctx.set_on_candlestick(self._on_candlestick)
        self._subscribe_quotes([self.cfg['symbol']])
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

        # 启动立即同步一次长桥订单（覆盖为今天的）
        try:
            self._sync_longbridge_orders()
            print(f"📤 启动同步长桥订单完成")
        except Exception as e:
            print(f"⚠️ 启动同步订单失败: {e}")
        try:
            while self.running:
                # 配置热重载（检测 settings.json 变化）
                _maybe_reload_config()
                # 每20秒检测一次信号
                self._check_signal_20s()
                self._check_position(triggered_by_push=False)
                
                # 每60秒同步一次长桥订单到文件
                now = time.time()
                if now - last_order_sync >= 60:
                    last_order_sync = now
                    self._sync_longbridge_orders()
                
                time.sleep(self.cfg['check_interval'])  # 20秒检测一次
        except KeyboardInterrupt:
            self.stop()

    def stop(self):
        """停止交易系统"""
        self.running = False
        print("\n🛑 交易系统停止")
        if self.position:
            self._close_position("系统停止")
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

        now = datetime.now()

        # 日初重置（检测新交易日，用美东时间）
        today_str = now.astimezone(TZ_ET).strftime('%Y-%m-%d')
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
            self.cooldown_remaining = 0  # 新交易日重置冷却
            self.last_loss_dir = None    # 新交易日重置亏损方向
            self.big_loss_cooldown = 0   # 新交易日重置大亏冷却
            self.engine.reset_day()  # v6.2 重置 FilterEngine
            # 只在非预加载情况下清空K线数据
            if not self.one_min_candles:
                self.kline_buffer = []
                self.close_history = []
                self.volume_history = []
            self.csv_initialized = False

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

        # 写入today.csv（供cron监控读取）
        self._write_csv(one_min)
        self.current_price = one_min['close']

        # 更新指标历史
        self.close_history.append(one_min['close'])
        self.volume_history.append(one_min['volume'])
        if len(self.close_history) > 1000:
            self.close_history = self.close_history[-1000:]
            self.volume_history = self.volume_history[-1000:]

        # v6.3 FilterEngine 预计算 + regime信息
        self.engine.update(one_min)
        self.filter_status = self.engine.status()
        # 添加regime信息到filter_status
        regime_params = self.engine.get_regime_params()
        self.filter_status['mode'] = f"{regime_params['regime']}({regime_params['lookback']}根)"
        self.filter_status['regime_detail'] = regime_params['detail']
        self._save_state()

        # 打印1分钟K线
        d = "🟢" if one_min['dir'] > 0 else "🔴"
        sma = np.mean(self.close_history[-20:]) if len(self.close_history) >= 20 else 0
        sma_str = f" SMA20:{sma:.2f}" if sma > 0 else ""
        print(f"  {d} 1min {now.strftime('%H:%M')} "
              f"O:{one_min['open']:.2f} H:{one_min['high']:.2f} "
              f"L:{one_min['low']:.2f} C:{one_min['close']:.2f} "
              f"Vol:{one_min['volume']:,}{sma_str}")

        # ===== 每根1分钟K线都检测信号 =====
        if len(self.one_min_candles) >= self.cfg['lookback'] + 1:
            # 时间转换：长桥返回HKT(UTC+8)，需转美东(UTC-4夏令时)
            et_now = now.astimezone(TZ_ET)
            cur_min_et = et_now.hour * 60 + et_now.minute

            # 直接用1分钟K线检测
            self._check_breakout(one_min, cur_min_et)

            # 衰竭反转检测（突破未触发时检查反转）
            if not self.position:
                self._check_reversal(one_min, cur_min_et)

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

        now = datetime.now()

        # 时间转换：HKT → ET
        et_now = now.astimezone(TZ_ET)
        cur_min_et = et_now.hour * 60 + et_now.minute

        # 检查时间窗口
        s_h, s_m = map(int, self.cfg['start_time'].split(':'))
        e_h, e_m = map(int, self.cfg['end_time'].split(':'))
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

        # v6.2 同步 FilterEngine（用于20秒轮询的滤镜状态）
        self.engine.session_high = self.session_high
        self.engine.session_low = self.session_low
        # 注意：不调用 engine.update(fake_bar)，避免假K线污染SMA/趋势计算

        # 检测突破信号
        self._check_breakout(fake_bar, cur_min_et)

        # 检测衰竭反转
        if not self.position:
            self._check_reversal(fake_bar, cur_min_et)

    def _update_filters_current(self, bar):
        """用当前K线更新过滤器状态并保存（供Web实时显示）"""
        entry_price = bar['close']
        ref_dir = 'call' if bar['close'] >= bar['open'] else 'put'
        vh = self.volume_history
        vol_avg = np.mean(vh[-20:]) if len(vh) >= 20 else 0
        core_ok, core_filters = self.engine.check_filters(ref_dir, entry_price, bar, vol_avg)
        pre_ok, pre_filters, _ = self.engine.check_preloaded(ref_dir)
        # v6.3: 包含regime信息
        regime_params = self.engine.get_regime_params()
        regime = regime_params['regime']
        lb = regime_params['lookback']
        self.filter_status.update({
            'sma20': pre_filters.get('sma20', self.filter_status.get('sma20', {})),
            'sma50': pre_filters.get('sma50', self.filter_status.get('sma50', {})),
            'volume': core_filters.get('volume', self.filter_status.get('volume', {})),
            'momentum': core_filters.get('momentum', self.filter_status.get('momentum', {})),
            'body': core_filters.get('body', self.filter_status.get('body', {})),
            'price_pos': pre_filters.get('price_pos', self.filter_status.get('price_pos', {})),
            'trend': pre_filters.get('trend', self.filter_status.get('trend', {})),
            'vwap': pre_filters.get('vwap', self.filter_status.get('vwap', {})),
            'macd': pre_filters.get('macd', self.filter_status.get('macd', {})),
            'atr': self.engine.state.get('atr', self.filter_status.get('atr', {})),
            'dir': '做多' if ref_dir == 'call' else '做空',
            'mode': f'{regime}({lb}根)',
            'regime_detail': regime_params['detail'],
            'price': f'${entry_price:.2f}',
            'all_ok': False,
        })
        self._save_state()

    def _check_breakout(self, bar, cur_min):
        """v6.3 动态过滤突破信号（根据市场状态自适应）"""
        # 时间窗口
        s_h, s_m = map(int, self.cfg['start_time'].split(':'))
        e_h, e_m = map(int, self.cfg['end_time'].split(':'))
        if not (s_h*60+s_m <= cur_min <= e_h*60+e_m):
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
        # 动态lookback：趋势市3根，震荡市2根，中性3根
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

        if not sig_dir:
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
        if sig_dir == 'call' and (rsi_val < 40 or rsi_val > 70):
            print(f"  ⛔ RSI过滤: RSI={rsi_val:.1f}，做多要求40-70")
            return
        if sig_dir == 'put' and (rsi_val < 30 or rsi_val > 60):
            print(f"  ⛔ RSI过滤: RSI={rsi_val:.1f}，做空要求30-60")
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
            price_pos = (entry_price - self.session_low) / (self.session_high - self.session_low)
            # 趋势市放宽到 2.0*ATR，中性/震荡用 1.5*ATR
            atr_mult = 2.0 if regime == 'trending' else 1.5
            atr_threshold = atr * atr_mult
            session_range = self.session_high - self.session_low
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

        # ===== 冷却检查 =====
        # E. 大亏冷却：每根K线递减
        if self.big_loss_cooldown > 0:
            self.big_loss_cooldown -= 1
            if sig_dir == getattr(self, '_big_loss_dir', None):
                print(f"  ⏳ 大亏冷却中({sig_dir}方向)，剩余{self.big_loss_cooldown}根K线")
                return
        if self.cooldown_remaining > 0 and self.last_loss_dir is not None:
            self.cooldown_remaining -= 1  # 任何信号都递减冷却（防止无限卡住）
            if sig_dir == self.last_loss_dir:
                print(f"  ⏳ 冷却中({self.last_loss_dir}方向)，跳过有效信号，剩余{self.cooldown_remaining}次")
                return
            else:
                print(f"  ⏳ 冷却中但方向相反({sig_dir}≠{self.last_loss_dir})，允许交易，冷却剩余{self.cooldown_remaining}次")

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

        self.daily_signals += 1
        self._add_event(f"🎯 {direction}突破@${entry_price:.2f} | {filters_str}", "signal")
        self._execute_trade(sig)

    def _check_reversal(self, bar, cur_min_et):
        """衰竭反转信号检测 - 抓超跌反弹/超涨回调"""
        # 时间窗口
        s_h, s_m = map(int, self.cfg['start_time'].split(':'))
        e_h, e_m = map(int, self.cfg['end_time'].split(':'))
        if not (s_h*60+s_m <= cur_min_et <= e_h*60+e_m):
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

        # ===== 超跌反弹（做多） =====
        if self.session_high > 0:
            drop_from_high = (self.session_high - entry) / self.session_high
            if drop_from_high >= self.cfg['reversal_drop']:
                # 确认反弹：前一根K线收阳 + 实体足够大（用cs[-2]不是bar）
                bounce_body = abs(prev['close'] - prev['open']) / prev['open'] if prev['open'] else 0
                if prev['close'] >= prev['open'] and bounce_body >= self.cfg['reversal_bounce']:
                    # SMA20不限制（反转策略逆势入场）
                    # 量能不限制（地量反弹也是信号）
                    sig = {
                        'dir': 'call',
                        'reason': f'超跌反弹|从{self.session_high:.2f}跌{drop_from_high*100:.1f}%',
                        'price': entry,
                        'sl': entry * (1 - self.cfg['sl']),
                        'tp': entry * (1 + self.cfg['tp']),
                    }
                    # 亏损冷却：任何信号都递减，同向才跳过
                    if self.cooldown_remaining > 0:
                        self.cooldown_remaining -= 1
                        if self.last_loss_dir == 'call':
                            print(f"  ⏳ 冷却中(call方向)，剩余{self.cooldown_remaining}次，跳过同向信号")
                            return
                    self.reversal_fired = True
                    self.daily_signals += 1
                    print(f"  🔄 衰竭反转做多! 从高点跌{drop_from_high*100:.1f}%")
                    self._add_event(f"🔄 衰竭反转做多! 跌{drop_from_high*100:.1f}%", "signal")
                    self._execute_trade(sig)
                    return

        # ===== 超涨回调（做空） =====
        if self.session_low < 999999:
            rise_from_low = (entry - self.session_low) / self.session_low
            if rise_from_low >= self.cfg['reversal_drop']:
                # 确认回调：前一根K线收阴 + 实体足够大（用cs[-2]不是bar）
                drop_body = abs(prev['close'] - prev['open']) / prev['open'] if prev['open'] else 0
                if prev['close'] <= prev['open'] and drop_body >= self.cfg['reversal_bounce']:
                    sig = {
                        'dir': 'put',
                        'reason': f'超涨回调|从{self.session_low:.2f}涨{rise_from_low*100:.1f}%',
                        'price': entry,
                        'sl': entry * (1 + self.cfg['sl']),
                        'tp': entry * (1 - self.cfg['tp']),
                    }
                    # 亏损冷却：任何信号都递减，同向才跳过
                    if self.cooldown_remaining > 0:
                        self.cooldown_remaining -= 1
                        if self.last_loss_dir == 'put':
                            print(f"  ⏳ 冷却中(put方向)，剩余{self.cooldown_remaining}次，跳过同向信号")
                            return
                    self.reversal_fired = True
                    self.daily_signals += 1
                    print(f"  🔄 衰竭反转做空! 从低点涨{rise_from_low*100:.1f}%")
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
            return self._lb_pos_cache  # 异常时返回缓存值

    def _execute_trade(self, sig):
        """执行期权交易 - 增强版订单验证"""
        # ===== 防重入锁：防止下单等待期间重复开仓 =====
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
            acct_currency = 'USD'  # 默认美元
            if assets:
                for asset in assets:
                    if hasattr(asset, 'total_cash') and asset.total_cash:
                        total_cash += float(asset.total_cash)
                    if hasattr(asset, 'currency') and asset.currency:
                        acct_currency = str(asset.currency)
            # 根据实际货币决定是否转换
            if acct_currency == 'HKD':
                capital = total_cash / 7.8
                print(f"  💰 账户余额: HKD {total_cash:,.2f} → USD {capital:,.2f}")
            else:
                capital = total_cash
                print(f"  💰 账户余额: USD {capital:,.2f}")
            self.actual_capital = capital if capital > 0 else self.cfg['capital']
        except Exception as e:
            print(f"  ⚠️ 获取余额失败: {e}，使用默认资金: ${self.cfg['capital']:,}")
            capital = self.cfg['capital']

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

        # ===== 按资金百分比计算张数（动态仓位倍数）=====
        pos_mult = sig.get('pos_mult', 1.0)  # 震荡市0.5，趋势市1.0
        order_amount = capital * self.cfg['order_pct'] / 100 * pos_mult  # 动态下单金额
        contracts = max(1, int(order_amount / (opt_price * self.cfg['contract_multiplier'])))
        qty = contracts * self.cfg['contract_multiplier']
        print(f"  📊 下单: {contracts}张 × ${opt_price:.2f} × {self.cfg['contract_multiplier']}股 = ${order_amount:,.2f} ({self.cfg['order_pct']}%资金)")

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
                                    f"❌ 订单超时取消\n"
                                    f"期权: {opt_symbol}\n"
                                    f"原因: {max_retries}次重试后仍未成交"
                                )
                                return
                    else:
                        print(f"  ⚠️ 未找到订单: {order_id} (尝试 {attempt + 1}/{max_retries})")
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
                'entry_time': datetime.now(),
                'entry_bar': len(self.one_min_candles),
                'reason': sig['reason'],
                'max_pnl_pct': 0,
                'half_closed': False,  # 动态止盈：是否已平仓一半
                'half_closed_max_pct': 0.0,  # 半仓后的峰值（用于跟踪止盈）
                'order_status': 'filled',  # 订单状态
                # v6.3 动态参数（供_check_position使用）
                'tp_partial_pct': sig.get('tp_partial_pct', 1.00),  # 动态止盈阈值
                'timeout_bars': sig.get('timeout_bars', 10),        # 动态超时
                'regime': sig.get('regime', 'neutral'),             # 市场状态
                # 正股跟踪止损
                'stock_peak': float(price),  # 正股最高价(Call)/最低价(Put)
                'peak_opt_pnl': 0,           # 期权峰值盈利(用于半仓跟踪)
            }
            self._subscribe_position_quote(opt_symbol)
            self.trades_today.append(self.position.copy())
            self._add_event(f"📈 开仓: {opt_symbol} x{contracts}张 @${executed_price:.2f}", "trade")

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
                f"🎯 {'做多' if sig['dir']=='call' else '做空'} {opt_symbol}\n"
                f"正股入场: ${float(price):.2f}\n"
                f"期权入场: ${self.position.get('entry_opt_price', 0):.2f}\n"
                f"张数: {contracts}张 ({qty}股)\n"
                f"原因: {sig['reason']}\n"
                f"状态: ✅已成交"
            )

            # ===== 同步验证长桥持仓 =====
            self._verify_position(opt_symbol, contracts)

        except Exception as e:
            print(f"  ❌ 下单失败: {e}")
            import traceback
            traceback.print_exc()

    def _log_order(self, order_id, opt_symbol, direction, contracts, status, executed_qty=0, executed_price=0):
        """记录订单日志（用于追踪所有提交的订单）"""
        try:
            log_dir = os.path.join(_app_dir(), 'logs')
            os.makedirs(log_dir, exist_ok=True)
            today = datetime.now().strftime('%Y-%m-%d')
            log_file = os.path.join(log_dir, f'orders_{today}.log')
            
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
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
            return False
            
        except Exception as e:
            print(f"  ⚠️ 持仓验证失败: {e}")
            print(f"  📝 继续执行（订单已确认成交）")
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
                        
                        # 恢复内部持仓状态
                        self.position = {
                            'order_id': 'synced',
                            'dir': 'call' if 'C' in str(symbol) else 'put',
                            'entry_price': cost,
                            'opt_symbol': symbol,
                            'entry_opt_price': cost,
                            'sl_pct': self.cfg['sl'],
                            'tp_pct': self.cfg['tp'],
                            'contracts': qty,
                            'quantity': qty * self.cfg['contract_multiplier'],
                            'entry_time': datetime.now(),
                            'entry_bar': len(self.one_min_candles),
                            'reason': '长桥持仓同步',
                            'max_pnl_pct': pnl_pct,
                            'half_closed': False,
                            'half_closed_max_pct': 0.0,
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
        if now_et.hour >= 16 and now_et.minute >= 0:
            if self.position:
                self._close_position("⏰ 16:00 ET 强制收盘平仓")
            return

        # 如果没有内部持仓，尝试从长桥同步
        if not self.position:
            self._sync_position_from_longbridge()
            if not self.position:
                # 即使没有持仓，也要检查长桥是否有持仓（防止手动平仓后系统不知道）
                # 使用缓存避免API限频
                lb_count = self._check_longbridge_position()
                if lb_count == 0:
                    self._unsubscribe_quotes([self.position.get('opt_symbol') if self.position else None])
                    self.position = None
                return

        pos = self.position
        entry_stock = pos['entry_price']  # 正股入场价

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

        # ===== v6.2 正股跟踪止损：更新正股峰值 =====
        if pos['dir'] == 'call':
            pos['stock_peak'] = max(pos.get('stock_peak', entry_stock), current_stock)
        else:  # put
            pos['stock_peak'] = min(pos.get('stock_peak', entry_stock), current_stock)

        # 持仓K线数
        bars_held = len(self.one_min_candles) - pos['entry_bar']

        # ===== v6.3 动态退出条件（使用regime参数）=====
        ex = None
        sl_pct = pos.get('sl_pct', self.cfg['sl']) * 100    # 动态止损（震荡30%/趋势25%）
        tp_partial = pos.get('tp_partial_pct', 1.00) * 100  # 动态止盈（震荡50%/趋势100%）
        tp_trail_drop = self.cfg['tp_trail_drop'] * 100  # 30%

        # --- 1. 止损（期权价格，最高优先）---
        if pnl_pct <= -sl_pct:
            ex = f"止损({pnl_pct:.1f}%≤-{sl_pct:.0f}%)"

        # --- 2. 分阶段超时（v6.3: 使用动态timeout_bars）---
        if not ex and not pos['half_closed']:
            dynamic_timeout = pos.get('timeout_bars', 10)  # 震荡5分钟/趋势15分钟
            s1_bars = max(dynamic_timeout // 3, 5)  # 最少5分钟，给0DTE足够时间
            s1_min = self.cfg.get('timeout_stage1_min', 0.30) * 100  # 30%
            s2_bars = max(dynamic_timeout * 2 // 3, 8)  # 最少8分钟
            s2_min = self.cfg.get('timeout_stage2_min', 0.60) * 100  # 60%
            s3_bars = dynamic_timeout

            if bars_held >= s3_bars:
                ex = f"硬超时({s3_bars}分钟)"
            elif bars_held >= s2_bars and pnl_pct < s2_min:
                ex = f"阶段超时({s2_bars}min盈利{pnl_pct:.1f}%<{s2_min:.0f}%)"
            elif bars_held >= s1_bars and pnl_pct < s1_min:
                ex = f"阶段超时({s1_bars}min盈利{pnl_pct:.1f}%<{s1_min:.0f}%)"

        # --- 3. 动态止盈：盈利≥150%平仓一半 ---
        if not ex and not pos['half_closed'] and pnl_pct >= tp_partial:
            self._close_partial(f"盈利{tp_partial:.0f}%平仓一半")
            return

        # --- 4. 半仓后：正股跟踪止损（替代期权峰值回撤，更稳定）---
        if not ex and pos['half_closed']:
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
            s3_bars = pos.get('timeout_bars', 10)  # 使用动态超时
            if bars_held >= s3_bars:
                ex = f"硬超时({s3_bars}分钟)"

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

            # 获取平仓时的期权价格
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
            self.daily_pnl += pnl_usd

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
            print(f"  📋 订单: {resp.order_id}")
            print(f"  {'='*50}\n")

            self._notify(
                f"✂️ 部分平仓: {reason}\n"
                f"期权: {pos['opt_symbol']}\n"
                f"平仓: {half}张 | 剩余: {pos['contracts']}张\n"
                f"本次盈亏: {pnl_pct:+.2f}% (${pnl_usd:+,.2f})"
            )

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
                        
                        # ⚠️ 订单被拒：立即清除持仓，不再重试
                        if str(order_status) == 'OrderStatus.Rejected':
                            print(f"  ❌ 平仓订单被拒! 清除内部持仓")
                            self._unsubscribe_quotes([pos.get('opt_symbol')])
                            self.position = None
                            self._save_state()
                            return
                        
                        if executed_qty >= pos['contracts']:
                            close_filled = True
                            exit_opt = float(executed_price) if executed_price > 0 else 0
                            print(f"  ✅ 平仓完全成交!")
                            break
                        elif executed_qty > 0:
                            # 部分成交
                            print(f"  ⚠️ 平仓部分成交: {executed_qty}/{pos['contracts']}张")
                            if attempt == max_retries - 1:
                                close_filled = True
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

            entry_opt = pos.get('entry_opt_price') or exit_opt
            if entry_opt <= 0:
                entry_opt = 1.0

            pnl_pct = (exit_opt - entry_opt) / entry_opt * 100

            # 计算盈亏金额（张数 × 100股 × 权利金变动）
            pnl_usd = pos['contracts'] * self.cfg['contract_multiplier'] * (exit_opt - entry_opt)

            self.daily_pnl += pnl_usd

            # 标记盈亏
            pos['win'] = pnl_pct > 0
            pos['exit_opt_price'] = exit_opt
            pos['exit_time'] = datetime.now()
            pos['pnl_pct'] = pnl_pct
            pos['pnl_usd'] = pnl_usd
            pos['exit_reason'] = reason

            # 同步更新 trades_today 中的记录
            for t in self.trades_today:
                if t.get('opt_symbol') == pos['opt_symbol'] and t.get('exit_opt_price') is None:
                    t.update({
                        'win': pos['win'],
                        'exit_opt_price': exit_opt,
                        'exit_time': pos['exit_time'],
                        'pnl_pct': pnl_pct,
                        'pnl_usd': pnl_usd,
                        'exit_reason': reason,
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

            self._notify(
                f"🏁 平仓: {reason}\n"
                f"期权: {pos['opt_symbol']}\n"
                f"{entry_opt:.2f} → {exit_opt:.2f}\n"
                f"{'盈利' if pnl_pct>0 else '亏损'}: {pnl_pct:+.2f}% (${pnl_usd:+,.2f})"
            )

            closed_symbol = pos.get('opt_symbol')
            self.position = None  # 成功后才清空
            self._unsubscribe_quotes([closed_symbol])
            # 更新亏损冷却
            if pnl_pct < 0:
                self.consecutive_losses += 1
                self.last_loss_dir = pos['dir']  # 记录亏损方向，冷却期间允许反向
                if self.consecutive_losses >= 2:
                    self.cooldown_remaining = self.cfg['loss_cooldown']
                    print(f"  ⏳ 连续亏损{self.consecutive_losses}次，冷却{self.cooldown_remaining}次检测（{self.last_loss_dir}方向）")
                # E. 单笔大亏(>20%)：同方向冷却5根K线
                if pnl_pct <= -20:
                    self.big_loss_cooldown = 5
                    self._big_loss_dir = pos['dir']
                    print(f"  ⏳ 大亏{pnl_pct:+.1f}%，{pos['dir']}方向冷却5根K线")
            else:
                self.consecutive_losses = 0
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
                    f.write(f'[{datetime.now():%H:%M}] {msg}\n')
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
                f.write(f'[{datetime.now():%H:%M}] {msg}\n')

    def _notify_telegram(self, msg):
        """Telegram通知 - 发送消息到用户"""
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
                    f.write(f'[{datetime.now():%H:%M}] {msg}\n')
                return

            text = f"[QQQ Trader]\n{msg}"
            resp = requests.post(
                f'https://api.telegram.org/bot{bot_token}/sendMessage',
                json={'chat_id': chat_id, 'text': text},
                timeout=10
            )
            result = resp.json()
            if result.get('ok'):
                print(f"  ✅ Telegram推送成功")
            else:
                print(f"  ⚠️ Telegram推送失败: {result}")
        except Exception as e:
            import traceback
            print(f"  ⚠️ Telegram通知异常: {e}")
            traceback.print_exc()
            log_path = os.path.join(_app_dir(), 'logs', 'trade_log.txt')
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            with open(log_path, 'a', encoding='utf-8') as f:
                f.write(f'[{datetime.now():%H:%M}] {msg}\n')

    def _notify(self, msg):
        """统一通知 - 同时发送飞书和Telegram"""
        if self.cfg.get('feishu', {}).get('enabled', True):
            self._notify_feishu(msg)
        if self.cfg.get('telegram', {}).get('enabled', False):
            self._notify_telegram(msg)

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

    def _print_summary(self):
        """打印今日总结"""
        wins = len([t for t in self.trades_today if t.get('win')])
        total = len(self.trades_today)
        print("\n" + "=" * 60)
        print("📊 今日交易总结")
        print("=" * 60)
        print(f"  策略版本: v6.2双路径(Classic/Accelerated) | 09:35-15:50美东")
        print(f"  交易次数: {total} (做多: {sum(1 for t in self.trades_today if t.get('dir')=='call')}, "
              f"做空: {sum(1 for t in self.trades_today if t.get('dir')=='put')})")
        print(f"  胜率: {wins}/{total} ({wins/total*100:.0f}%)" if total > 0 else "  胜率: N/A")
        print(f"  累计盈亏: ${self.daily_pnl:+,.2f}")
        print("=" * 60)

    def _sync_longbridge_orders(self):
        """从长桥同步今日所有订单信息，保存到本地文件供web端读取"""
        try:
            all_orders = self.trade_ctx.today_orders()
            if not all_orders:
                print(f"  ⚠️ 长桥返回空订单列表")
                return
            
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
                    'updated': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                }, f, ensure_ascii=False, indent=2, default=_json_default)
            os.replace(tmp_path, filepath)  # 原子替换
            
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

    def _save_pending_records(self):
        """启动时保存上次未写入的交易记录（进程被kill -9不会调stop()）"""
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

        # 0DTE期权到期日=美东交易日，records文件用北京时间（+1天）
        # 例如: 到期日2026-04-29(ET) → records/2026-04-30.json(北京)
        from datetime import timedelta
        et_date = datetime.strptime(most_common_date, '%Y-%m-%d')
        beijing_date = (et_date + timedelta(days=1)).strftime('%Y-%m-%d')

        # 检查是否已经有该日期的records文件
        records_dir = os.path.join(script_dir, 'records')
        record_file = os.path.join(records_dir, f'{beijing_date}.json')

        # 先用broker数据计算期望的交易数
        expected_trades = self._count_broker_trades(lb_data)

        if os.path.exists(record_file):
            try:
                with open(record_file, encoding='utf-8') as f:
                    existing = json.load(f)
                # 如果已有文件且交易数>=broker对账数，跳过（已完整保存）
                existing_count = len(existing.get('trades', []))
                if existing_count >= expected_trades:
                    print(f"📋 {beijing_date}记录已存在({existing_count}笔,期望{expected_trades}笔)，跳过")
                    return
                else:
                    print(f"⚠️ {beijing_date}记录不完整({existing_count}/{expected_trades}笔)，将用broker数据覆盖")
            except:
                pass
        print(f"🔄 发现未保存的{most_common_date}(ET)/{beijing_date}(BJ)交易记录，正在对账...")
        # 用对账逻辑重建并保存
        self._reconcile_and_save(lb_data, beijing_date)

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
        os.replace(tmp_path, filepath)
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
        beijing_now = datetime.now()

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
                'time': beijing_now.strftime('%H:%M'),  # 保存时间（非精确交易时间）
                'dir': direction,
                'entry_price': strike,
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
                '_source': 'broker_reconcile',  # 标记来源
            })

        return reconciled

    def _save_daily_records(self):
        """保存今日交易记录到 JSON 文件"""
        from zoneinfo import ZoneInfo
        TZ_ET = ZoneInfo("America/New_York")

        # 先同步长桥订单信息
        self._sync_longbridge_orders()

        # 从broker对账重建交易记录（即使trades_today为空也能记录）
        broker_trades = self._reconcile_trades_from_broker()

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
            key = f"{opt_sym}_{contracts}"
            if key not in seen_symbols:
                # 这笔交易broker没有对账记录，用internal数据
                entry_time = t.get('entry_time', '')
                if isinstance(entry_time, datetime):
                    time_str = entry_time.strftime('%H:%M')
                else:
                    time_str = str(entry_time)[:5]
                pnl = t.get('pnl_pct', t.get('max_pnl_pct', 0))
                all_trades.append({
                    'date': datetime.now(TZ_ET).strftime('%Y-%m-%d'),
                    'time': time_str,
                    'dir': t.get('dir', ''),
                    'entry_price': t.get('entry_price', 0),
                    'exit_price': t.get('exit_opt_price', t.get('entry_price', 0)),
                    'qty': t.get('quantity', 0),
                    'contracts': t.get('contracts', 0),
                    'pnl_pct': round(pnl, 2) if pnl else 0,
                    'pnl_usd': round(t.get('pnl_usd', 0), 2),
                    'result': 'win' if t.get('win') else ('lose' if t.get('win') is False else ''),
                    'reason': t.get('reason', ''),
                    'exit_reason': t.get('exit_reason', ''),
                    'opt_symbol': opt_sym,
                    '_source': 'internal',
                })
                seen_symbols.add(key)
        
        if not all_trades:
            print("📋 今日无交易记录，跳过保存")
            return

        try:
            # 用北京时间作为日期（与历史records文件命名一致）
            today = datetime.now().strftime('%Y-%m-%d')
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
                }, f, ensure_ascii=False, indent=2, default=_json_default)
            os.replace(tmp_path, filepath)  # 原子替换

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
        trader.stop()
        sys.exit(0)

    try:
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
    except (ValueError, OSError):
        pass  # Windows console=False 时 signal 可能不可用

    trader.start()


if __name__ == '__main__':
    main()
