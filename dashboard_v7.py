"""
v7 FastAPI WebSocket Dashboard
合并旧Flask dashboard的所有功能 + v7多引擎显示
"""
import asyncio
import json
import os
import sys
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
import uvicorn

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from signal_names import display_signal_name


app = FastAPI(title="QQQ 0DTE v7 Dashboard")


def _json_safe(value: Any):
    """Convert dashboard state into values FastAPI/WebSocket JSON can serialize."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    if hasattr(value, "value"):
        return _json_safe(value.value)
    return str(value)


class DashboardState:
    def __init__(self):
        self.signal_manager: Optional[Any] = None
        self.connected_clients: List[WebSocket] = []
        
        # 账户信息
        self.account_info: Dict = {}
        self.current_position: Optional[Dict] = None
        self.broker_positions: List[Dict] = []
        self.daily_pnl: float = 0.0
        self.daily_trades: int = 0
        self.trades_today: List[Dict] = []
        self.signal_probes: List[Dict] = []
        
        # 行情
        self.current_price: float = 0.0
        self.candle_count: int = 0
        
        # VIX
        self.vix_state: Dict = {}
        
        # 系统
        self.running: bool = False
        self.connected: bool = False
        self.events: List[Dict] = []
        self.filter_status: Dict = {}
        self.start_time: datetime = datetime.now()
        
    def to_dict(self) -> Dict:
        # 计算运行时间
        uptime = datetime.now() - self.start_time
        hours = int(uptime.total_seconds() // 3600)
        minutes = int((uptime.total_seconds() % 3600) // 60)
        uptime_str = f"{hours}h{minutes}m"
        
        # 引擎状态
        engine_states = []
        if self.signal_manager:
            engine_states = self.signal_manager.get_engine_states()
            engine_states = [
                {
                    **engine,
                    'raw_name': engine.get('name'),
                    'name': display_signal_name(engine.get('name')),
                }
                for engine in engine_states
            ]
            
        # 最新信号
        last_signal = None
        if self.signal_manager and self.signal_manager.last_signal:
            sig = self.signal_manager.last_signal
            last_signal = {
                'engine': display_signal_name(sig.engine),
                'raw_engine': sig.engine,
                'direction': sig.direction.value,
                'strength': sig.strength,
                'entry_price': sig.entry_price,
                'reason': sig.reason,
            }
            
        # 账户
        account = self.account_info
        net_assets = 0
        cash = 0
        buying_power = 0
        if isinstance(account, dict):
            net_assets = account.get('net_assets', 0)
            cash = account.get('cash', 0)
            buying_power = account.get('buying_power', 0)
            
        # 持仓
        positions = []
        if self.current_position:
            positions.append(self.current_position)
        positions.extend(self.broker_positions)
        
        # 交易记录 - 保持和旧dashboard一样的格式
        trades = []
        for t in self.trades_today:
            opt = t.get('opt_symbol', '')
            # 提取 HH:MM:SS 格式时间 - 优先用exit_time（平仓时间），其次entry_time
            raw_time = t.get('exit_time') or t.get('entry_time') or t.get('time', '')
            # 处理datetime对象或ISO字符串
            if hasattr(raw_time, 'strftime'):
                # datetime对象
                time_str = raw_time.strftime('%H:%M:%S')
            elif isinstance(raw_time, str) and 'T' in raw_time:
                # ISO datetime "2026-05-28T14:06:30-04:00" → 取时分秒
                time_part = raw_time.split('T')[1]
                time_str = time_part[:8]  # "14:06:30"
            else:
                time_str = '--:--:--'
                
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
            
        data = {
            'timestamp': datetime.now().isoformat(),
            'connected': self.connected,
            'running': self.running,
            'current_price': self.current_price,
            'candle_count': self.candle_count,
            'uptime': uptime_str,
            
            # 账户
            'account': {
                'net_assets': net_assets,
                'cash': cash,
                'buying_power': buying_power,
            },
            
            # 持仓
            'positions': positions,
            
            # 交易
            'trades': trades,
            'signal_probes': self.signal_probes[-50:],
            'daily_pnl': self.daily_pnl,
            'daily_trades': self.daily_trades,
            
            # 信号
            'signal': last_signal,
            
            # 引擎
            'engines': engine_states,
            
            # VIX
            'vix': self.vix_state,
            
            # 过滤器
            'filters': self.filter_status,
            
            # 事件
            'events': self.events[-30:],
        }
        return _json_safe(data)


state = DashboardState()
_broadcast_task = None
_server_started = False


async def _broadcast_loop():
    while True:
        try:
            await asyncio.sleep(0.5)
            if state.connected_clients:
                data = state.to_dict()
                disconnected = []
                for client in state.connected_clients:
                    try:
                        await client.send_json(data)
                    except Exception:
                        disconnected.append(client)
                for client in disconnected:
                    state.connected_clients.remove(client)
        except Exception:
            pass


@app.on_event("startup")
async def startup_event():
    global _broadcast_task
    _broadcast_task = asyncio.create_task(_broadcast_loop())


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    state.connected_clients.append(websocket)
    try:
        await websocket.send_json(state.to_dict())
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        state.connected_clients.remove(websocket)


@app.get("/api/state")
async def get_state():
    return state.to_dict()


@app.get("/")
async def root():
    return HTMLResponse(content=DASHBOARD_HTML, status_code=200)


# 外部调用接口
def set_signal_manager(manager: Any):
    state.signal_manager = manager

def update_account(info: Dict):
    state.account_info = info

def update_position(pos: Optional[Dict]):
    state.current_position = pos

def update_broker_positions(positions: List[Dict]):
    state.broker_positions = positions

def update_pnl(pnl: float, trades: int):
    state.daily_pnl = pnl
    state.daily_trades = trades

def update_trades(trades: List[Dict]):
    state.trades_today = trades

def update_signal_probes(probes: List[Dict]):
    state.signal_probes = probes or []

def add_trade(trade: Dict):
    state.trades_today.append(trade)
    if len(state.trades_today) > 100:
        state.trades_today = state.trades_today[-100:]

def update_vix(vix_state: Dict):
    state.vix_state = vix_state

def update_price(price: float):
    state.current_price = price

def update_candle_count(count: int):
    state.candle_count = count

def update_filter_status(filters: Dict):
    state.filter_status = filters

def add_event(msg: str, tag: str = 'info'):
    ts = datetime.now().strftime('%H:%M:%S')
    state.events.append({'time': ts, 'msg': msg, 'tag': tag})
    if len(state.events) > 100:
        state.events = state.events[-100:]

def set_running(running: bool):
    state.running = running

def set_connected(connected: bool):
    state.connected = connected


def run_dashboard(host: str = "0.0.0.0", port: int = 8080):
    import time
    import socket
    global _server_started

    if _server_started:
        print(f"📊 Dashboard已在当前进程启动，跳过重复启动: http://localhost:{port}")
        return
    _server_started = True
    
    # 等待端口释放（Windows Ctrl+C后端口可能还在TIME_WAIT）
    for attempt in range(15):
        try:
            # 尝试绑定端口
            test_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            test_sock.settimeout(1)
            test_sock.bind((host, port))
            test_sock.close()
            print(f"✅ 端口{port}可用，启动Dashboard...")
            break
        except OSError:
            if attempt < 14:
                print(f"⏳ 端口{port}占用，等待2秒... ({attempt+1}/15)")
                time.sleep(2)
            else:
                _server_started = False
                print(f"❌ 端口{port}持续占用，请手动关闭占用进程")
                return
    
    uvicorn.run(app, host=host, port=port, log_level="warning")


# Dashboard HTML
DASHBOARD_HTML = '''<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>QQQ 0DTE v7 Dashboard</title>
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
.grid-5{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin-bottom:12px}
.engine-card{position:relative}
.engine-card .priority{position:absolute;top:10px;right:10px;background:var(--surface-2);padding:2px 6px;border-radius:4px;font-size:10px;color:var(--text-2)}
.strength-bar{height:6px;background:var(--surface-2);border-radius:3px;margin-top:8px;overflow:hidden}
.strength-fill{height:100%;border-radius:3px;transition:width 0.3s}
.strength-high{background:var(--g)}.strength-mid{background:var(--cyan)}.strength-low{background:var(--r)}
@media(max-width:900px){.grid-4,.grid-5{grid-template-columns:repeat(2,1fr)}.grid-2,.signal-grid{grid-template-columns:1fr}}
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
      <div class="kv"><span class="kv-label">VIX</span><span class="kv-val" id="v-vix">--</span></div>
      <div class="kv"><span class="kv-label">VIX区间</span><span class="kv-val" id="v-vix-regime">--</span></div>
    </div>
    <div class="card"><div class="card-title">💰 资金</div>
      <div class="kv"><span class="kv-label">总资产</span><span class="kv-val" id="v-equity">--</span></div>
      <div class="kv"><span class="kv-label">现金</span><span class="kv-val" id="v-cash">--</span></div>
      <div class="kv"><span class="kv-label">购买力</span><span class="kv-val" id="v-power">--</span></div>
    </div>
    <div class="card"><div class="card-title">⚙️ 系统</div>
      <div class="kv"><span class="kv-label">运行时间</span><span class="kv-val" id="v-uptime">--</span></div>
      <div class="kv"><span class="kv-label">K线数</span><span class="kv-val" id="v-candles">--</span></div>
      <div class="kv"><span class="kv-label">仓位系数</span><span class="kv-val" id="v-pos-mult">--</span></div>
    </div>
  </div>

  <div class="signal-grid">
    <div class="card signal">
      <div class="card-title" style="justify-content:center;text-align:center">🎯 最新信号</div>
      <div class="signal-icon" id="sig-icon">⏳</div>
      <div class="signal-dir dim" id="sig-dir">无信号</div>
      <div class="signal-price" id="sig-price"></div>
      <div style="font-size:11px;color:var(--text-2)" id="sig-reason"></div>
      <div class="strength-bar"><div class="strength-fill" id="sig-strength"></div></div>
    </div>
    <div class="card filters">
      <div class="card-title">🔍 v7 引擎状态</div>
      <div id="engines"></div>
    </div>
  </div>

  <div class="grid-5" id="engines-grid">
    <!-- 引擎卡片 -->
  </div>

  <div class="grid-2">
    <div class="card"><div class="card-title">📋 当前持仓</div>
      <table><thead><tr><th>标的</th><th>方向</th><th>数量</th><th>成本</th></tr></thead>
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

let ws=null,reconnectTimer=null;
function connect(){
  const protocol=location.protocol==='https:'?'wss:':'ws:';
  ws=new WebSocket(`${protocol}//${location.host}/ws`);
  ws.onopen=()=>{
    $('s-conn').textContent='已连接';
    $('dot-conn').className='status-dot dot-green';
    if(reconnectTimer){clearTimeout(reconnectTimer);reconnectTimer=null}
  };
  ws.onmessage=e=>update(JSON.parse(e.data));
  ws.onclose=()=>{
    $('s-conn').textContent='断开';
    $('dot-conn').className='status-dot dot-red';
    reconnectTimer=setTimeout(connect,3000);
  };
  setInterval(()=>{if(ws&&ws.readyState===1)ws.send('ping')},30000);
}

function update(d){
  // 状态栏
  $('s-engine').textContent=d.running?'运行中':'已停止';
  $('dot-engine').className='status-dot '+(d.running?'dot-green':'dot-red');
  
  // 时钟
  $('clock').textContent=new Date().toLocaleTimeString('zh-CN',{hour12:false});
  
  // 当日概况
  const pnl=d.daily_pnl||0;
  $('v-pnl').textContent=fmt$(pnl);
  $('v-pnl').className='kv-val '+cls(pnl);
  $('v-trades').textContent=d.daily_trades||0;
  
  // 胜率
  const wins=d.trades.filter(t=>t.pnl_usd>0).length;
  const total=d.trades.length;
  $('v-wr').textContent=total>0?Math.round(wins/total*100)+'%':'--';
  $('v-hold').textContent=d.positions.length;
  
  // 行情
  $('v-qqq').textContent=d.current_price?'$'+d.current_price.toFixed(2):'--';
  $('v-vix').textContent=d.vix.vix?d.vix.vix.toFixed(1):'--';
  $('v-vix-regime').textContent=d.vix.regime||'--';
  
  // 资金
  $('v-equity').textContent=fmt$(d.account.net_assets);
  $('v-cash').textContent=fmt$(d.account.cash);
  $('v-power').textContent=fmt$(d.account.buying_power);
  
  // 系统
  $('v-uptime').textContent=d.uptime||'--';
  $('v-candles').textContent=d.candle_count||0;
  $('v-pos-mult').textContent=d.vix.position_mult?d.vix.position_mult.toFixed(1)+'x':'1.0x';
  
  // 信号
  if(d.signal){
    const sig=d.signal;
    $('sig-icon').textContent=sig.direction==='call'?'🟢':'🔴';
    $('sig-dir').textContent=sig.direction==='call'?'做多':'做空';
    $('sig-dir').className='signal-dir '+(sig.direction==='call'?'up':'down');
    $('sig-price').textContent='$'+sig.entry_price.toFixed(2);
    $('sig-reason').textContent=sig.engine+': '+sig.reason;
    
    const strength=sig.strength||0;
    const el=$('sig-strength');
    el.style.width=strength+'%';
    el.className='strength-fill '+(strength>70?'strength-high':strength>40?'strength-mid':'strength-low');
  }else{
    $('sig-icon').textContent='⏳';
    $('sig-dir').textContent='无信号';
    $('sig-dir').className='signal-dir dim';
    $('sig-price').textContent='';
    $('sig-reason').textContent='';
    $('sig-strength').style.width='0%';
  }
  
  // 引擎状态
  const enginesDiv=$('engines');
  enginesDiv.innerHTML=d.engines.map((e,i)=>`
    <div class="f-item">
      <div class="f-dot" style="background:${e.enabled?'var(--g)':'var(--r)'}"></div>
      <div class="f-name">#${i+1} ${e.name}</div>
      <div class="f-val">Bars: ${e.bars_count||0}</div>
    </div>
  `).join('');
  
  // 引擎卡片
  const grid=$('engines-grid');
  grid.innerHTML=d.engines.map((e,i)=>`
    <div class="card engine-card">
      <div class="priority">#${i+1}</div>
      <div class="card-title">${e.name}</div>
      <div style="font-size:12px;color:var(--text-2)">Bars: ${e.bars_count||0}</div>
      <div style="margin-top:8px;font-size:11px;color:var(--text-2)">${e.enabled?'✅ 启用':'❌ 禁用'}</div>
    </div>
  `).join('');
  
  // 持仓
  const posBody=$('tb-pos');
  posBody.innerHTML=d.positions.map(p=>`
    <tr>
      <td>${p.opt_symbol||p.symbol||'--'}</td>
      <td class="${p.dir==='call'?'t-up':'t-down'}">${p.dir==='call'?'做多':'做空'}</td>
      <td>${p.contracts||p.quantity||0}</td>
      <td>${p.entry_opt_price?'$'+p.entry_opt_price.toFixed(2):'--'}</td>
    </tr>
  `).join('')||'<tr><td colspan="4" class="dim">无持仓</td></tr>';
  
  // 交易记录
  const trdBody=$('tb-trd');
  trdBody.innerHTML=d.trades.map(t=>`
    <tr>
      <td>${t.time}</td>
      <td class="${t.dir_up?'t-up':'t-down'}">${t.dir}</td>
      <td>${t.opt||'--'}</td>
      <td>${t.ep}</td>
      <td>${t.exit_price}</td>
      <td>${t.qty}</td>
      <td class="${t.pnl_usd>=0?'t-up':'t-down'}">${t.pnl_usd>=0?'+':''}$${t.pnl_usd.toFixed(2)}</td>
    </tr>
  `).join('')||'<tr><td colspan="7" class="dim">无交易记录</td></tr>';

  const probesBody=$('tb-probes');
  const fmtPct=v=>{
    if(v===null||v===undefined||v==='')return '--';
    const n=Number(v);
    if(!Number.isFinite(n))return '--';
    return (n>=0?'+':'')+n.toFixed(2)+'%';
  };
  probesBody.innerHTML=(d.signal_probes||[]).slice(-20).reverse().map(p=>{
    const rawTime=p.time||p.entry_time||'';
    const time=rawTime.includes(' ')?rawTime.split(' ').pop().slice(0,8):String(rawTime).slice(0,8);
    const dirUp=p.dir==='call';
    const cell=v=>`<td class="${Number(v)>0?'t-up':Number(v)<0?'t-down':''}">${fmtPct(v)}</td>`;
    return `<tr>
      <td>${p.id||''}</td>
      <td>${time||'--:--:--'}</td>
      <td>${p.signal||p.regime||'QQQ_Breakout'}</td>
      <td class="${dirUp?'t-up':'t-down'}">${dirUp?'多':'空'}</td>
      <td>${p.entry_price?'$'+Number(p.entry_price).toFixed(2):'--'}</td>
      ${cell(p.m5_pct)}${cell(p.m10_pct)}${cell(p.m20_pct)}
    </tr>`;
  }).join('')||'<tr><td colspan="8" class="dim">暂无信号追踪</td></tr>';
  
  // 事件日志
  const logBox=$('log-box');
  logBox.innerHTML=(d.events||[]).map(e=>`
    <div class="log-line ${e.tag}">[${e.time}] ${e.msg}</div>
  `).join('');
  logBox.scrollTop=logBox.scrollHeight;
}

connect();
</script>
</body>
</html>'''


if __name__ == "__main__":
    run_dashboard()
