#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
QQQ 0DTE 双向突破策略 - Web可视化版
Flask + HTML/CSS 卡片式仪表盘
v6全过滤: 09:35-15:00美东 + SMA20 + 量能×1.2 + 动量 + K线实体
"""
import os, sys, time, json, threading
import numpy as np
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

# 时区常量
TZ_ET = ZoneInfo("America/New_York")    # 美东（自动EDT/EST切换）
TZ_HKT = timezone(timedelta(hours=8))   # 北京/香港时间

def _app_dir():
    """获取应用根目录（打包后exe目录 / 开发时脚本目录）"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

# stdout兜底（打包后console=False时为None，由入口main_app.py统一处理）
if sys.stdout is None:
    sys.stdout = open(os.devnull, 'w', encoding='utf-8', errors='replace')
if sys.stderr is None:
    sys.stderr = open(os.devnull, 'w', encoding='utf-8', errors='replace')

# 兜底：console=False 时 stdout/stderr 可能为 None
if sys.stdout is None:
    sys.stdout = open(os.devnull, 'w', encoding='utf-8')
if sys.stderr is None:
    sys.stderr = open(os.devnull, 'w', encoding='utf-8')

# Flask延迟导入 - 仅在main()中使用，Engine类不需要Flask
Flask = None
render_template_string = None
jsonify = None
request = None

from longbridge.openapi import (
    Config, QuoteContext, TradeContext,
    Period, TradeSessions, SubType,
    OrderSide, OrderType, TimeInForceType, OutsideRTH
)

# ===== 策略配置（与live_trader.py同步）=====
CONFIG = {
    'symbol': 'QQQ.US',
    # 策略参数（与 live_trader.py v6.3 同步）
    'sl': 0.25,               # 止损 25%
    'tp': 0.30,               # 止盈（旧逻辑保留兼容）
    'lookback': 3,            # 默认突破窗口
    'lookback_accel': 2,      # 默认加速窗口
    'pullback_confirm': False,
    'rsi_period': 14,
    'rsi_overbought': 75,
    'rsi_oversold': 25,
    'loss_cooldown': 3,
    # 动态止盈参数
    'tp_partial_pct': 1.00,   # 盈利100%平仓一半
    'tp_trail_drop': 0.30,    # 最高盈利回撤30%全部平仓
    # 正股跟踪止损
    'stock_trail_pct': 0.003, # 正股从高点回撤0.3%触发止损
    # 分阶段超时
    'timeout_stage1_bars': 5,
    'timeout_stage1_min': 0.30,
    'timeout_stage2_bars': 10,
    'timeout_stage2_min': 0.60,
    'timeout_stage3_bars': 15,
    # 期权参数
    'option_offset': 2.0,
    'order_pct': 8,
    'contract_multiplier': 100,
    # 资金管理
    'pos_pct': 8,
    'max_trades': 999,
    'daily_limit': 25,
    # 交易窗口
    'start_time': '09:35',
    'end_time': '15:50',
    # 跟踪止损（备用）
    'trail_activate': 0.10,
    'trail_drop': 0.05,
    # 过滤
    'max_gap': 0.0020,
    'vol_mult': 0.8,
    'min_body': 0.0003,
    # 衰竭反转参数
    'reversal_drop': 0.002,
    'reversal_bounce': 0.001,
    # 账户
    'capital': 100000,
}

# ============================================================
# 交易引擎
# ============================================================
class Engine:
    def __init__(self, cfg):
        self.cfg = cfg
        self.running = False
        self.position = None
        self.trades_today = []
        self.daily_pnl = 0
        self.kline_buffer = []       # 1分钟K线缓冲
        self.one_min_candles = []   # 1分钟K线（直接用于信号检测）
        self.close_history = []      # 收盘价历史（SMA20）
        self.volume_history = []     # 成交量历史（均量）
        self.current_price = 0
        self.account_info = {}
        self.holdings = []
        self.current_signal = None
        self.filter_status = {  # 5个过滤器最新状态（含默认值）
            'sma20': {'ok': None, 'val': '--', 'detail': '--'},
            'volume': {'ok': None, 'val': '--', 'detail': '--'},
            'momentum': {'ok': None, 'val': '--', 'detail': '--'},
            'body': {'ok': None, 'val': '--', 'detail': '--'},
            'dir': '', 'price': '--', 'all_ok': False,
        }
        self.logs = []
        self.connected = False
        self.qc = None
        self.tc = None

        # 衰竭反转追踪
        self.session_high = 0
        self.session_low = 999999
        self.reversal_fired = False

        # CSV文件路径（与脚本同目录）
        sd = _app_dir()
        self.csv_path = os.path.join(sd, 'today.csv')
        self.csv_initialized = False

    def _log(self, msg):
        ts = datetime.now().strftime('%H:%M:%S')
        entry = {'time': ts, 'msg': msg}
        self.logs.append(entry)
        if len(self.logs) > 200:
            self.logs = self.logs[-200:]

    def _init_api(self):
        """初始化长桥API"""
        if self.qc is not None:
            return
        sd = _app_dir()
        for f in [os.path.join(sd, '.env'), os.path.expanduser('~/.hermes/.env'), r'C:\Users\Admin\.hermes\.env']:
            if os.path.exists(f):
                with open(f, encoding='utf-8') as fh:
                    for line in fh:
                        line = line.strip()
                        if line and '=' in line and not line.startswith('#'):
                            k, v = line.split('=', 1)
                            if 'LONGPORT' in k or 'MINIMAX' in k or 'GITHUB' in k:
                                os.environ[k] = v.strip('"').strip("'")
                break
        self.qc = QuoteContext(Config.from_apikey_env())
        self.tc = TradeContext(Config.from_apikey_env())
        self.connected = True
        self._log("✅ 长桥API连接成功")

    def _sync_acct(self):
        if not self.tc:
            return
        try:
            b = self.tc.account_balance()
            if b:
                self.account_info = {
                    'net': float(b[0].net_assets or 0),
                    'cash': float(b[0].total_cash or 0),
                    'power': float(b[0].buy_power or 0),
                    'currency': str(b[0].currency),
                }
        except:
            pass
        try:
            r = self.tc.stock_positions()
            self.holdings = []
            total_value = 0
            if r and r.channels:
                syms, raw = [], []
                for ch in r.channels:
                    for p in ch.positions:
                        if int(p.quantity) > 0:
                            syms.append(p.symbol)
                            raw.append(p)
                pm = {}
                if syms:
                    try:
                        for q in self.qc.quote(list(set(syms))):
                            pm[q.symbol] = float(q.last_done)
                    except:
                        pass
                # 计算总市值
                for p in raw:
                    qty = int(p.quantity)
                    cur = pm.get(p.symbol, float(p.cost_price))
                    is_option = '.US' in str(p.symbol) and ('C' in str(p.symbol) or 'P' in str(p.symbol))
                    if is_option:
                        total_value += cur * qty * 100  # 期权：现价 × 张数 × 100
                    else:
                        total_value += cur * qty  # 股票：现价 × 股数
                
                # 总资产 = 持仓市值(USD) + 现金(转换为USD)
                cash_usd = self.account_info.get('cash', 0)
                # 如果是港币，除以7.8转换为美元
                if self.account_info.get('currency') == 'HKD':
                    cash_usd = cash_usd / 7.8
                total_assets = total_value + cash_usd

                # 统一将账户信息转换为USD（供前端显示）
                if self.account_info.get('currency') == 'HKD':
                    rate = 7.8
                    self.account_info['cash'] = self.account_info.get('cash', 0) / rate
                    self.account_info['net'] = self.account_info.get('net', 0) / rate
                    self.account_info['power'] = self.account_info.get('power', 0) / rate
                    self.account_info['currency'] = 'USD'
                
                for p in raw:
                    qty = int(p.quantity)
                    cost = float(p.cost_price)
                    cur = pm.get(p.symbol, cost)
                    is_option = '.US' in str(p.symbol) and ('C' in str(p.symbol) or 'P' in str(p.symbol))
                    
                    if is_option:
                        # 期权：盈亏 = (现价 - 成本) × 张数 × 100
                        market_value = cur * qty * 100
                        pnl = (cur - cost) * qty * 100
                        pct = (cur - cost) / cost * 100 if cost else 0
                    else:
                        # 股票：盈亏 = (现价 - 成本) × 股数
                        market_value = cur * qty
                        pnl = (cur - cost) * qty
                        pct = (cur - cost) / cost * 100 if cost else 0
                    
                    holding_pct = market_value / total_assets * 100 if total_assets > 0 else 0
                    
                    self.holdings.append({
                        'sym': p.symbol, 'qty': qty, 'cost': cost,
                        'cur': cur, 'pnl': pnl, 'pct': pct,
                        'market_value': market_value,
                        'holding_pct': holding_pct,
                        'is_option': is_option,
                    })
        except:
            pass

    def _acct_loop(self):
        while self.running:
            self._sync_acct()
            time.sleep(10)

    def start(self):
        self.running = True
        self._init_api()
        self._log("🚀 Web面板启动（仅显示模式，交易由live_trader.py执行）")
        self._sync_acct()

        threading.Thread(target=self._acct_loop, daemon=True).start()

        while self.running:
            self._chk_pos()
            time.sleep(1)

    def stop(self):
        self.running = False
        if self.position:
            self._close("系统停止")

    def _write_csv(self, candle):
        """写入K线数据到today.csv（供cron监控读取）"""
        try:
            if not self.csv_initialized:
                with open(self.csv_path, 'w', newline='', encoding='utf-8') as f:
                    f.write('timestamp,open,high,low,close,volume,turnover\n')
                self.csv_initialized = True
            with open(self.csv_path, 'a', newline='', encoding='utf-8') as f:
                ts = candle['t']
                f.write(f"{ts},{candle['o']},{candle['h']},{candle['l']},"
                        f"{candle['c']},{candle['v']},0\n")
        except Exception as e:
            self._log(f"⚠️ CSV写入失败: {e}")

    def _on_kline(self, sym, candle):
        """K线回调 - 每1分钟检测突破信号"""
        # PushCandlestick结构: candle.candlestick.open/high/low/close
        if not candle.is_confirmed:
            return
        c = candle.candlestick
        if not self.running:
            return
        now = datetime.now()

        # 日初重置（检测新交易日，必须用美东时间！）
        today_str = now.astimezone(TZ_ET).strftime('%Y-%m-%d')
        if not hasattr(self, '_last_date') or self._last_date != today_str:
            if hasattr(self, '_last_date') and self._last_date:
                self._log(f"📅 新交易日: {today_str}")
            self._last_date = today_str
            self.session_high = 0
            self.session_low = 999999
            self.reversal_fired = False
            self.trades_today = []
            self.daily_pnl = 0
            self.position = None
            self.kline_buffer = []
            self.one_min_candles = []
            self.close_history = []
            self.volume_history = []
            self.csv_initialized = False

        bar = {'t': now.strftime('%H:%M'), 'o': float(c.open), 'h': float(c.high),
               'l': float(c.low), 'c': float(c.close), 'v': int(c.volume)}
        self.current_price = bar['c']
        self.session_high = max(self.session_high, bar['h'])
        self.session_low = min(self.session_low, bar['l'])
        self.kline_buffer.append(bar)

        # ===== 直接存储1分钟K线用于信号检测 =====
        m = {
            't': bar['t'],
            'o': bar['o'],
            'h': bar['h'],
            'l': bar['l'],
            'c': bar['c'],
            'v': bar['v'],
        }
        m['body'] = abs(m['c'] - m['o']) / m['o'] * 100 if m['o'] else 0
        m['d'] = 1 if m['c'] >= m['o'] else -1
        self.one_min_candles.append(m)

        # 更新指标历史
        self.close_history.append(m['c'])
        self.volume_history.append(m['v'])
        if len(self.close_history) > 1000:
            self.close_history = self.close_history[-1000:]
            self.volume_history = self.volume_history[-1000:]

        # 写入today.csv
        self._write_csv(bar)

        d = "🟢" if m['d'] > 0 else "🔴"
        self._log(f"{d} 1min {now.strftime('%H:%M')} C:{m['c']:.2f} H:{m['h']:.2f} L:{m['l']:.2f} Vol:{m['v']:,}")

        # ===== 每根1分钟K线都检测突破信号 =====
        if len(self.one_min_candles) >= self.cfg['lookback'] + 1:
            # 时间转换：长桥返回HKT(UTC+8)，需转美东(UTC-4夏令时)
            et_now = now.astimezone(TZ_ET)
            cm_et = et_now.hour * 60 + et_now.minute
            self._chk_breakout(bar, cm_et)

            # 衰竭反转检测
            if not self.position:
                self._chk_reversal(bar, cm_et)

    def _update_filters_current(self, bar):
        """用当前K线更新过滤器状态（供Web实时显示）"""
        entry = bar['c']
        ref_dir = 'call' if bar['c'] >= bar['o'] else 'put'
        direction = '做多' if ref_dir == 'call' else '做空'
        ch = self.close_history
        vh = self.volume_history

        sma_ok = True
        sma20 = 0
        if len(ch) >= 20:
            sma20 = np.mean(ch[-20:])
            if ref_dir == 'call' and entry < sma20:
                sma_ok = False
            if ref_dir == 'put' and entry > sma20:
                sma_ok = False

        vol_ok = True
        cur_vol = 0
        vol_avg = 0
        if len(vh) >= 20:
            vol_avg = np.mean(vh[-20:])
            cur_vol = bar['v']
            vol_ok = cur_vol >= vol_avg * self.cfg.get('vol_mult', 1.0)

        mom_ok = bar['c'] >= bar['o'] if ref_dir == 'call' else bar['c'] <= bar['o']
        d = '阳' if bar['c'] >= bar['o'] else '阴'

        cur_body = abs(bar['c'] - bar['o']) / bar['o'] if bar['o'] else 0
        min_body = self.cfg.get('min_body', 0)
        body_ok = cur_body >= min_body if min_body > 0 else True

        self.filter_status = {
            'sma20': {'ok': sma_ok, 'val': f'{sma20:.2f}' if sma20 else '--',
                       'detail': f'{"≥" if sma_ok else "<"}{sma20:.2f}' if sma20 else '数据不足'},
            'volume': {'ok': vol_ok, 'val': f'{cur_vol:,}',
                        'detail': f'{cur_vol:,}>={vol_avg*self.cfg.get("vol_mult",1.0):,.0f}' if vol_avg else '数据不足'},
            'momentum': {'ok': mom_ok, 'val': d,
                          'detail': f'{"阳线✓" if mom_ok else "非阳线✗"}' if ref_dir=="call" else f'{"阴线✓" if mom_ok else "非阴线✗"}'},
            'body': {'ok': body_ok, 'val': f'{cur_body*100:.3f}%',
                      'detail': f'{cur_body*100:.3f}%{"≥" if body_ok else "<"}{min_body*100:.2f}%'},
            'dir': direction,
            'price': f'${entry:.2f}',
            'all_ok': False,
        }

    def _chk_breakout(self, bar, cm):
        """全过滤双向突破信号检测"""
        sh, sm = map(int, self.cfg['start_time'].split(':'))
        eh, em = map(int, self.cfg['end_time'].split(':'))
        if not (sh * 60 + sm <= cm <= eh * 60 + em):
            return
        if self.position or len(self.trades_today) >= self.cfg['max_trades']:
            return
        if self.daily_pnl <= -self.cfg['capital'] * self.cfg['daily_limit'] / 100:
            self._update_filters_current(bar)
            return

        cs = self.one_min_candles
        lb = self.cfg['lookback']

        # 前N根1分钟K线的高低点
        # 计算前N根1分钟K线的高低点（不包括当前K线）
        upper = max(c['h'] for c in cs[-lb-1:-1])
        lower = min(c['l'] for c in cs[-lb-1:-1])
        entry = bar['c']

        sig = None

        # ===== 向上突破：做多Call =====
        if entry > upper:
            gap = (entry - upper) / upper if upper > 0 else 0
            if gap < self.cfg['max_gap']:
                sig = {'dir': 'call', 'reason': f'突破{upper:.2f}做多', 'price': entry}

        # ===== 向下突破：做空Put =====
        elif entry < lower:
            gap = (lower - entry) / lower if lower > 0 else 0
            if gap < self.cfg['max_gap']:
                sig = {'dir': 'put', 'reason': f'跌破{lower:.2f}做空', 'price': entry}

        # ===== 全过滤条件（每根K线都更新，不依赖信号是否存在） =====
        ch = self.close_history
        vh = self.volume_history
        # 用当前K线方向作为参考（无信号时也能显示过滤器状态）
        ref_dir = sig['dir'] if sig else ('call' if bar['c'] >= bar['o'] else 'put')
        direction = '做多' if ref_dir == 'call' else '做空'
        filters = []

        # 1. SMA20趋势过滤
        sma_ok = True
        sma20 = 0
        if len(ch) >= 20:
            sma20 = np.mean(ch[-20:])
            if ref_dir == 'call' and entry < sma20:
                sma_ok = False
            if ref_dir == 'put' and entry > sma20:
                sma_ok = False
            filters.append(f"{'✅' if sma_ok else '❌'}SMA20({sma20:.2f})")
        else:
            filters.append('➖SMA20(数据不足)')

        # 2. 成交量确认
        vol_ok = True
        cur_vol = 0
        vol_avg = 0
        if len(vh) >= 20:
            vol_avg = np.mean(vh[-20:])
            cur_vol = bar['v']  # 用传入的bar，而非cs[-1]
            vol_ok = cur_vol >= vol_avg * self.cfg.get('vol_mult', 1.0)
            filters.append(f"{'✅' if vol_ok else '❌'}量能({cur_vol:,}>={vol_avg*self.cfg.get('vol_mult',1.0):,.0f})")
        else:
            filters.append('➖量能(数据不足)')

        # 3. 动量确认（用传入的bar而非cs[-1]）
        mom_ok = True
        if ref_dir == 'call':
            mom_ok = bar['c'] >= bar['o']
        else:
            mom_ok = bar['c'] <= bar['o']
        d = '阳' if bar['c'] >= bar['o'] else '阴'
        filters.append(f"{'✅' if mom_ok else '❌'}动量({d})")

        # 4. K线实体确认（用传入的bar而非cs[-2]）
        body_ok = True
        cur_body = 0
        min_body = self.cfg.get('min_body', 0)
        if min_body > 0:
            cur_body = abs(bar['c'] - bar['o']) / bar['o'] if bar['o'] else 0
            body_ok = cur_body >= min_body
            filters.append(f"{'✅' if body_ok else '❌'}实体({cur_body*100:.3f}%{'≥' if body_ok else '<'}{min_body*100:.2f}%)")
        else:
            filters.append('➖实体(跳过)')

        # 记录完整判定
        all_ok = sma_ok and vol_ok and mom_ok and body_ok

        # 每根K线都保存过滤状态（供Web界面实时显示）
        self.filter_status = {
            'sma20': {'ok': sma_ok, 'val': f'{sma20:.2f}' if sma20 else '--',
                       'detail': f'{"≥" if sma_ok else "<"}{sma20:.2f}' if sma20 else '数据不足'},
            'volume': {'ok': vol_ok, 'val': f'{cur_vol:,}',
                        'detail': f'{cur_vol:,}>={vol_avg*self.cfg.get("vol_mult",1.0):,.0f}' if vol_avg else '数据不足'},
            'momentum': {'ok': mom_ok, 'val': d,
                          'detail': f'{"阳线✓" if mom_ok else "非阳线✗"}' if ref_dir=="call" else f'{"阴线✓" if mom_ok else "非阴线✗"}'},
            'body': {'ok': body_ok, 'val': f'{cur_body*100:.3f}%',
                      'detail': f'{cur_body*100:.3f}%{"≥" if body_ok else "<"}{min_body*100:.2f}%'},
            'dir': direction,
            'price': f'${entry:.2f}',
            'all_ok': all_ok,
        }

        # 只有检测到信号时才执行交易
        if not sig:
            return

        tag = '🎯' if all_ok else '🔍'
        self._log(f"{tag} {direction}突破@${entry:.2f} | {' | '.join(filters)}")

        if not all_ok:
            return

        self.current_signal = sig
        self._exec(sig)

    def _chk_reversal(self, bar, cm):
        """衰竭反转信号检测 - 抓超跌反弹/超涨回调"""
        sh, sm = map(int, self.cfg['start_time'].split(':'))
        eh, em = map(int, self.cfg['end_time'].split(':'))
        if not (sh*60+sm <= cm <= eh*60+em):
            return
        if self.position:
            return
        if len(self.trades_today) >= self.cfg['max_trades']:
            return
        if self.daily_pnl <= -self.cfg['capital'] * self.cfg['daily_limit'] / 100:
            return
        if self.reversal_fired:
            return

        cs = self.one_min_candles
        if len(cs) < 3:
            return

        prev = cs[-2] if len(cs) >= 2 else cs[-1]  # 前一根K线（用于确认反弹）
        entry = bar['c']

        # 超跌反弹（做多）
        if self.session_high > 0:
            drop = (self.session_high - entry) / self.session_high
            if drop >= self.cfg['reversal_drop']:
                bounce = abs(prev['c'] - prev['o']) / prev['o'] if prev['o'] else 0
                bounce_ok = prev['c'] >= prev['o'] and bounce >= self.cfg['reversal_bounce']
                self._log(f"🔍 超跌反弹@${entry:.2f} | {'✅' if drop>=self.cfg['reversal_drop'] else '❌'}跌幅({drop*100:.1f}%≥0.5%) | {'✅' if bounce_ok else '❌'}反弹(实体{bounce*100:.3f}%{'≥' if bounce_ok else '<'}0.1%)")
                if bounce_ok:
                    sig = {'dir': 'call',
                           'reason': f'超跌反弹|从{self.session_high:.2f}跌{drop*100:.1f}%',
                           'price': entry}
                    self.reversal_fired = True
                    self.current_signal = sig
                    self._log(f"🔄 衰竭反转做多! 从高点跌{drop*100:.1f}%")
                    self._exec(sig)
                    return

        # 超涨回调（做空）
        if self.session_low < 999999:
            rise = (entry - self.session_low) / self.session_low
            if rise >= self.cfg['reversal_drop']:
                drop_body = abs(prev['c'] - prev['o']) / prev['o'] if prev['o'] else 0
                drop_ok = prev['c'] <= prev['o'] and drop_body >= self.cfg['reversal_bounce']
                self._log(f"🔍 超涨回调@${entry:.2f} | {'✅' if rise>=self.cfg['reversal_drop'] else '❌'}涨幅({rise*100:.1f}%≥0.5%) | {'✅' if drop_ok else '❌'}回调(实体{drop_body*100:.3f}%{'≥' if drop_ok else '<'}0.1%)")
                if drop_ok:
                    sig = {'dir': 'put',
                           'reason': f'超涨回调|从{self.session_low:.2f}涨{rise*100:.1f}%',
                           'price': entry}
                    self.reversal_fired = True
                    self.current_signal = sig
                    self._log(f"🔄 衰竭反做空! 从低点涨{rise*100:.1f}%")
                    self._exec(sig)

    def _exec(self, sig):
        """执行交易（已禁用 - 仅live_trader.py交易）"""
        self._log(f"⚠️ Web端交易已禁用，信号被忽略: {sig.get('dir','')} @ {sig.get('price',0):.2f}")
        return
        p = Decimal(str(sig['price']))
        q = int(self.cfg['capital'] * self.cfg['pos_pct'] / 100 / float(p))
        if q <= 0:
            self._log("⚠️ 资金不足")
            return
        s = OrderSide.Buy if sig['dir'] == 'call' else OrderSide.Sell
        try:
            r = self.tc.submit_order(
                symbol=self.cfg['symbol'], order_type=OrderType.MO, side=s,
                submitted_quantity=Decimal(str(q)), time_in_force=TimeInForceType.Day,
                outside_rth=OutsideRTH.AnyTime, remark=f"v6_{sig['dir']}")
            self.position = {
                'oid': r.order_id, 'dir': sig['dir'], 'ep': float(p),
                'sl': float(p) * (1 - self.cfg['sl']),
                'tp': float(p) * (1 + self.cfg['tp']) if sig['dir'] == 'call'
                      else float(p) * (1 - self.cfg['tp']),
                'qty': q, 'et': datetime.now().strftime('%H:%M:%S'),
                'eb': len(self.one_min_candles), 'reason': sig['reason'], 'mp': 0,
            }
            self.trades_today.append(dict(self.position))
            d = "🟢做多" if sig['dir'] == 'call' else "🔴做空"
            self._log(f"🎯 {d} @ ${float(p):.2f} x{q} | SL:${self.position['sl']:.2f} TP:${self.position['tp']:.2f}")
        except Exception as e:
            self._log(f"❌ {e}")

    def _chk_pos(self):
        """检查持仓状态"""
        if not self.position or not self.qc:
            return
        try:
            qs = self.qc.quote([self.cfg['symbol']])
            if not qs:
                return
            cp = float(qs[0].last_done)
            self.current_price = cp
        except:
            return

        p = self.position
        e = p['ep']
        pp = ((cp - e) / e * 100) if p['dir'] == 'call' else ((e - cp) / e * 100)
        p['mp'] = max(p['mp'], pp)

        # 持仓1分钟K线数
        bh = len(self.one_min_candles) - p['eb']

        # 每5根K线打印一次持仓状态
        if bh > 0 and bh % 5 == 0:
            self._log(f"📊 持仓{bh}min | 正股${cp:.2f} | 盈亏:{pp:+.2f}% | 最大:{p['mp']:.2f}%")

        ex = None
        sl_pct = self.cfg['sl'] * 100
        tp_pct = self.cfg['tp'] * 100

        # 止损
        if pp <= -sl_pct:
            ex = f"止损({pp:.2f}%)"
        # 止盈
        if pp >= tp_pct:
            ex = f"止盈({pp:.2f}%)"
        # 跟踪止损
        trail_on = self.cfg['trail_activate'] * 100
        trail_drop = self.cfg['trail_drop'] * 100
        if p['mp'] > trail_on and pp < p['mp'] - trail_drop:
            ex = f"跟损({p['mp']:.1f}→{pp:.1f}%)"
        # 超时（60根1分钟=60分钟）
        if bh >= 60:
            ex = f"超时({bh}个5分)"
        if ex:
            self._close(ex)

    def _close(self, reason):
        """平仓（已禁用 - 仅live_trader.py交易）"""
        self._log(f"⚠️ Web端平仓已禁用: {reason}")
        return
        p = self.position
        if not p:
            return
        s = OrderSide.Sell if p['dir'] == 'call' else OrderSide.Buy
        try:
            self.tc.submit_order(
                symbol=self.cfg['symbol'], order_type=OrderType.MO, side=s,
                submitted_quantity=Decimal(str(p['qty'])), time_in_force=TimeInForceType.Day,
                outside_rth=OutsideRTH.AnyTime, remark="v6_close")
            qs = self.qc.quote([self.cfg['symbol']])
            xp = float(qs[0].last_done) if qs else p['ep']
            pp = ((xp - p['ep']) / p['ep'] * 100) if p['dir'] == 'call' else ((p['ep'] - xp) / p['ep'] * 100)
            pu = self.cfg['capital'] * self.cfg['pos_pct'] / 100 * pp / 100
            self.daily_pnl += pu

            # 同步更新 trades_today 中的记录
            for t in self.trades_today:
                if t.get('oid') == p.get('oid') and not t.get('exit_reason'):
                    t.update({
                        'exit_price': xp,
                        'pnl_pct': pp,
                        'pnl_usd': pu,
                        'exit_reason': reason,
                        'exit_time': datetime.now().strftime('%H:%M:%S'),
                        'win': pp > 0,
                    })
                    break

            self._log(f"🏁 {reason} | ${p['ep']:.2f}→${xp:.2f} | {'✅' if pp > 0 else '❌'}{pp:+.2f}%(${pu:+,.0f})")
        except Exception as e:
            self._log(f"❌ 平仓失败: {e}（持仓保留，下次重试）")
            return
        self.position = None  # 成功后才清空

    def get_state(self):
        # 读取live_trader.py写入的共享状态
        shared = {}
        try:
            state_file = os.path.join(_app_dir(), 'state.json')
            if os.path.exists(state_file):
                with open(state_file, encoding='utf-8') as f:
                    shared = json.load(f)
        except:
            pass

        quote = {}
        if self.qc:
            try:
                qs = self.qc.quote([self.cfg['symbol']])
                if qs:
                    q = qs[0]
                    last = float(q.last_done)
                    prev = float(q.prev_close)
                    chg = last - prev
                    pct = chg / prev * 100 if prev else 0
                    quote = {
                        'price': last, 'change': chg, 'pct': pct,
                        'open': float(q.open), 'high': float(q.high),
                        'low': float(q.low), 'vol': int(q.volume),
                    }
            except:
                pass

        pos_list = []
        for h in self.holdings:
            pos_list.append({
                'sym': h['sym'], 'qty': h['qty'],
                'cost': f"${h['cost']:.3f}", 'cur': f"${h['cur']:.3f}",
                'mv': f"{h.get('market_value', 0):,.2f}",
                'pnl': f"{h['pnl']:+,.2f}", 'pct': f"{h['pct']:+.2f}%",
                'hold_pct': f"{h.get('holding_pct', 0):.2f}%",
                'up': h['pnl'] >= 0,
            })

        trades = []
        for i, t in enumerate(self.trades_today):
            trades.append({
                'id': i + 1, 'time': t['et'],
                'dir': '做多' if t['dir'] == 'call' else '做空',
                'dir_up': t['dir'] == 'call',
                'ep': f"${t['ep']:.2f}", 'qty': t['qty'],
                'active': (t == self.position),
            })

        sig = self.current_signal or {}
        strat_pos = None
        if self.position:
            p = self.position
            strat_pos = {
                'dir': '做多' if p['dir'] == 'call' else '做空',
                'dir_up': p['dir'] == 'call',
                'price': f"${p['ep']:.2f}", 'qty': p['qty'],
                'pnl': f"{p.get('mp', 0):+.2f}%", 'reason': p.get('reason', ''),
            }

        wins = sum(1 for t in self.trades_today if t.get('dir') and not t.get('active', True) and
                   ((t.get('ep', 0) and t.get('ep', 0) > 0)))

        # 使用live_trader的共享状态（如果有）
        filters = shared.get('filter_status', self.filter_status)
        if not isinstance(filters, dict) or 'sma20' not in filters:
            filters = self.filter_status
        # 补全缺失的filter key（兼容旧版state.json）
        _default_filter = {'ok': None, 'val': '--', 'detail': '--'}
        for key in ('volume', 'momentum', 'body', 'sma20', 'sma50', 'price_pos', 'trend', 'vwap', 'macd'):
            if key not in filters or not isinstance(filters[key], dict):
                filters[key] = dict(_default_filter)
        daily_pnl = shared.get('daily_pnl', self.daily_pnl)
        shared_trades = shared.get('trades_today', [])
        shared_pos = shared.get('position')
        cur_signal = shared.get('current_signal')

        # ===== 实时持仓：从 position_snapshot.json 读取（由 live_trader 写入）=====
        positions = []
        try:
            script_dir = _app_dir()
            pos_file = os.path.join(script_dir, 'position_snapshot.json')
            if os.path.exists(pos_file):
                with open(pos_file, encoding='utf-8') as f:
                    raw = json.load(f)
                if isinstance(raw, list):
                    positions = raw
                elif isinstance(raw, dict):
                    positions = raw.get('positions', [raw])
        except Exception as e:
            print(f"  ⚠️ 读取实时持仓失败: {e}")

        # 补充小程序使用的 filters 字段（需在 cur_signal 赋值之后）
        if cur_signal:
            filters['dir'] = '做多' if cur_signal.get('dir') == 'call' else '做空'
        filters['all_ok'] = all(
            f.get('ok') is True
            for f in [filters.get('sma20'), filters.get('volume'), filters.get('momentum'), filters.get('body')]
            if f is not None
        )

        # ===== 交易记录：合并 shared_trades（真实交易）+ longbridge_orders（长桥完整记录）=====
        # shared_trades 有完整交易信息（reason/entry/exit），长桥有全部订单明细
        # 两者都读，合并去重（按 opt_symbol），都有则优先用 shared_trades
        trades = []
        lb_today_pnl = 0

        # 独立读取长桥订单（始终读取）
        lb_orders = []
        try:
            script_dir = _app_dir()
            lb_file = os.path.join(script_dir, 'longbridge_orders.json')
            if os.path.exists(lb_file):
                with open(lb_file, encoding='utf-8') as f:
                    lb_data = json.load(f)
                lb_orders = lb_data.get('orders', [])
        except Exception as e:
            print(f"  ⚠️ 读取长桥订单失败: {e}")

        # 从 longbridge orders 构建基础交易列表（按标的配对）
        order_map = {}
        for o in lb_orders:
            symbol = o.get('symbol', '')
            side = o.get('side', '')
            exec_qty = float(o.get('executed_qty', 0))
            exec_price = float(o.get('executed_price', 0))
            if exec_qty > 0 and exec_price > 0:
                if symbol not in order_map:
                    order_map[symbol] = {'buys': [], 'sells': []}
                if side == '买入':
                    order_map[symbol]['buys'].append({'qty': exec_qty, 'price': exec_price})
                else:
                    order_map[symbol]['sells'].append({'qty': exec_qty, 'price': exec_price})

        # 生成 lb_trades
        lb_trades = []
        for symbol, ords in order_map.items():
            buys, sells = ords['buys'], ords['sells']
            if not sells:
                total_buy_qty = sum(b['qty'] for b in buys)
                total_buy_cost = sum(b['qty'] * b['price'] for b in buys)
                avg_buy = total_buy_cost / total_buy_qty if total_buy_qty > 0 else 0
                lb_trades.append({
                    'opt_symbol': symbol,
                    'dir': 'call' if 'C' in symbol else 'put',
                    'entry_price': avg_buy,
                    'exit_price': 0,
                    'contracts': int(total_buy_qty),
                    'pnl_pct': 0,
                    'pnl_usd': 0,
                    'active': True,
                    'result': '',
                    'exit_reason': '',
                })
            else:
                total_buy_qty = sum(b['qty'] for b in buys)
                total_buy_cost = sum(b['qty'] * b['price'] for b in buys)
                total_sell_qty = sum(s['qty'] for s in sells)
                total_sell_revenue = sum(s['qty'] * s['price'] for s in sells)
                matched_qty = min(total_buy_qty, total_sell_qty)
                if matched_qty > 0 and total_buy_qty > 0:
                    avg_buy = total_buy_cost / total_buy_qty
                    avg_sell = total_sell_revenue / total_sell_qty if total_sell_qty > 0 else 0
                    pnl = (avg_sell - avg_buy) * matched_qty * 100
                    lb_today_pnl += pnl
                    lb_trades.append({
                        'opt_symbol': symbol,
                        'dir': 'call' if 'C' in symbol else 'put',
                        'entry_price': avg_buy,
                        'exit_price': avg_sell,
                        'contracts': int(matched_qty),
                        'pnl_pct': round((avg_sell - avg_buy) / avg_buy * 100, 2) if avg_buy > 0 else 0,
                        'pnl_usd': round(pnl, 2),
                        'active': False,
                        'result': 'win' if pnl > 0 else 'lose',
                        'exit_reason': f'卖出@${avg_sell:.2f}',
                    })

        # shared_trades 优先（真实交易），补充 lb_trades 中没有的
        known_symbols = set()
        for t in shared_trades:
            opt = t.get('opt_symbol', '')
            trades.append({
                'id': len(trades) + 1,
                'time': t.get('time', '--:--'),
                'dir': '做多' if t.get('dir') == 'call' else '做空',
                'dir_up': t.get('dir') == 'call',
                'ep': f"${t.get('entry_price', 0):.2f}",
                'qty': t.get('contracts', t.get('qty', 0)),
                'opt': opt,
                'active': False,
                'pnl_pct': round(t.get('pnl_pct', 0), 2),
                'pnl_usd': round(t.get('pnl_usd', 0), 2),
                'exit_reason': t.get('exit_reason', ''),
                'exit_price': f"${t.get('exit_price', 0):.2f}" if t.get('exit_price') else '',
                'result': t.get('result', '') or ('win' if t.get('pnl_pct', 0) > 0 else 'lose' if t.get('pnl_pct', 0) < 0 else ''),
            })
            lb_today_pnl += t.get('pnl_usd', 0)
            known_symbols.add(opt)

        # 补充 lb_trades 中 shared_trades 没有的标的
        for t in lb_trades:
            if t['opt_symbol'] not in known_symbols:
                trades.append({
                    'id': len(trades) + 1,
                    'time': '--',
                    'dir': '做多' if t['dir'] == 'call' else '做空',
                    'dir_up': t['dir'] == 'call',
                    'ep': f"${t['entry_price']:.2f}",
                    'qty': t['contracts'],
                    'opt': t['opt_symbol'],
                    'active': t['active'],
                    'pnl_pct': t['pnl_pct'],
                    'pnl_usd': t['pnl_usd'],
                    'exit_reason': t.get('exit_reason', ''),
                    'exit_price': f"${t['exit_price']:.2f}" if t['exit_price'] else '',
                    'result': t['result'],
                })
        # 信号方向
        sig_dir = ''
        sig_up = None
        sig_price = '--'
        if cur_signal:
            sig_dir = '🟢做多' if cur_signal.get('dir') == 'call' else '🔴做空'
            sig_up = cur_signal.get('dir') == 'call'
            sig_price = f"${cur_signal.get('price', 0):.2f}"

        # 持仓：优先用 positions 实时数据（longbridge），其次用 shared_pos
        strat_pos = None
        if positions:
            # 有实际 longbridge 持仓时，用 shared_trades 最后一笔填充 strat_pos
            last_trade = shared_trades[-1] if shared_trades else None
            strat_pos = {
                'dir': last_trade.get('dir', '').replace('做多', '做多').replace('做空', '做空') or ('做多' if 'C' in positions[0].get('sym', '') else '做空'),
                'dir_up': last_trade.get('dir') == 'call' if last_trade else 'C' in positions[0].get('sym', ''),
                'price': f"${last_trade.get('entry_price', 0):.2f}" if last_trade and last_trade.get('entry_price') else positions[0].get('cost', '--'),
                'qty': sum(int(p.get('qty', 0)) for p in positions),
                'pnl': f"${sum(float(p.get('pnl', 0).replace(',', '').replace('+', '')) for p in positions):+,.2f}",
                'reason': last_trade.get('reason', '') if last_trade else '',
            }
        elif shared_pos:
            # 没有 longbridge 持仓时，shared_pos 也不应该显示（已平仓）
            strat_pos = None

        # 为每笔交易添加日期字段（小程序按日期过滤用）
        today_str = datetime.now().strftime('%Y-%m-%d')
        for t in trades:
            t['date'] = today_str

        return {
            'connected': shared.get('connected', self.connected),
            'running': shared.get('running', self.running),
            'quote': quote,
            'account': self.account_info,
            'positions': pos_list,
            'strat_pos': strat_pos,
            'signal': {
                'dir': sig_dir or '无信号',
                'up': sig_up,
                'price': sig_price,
                'reason': cur_signal.get('reason', '--') if cur_signal else '--',
            },
            'filters': filters,
            'trades': trades,
            'lb_orders': lb_orders,  # 长桥实际订单
            # daily: Web模板用的今日汇总（dict）- 用 positions 实时数据判断持仓
            'daily': {
                'open': len(positions),  # 用实际持仓数
                'closed': len([t for t in trades if not t.get('active')]),
                'holding': len(positions),
                'pnl': lb_today_pnl,
                'pnl_str': f"${lb_today_pnl:+,.2f}",
                'count': len(trades),
                'max': self.cfg['max_trades'],
                'lb_buy': sum(1 for o in lb_orders if o['side'] == '买入'),
                'lb_sell': sum(1 for o in lb_orders if o['side'] == '卖出'),
                'account': self.account_info,
            },
            # today: 小程序读取今日汇总的入口
            'today': {
                'open': 1 if shared_pos else 0,
                'closed': len([t for t in trades if not t.get('active')]),
                'holding': 1 if shared_pos else 0,
                'pnl': lb_today_pnl,
                'pnl_str': f"${lb_today_pnl:+,.2f}",
                'count': len(lb_orders) if lb_orders else len(trades),
                'max': self.cfg['max_trades'],
                'lb_buy': sum(1 for o in lb_orders if o['side'] == '买入'),
                'lb_sell': sum(1 for o in lb_orders if o['side'] == '卖出'),
            },
            # daily_history: 小程序历史页用的每日汇总数组
            'daily_history': self._load_daily_summaries(today_str, lb_today_pnl),
            'logs': self.logs[-30:],
            'config': {
                'strategy': 'v6全过滤',
                'symbol': self.cfg['symbol'],
                'window': f"{self.cfg['start_time']}-{self.cfg['end_time']}",
                'sl': f"{self.cfg['sl']*100:.2f}%",
                'tp': f"{self.cfg['tp']*100:.2f}%",
                'lookback': self.cfg['lookback'],
                'max_trades': self.cfg['max_trades'],
                'vol_mult': self.cfg.get('vol_mult', 0.8),
                'min_body': self.cfg.get('min_body', 0.0003),
                'pos_pct': self.cfg['pos_pct'],
            },
        }

    def _load_daily_summaries(self, today_str, today_pnl):
        """加载历史每日汇总（从daily_log.json读取，合并今日）"""
        daily_list = []
        try:
            sd = _app_dir()
            log_file = os.path.join(sd, 'daily_log.json')
            if os.path.exists(log_file):
                with open(log_file, encoding='utf-8') as f:
                    daily_list = json.load(f)
        except:
            pass

        # 合并/更新今日数据
        today_entry = {
            'date': today_str,
            'pnl': today_pnl,
            'pnl_str': f"${today_pnl:+,.2f}",
        }
        found = False
        for d in daily_list:
            if d.get('date') == today_str:
                d.update(today_entry)
                found = True
                break
        if not found:
            daily_list.append(today_entry)

        # 按日期排序
        daily_list.sort(key=lambda x: x.get('date', ''))
        return daily_list


# ============================================================
# Flask
# ============================================================
engine = None

# Flask相关代码延迟到main()中执行，Engine类不需要Flask
# 使用FlaskProxy延迟装饰器注册
class _FlaskProxy:
    """Flask代理 - 收集路由装饰器，延迟到create_flask_app()时应用"""
    def __init__(self):
        self._routes = []
        self._before = []
    def route(self, rule, **opts):
        def decorator(f):
            self._routes.append((rule, f, opts))
            return f
        return decorator
    def before_request(self, f):
        self._before.append(f)
        return f
app = _FlaskProxy()

# ===== API鉴权（从环境变量读取）=====
API_TOKEN = os.environ.get('API_TOKEN', 'qqq_trading_2026')

# ===== GitHub Gist 同步配置（从环境变量读取）=====
GIST_ID = os.environ.get('GIST_ID', '')
GIST_FILENAME = 'qqq_records.json'
GIST_SYNC_INTERVAL = 60  # 同步间隔（秒）

def get_github_token():
    """从环境变量或.env文件读取GitHub Token"""
    token = os.environ.get('GITHUB_TOKEN', '')
    if token:
        return token
    # 尝试从.env文件读取
    sd = _app_dir()
    for f in [os.path.join(sd, '.env'), os.path.expanduser('~/.hermes/.env')]:
        if os.path.exists(f):
            with open(f, encoding='utf-8') as fh:
                for line in fh:
                    line = line.strip()
                    if line.startswith('GITHUB_TOKEN='):
                        return line.split('=', 1)[1].strip('"').strip("'")
    return ''

def gist_sync_loop():
    """定时同步引擎状态到GitHub Gist"""
    import urllib.request
    import urllib.error

    token = get_github_token()
    if not token:
        print("⚠️ GITHUB_TOKEN未设置，Gist同步已禁用")
        print("  设置方法: 在.env文件中添加 GITHUB_TOKEN=ghp_xxxxx")
        return

    print(f"🚀 Gist同步已启动 | 间隔={GIST_SYNC_INTERVAL}s | Gist={GIST_ID[:8]}...")

    while True:
        try:
            time.sleep(GIST_SYNC_INTERVAL)
            if not engine:
                continue

            # 获取当前状态
            state = engine.get_state()

            # 保存每日汇总到本地文件（供历史查询）
            try:
                sd = _app_dir()
                daily_file = os.path.join(sd, 'daily_log.json')
                with open(daily_file, 'w', encoding='utf-8') as f:
                    json.dump(state.get('daily_history', []), f, ensure_ascii=False, indent=2)
            except Exception as e:
                print(f"  ⚠️ daily_log.json保存失败: {e}")

            # 序列化为JSON
            payload = json.dumps(state, ensure_ascii=False, default=str)

            # 更新Gist
            url = f'https://api.github.com/gists/{GIST_ID}'
            data = json.dumps({
                'files': {
                    GIST_FILENAME: {
                        'content': payload
                    }
                }
            }).encode('utf-8')

            req = urllib.request.Request(url, data=data, method='PATCH')
            req.add_header('Authorization', f'token {token}')
            req.add_header('Accept', 'application/vnd.github.v3+json')
            req.add_header('Content-Type', 'application/json')

            with urllib.request.urlopen(req, timeout=30) as resp:
                if resp.status == 200:
                    ts = datetime.now().strftime('%H:%M:%S')
                    size_kb = len(payload) / 1024
                    print(f"  ✅ Gist同步成功 [{ts}] {size_kb:.1f}KB")
                else:
                    print(f"  ⚠️ Gist同步异常: HTTP {resp.status}")

        except urllib.error.HTTPError as e:
            print(f"  ❌ Gist同步失败: HTTP {e.code} - {e.reason}")
        except Exception as e:
            print(f"  ❌ Gist同步失败: {e}")

@app.before_request
def check_auth():
    """API鉴权 - 仅/api/*路径需要token"""
    from flask import request as req
    if req.path.startswith('/api/'):
        auth = req.headers.get('Authorization', '')
        token = req.args.get('token', '')
        if auth != f'Bearer {API_TOKEN}' and token != API_TOKEN:
            from flask import jsonify as jfy
            return jfy({'error': 'unauthorized'}), 401

HTML = '''<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>热血青年的交易所</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Orbitron:wght@400;500;600;700;800;900&family=Rajdhani:wght@400;500;600;700&family=Share+Tech+Mono&display=swap');

*,*::before,*::after{margin:0;padding:0;box-sizing:border-box}

:root{
  --bg:#0a0e1a;
  --surface:rgba(12,18,40,.85);
  --surface-2:rgba(18,26,56,.9);
  --cyan:#00f0ff;
  --cyan-dim:rgba(0,240,255,.08);
  --cyan-border:rgba(0,240,255,.20);
  --cyan-glow:rgba(0,240,255,.35);
  --blue:#4d7cff;
  --purple:#a855f7;
  --magenta:#ff2d95;
  --r:#ff3b5c;
  --r-dim:rgba(255,59,92,.10);
  --r-border:rgba(255,59,92,.25);
  --g:#00ff88;
  --g-dim:rgba(0,255,136,.08);
  --g-border:rgba(0,255,136,.20);
  --a:#ffb800;
  --a-dim:rgba(255,184,0,.08);
  --a-border:rgba(255,184,0,.20);
  --text:#e0e8ff;
  --text-2:rgba(200,210,240,.6);
  --text-3:rgba(200,210,240,.3);
  --border:rgba(0,240,255,.08);
  --border-h:rgba(0,240,255,.18);
  --mono:'Share Tech Mono',monospace;
  --sans:'Rajdhani',system-ui,sans-serif;
  --display:'Orbitron',sans-serif;
}

html{font-size:16px}
body{
  font-family:var(--sans);background:var(--bg);color:var(--text);
  min-height:100vh;-webkit-font-smoothing:antialiased;
  background-image:
    radial-gradient(ellipse 100% 80% at 20% 0%,rgba(0,240,255,.04),transparent),
    radial-gradient(ellipse 80% 60% at 80% 100%,rgba(168,85,247,.03),transparent);
  position:relative;overflow-x:hidden;
}

/* Grid overlay */
body::before{
  content:'';position:fixed;inset:0;z-index:0;pointer-events:none;
  background-image:
    linear-gradient(rgba(0,240,255,.025) 1px,transparent 1px),
    linear-gradient(90deg,rgba(0,240,255,.025) 1px,transparent 1px);
  background-size:80px 80px;
  mask-image:radial-gradient(ellipse 90% 80% at 50% 30%,black,transparent);
}

/* Scanline */
body::after{
  content:'';position:fixed;top:0;left:0;right:0;height:2px;z-index:999;
  background:linear-gradient(90deg,transparent,var(--cyan),transparent);
  opacity:.15;
  animation:scanline 4s linear infinite;
  pointer-events:none;
}
@keyframes scanline{0%{top:-2px}100%{top:100vh}}

.wrap{max-width:1480px;margin:0 auto;padding:0 24px 60px;position:relative;z-index:1}

/* ═══ TICKER ═══ */
.ticker{
  position:sticky;top:0;z-index:50;
  background:rgba(6,10,26,.95);backdrop-filter:blur(20px);
  border-bottom:1px solid var(--border);
  padding:16px 0;
}
.ticker::after{
  content:'';position:absolute;bottom:0;left:0;right:0;height:1px;
  background:linear-gradient(90deg,transparent,var(--cyan-glow),transparent);
}
.ticker-in{
  max-width:1480px;margin:0 auto;padding:0 24px;
  display:flex;align-items:center;gap:24px;
}
.ticker-sym{
  font-family:var(--display);font-size:14px;font-weight:700;
  color:var(--cyan);letter-spacing:2px;
}
.ticker-price{
  font-family:var(--display);font-size:36px;font-weight:800;
  letter-spacing:1px;transition:color .3s;
}
.ticker-price.up{color:var(--r);text-shadow:0 0 20px var(--r-dim)}
.ticker-price.dn{color:var(--g);text-shadow:0 0 20px var(--g-dim)}
.ticker-chg{
  font-family:var(--mono);font-size:16px;font-weight:500;
  padding:4px 14px;border-radius:6px;
  border:1px solid;
}
.ticker-chg.up{color:var(--r);border-color:var(--r-border);background:var(--r-dim)}
.ticker-chg.dn{color:var(--g);border-color:var(--g-border);background:var(--g-dim)}
.ticker-ohlc{
  display:flex;gap:20px;margin-left:auto;
  font-family:var(--mono);font-size:14px;color:var(--text-3);
}
.ticker-ohlc span b{color:var(--text-2);font-weight:400}
.ticker-right{display:flex;align-items:center;gap:16px;margin-left:20px}
.ticker-clock{font-family:var(--mono);font-size:13px;color:var(--text-3)}
.status{
  display:flex;align-items:center;gap:8px;
  font-family:var(--sans);font-size:13px;font-weight:600;
  padding:6px 16px;border-radius:20px;border:1px solid;
}
.status.live{color:var(--g);border-color:var(--g-border);background:var(--g-dim)}
.status.off{color:var(--r);border-color:var(--r-border);background:var(--r-dim)}
.status .pip{
  width:6px;height:6px;border-radius:50%;
  animation:pulse 2s infinite;
}
.status.live .pip{background:var(--g);box-shadow:0 0 10px var(--g)}
.status.off .pip{background:var(--r)}
@keyframes pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.3;transform:scale(.6)}}

/* ═══ STRATEGY BAR ═══ */
.stratbar{
  background:var(--surface);border-bottom:1px solid var(--border);
  padding:14px 0;
}
.stratbar-in{
  max-width:1480px;margin:0 auto;padding:0 24px;
  display:flex;align-items:center;gap:16px;
}
.stratbar-label{
  font-family:var(--sans);font-size:14px;font-weight:600;
  color:var(--text-3);
}
.stratbar-dir{
  font-family:var(--display);font-size:13px;font-weight:700;
  padding:4px 14px;border-radius:6px;letter-spacing:1px;
}
.stratbar-dir.call{background:var(--r-dim);color:var(--r);border:1px solid var(--r-border)}
.stratbar-dir.put{background:var(--g-dim);color:var(--g);border:1px solid var(--g-border)}
.stratbar-info{font-family:var(--mono);font-size:15px;color:var(--text-2)}
.stratbar-pnl{font-family:var(--display);font-size:18px;font-weight:700;margin-left:auto}

/* ═══ MAIN 2-COL ═══ */
.main{display:grid;grid-template-columns:300px 1fr;gap:20px;padding-top:20px}
.data-row{display:grid;grid-template-columns:1fr 1fr;gap:14px}

/* ═══ LEFT SIDEBAR ═══ */
.side{display:flex;flex-direction:column;gap:14px}

.acct{
  background:var(--surface);border:1px solid var(--border);border-radius:14px;
  padding:20px;backdrop-filter:blur(12px);
  position:relative;overflow:hidden;
}
.acct::before{
  content:'';position:absolute;top:0;left:0;right:0;height:2px;
  background:linear-gradient(90deg,var(--cyan),var(--blue));
}
.acct-title{
  font-family:var(--display);font-size:11px;font-weight:600;
  color:var(--cyan);letter-spacing:2px;text-transform:uppercase;
  margin-bottom:16px;
}
.acct-pnl{text-align:center;padding:8px 0 12px}
.acct-pnl .big{
  font-family:var(--display);font-size:36px;font-weight:800;
  letter-spacing:1px;
}
.acct-pnl .big.up{color:var(--r);text-shadow:0 0 24px var(--r-dim)}
.acct-pnl .big.dn{color:var(--g);text-shadow:0 0 24px var(--g-dim)}
.acct-pnl .sub{font-size:14px;color:var(--text-3);margin-top:4px}
.acct-hr{height:1px;background:var(--border);margin:10px 0}
.acct-row{display:flex;justify-content:space-between;align-items:baseline;padding:6px 0}
.acct-row .k{font-size:14px;color:var(--text-3)}
.acct-row .v{font-family:var(--mono);font-size:16px;font-weight:500;color:var(--text)}

/* Filters */
.flabel{
  font-family:var(--display);font-size:10px;font-weight:600;
  color:var(--cyan);letter-spacing:2px;text-transform:uppercase;
  margin-bottom:10px;
}
.fstack{display:flex;flex-direction:column;gap:8px}
.fbar{
  background:var(--surface);border:1px solid var(--border);border-radius:10px;
  padding:12px 16px;display:flex;align-items:center;gap:12px;
  transition:all .3s;backdrop-filter:blur(8px);
}
.fbar:hover{border-color:var(--border-h)}
.fbar.ok{border-color:var(--g-border);background:var(--g-dim)}
.fbar.ng{border-color:var(--r-border);background:var(--r-dim)}
.fbar.idle{opacity:.35}
.fbar-dot{
  width:10px;height:10px;border-radius:50%;flex-shrink:0;
  transition:all .3s;
}
.fbar.ok .fbar-dot{background:var(--g);box-shadow:0 0 12px var(--g);animation:pulse 2s infinite}
.fbar.ng .fbar-dot{background:var(--r);box-shadow:0 0 12px var(--r)}
.fbar.idle .fbar-dot{background:var(--text-3)}
.fbar-name{font-size:14px;font-weight:600;color:var(--text);flex:1}
.fbar-val{font-family:var(--mono);font-size:13px;color:var(--text-2)}

.verdict{
  background:var(--surface);border:1px solid var(--border);border-radius:10px;
  padding:16px;text-align:center;backdrop-filter:blur(8px);transition:all .3s;
}
.verdict.go{border-color:var(--g-border);background:var(--g-dim)}
.verdict.no{border-color:var(--r-border);background:var(--r-dim)}
.verdict .vt{
  font-family:var(--display);font-size:22px;font-weight:800;
  letter-spacing:3px;
}
.verdict.go .vt{color:var(--g);text-shadow:0 0 16px var(--g-dim)}
.verdict.no .vt{color:var(--r);text-shadow:0 0 16px var(--r-dim)}
.verdict .vs{font-size:13px;color:var(--text-3);margin-top:4px}

.params{
  font-family:var(--mono);font-size:12px;color:var(--text-3);line-height:1.8;
}

/* ═══ PANELS ═══ */
.content{display:flex;flex-direction:column;gap:14px}

.panel{
  background:var(--surface);border:1px solid var(--border);border-radius:14px;
  backdrop-filter:blur(12px);overflow:hidden;
  transition:all .3s;
}
.panel:hover{border-color:var(--border-h)}
.panel-head{
  display:flex;align-items:center;gap:10px;
  padding:14px 20px;border-bottom:1px solid var(--border);
}
.panel-head .ico{font-size:18px}
.panel-head .title{
  font-family:var(--display);font-size:11px;font-weight:600;
  color:var(--cyan);letter-spacing:2px;text-transform:uppercase;
}
.panel-head .cnt{
  margin-left:auto;font-family:var(--mono);font-size:12px;color:var(--text-3);
}
.panel-head .btn{
  margin-left:auto;font-family:var(--sans);
  font-size:13px;font-weight:700;padding:6px 18px;border-radius:20px;
  border:1px solid var(--cyan-border);background:var(--cyan-dim);
  color:var(--cyan);cursor:pointer;transition:all .25s;letter-spacing:.5px;
}
.panel-head .btn:hover{
  background:rgba(0,240,255,.15);box-shadow:0 0 20px var(--cyan-glow);
  transform:translateY(-1px);
}

/* Tables */
.panel table{width:100%;border-collapse:collapse;font-size:14px}
.panel th{
  text-align:left;padding:10px 14px;color:var(--text-3);
  font-family:var(--sans);font-size:11px;font-weight:700;
  text-transform:uppercase;letter-spacing:1px;
  border-bottom:1px solid var(--border);background:rgba(0,0,0,.2);
}
.panel td{
  padding:11px 14px;border-bottom:1px solid rgba(0,240,255,.03);
  color:var(--text-2);transition:background .15s;
}
.panel tr:hover td{background:rgba(0,240,255,.03)}
.panel td.sym{color:var(--text);font-weight:600;font-size:15px}
.panel td.mono{font-family:var(--mono);font-size:13px}
.panel td.up{color:var(--r);font-family:var(--mono);font-weight:600}
.panel td.dn{color:var(--g);font-family:var(--mono);font-weight:600}
.panel td.empty{text-align:center;color:var(--text-3);padding:36px 0;font-size:15px}
.tbl-wrap{max-height:340px;overflow-y:auto}
.tbl-wrap::-webkit-scrollbar{width:4px}
.tbl-wrap::-webkit-scrollbar-thumb{background:rgba(0,240,255,.12);border-radius:2px}

.chip{
  display:inline-block;font-size:11px;font-weight:700;
  padding:3px 10px;border-radius:5px;letter-spacing:.3px;
}
.chip.hold{background:var(--a-dim);color:var(--a);border:1px solid var(--a-border)}
.chip.win{background:var(--g-dim);color:var(--g);border:1px solid var(--g-border)}
.chip.lose{background:var(--r-dim);color:var(--r);border:1px solid var(--r-border)}
.chip.done{color:var(--text-3)}

/* Order cards */
.orders-body{padding:14px 20px;max-height:360px;overflow-y:auto}
.orders-body::-webkit-scrollbar{width:4px}
.orders-body::-webkit-scrollbar-thumb{background:rgba(0,240,255,.12);border-radius:2px}
.ocard{
  display:flex;align-items:center;gap:14px;
  padding:14px 18px;border-radius:10px;margin-bottom:8px;
  background:rgba(0,0,0,.2);border:1px solid var(--border);
  transition:all .25s;
}
.ocard:last-child{margin-bottom:0}
.ocard:hover{border-color:var(--border-h);background:rgba(0,240,255,.03)}
.ocard-side{
  font-family:var(--display);font-size:11px;font-weight:700;
  padding:4px 12px;border-radius:6px;letter-spacing:.5px;min-width:44px;text-align:center;
}
.ocard-side.buy{background:var(--r-dim);color:var(--r);border:1px solid var(--r-border)}
.ocard-side.sell{background:var(--g-dim);color:var(--g);border:1px solid var(--g-border)}
.ocard-sym{font-size:15px;font-weight:600;color:var(--text);flex:1}
.ocard-qty{font-family:var(--mono);font-size:14px;color:var(--text-2)}
.ocard-exec{font-family:var(--mono);font-size:14px;font-weight:500;color:var(--text)}
.ocard-status{
  font-size:11px;font-weight:700;padding:3px 10px;border-radius:5px;
}
.ocard-status.filled{background:var(--a-dim);color:var(--a);border:1px solid var(--a-border)}
.ocard-status.cancel{color:var(--text-3)}
.ocard-status.pending{background:rgba(0,240,255,.06);color:var(--cyan);border:1px solid var(--cyan-border)}

/* ═══ LOG ═══ */
.log-panel{background:rgba(8,12,28,.95);border:1px solid var(--border);border-radius:14px;overflow:hidden}
.log-panel .panel-head{background:rgba(0,0,0,.3);border-bottom-color:var(--border)}
.log-panel .panel-head .title{color:var(--cyan)}
.log-box{
  max-height:180px;overflow-y:auto;padding:12px 20px;
  font-family:var(--mono);font-size:13px;line-height:1.9;color:var(--text-3);
}
.log-box::-webkit-scrollbar{width:4px}
.log-box::-webkit-scrollbar-thumb{background:rgba(0,240,255,.1);border-radius:2px}
.log-box .le{padding:1px 0}
.log-box .lt{color:rgba(0,240,255,.2);margin-right:10px}

/* ═══ FOOTER ═══ */
.foot{
  display:flex;justify-content:space-between;align-items:center;
  padding:18px 0;border-top:1px solid var(--border);
  font-family:var(--sans);font-size:12px;color:var(--text-3);margin-top:16px;
}
.foot .pip{
  display:inline-block;width:5px;height:5px;border-radius:50%;
  background:var(--g);margin-right:8px;animation:pulse 2s infinite;
  box-shadow:0 0 6px var(--g);
}

/* ═══ ANIMATIONS ═══ */
@keyframes fadeUp{from{opacity:0;transform:translateY(12px)}to{opacity:1;transform:none}}
.panel,.acct,.fbar,.verdict{animation:fadeUp .5s ease both}
.panel:nth-child(2){animation-delay:.08s}
.fbar:nth-child(2){animation-delay:.04s}
.fbar:nth-child(3){animation-delay:.08s}
.fbar:nth-child(4){animation-delay:.12s}

/* ═══ RESPONSIVE ═══ */
@media(max-width:1100px){
  .main{grid-template-columns:1fr}
  .side{display:grid;grid-template-columns:1fr 1fr;gap:12px}
  .fstack{grid-column:1/-1}
  .verdict{grid-column:1/-1}
  .params{grid-column:1/-1}
  .data-row{grid-template-columns:1fr}
}
@media(max-width:600px){
  .wrap{padding:0 12px 40px}
  .ticker-in{flex-wrap:wrap;gap:10px}
  .ticker-ohlc{display:none}
  .ticker-price{font-size:28px}
  .side{grid-template-columns:1fr}
  .stratbar-in{flex-wrap:wrap}
  .data-row{grid-template-columns:1fr}
}

::-webkit-scrollbar{width:5px;height:4px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:rgba(0,240,255,.08);border-radius:3px}
</style>
</head>
<body>

<!-- TOPBAR -->
<div class="ticker" style="padding:12px 0;border-bottom:1px solid var(--border)">
  <div class="ticker-in">
    <div style="font-family:var(--display);font-size:18px;font-weight:800;background:linear-gradient(135deg,var(--cyan),var(--purple));-webkit-background-clip:text;-webkit-text-fill-color:transparent;letter-spacing:1px">⚡ 热血青年的交易所</div>
    <div class="ticker-sym" style="margin-left:16px">QQQ 0DTE · {{config.strategy}}</div>
    <div class="ticker-spacer" style="flex:1"></div>
    <div class="ticker-clock" id="clock"></div>
    <div class="status {%if connected%}live{%else%}off{%endif%}" id="badge"><span class="pip"></span><span id="connTxt">{%if connected%}已连接{%else%}连接中{%endif%}</span></div>
  </div>
</div>

<!-- TICKER -->
<div class="ticker">
  <div class="ticker-in">
    <div class="ticker-sym">QQQ.US</div>
    <div class="ticker-price {%if quote.change|default(0)>=0%}up{%else%}dn{%endif%}" id="tickerPrice">${{quote.price|default(0)|round(2)}}</div>
    <div class="ticker-chg {%if quote.change|default(0)>=0%}up{%else%}dn{%endif%}" id="tickerChg">{{'%+.2f'|format(quote.pct|default(0))}}%</div>
    <div class="ticker-ohlc" id="tickerOHLC">
      <span>开 <b>{{quote.open|default(0)|round(2)}}</b></span>
      <span>高 <b>{{quote.high|default(0)|round(2)}}</b></span>
      <span>低 <b>{{quote.low|default(0)|round(2)}}</b></span>
      <span>量 <b>{{quote.vol|default(0)}}</b></span>
    </div>
    </div>
  </div>
</div>

<!-- STRATEGY BAR -->
{%if strat_pos%}
<div class="stratbar" id="stratbar">
  <div class="stratbar-in">
    <span class="stratbar-label">📌 策略持仓</span>
    <span class="stratbar-dir {%if strat_pos.dir_up%}call{%else%}put{%endif%}">{{strat_pos.dir}}</span>
    <span class="stratbar-info">{{strat_pos.price}} ×{{strat_pos.qty}} · {{strat_pos.reason}}</span>
    <span class="stratbar-pnl {%if strat_pos.dir_up%}up{%else%}dn{%endif%}">{{strat_pos.pnl}}</span>
  </div>
</div>
{%endif%}

<!-- MAIN -->
<div class="wrap">
<div class="main">

  <!-- LEFT SIDEBAR -->
  <div class="side">
    <div class="acct">
      <div class="acct-title">💰 账户概览</div>
      <div class="acct-pnl">
        <div class="big {%if daily.pnl>=0%}up{%else%}dn{%endif%}" id="acctPnl">{{daily.pnl_str}}</div>
        <div class="sub" id="acctPnlSub">今日盈亏</div>
      </div>
      <div class="acct-hr"></div>
      <div class="acct-row"><span class="k">可用资金</span><span class="v" id="acctCash">${{daily.account.cash|default(0)|int}}</span></div>
      <div class="acct-row"><span class="k">净值</span><span class="v" id="acctNet">${{daily.account.net|default(0)|int}}</span></div>
      <div class="acct-row"><span class="k">购买力</span><span class="v" id="acctPower">${{daily.account.power|default(0)|int}}</span></div>
      <div class="acct-hr"></div>
      <div class="acct-row"><span class="k">今日交易</span><span class="v" id="acctTrades">{{daily.count}} / {{daily.max}}</span></div>
      <div class="acct-row"><span class="k">开仓 / 平仓</span><span class="v" id="acctOC">{{daily.open}} / {{daily.closed}}</span></div>
      <div class="acct-row"><span class="k">持仓中</span><span class="v" id="acctHolding">{{daily.holding}}</span></div>
      <div class="acct-row"><span class="k">买入 / 卖出</span><span class="v" id="acctBS">{{daily.lb_buy|default(0)}} / {{daily.lb_sell|default(0)}}</span></div>
    </div>

    <div class="flabel">🔍 信号过滤</div>
    <div class="fstack">
      <div class="fbar {%if filters.sma20.ok==True%}ok{%elif filters.sma20.ok==False%}ng{%else%}idle{%endif%}" id="fSMA">
        <div class="fbar-dot"></div>
        <div class="fbar-name">SMA20 趋势</div>
        <div class="fbar-val">{{filters.sma20.detail}}</div>
      </div>
      <div class="fbar {%if filters.volume.ok==True%}ok{%elif filters.volume.ok==False%}ng{%else%}idle{%endif%}" id="fVol">
        <div class="fbar-dot"></div>
        <div class="fbar-name">量能 ×{{config.vol_mult}}</div>
        <div class="fbar-val">{{filters.volume.detail}}</div>
      </div>
      <div class="fbar {%if filters.momentum.ok==True%}ok{%elif filters.momentum.ok==False%}ng{%else%}idle{%endif%}" id="fMom">
        <div class="fbar-dot"></div>
        <div class="fbar-name">动量确认</div>
        <div class="fbar-val">{{filters.momentum.detail}}</div>
      </div>
      <div class="fbar {%if filters.body.ok==True%}ok{%elif filters.body.ok==False%}ng{%else%}idle{%endif%}" id="fBody">
        <div class="fbar-dot"></div>
        <div class="fbar-name">K线实体</div>
        <div class="fbar-val">{{filters.body.detail}}</div>
      </div>
    </div>
    <div class="verdict {%if filters.all_ok%}go{%elif filters.dir%}no{%else%}{%endif%}" id="verdict">
      <div class="vt">{%if filters.all_ok%}PASS{%elif filters.dir%}BLOCK{%else%}WAIT{%endif%}</div>
      <div class="vs">{%if filters.all_ok%}🎯 {{filters.dir}}信号通过{%elif filters.dir%}❌ {{filters.dir}}信号拒绝{%else%}⏳ 等待信号{%endif%}</div>
    </div>
    <div class="params" id="params">
      目标 {{filters.price|default('--')}}<br>
      mode: {{filters.mode|default('--')}}<br>
      body ≥ {{(config.min_body*100)|round(2)}}% &nbsp;·&nbsp; vol ≥ {{config.vol_mult}}×<br>
      lookback = {{config.lookback}} &nbsp;·&nbsp; SL {{config.sl}} &nbsp;·&nbsp; TP {{config.tp}}<br>
      {{config.window}} ET
    </div>
  </div>

  <!-- MAIN CONTENT -->
  <div class="content">
    <!-- Positions + Trades side by side -->
    <div class="data-row">
    <div class="panel">
      <div class="panel-head">
        <span class="ico">💼</span>
        <span class="title">当前持仓</span>
      </div>
      <div class="tbl-wrap">
      <table><tbody id="posBody">
        <tr><th>代码</th><th>数量</th><th>成本</th><th>现价</th><th>市值</th><th>盈亏</th><th>盈亏%</th><th>占比</th></tr>
        {%for p in positions%}
        <tr>
          <td class="sym">{{p.sym}}</td>
          <td class="mono">{{p.qty}}</td>
          <td class="mono">{{p.cost}}</td>
          <td class="mono">{{p.cur}}</td>
          <td class="mono">${{p.mv}}</td>
          <td class="{%if p.up%}up{%else%}dn{%endif%}">${{p.pnl}}</td>
          <td class="{%if p.up%}up{%else%}dn{%endif%}">{{p.pct}}</td>
          <td class="mono">{{p.hold_pct}}</td>
        </tr>
        {%endfor%}
        {%if not positions%}
        <tr><td colspan="8" class="empty">暂无持仓</td></tr>
        {%endif%}</tbody>
      </table>
      </div>
    </div>

    <div class="panel">
      <div class="panel-head">
        <span class="ico">📋</span>
        <span class="title">交易记录</span>
        <span class="cnt" id="tradeCnt">{{trades|length}} 笔</span>
      </div>
      <div class="tbl-wrap">
      <table><tbody id="tradeBody">
        <tr><th>#</th><th>时间</th><th>方向</th><th>入场价</th><th>合约</th><th>盈亏</th><th>原因</th><th>状态</th></tr>
        {%for t in trades%}
        <tr>
          <td style="color:var(--text-3)">{{t.id}}</td>
          <td class="mono">{{t.time}}</td>
          <td class="{%if t.dir_up%}up{%else%}dn{%endif%}" style="font-weight:700">{{t.dir}}</td>
          <td class="mono">{{t.ep}}</td>
          <td class="mono">{{t.qty}}</td>
          <td class="{%if t.pnl_pct is defined and t.pnl_pct>=0%}up{%elif t.pnl_pct is defined%}dn{%endif%}">{%if t.pnl_pct is defined%}{{t.pnl_pct}}% (${{t.pnl_usd|int}}){%else%}—{%endif%}</td>
          <td style="font-size:12px;color:var(--text-3);max-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="{{t.exit_reason|default('')}}">{{t.exit_reason|default('—')}}</td>
          <td>{%if t.active%}<span class="chip hold">持仓中</span>{%elif t.result=='win'%}<span class="chip win">盈利</span>{%elif t.result=='lose'%}<span class="chip lose">亏损</span>{%else%}<span class="chip done">已平仓</span>{%endif%}</td>
        </tr>
        {%endfor%}
        {%if not trades%}
        <tr><td colspan="8" class="empty">暂无交易</td></tr>
        {%endif%}</tbody>
      </table>
      </div>
    </div>
    </div><!-- /data-row -->

    <!-- Orders -->
    <div class="panel">
      <div class="panel-head">
        <span class="ico">🏦</span>
        <span class="title">长桥订单</span>
        <button class="btn" onclick="syncOrders()">⚡ 同步</button>
      </div>
      <div class="tbl-wrap" id="lbOrders" style="max-height:260px">
        <table>
          <tr><th>#</th><th>标的</th><th>方向</th><th>委托量</th><th>成交量</th><th>委托价</th><th>成交价</th><th>状态</th></tr>
          <tr><td colspan="8" class="empty">点击同步加载订单</td></tr>
        </table>
      </div>
    </div>

    <!-- Log -->
    <div class="log-panel">
      <div class="panel-head">
        <span class="ico">🖥️</span>
        <span class="title">系统日志</span>
      </div>
      <div class="log-box" id="logBox">
        {%for l in logs%}
        <div class="le"><span class="lt">[{{l.time}}]</span>{{l.msg}}</div>
        {%endfor%}
      </div>
    </div>
  </div>

</div>

<div class="foot">
  <span><span class="pip"></span>10s 自动刷新</span>
  <span>QQQ 0DTE · {{config.strategy}} · 长桥证券 API</span>
  <span id="lastUpdate"></span>
</div>
</div>

<script>
// Clock
function updateClock(){
  var n=new Date();
  var b=n.toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false});
  var y=n.toLocaleString('en-US',{timeZone:'America/New_York',hour12:false});
  document.getElementById('clock').innerHTML=b+' │ '+y;
}
setInterval(updateClock,1000);updateClock();

// Helpers
function $(id){return document.getElementById(id)}
function esc(s){var d=document.createElement('div');d.textContent=s;return d.innerHTML}
function pnl(up){return up?'up':'dn'}

// Render positions table rows from JSON
function renderPositions(positions){
  if(!positions||!positions.length) return '<tr><td colspan="8" class="empty">暂无持仓</td></tr>';
  var h='';
  positions.forEach(function(p){
    var c=pnl(p.up);
    h+='<tr>';
    h+='<td class="sym">'+esc(p.sym)+'</td>';
    h+='<td class="mono">'+p.qty+'</td>';
    h+='<td class="mono">'+esc(p.cost)+'</td>';
    h+='<td class="mono">'+esc(p.cur)+'</td>';
    h+='<td class="mono">$'+esc(p.mv)+'</td>';
    h+='<td class="'+c+'">$'+esc(p.pnl)+'</td>';
    h+='<td class="'+c+'">'+esc(p.pct)+'</td>';
    h+='<td class="mono">'+esc(p.hold_pct)+'</td>';
    h+='</tr>';
  });
  return h;
}

// Render trades table rows
function renderTrades(trades){
  if(!trades||!trades.length) return '<tr><td colspan="8" class="empty">暂无交易</td></tr>';
  var h='';
  trades.forEach(function(t){
    var c=pnl(t.dir_up);
    var pnlCell='—';
    if(t.pnl_pct!==undefined&&t.pnl_pct!==null) pnlCell=t.pnl_pct+'% ($'+Math.round(t.pnl_usd||0)+')';
    var status='';
    if(t.active) status='<span class="chip hold">持仓中</span>';
    else if(t.result=='win') status='<span class="chip win">盈利</span>';
    else if(t.result=='lose') status='<span class="chip lose">亏损</span>';
    else status='<span class="chip done">已平仓</span>';
    h+='<tr>';
    h+='<td style="color:var(--text-3)">'+t.id+'</td>';
    h+='<td class="mono">'+esc(t.time)+'</td>';
    h+='<td class="'+c+'" style="font-weight:700">'+esc(t.dir)+'</td>';
    h+='<td class="mono">'+esc(t.ep)+'</td>';
    h+='<td class="mono">'+t.qty+'</td>';
    h+='<td class="'+(t.pnl_pct>=0?'up':'dn')+'">'+pnlCell+'</td>';
    h+='<td style="font-size:12px;color:var(--text-3);max-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+esc(t.exit_reason||'—')+'</td>';
    h+='<td>'+status+'</td>';
    h+='</tr>';
  });
  return h;
}

// Render logs
function renderLogs(logs){
  if(!logs||!logs.length) return '';
  var h='';
  logs.forEach(function(l){
    h+='<div class="le"><span class="lt">['+esc(l.time)+']</span>'+esc(l.msg)+'</div>';
  });
  return h;
}

// Render filter bar
function renderFilter(box,val,ok){
  box.className='fbar '+(ok===true?'ok':ok===false?'ng':'idle');
  box.querySelector('.fbar-val').textContent=val;
}

// Main refresh - NO page reload
var token='{{token}}';
function refresh(){
  fetch('/api/state?token='+token).then(function(r){return r.json()}).then(function(d){
    if(d.error) return;

    // Badge
    var badge=$('badge'), txt=$('connTxt');
    if(d.connected){badge.className='status live';txt.textContent='已连接';}
    else{badge.className='status off';txt.textContent='未连接';}

    // Ticker
    var q=d.quote||{};
    var isUp=(q.change||0)>=0;
    var tc=isUp?'up':'dn';
    var tp=$('tickerPrice');
    if(tp){tp.className='ticker-price '+tc;tp.textContent='$'+(q.price||0).toFixed(2);}
    var tg=$('tickerChg');
    if(tg){tg.className='ticker-chg '+tc;tg.textContent=(q.pct>=0?'+':'')+q.pct.toFixed(2)+'%';}
    var to=$('tickerOHLC');
    if(to){to.innerHTML='开 <b>'+q.open.toFixed(2)+'</b> 高 <b>'+q.high.toFixed(2)+'</b> 低 <b>'+q.low.toFixed(2)+'</b> 量 <b>'+q.vol+'</b>';}

    // Strategy position
    var sb=$('stratbar');
    if(sb){
      if(d.strat_pos){
        var sp=d.strat_pos;
        sb.style.display='';
        sb.innerHTML='<span class="stratbar-label">📌 策略持仓</span>'
          +'<span class="stratbar-dir '+(sp.dir_up?'call':'put')+'">'+esc(sp.dir)+'</span>'
          +'<span class="stratbar-info">'+esc(sp.price)+' ×'+sp.qty+' · '+esc(sp.reason)+'</span>'
          +'<span class="stratbar-pnl '+(sp.dir_up?'up':'dn')+'">'+esc(sp.pnl)+'</span>';
      } else {
        sb.style.display='none';
      }
    }

    // Account
    var dd=d.daily||{};
    var aa=dd.account||{};
    var pnlUp=dd.pnl>=0;
    var ap=$('acctPnl');
    if(ap){ap.className='big '+(pnlUp?'up':'dn');ap.textContent=dd.pnl_str;}
    var aps=$('acctPnlSub');
    if(aps){aps.textContent='今日盈亏';}
    $('acctCash')&&($('acctCash').textContent='$'+(aa.cash||0).toFixed(0));
    $('acctNet')&&($('acctNet').textContent='$'+(aa.net||0).toFixed(0));
    $('acctPower')&&($('acctPower').textContent='$'+(aa.power||0).toFixed(0));
    $('acctTrades')&&($('acctTrades').textContent=dd.count+' / '+dd.max);
    $('acctOC')&&($('acctOC').textContent=dd.open+' / '+dd.closed);
    $('acctHolding')&&($('acctHolding').textContent=dd.holding);
    $('acctBS')&&($('acctBS').textContent=(dd.lb_buy||0)+' / '+(dd.lb_sell||0));

    // Filters
    var ff=d.filters||{};
    if(ff.sma20){var fb=$('fSMA');if(fb)renderFilter(fb,ff.sma20.detail,ff.sma20.ok);}
    if(ff.sma50){var fb=$('fSMA50');if(fb)renderFilter(fb,ff.sma50.detail,ff.sma50.ok);}
    if(ff.vwap){var fb=$('fVWAP');if(fb)renderFilter(fb,ff.vwap.detail,ff.vwap.ok);}
    if(ff.macd){var fb=$('fMACD');if(fb)renderFilter(fb,ff.macd.detail,ff.macd.ok);}
    if(ff.price_pos){var fb=$('fPos');if(fb)renderFilter(fb,ff.price_pos.detail,ff.price_pos.ok);}
    if(ff.trend){var fb=$('fTrend');if(fb)renderFilter(fb,ff.trend.detail,ff.trend.ok);}
    if(ff.volume){var fb=$('fVol');if(fb)renderFilter(fb,ff.volume.detail,ff.volume.ok);}
    if(ff.momentum){var fb=$('fMom');if(fb)renderFilter(fb,ff.momentum.detail,ff.momentum.ok);}
    if(ff.body){var fb=$('fBody');if(fb)renderFilter(fb,ff.body.detail,ff.body.ok);}
    // Mode (regime)
    var md=$('modeTag');
    if(md){md.textContent=ff.mode||'--';}

    // Verdict
    var vd=$('verdict');
    if(vd){
      var allOk=ff.all_ok, hasDir=ff.dir;
      vd.className='verdict '+(allOk?'go':hasDir?'no':'');
      var vt=vd.querySelector('.vt');
      var vs=vd.querySelector('.vs');
      if(vt) vt.textContent=allOk?'PASS':hasDir?'BLOCK':'WAIT';
      if(vs) vs.textContent=allOk?'🎯 '+ff.dir+'信号通过':hasDir?'❌ '+ff.dir+'信号拒绝':'⏳ 等待信号';
    }

    // Params
    var pm=$('params');
    if(pm){
      var cfg=d.config||{};
      pm.innerHTML='目标 '+esc(ff.price||'--')+'<br>mode: '+esc(ff.mode||'--')+'<br>body ≥ '+((cfg.min_body||0)*100).toFixed(2)+'% · vol ≥ '+(cfg.vol_mult||0)+'×<br>lookback = '+(cfg.lookback||0)+' · SL '+(cfg.sl||'')+' · TP '+(cfg.tp||'')+'<br>'+(cfg.window||'')+' ET';
    }

    // Positions table
    var pt=$('posBody');
    if(pt) pt.innerHTML=renderPositions(d.positions);

    // Trades table
    var tt=$('tradeBody');
    if(tt) tt.innerHTML=renderTrades(d.trades);

    // Trades count
    var tc2=$('tradeCnt');
    if(tc2) tc2.textContent=(d.trades||[]).length+' 笔';

    // Logs
    var lg=$('logBox');
    if(lg){
      var wasAtBottom=lg.scrollTop+lg.clientHeight>=lg.scrollHeight-10;
      lg.innerHTML=renderLogs(d.logs);
      if(wasAtBottom) lg.scrollTop=lg.scrollHeight;
    }

    // Last update
    var lu=$('lastUpdate');
    if(lu) lu.textContent=new Date().toLocaleTimeString('zh-CN',{hour12:false});

  }).catch(function(){});
}

setInterval(refresh,10000);
refresh();

// Orders sync
function syncOrders(){
  var d=$('lbOrders');
  d.innerHTML='<table><tr><td colspan="8" class="empty">⏳ 同步中…</td></tr></table>';
  fetch('/api/longbridge_orders?token='+token).then(function(r){return r.json()}).then(function(x){
    if(x.error){d.innerHTML='<table><tr><td colspan="8" class="empty" style="color:var(--r)">'+esc(x.error)+'</td></tr></table>';return}
    if(!x.orders||!x.orders.length){d.innerHTML='<table><tr><td colspan="8" class="empty">暂无订单</td></tr></table>';return}
    var h='<table><tr><th>#</th><th>标的</th><th>方向</th><th>委托量</th><th>成交量</th><th>委托价</th><th>成交价</th><th>状态</th></tr>';
    var stMap={'Filled':'已成交','Canceled':'已取消','Pending':'待成交','Submitted':'已提交','Rejected':'已拒绝'};
    x.orders.forEach(function(o,i){
      var sc=o.side=='买入'?'var(--r)':'var(--g)';
      var stc=o.status=='Filled'?'var(--a)':o.status=='Canceled'?'var(--text-3)':'var(--cyan)';
      var stText=stMap[o.status]||o.status;
      h+='<tr>';
      h+='<td style="color:var(--text-3)">'+(i+1)+'</td>';
      h+='<td class="sym">'+esc(o.symbol)+'</td>';
      h+='<td style="color:'+sc+';font-weight:700">'+esc(o.side)+'</td>';
      h+='<td class="mono">'+o.quantity+'</td>';
      h+='<td class="mono">'+o.executed_qty+'</td>';
      h+='<td class="mono">'+(o.price>0?'$'+Number(o.price).toFixed(2):'—')+'</td>';
      h+='<td class="mono">'+(o.executed_price>0?'$'+o.executed_price.toFixed(2):'—')+'</td>';
      h+='<td style="color:'+stc+';font-weight:600">'+esc(stText)+'</td>';
      h+='</tr>';
    });
    h+='</table>';
    d.innerHTML=h;
  }).catch(function(){d.innerHTML='<table><tr><td colspan="8" class="empty" style="color:var(--r)">网络错误</td></tr></table>'});
}
window.onload=function(){setTimeout(syncOrders,600)};
</script>
</body>
</html>'''

  


@app.route('/')
def index():
    if engine:
        state = engine.get_state()
    else:
        state = {'connected': False, 'running': False, 'quote': {}, 'account': {},
                 'positions': [], 'strat_pos': None, 'signal': {}, 'filters': {}, 'trades': [],
                 'daily': {'open': 0, 'closed': 0, 'holding': 0, 'pnl': 0, 'pnl_str': '$0', 'count': 0, 'max': 8,
                           'account': {'net': 0, 'cash': 0, 'power': 0, 'currency': 'USD'}},
                 'filters': {'sma20': {'ok': None, 'val': '--', 'detail': '--'},
                             'volume': {'ok': None, 'val': '--', 'detail': '--'},
                             'momentum': {'ok': None, 'val': '--', 'detail': '--'},
                             'body': {'ok': None, 'val': '--', 'detail': '--'},
                             'dir': '', 'price': '--', 'all_ok': False},
                 'logs': [], 'config': {'strategy': 'v6全过滤', 'symbol': 'QQQ.US', 'window': '09:35-15:00',
                                        'vol_mult': 0.8, 'min_body': 0.0003, 'lookback': 5,
                                        'sl': '0.30%', 'tp': '0.40%'}}
    state['token'] = API_TOKEN
    return render_template_string(HTML, **state)


@app.route('/api/state')
def api_state():
    if engine:
        return jsonify(engine.get_state())
    return jsonify({'error': 'engine not ready'})


@app.route('/api/history')
def api_history():
    """历史记录查询 - 按日期"""
    date = request.args.get('date', '')
    if not engine:
        return jsonify({'error': 'engine not ready'})

    # 从日志文件读取历史交易
    import glob
    log_dir = os.path.join(_app_dir(), 'logs')
    trades = []
    daily_pnl = 0

    if os.path.exists(log_dir):
        # 匹配日期的日志文件
        pattern = os.path.join(log_dir, f'*{date}*.json')
        files = glob.glob(pattern)
        for f in files:
            try:
                with open(f, encoding='utf-8') as fh:
                    data = json.load(fh)
                    if isinstance(data, list):
                        trades.extend(data)
                    elif isinstance(data, dict):
                        trades.extend(data.get('trades', []))
                        daily_pnl += data.get('daily_pnl', 0)
            except:
                pass

        # 也尝试从csv日志读取
        csv_pattern = os.path.join(log_dir, f'*{date}*.csv')
        csv_files = glob.glob(csv_pattern)
        for f in csv_files:
            try:
                import csv
                with open(f, encoding='utf-8') as fh:
                    reader = csv.DictReader(fh)
                    for row in reader:
                        trades.append({
                            'time': row.get('time', ''),
                            'dir': row.get('dir', ''),
                            'price': row.get('price', row.get('ep', '')),
                            'qty': row.get('qty', '0'),
                            'pnl': row.get('pnl', ''),
                        })
            except:
                pass

    # 如果今天查今天的数据，直接从内存取
    from datetime import datetime
    today = datetime.now().strftime('%Y-%m-%d')
    if date == today and engine.trades_today:
        for t in engine.trades_today:
            trades.append({
                'time': t.get('et', ''),
                'dir': t.get('dir', ''),
                'price': str(t.get('ep', '')),
                'qty': t.get('qty', 0),
                'pnl': t.get('pnl', ''),
            })
        daily_pnl = engine.daily_pnl

    return jsonify({
        'date': date,
        'trades': trades,
        'daily_pnl': daily_pnl,
    })


@app.route('/api/longbridge_orders')
def api_longbridge_orders():
    """从本地文件读取长桥订单（由live_trader同步）"""
    try:
        script_dir = _app_dir()
        filepath = os.path.join(script_dir, 'longbridge_orders.json')
        if os.path.exists(filepath):
            with open(filepath, encoding='utf-8') as f:
                data = json.load(f)
            return jsonify(data)
        else:
            return jsonify({'orders': [], 'total': 0, 'buy_count': 0, 'sell_count': 0, 'error': '无订单文件'})
    except Exception as e:
        return jsonify({'error': str(e), 'orders': []})


def main():
    global engine
    import webbrowser
    from flask import Flask as _Flask, render_template_string as _rts, jsonify as _jf, request as _req

    # 替换代理为真实Flask app
    global app, render_template_string, jsonify, request
    render_template_string = _rts
    jsonify = _jf
    request = _req
    real_app = _Flask(__name__)
    # 应用代理收集的路由
    for rule, func, opts in app._routes:
        real_app.add_url_rule(rule, func.__name__, func, **opts)
    for func in app._before:
        real_app.before_request(func)
    app = real_app

    engine = Engine(CONFIG)

    def run_engine():
        engine.start()

    threading.Thread(target=run_engine, daemon=True).start()

    def run_flask():
        app.run(host='0.0.0.0', port=8080, debug=False, use_reloader=False)

    threading.Thread(target=run_flask, daemon=True).start()

    # 启动Gist同步线程
    threading.Thread(target=gist_sync_loop, daemon=True).start()

    time.sleep(1.5)
    webbrowser.open('http://127.0.0.1:8080')
    print("Browser opened: http://127.0.0.1:8080")
    print("Close this window or press Ctrl+C to stop.")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        engine.stop()
        print("Stopped.")


if __name__ == '__main__':
    main()
