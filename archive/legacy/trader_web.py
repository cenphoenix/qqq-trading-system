#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
QQQ 0DTE 双向突破策略 - Web可视化版
Flask + HTML/CSS 卡片式仪表盘

注意：此文件仅用于 Web 仪表盘显示，实际交易由 live_trader.py 执行
"""
import os
import sys
import json
import time
import threading
import webbrowser
from datetime import datetime, timezone, timedelta

TZ_ET = __import__('zoneinfo').ZoneInfo("America/New_York")


def _app_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

# stdout兜底
if sys.stdout is None:
    sys.stdout = open(os.devnull, 'w', encoding='utf-8', errors='replace')
if sys.stderr is None:
    sys.stderr = open(os.devnull, 'w', encoding='utf-8', errors='replace')


# ============================================================
# 状态读取器（替代原来的 Engine 类 - 仅读取状态）
# ============================================================
class StateReader:
    """只读状态读取器，从 state.json 读取"""

    def __init__(self):
        self.logs = []
        self.running = True

    def _log(self, msg):
        ts = datetime.now().strftime('%H:%M:%S')
        self.logs.append({'time': ts, 'msg': msg})
        if len(self.logs) > 200:
            self.logs = self.logs[-200:]

    def get_state(self):
        shared = {}
        try:
            state_file = os.path.join(_app_dir(), 'state.json')
            if os.path.exists(state_file):
                with open(state_file, encoding='utf-8') as f:
                    shared = json.load(f)
                pass
        except Exception as e:
            pass

        positions = []
        try:
            pos_file = os.path.join(_app_dir(), 'position_snapshot.json')
            if os.path.exists(pos_file):
                with open(pos_file, encoding='utf-8') as f:
                    raw = json.load(f)
                if isinstance(raw, list):
                    positions = raw
                elif isinstance(raw, dict):
                    positions = raw.get('positions', [raw])
        except Exception:
            pass

        lb_orders = []
        lb_today_pnl = 0
        try:
            lb_file = os.path.join(_app_dir(), 'longbridge_orders.json')
            if os.path.exists(lb_file):
                with open(lb_file, encoding='utf-8') as f:
                    lb_data = json.load(f)
                lb_orders = lb_data.get('orders', [])

                order_map = {}
                for o in lb_orders:
                    symbol = o.get('symbol', '')
                    side = o.get('side', '')
                    exec_qty = float(o.get('executed_qty', 0) or 0)
                    exec_price = float(o.get('executed_price', 0) or 0)
                    if exec_qty > 0 and exec_price > 0:
                        if symbol not in order_map:
                            order_map[symbol] = {'buys': [], 'sells': []}
                        if side == '买入':
                            order_map[symbol]['buys'].append({'qty': exec_qty, 'price': exec_price})
                        else:
                            order_map[symbol]['sells'].append({'qty': exec_qty, 'price': exec_price})

                for symbol, ords in order_map.items():
                    buys, sells = ords['buys'], ords['sells']
                    if sells:
                        total_buy_qty = sum(b['qty'] for b in buys)
                        total_buy_cost = sum(b['qty'] * b['price'] for b in buys)
                        total_sell_qty = sum(s['qty'] for s in sells)
                        total_sell_revenue = sum(s['qty'] * s['price'] for s in sells)
                        if total_buy_qty > 0 and total_sell_qty > 0:
                            pnl = (total_sell_revenue / total_sell_qty - total_buy_cost / total_buy_qty) * min(total_buy_qty, total_sell_qty) * 100
                            lb_today_pnl += pnl
        except Exception:
            pass

        shared_trades = shared.get('trades_today', [])
        # 如果 state.json 里没有 trades_today，尝试从 records/ 目录恢复
        if not shared_trades:
            try:
                from datetime import datetime
                from zoneinfo import ZoneInfo
                today_et = datetime.now(ZoneInfo("America/New_York")).strftime('%Y-%m-%d')
                rec_dir = _app_dir()
                rec_file = os.path.join(rec_dir, 'records', f'{today_et}.json')
                if os.path.exists(rec_file):
                    with open(rec_file, encoding='utf-8') as f:
                        rec_data = json.load(f)
                    shared_trades = rec_data.get('trades', [])
            except Exception:
                pass

        trades = []
        for t in shared_trades:
            opt = t.get('opt_symbol', '')
            # 提取 HH:MM:SS 格式时间
            raw_time = t.get('time', t.get('entry_time', ''))
            if isinstance(raw_time, str) and 'T' in raw_time:
                # ISO datetime → 取时分秒
                time_part = raw_time.split('T')[1].split('.')[0] if '.' in raw_time.split('T')[-1] else raw_time.split('T')[-1]
                time_str = time_part[:8]
            else:
                # 日期字符串 YYYY-MM-DD 直接显示，时间字符串 HH:MM:SS 截取前8位
                raw = str(raw_time)
                time_str = raw if '-' in raw else (raw[:8] if len(raw) >= 8 else raw)

            trades.append({
                'id': len(trades) + 1,
                'time': time_str or '--:--:--',
                'dir': '做多' if t.get('dir') == 'call' else '做空' if t.get('dir') == 'put' else str(t.get('dir', '--')),
                'dir_up': t.get('dir') == 'call',
                'ep': f"${t.get('entry_opt_price', 0):.2f}" if t.get('entry_opt_price', 0) > 0 else f"${t.get('entry_price', 0):.2f}" if t.get('entry_price', 0) > 0 else '--',
                'qty': t.get('contracts', t.get('qty', 0)),
                'opt': opt,
                'active': False,
                'pnl_pct': round(t.get('pnl_pct', 0), 2),
                'pnl_usd': round(t.get('pnl_usd', 0), 2),
                'exit_reason': t.get('exit_reason', ''),
                'exit_price': f"${t.get('exit_price', 0):.2f}" if t.get('exit_price', 0) > 0 else '--',
                'result': t.get('result', '') or ('win' if t.get('pnl_pct', 0) > 0 else 'lose' if t.get('pnl_pct', 0) < 0 else ''),
            })

        signal_probes_raw = shared.get('signal_probes', [])
        if not signal_probes_raw:
            try:
                from datetime import datetime
                from zoneinfo import ZoneInfo
                today_et = datetime.now(ZoneInfo("America/New_York")).strftime('%Y-%m-%d')
                probe_file = os.path.join(_app_dir(), 'records', f'signal_probes_{today_et}.json')
                if os.path.exists(probe_file):
                    with open(probe_file, encoding='utf-8') as f:
                        probe_data = json.load(f)
                    signal_probes_raw = probe_data.get('probes', [])
            except Exception:
                pass

        signal_probes = []
        for p in signal_probes_raw:
            raw_time = str(p.get('time') or p.get('entry_time') or '')
            if 'T' in raw_time:
                time_str = raw_time.split('T')[-1].split('.')[0][:8]
            elif ' ' in raw_time:
                time_str = raw_time.split(' ')[-1][:8]
            else:
                time_str = raw_time[:8] if raw_time else '--:--:--'
            signal_probes.append({
                'id': p.get('id', len(signal_probes) + 1),
                'time': time_str,
                'signal': p.get('signal') or p.get('regime') or 'QQQ_Breakout',
                'dir': '多' if p.get('dir') == 'call' else '空' if p.get('dir') == 'put' else str(p.get('dir', '--')),
                'dir_up': p.get('dir') == 'call',
                'entry_price': p.get('entry_price', 0),
                'm5_pct': p.get('m5_pct'),
                'm10_pct': p.get('m10_pct'),
                'm20_pct': p.get('m20_pct'),
                'reason': p.get('reason', ''),
            })

        # 优先显示真实持仓（从长桥 API 拉取）
        positions = []
        broker_positions = shared.get('broker_positions', [])
        
        # 🔧 如果 shared 中没有（可能是旧版本 state.json），直接从 state.json 读取
        if not broker_positions:
            try:
                with open(os.path.join(_app_dir(), 'state.json'), encoding='utf-8') as f:
                    state_data = json.load(f)
                    broker_positions = state_data.get('broker_positions', [])
            except Exception:
                pass
        
        if broker_positions:
            for bp in broker_positions:
                positions.append({
                    'sym': bp.get('symbol', '--'),
                    'qty': bp.get('qty', 0),
                    'cost': f"${bp.get('cost', 0):.4f}" if bp.get('cost', 0) > 0 else '--',
                    'cur': '--',
                    'pnl': 0,
                    'channel': bp.get('channel', ''),
                })

        cur_signal = shared.get('current_signal')
        sig_dir = ''
        sig_up = None
        sig_price = '--'
        if cur_signal:
            sig_dir = '🟢做多' if cur_signal.get('dir') == 'call' else '🔴做空'
            sig_up = cur_signal.get('dir') == 'call'
            sig_price = f"${cur_signal.get('price', 0):.2f}"

        filters = shared.get('filter_status', {})
        if not isinstance(filters, dict) or 'sma20' not in filters:
            filters = {'sma20': {'ok': None, 'val': '--', 'detail': '--'},
                       'volume': {'ok': None, 'val': '--', 'detail': '--'},
                       'momentum': {'ok': None, 'val': '--', 'detail': '--'},
                       'body': {'ok': None, 'val': '--', 'detail': '--'}}
        if cur_signal:
            filters['dir'] = '做多' if cur_signal.get('dir') == 'call' else '做空'

        account = shared.get('account', {})
        net_assets = 0
        cash_val = 0
        buying_power = 0
        
        # 兼容两种结构：
        # 1. 扁平结构（新）: {net_assets: x, cash: y, buying_power: z}  # 已是 USD 总值
        # 2. 多币种结构（旧）: {"HKD": {net_assets: ...}, "USD": {...}}
        if isinstance(account, dict):
            # 检测是否为扁平结构（有 net_assets/cash/buying_power 键且值为数字）
            if all(k in account and isinstance(account[k], (int, float)) for k in ['net_assets', 'cash', 'buying_power']):
                # 扁平结构：直接使用
                net_assets = account.get('net_assets', 0)
                cash_val = account.get('cash', 0)
                buying_power = account.get('buying_power', 0)
            else:
                # 多币种结构：遍历合并
                for cur, info in account.items():
                    if isinstance(info, dict):
                        net_assets += info.get('net_assets', 0)
                        cash_val += info.get('total_cash', 0)
                        # 购买力取各币种净值×2的最大值（模拟盘通常无杠杆，取最大即可）
                        buying_power = max(buying_power, info.get('net_assets', 0) * 2)
                    else:
                        # 扁平结构：info 是数值，根据 cur 字段累加到对应变量
                        val = float(info) if info else 0
                        if cur == 'net_assets':
                            net_assets += val
                        elif cur == 'cash':
                            cash_val += val
                        elif cur == 'buying_power':
                            buying_power = max(buying_power, val)  # 取最大

        # 运行时间：从 events 里第一条工程事件算起
        uptime_str = '--'
        events = shared.get('events', [])
        if events:
            try:
                first_event = events[0]
                first_ts = first_event.get('time', '')
                updated_ts = shared.get('updated', '')
                if first_ts and updated_ts:
                    # 简单按时间字符串差值计算
                    def _ts_to_sec(ts):
                        parts = ts.split(':')
                        if len(parts) == 3:
                            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
                        return 0
                    diff = abs(_ts_to_sec(updated_ts) - _ts_to_sec(first_ts))
                    uptime_str = f"{diff // 3600}h {diff % 3600 // 60}m {diff % 60}s"
            except:
                pass

        # 账户资金：从长桥拉取的按币种分账户数据（HKD/USD/CNY）
        # net_assets/cash/buying_power 按币种独立，前端可自行加总为"总净值"
        account_data = {}
        for cur, info in account.items():
            account_data[cur] = info

        broker_positions_raw = shared.get('broker_positions', [])
        broker_positions_list = broker_positions_raw if isinstance(broker_positions_raw, list) else []
        
        return {
            'connected': shared.get('connected', False),
            'running': shared.get('running', False),
            'current_price': shared.get('current_price', 0),
            'quote': {},
            'account': {
                'currencies': account_data,
                'net_assets': net_assets,
                'cash': cash_val,
                'buying_power': buying_power,
            },
            'positions': positions,
            'broker_positions': broker_positions_list,
            'strat_pos': None,
            'signal': {
                'dir': sig_dir or '无信号',
                'up': sig_up,
                'price': sig_price,
                'reason': cur_signal.get('reason', '--') if cur_signal else '--',
            },
            'filters': filters,
            'trades': trades,
            'signal_probes': signal_probes,
            'lb_orders': lb_orders,
            'daily_pnl': shared.get('daily_pnl', 0),
            'uptime': uptime_str,
            'updated': shared.get('updated', '--'),  # 最近更新时间
            'candle_count': shared.get('candle_count', 0),  # K线数
            'daily': {
                'open': len(positions),
                'closed': len([t for t in trades if not t.get('active')]),
                'holding': len(positions),
                'pnl': shared.get('daily_pnl', 0) + lb_today_pnl,
                'pnl_str': f"${shared.get('daily_pnl', 0) + lb_today_pnl:+,.2f}",
                'count': len(trades),
                'max': 999,
            },
            'today': {
                'open': len(positions),
                'closed': len([t for t in trades if not t.get('active')]),
                'holding': len(positions),
                'pnl': shared.get('daily_pnl', 0),
                'pnl_str': f"${shared.get('daily_pnl', 0):+,.2f}",
                'count': len(lb_orders) if lb_orders else len(trades),
                'max': 999,
            },
            'daily_history': [],
            'events': shared.get('events', []),
            'filter_status': shared.get('filter_status', {}),
            'logs': self.logs[-30:],
        }


# ============================================================
# Flask 应用
# ============================================================
from flask import Flask, jsonify, request

app = Flask(__name__)
state_reader = StateReader()
API_TOKEN = os.environ.get('API_TOKEN', 'qqq_trading_2026')


@app.before_request
def check_auth():
    """本地使用，无需鉴权"""
    pass


HTML = '''<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>天才浚浚交易所</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Orbitron:wght@400;500;600;700;800;900&family=Rajdhani:wght@400;500;600;700&family=Share+Tech+Mono&display=swap');
*,*::before,*::after{margin:0;padding:0;box-sizing:border-box}
:root{
  --bg:#0a0e1a;--surface:rgba(12,18,40,.85);--surface-2:rgba(18,26,56,.9);
  --cyan:#00f0ff;--cyan-dim:rgba(0,240,255,.08);--cyan-border:rgba(0,240,255,.20);
  --cyan-glow:rgba(0,240,255,.35);--blue:#4d7cff;--purple:#a855f7;
  --magenta:#ff2d95;--r:#ff3b5c;--r-dim:rgba(255,59,92,.10);
  --g:#00ff88;--g-dim:rgba(0,255,136,.08);--text:#e0e8ff;--text-2:rgba(200,210,240,.6);
  --border:rgba(0,240,255,.08);--border-h:rgba(0,240,255,.18);
  --mono:'Share Tech Mono',monospace;--sans:'Rajdhani',system-ui,sans-serif;
  --display:'Orbitron',sans-serif;
}
html{font-size:16px}
body{font-family:var(--sans);background:var(--bg);color:var(--text);min-height:100vh;-webkit-font-smoothing:antialiased}
.wrap{max-width:1480px;margin:0 auto;padding:0 24px 60px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:16px 20px;backdrop-filter:blur(10px)}
.card-title{font-size:14px;font-weight:600;color:var(--cyan);text-transform:uppercase;letter-spacing:1px;margin-bottom:12px}
.card-title::before{content:'';display:inline-block;width:3px;height:12px;background:var(--cyan);margin-right:8px;border-radius:2px}
.kv{display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--border)}
.kv:last-child{border:none}
.kv-label{color:var(--text-2);font-size:13px}
.kv-val{font-family:var(--mono);font-size:14px;font-weight:600}
.up{color:var(--g)}.down{color:var(--r)}.dim{color:var(--text-2)}.info{color:var(--cyan)}
.signal-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin:16px 0}
.signal{text-align:center;padding:20px;border:1px solid var(--cyan-border);border-radius:12px;background:var(--cyan-dim)}
.signal-icon{font-size:48px;margin-bottom:8px}
.signal-dir{font-size:18px;font-weight:700}
.signal-price{font-family:var(--mono);font-size:16px;margin:8px 0}
.filters{min-height:120px}
.f-item{display:flex;align-items:center;gap:10px;padding:6px 0}
.f-dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
.f-name{font-size:12px;color:var(--text-2);width:60px}
.f-val{font-family:var(--mono);font-size:12px;flex:1}
.f-det{font-size:10px;color:var(--text-2);text-align:right}
table{width:100%;border-collapse:collapse;font-size:12px}
th{text-align:left;color:var(--text-2);font-weight:600;padding:8px 12px;border-bottom:1px solid var(--border);font-size:11px;text-transform:uppercase}
td{padding:8px 12px;border-bottom:1px solid var(--border);font-family:var(--mono);font-size:12px}
tr:hover{background:var(--surface-2)}
.t-up{color:var(--g)}.t-down{color:var(--r)}
.log-box{background:var(--surface-2);border-radius:8px;padding:12px;max-height:200px;overflow-y:auto;font-family:var(--mono);font-size:12px;line-height:1.8}
.log-line{white-space:nowrap}
.log-line.sig{color:var(--g)}.log-line.err{color:var(--r)}.log-line.trade{color:var(--cyan)}.log-line.info{color:var(--text-2)}
.status-bar{display:flex;gap:20px;padding:12px 16px;background:var(--surface);border-radius:12px;margin-bottom:16px;font-size:13px}
.status-dot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:6px}
.dot-green{background:var(--g)}.dot-red{background:var(--r)}.dot-yellow{background:var(--cyan)}.dot-gray{background:var(--text-2)}
.grid-4{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:12px}
.grid-2{display:grid;grid-template-columns:2fr 3fr;gap:12px;margin-bottom:12px}
@media(max-width:900px){.grid-4{grid-template-columns:repeat(2,1fr)}.grid-2,.signal-grid{grid-template-columns:1fr}}
</style>
</head>
<body>
<div class="wrap">
  <div class="status-bar">
    <span><span class="status-dot dot-gray" id="dot-engine"></span>引擎: <b id="s-engine">--</b></span>
    <span><span class="status-dot dot-gray" id="dot-conn"></span>连接: <b id="s-conn">--</b></span>
    <span style="flex:1"></span>
    <span id="clock" style="font-family:var(--mono)"></span>
  </div>

  <div class="grid-4">
    <div class="card"><div class="card-title">📊 当日概况</div>
      <div class="kv"><span class="kv-label">今日盈亏</span><span class="kv-val" id="v-pnl">--</span></div>
      <div class="kv"><span class="kv-label">交易次数</span><span class="kv-val" id="v-trades">--</span></div>
      <div class="kv"><span class="kv-label">胜率</span><span class="kv-val" id="v-wr">--</span></div>
      <div class="kv"><span class="kv-label">持仓</span><span class="kv-val" id="v-hold">--</span></div>
    </div>
    <div class="card"><div class="card-title">📈 行情</div>
      <div class="kv"><span class="kv-label">QQQ</span><span class="kv-val" id="v-qqq">--</span></div>
      <div class="kv"><span class="kv-label">涨跌</span><span class="kv-val" id="v-chg">--</span></div>
      <div class="kv"><span class="kv-label">成交量</span><span class="kv-val" id="v-vol">--</span></div>
    </div>
    <div class="card"><div class="card-title">💰 资金</div>
      <div class="kv"><span class="kv-label">总资产</span><span class="kv-val" id="v-equity">--</span></div>
      <div class="kv"><span class="kv-label">现金</span><span class="kv-val" id="v-cash">--</span></div>
      <div class="kv"><span class="kv-label">购买力</span><span class="kv-val" id="v-power">--</span></div>
    </div>
    <div class="card"><div class="card-title">⚙️ 系统</div>
      <div class="kv"><span class="kv-label">运行时间</span><span class="kv-val" id="v-uptime">--</span></div>
      <div class="kv"><span class="kv-label">K线数</span><span class="kv-val" id="v-candles">--</span></div>
      <div class="kv"><span class="kv-label">更新</span><span class="kv-val dim" id="v-updated">--</span></div>
    </div>
  </div>

  <div class="signal-grid">
    <div class="card signal">
      <div class="card-title" style="justify-content:center;text-align:center">🎯 信号</div>
      <div class="signal-icon" id="sig-icon">⏳</div>
      <div class="signal-dir dim" id="sig-dir">无信号</div>
      <div class="signal-price" id="sig-price"></div>
      <div style="font-size:11px;color:var(--text-2)" id="sig-reason"></div>
    </div>
    <div class="card filters">
      <div class="card-title">🔍 过滤器</div>
      <div id="filters"></div>
    </div>
  </div>

  <div class="grid-2">
    <div class="card"><div class="card-title">📋 当前持仓</div>
      <table><thead><tr><th>标的</th><th>数量</th><th>成本</th></tr></thead>
      <tbody id="tb-pos"></tbody></table>
    </div>

    <div class="card"><div class="card-title">📝 交易记录</div>
      <table><thead><tr><th>时间</th><th>方向</th><th>期权</th><th>开仓</th><th>平仓</th><th>数量</th><th>盈亏</th></tr></thead>
      <tbody id="tb-trd"></tbody></table>
    </div>
  </div>

  <div class="card" style="margin-bottom:12px"><div class="card-title">💹 信号后5/10/20根K线</div>
    <table><thead><tr><th>#</th><th>时间</th><th>信号</th><th>方向</th><th>入场价</th><th>+5根</th><th>+10根</th><th>+20根</th></tr></thead>
    <tbody id="tb-probes"></tbody></table>
  </div>

  <div class="card"><div class="card-title">📋 实时事件</div><div class="log-box" id="log-box"></div></div>
</div>

<script>
const $=id=>document.getElementById(id);
function fmt$(v){return v?'$'+Number(v).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2}):'--'}
function cls(v){return v>=0?'up':'down'}

function render(d){
  $('dot-engine').className='status-dot '+(d.running?'dot-green':'dot-red');
  $('s-engine').textContent=d.running?'运行中':'已停止';
  $('dot-conn').className='status-dot '+(d.connected?'dot-green':'dot-yellow');
  $('s-conn').textContent=d.connected?'已连接':'未连接';
  $('v-updated').textContent=d.updated||'--';

  const pnl=d.daily_pnl||d.today?.pnl||0;
  $('v-pnl').textContent='$'+pnl.toLocaleString('en-US',{minimumFractionDigits:2,signDisplay:'always'});
  $('v-pnl').className='kv-val '+(pnl>=0?'up':'down');
  const trades=d.trades||[];
  $('v-trades').textContent=trades.length;
  const closed=trades.filter(t=>t.result);
  const wins=closed.filter(t=>t.result=='win').length;
  const wr=closed.length?Math.round(wins/closed.length*100):0;
  $('v-wr').textContent=wr+'%';
  $('v-wr').className='kv-val '+(wr>=50?'up':'down');
  $('v-hold').textContent=(d.today?.holding||0);

  const sig=d.signal||{};
  if(sig.dir&&sig.dir!='无信号'){
    const isCall=sig.dir.includes('做多');
    $('sig-icon').textContent=isCall?'🟢':'🔴';
    $('sig-dir').textContent=sig.dir;
    $('sig-dir').className='signal-dir '+(isCall?'up':'down');
    $('sig-price').textContent=sig.price||'';
    $('sig-reason').textContent=sig.reason||'';
  }else{
    $('sig-icon').textContent='⏳';
    $('sig-dir').textContent='无信号';
    $('sig-dir').className='signal-dir dim';
    $('sig-price').textContent='';
    $('sig-reason').textContent='';
  }

  // 💰 Account data (后端已展平为 {net_assets, cash, buying_power})
  const acct=d.account||{};
  $('v-equity').textContent=acct.net_assets?'$'+Number(acct.net_assets).toLocaleString('en-US',{minimumFractionDigits:2}):'--';
  $('v-cash').textContent=acct.cash?'$'+Number(acct.cash).toLocaleString('en-US',{minimumFractionDigits:2}):'--';
  $('v-power').textContent=acct.buying_power?'$'+Number(acct.buying_power).toLocaleString('en-US',{minimumFractionDigits:2}):'--';

  // ⏱️ Uptime
  if(d.uptime)$('v-uptime').textContent=d.uptime;

  // 📊 QQQ price
  if(d.current_price&&d.current_price>0)$('v-qqq').textContent='$'+d.current_price.toFixed(2);
  if(d.candle_count)$('v-candles').textContent=d.candle_count;

  const fs=d.filters||{};
  const fmap=[['sma20','SMA20'],['volume','量能'],['momentum','动量'],['body','K线实体']];
  let fhtml='';
  fmap.forEach(([k,label])=>{
    const f=fs[k]||{};
    const ok=f.ok;
    const dotColor=ok===true?'var(--g)':ok===false?'var(--r)':'var(--text-2)';
    const valColor=ok===true?'up':ok===false?'down':'dim';
    fhtml+=`<div class="f-item"><span class="f-dot" style="background:${dotColor}"></span><span class="f-name">${label}</span><span class="f-val ${valColor}">${f.val||'--'}</span><span class="f-det">${f.detail||''}</span></div>`;
  });
  $('filters').innerHTML=fhtml||'<div style="color:var(--text-2);text-align:center;padding:20px">无过滤数据</div>';

  // 📋 Positions table
  let phtml='';
  const bp=Array.isArray(d.broker_positions)?d.broker_positions:[];
  if(bp.length){
    bp.forEach(p=>{
      const sym=p.symbol||'--';
      const qty=p.qty||0;
      const cost=p.cost||0;
      phtml+=`<tr><td>${sym}</td><td>${qty}</td><td>${cost>0?'$'+cost.toFixed(2):'--'}</td></tr>`;
    });
  }else{
    phtml='<tr><td colspan="3" style="text-align:center;color:var(--text-2)">无持仓</td></tr>';
  }
  $('tb-pos').innerHTML=phtml;

  let thtml='';
  trades.slice(-15).reverse().forEach(t=>{
    const pnl=t.pnl_usd||0;
    const dirClass=(t.dir=='做多')?'t-up':'t-down';
    thtml+=`<tr><td>${t.time||'--:--:--'}</td><td class="${dirClass}">${t.dir||'--'}</td><td>${t.opt||'--'}</td><td>${t.ep||'--'}</td><td>${t.exit_price||'--'}</td><td>${t.qty||0}</td><td class="${pnl>0?'t-up':pnl<0?'t-down':''}">${pnl?'$'+pnl.toLocaleString('en-US',{signDisplay:'always',minimumFractionDigits:2}):'--'}</td></tr>`;
  });
  $('tb-trd').innerHTML=thtml||'<tr><td colspan="7" style="text-align:center;color:var(--text-2)">无交易记录</td></tr>';

  let probeHtml='';
  const probes=Array.isArray(d.signal_probes)?d.signal_probes:[];
  const fmtPct=v=>{
    if(v===null||v===undefined||v==='')return '--';
    const n=Number(v);
    if(!Number.isFinite(n))return '--';
    return (n>=0?'+':'')+n.toFixed(2)+'%';
  };
  probes.slice(-20).reverse().forEach(p=>{
    const cell=v=>`<td class="${Number(v)>0?'t-up':Number(v)<0?'t-down':''}">${fmtPct(v)}</td>`;
    probeHtml+=`<tr><td>${p.id||''}</td><td>${p.time||'--:--:--'}</td><td>${p.signal||'--'}</td><td class="${p.dir_up?'t-up':'t-down'}">${p.dir||'--'}</td><td>${p.entry_price?'$'+Number(p.entry_price).toFixed(2):'--'}</td>${cell(p.m5_pct)}${cell(p.m10_pct)}${cell(p.m20_pct)}</tr>`;
  });
  $('tb-probes').innerHTML=probeHtml||'<tr><td colspan="8" style="text-align:center;color:var(--text-2)">暂无信号追踪</td></tr>';

  // 实时事件（去重：Set保存所有已显示过的key）
  const seenKeys=$('log-box').__seen||new Set();
  $('log-box').__seen=seenKeys;
  (d.events||[]).forEach(e=>{
    const key=e.time+'|'+e.msg;
    if(!seenKeys.has(key)){
      seenKeys.add(key);
      const tag=e.tag==='signal'?'sig':e.tag==='error'?'err':e.tag==='trade'?'trade':'info';
      addLog(e.msg,tag);
    }
  });
}

function addLog(msg,tag){
  const box=$('log-box');
  const now=new Date().toTimeString().slice(0,8);
  const div=document.createElement('div');
  div.className='log-line '+tag;
  div.textContent='['+now+'] '+msg;
  box.appendChild(div);
  if(box.children.length>200)box.removeChild(box.firstChild);
  box.scrollTop=box.scrollHeight;
}

async function poll(){
  try{
    const r=await fetch('/api/state');
    if(r.ok)render(await r.json());
  }catch(e){}
}

$('clock').textContent=new Date().toLocaleTimeString('zh-CN');
setInterval(()=>{$('clock').textContent=new Date().toLocaleTimeString('zh-CN')},1000);
poll();setInterval(poll,5000);
</script>
</body>
</html>'''


@app.route('/')
def index():
    return HTML


@app.route('/api/state')
def api_state():
    return jsonify(state_reader.get_state())


def start_web(port=8080):
    """供 run_web.py 调用：启动 Flask Web 仪表盘（阻塞式）"""
    # 关闭 Werkzeug HTTP 访问日志（每 5 秒刷一次太吵）
    import logging
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.WARNING)

    print(f"🌐 Web仪表盘启动于 http://0.0.0.0:{port}")
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)


def main():
    """独立运行入口"""
    print("🚀 Web仪表盘启动 (仅显示模式，交易由 live_trader.py 执行)")
    threading.Thread(target=lambda: app.run(host='0.0.0.0', port=8080, debug=False, use_reloader=False), daemon=True).start()
    time.sleep(1)
    webbrowser.open('http://127.0.0.1:8080')
    print("Browser opened: http://127.0.0.1:8080")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("Stopped.")


if __name__ == '__main__':
    main()
