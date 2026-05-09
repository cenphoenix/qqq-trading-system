"""
热血青年交易所 - 零依赖Web仪表盘
用 Python 内置 http.server，不需要 Flask
启动后自动打开浏览器
"""
import os
import sys
import json
import time
import threading
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from datetime import datetime

BASE_DIR = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent

HTML = r'''<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>热血青年交易所</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{
  --bg:#0c0f1a;--surface:#161b2e;--surface2:#1a1f35;--border:#252a40;
  --fg:#e8eaf0;--fg2:#8b90a5;--fg3:#555a70;
  --green:#00d68f;--green-bg:rgba(0,214,143,.08);
  --red:#ff4d6a;--red-bg:rgba(255,77,106,.08);
  --blue:#5b8def;--cyan:#00d2d3;--purple:#a55eea;--orange:#ffa502;
  --radius:12px;
}
body{background:var(--bg);color:var(--fg);font:14px/1.5 'Segoe UI','Microsoft YaHei',system-ui,sans-serif;min-height:100vh}
.header{background:linear-gradient(135deg,#5b8def 0%,#a55eea 100%);padding:14px 24px;display:flex;align-items:center;justify-content:space-between}
.header h1{font-size:18px;font-weight:700;color:#fff}
.header small{color:rgba(255,255,255,.7);font-size:12px}
.container{max-width:1280px;margin:0 auto;padding:12px}
.grid4{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:10px}
.grid2{display:grid;grid-template-columns:2fr 3fr;gap:10px;margin-bottom:10px}
.grid2e{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:14px 16px}
.card-title{font-size:12px;color:var(--fg2);font-weight:600;text-transform:uppercase;letter-spacing:.5px;margin-bottom:10px;display:flex;align-items:center;gap:6px}
.card-title::before{content:'';display:inline-block;width:3px;height:14px;border-radius:2px;background:var(--blue)}
.kv{display:flex;justify-content:space-between;align-items:center;padding:3px 0}
.kv-label{color:var(--fg2);font-size:12px}
.kv-val{font-family:'Consolas','SFMono-Regular',monospace;font-size:14px;font-weight:600}
.up{color:var(--green)}.down{color:var(--red)}.dim{color:var(--fg3)}.info{color:var(--cyan)}

/* 信号区 */
.signal{text-align:center;padding:16px}
.signal-icon{font-size:40px;margin-bottom:4px}
.signal-dir{font-size:20px;font-weight:700}
.signal-price{font-family:monospace;font-size:15px;color:var(--fg);margin:4px 0}
.signal-reason{font-size:11px;color:var(--fg3);max-width:220px;margin:0 auto}

/* 过滤器 */
.filter-item{display:flex;align-items:center;gap:8px;padding:5px 0;border-bottom:1px solid var(--border)}
.filter-item:last-child{border:none}
.filter-dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
.filter-name{font-size:12px;color:var(--fg2);width:60px}
.filter-val{font-family:monospace;font-size:12px;flex:1}
.filter-det{font-size:10px;color:var(--fg3);text-align:right}

/* 表格 */
table{width:100%;border-collapse:collapse;font-size:12px}
th{text-align:left;color:var(--fg2);font-weight:600;padding:6px 8px;border-bottom:1px solid var(--border);font-size:11px;text-transform:uppercase;letter-spacing:.3px}
td{padding:5px 8px;border-bottom:1px solid var(--border);font-family:monospace;font-size:12px}
tr:hover{background:var(--surface2)}
.t-up{color:var(--green)}.t-down{color:var(--red)}

/* 日志 */
.log-box{background:var(--surface2);border-radius:8px;padding:10px 12px;max-height:200px;overflow-y:auto;font-family:monospace;font-size:12px;line-height:1.8}
.log-line{white-space:nowrap}
.log-line.sig{color:var(--green)}.log-line.sig_r{color:var(--red)}
.log-line.trade{color:var(--cyan)}.log-line.engine{color:var(--purple)}
.log-line.ok{color:var(--green)}.log-line.no{color:var(--red)}.log-line.warn{color:var(--orange)}
.log-line.info{color:var(--fg3)}

/* 状态栏 */
.status-bar{display:flex;gap:16px;align-items:center;padding:8px 16px;background:var(--surface);border-radius:var(--radius);margin-bottom:10px;font-size:12px}
.status-dot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:4px}
.dot-green{background:var(--green)}.dot-red{background:var(--red)}.dot-yellow{background:var(--orange)}.dot-gray{background:var(--fg3)}
.status-bar button{background:var(--border);color:var(--fg);border:none;padding:4px 12px;border-radius:6px;cursor:pointer;font-size:11px}
.status-bar button:hover{background:var(--blue)}

@media(max-width:900px){.grid4{grid-template-columns:repeat(2,1fr)}.grid2,.grid2e{grid-template-columns:1fr}}
</style>
</head>
<body>
<div class="header">
  <h1>🔥 热血青年交易所</h1>
  <small>QQQ 0DTE · v5.0 · <span id="clock"></span></small>
</div>
<div class="container">
  <!-- 状态栏 -->
  <div class="status-bar">
    <span><span class="status-dot dot-gray" id="dot-engine"></span>引擎: <b id="s-engine">--</b></span>
    <span><span class="status-dot dot-gray" id="dot-conn"></span>连接: <b id="s-conn">--</b></span>
    <span style="flex:1"></span>
    <span style="color:var(--fg3)">每5秒自动刷新</span>
  </div>

  <!-- 4卡片 -->
  <div class="grid4">
    <div class="card">
      <div class="card-title">📊 当日概况</div>
      <div class="kv"><span class="kv-label">今日盈亏</span><span class="kv-val" id="v-pnl">--</span></div>
      <div class="kv"><span class="kv-label">交易次数</span><span class="kv-val" id="v-trades">--</span></div>
      <div class="kv"><span class="kv-label">胜率</span><span class="kv-val" id="v-wr">--</span></div>
      <div class="kv"><span class="kv-label">持仓</span><span class="kv-val" id="v-hold">--</span></div>
    </div>
    <div class="card">
      <div class="card-title">📈 行情</div>
      <div class="kv"><span class="kv-label">QQQ</span><span class="kv-val" id="v-qqq">--</span></div>
      <div class="kv"><span class="kv-label">涨跌</span><span class="kv-val" id="v-chg">--</span></div>
      <div class="kv"><span class="kv-label">成交量</span><span class="kv-val" id="v-vol">--</span></div>
    </div>
    <div class="card">
      <div class="card-title">💰 资金</div>
      <div class="kv"><span class="kv-label">总资产</span><span class="kv-val" id="v-equity">--</span></div>
      <div class="kv"><span class="kv-label">现金</span><span class="kv-val" id="v-cash">--</span></div>
      <div class="kv"><span class="kv-label">购买力</span><span class="kv-val" id="v-power">--</span></div>
    </div>
    <div class="card">
      <div class="card-title">⚙️ 系统</div>
      <div class="kv"><span class="kv-label">运行时间</span><span class="kv-val" id="v-uptime">--</span></div>
      <div class="kv"><span class="kv-label">K线数</span><span class="kv-val" id="v-candles">--</span></div>
      <div class="kv"><span class="kv-label">更新</span><span class="kv-val dim" id="v-updated">--</span></div>
    </div>
  </div>

  <!-- 信号 + 过滤器 -->
  <div class="grid2">
    <div class="card signal">
      <div class="card-title" style="justify-content:center">🎯 信号</div>
      <div class="signal-icon" id="sig-icon">⏳</div>
      <div class="signal-dir dim" id="sig-dir">无信号</div>
      <div class="signal-price" id="sig-price"></div>
      <div class="signal-reason" id="sig-reason"></div>
    </div>
    <div class="card">
      <div class="card-title">🔍 过滤器</div>
      <div id="filters"></div>
    </div>
  </div>

  <!-- 持仓 + 交易 -->
  <div class="grid2e">
    <div class="card">
      <div class="card-title">📋 当前持仓</div>
      <table><thead><tr><th>标的</th><th>数量</th><th>成本</th><th>现价</th><th>盈亏</th><th>盈亏%</th></tr></thead>
      <tbody id="tb-pos"></tbody></table>
    </div>
    <div class="card">
      <div class="card-title">📝 交易记录</div>
      <table><thead><tr><th>时间</th><th>方向</th><th>期权</th><th>开仓</th><th>平仓</th><th>数量</th><th>盈亏</th></tr></thead>
      <tbody id="tb-trd"></tbody></table>
    </div>
  </div>

  <!-- 实时日志 -->
  <div class="card">
    <div class="card-title">📋 实时事件</div>
    <div class="log-box" id="log-box"></div>
  </div>
</div>

<script>
let prevEventKey='';
let prevFilter={};
const $=id=>document.getElementById(id);
function fmt$(v){return v?'$'+Number(v).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2}):'--'}
function cls(v){return v>=0?'up':'down'}

function render(d){
  // 系统
  const running=d.running;
  $('dot-engine').className='status-dot '+(running?'dot-green':'dot-red');
  $('s-engine').textContent=running?'运行中':'已停止';
  $('dot-conn').className='status-dot '+(d.connected?'dot-green':'dot-yellow');
  $('s-conn').textContent=d.connected?'已连接':'未连接';
  $('v-updated').textContent=d.updated||'--';
  $('v-candles').textContent=d.candle_count||'--';

  // 当日
  const pnl=d.daily_pnl||0;
  $('v-pnl').textContent='$'+pnl.toLocaleString('en-US',{minimumFractionDigits:2,signDisplay:'always'});
  $('v-pnl').className='kv-val '+(pnl>=0?'up':'down');
  const trades=d.trades_today||[];
  $('v-trades').textContent=trades.length;
  const closed=trades.filter(t=>t.exit_price);
  const wins=closed.filter(t=>(t.pnl_usd||0)>0).length;
  const wr=closed.length?Math.round(wins/closed.length*100):0;
  $('v-wr').textContent=wr+'%';
  $('v-wr').className='kv-val '+(wr>=50?'up':'down');
  $('v-hold').textContent=d.position?1:0;

  // 行情
  $('v-qqq').textContent=d.current_price?'$'+d.current_price.toFixed(2):'--';

  // 信号
  const sig=d.current_signal;
  if(sig){
    const isCall=sig.dir==='call';
    $('sig-icon').textContent=isCall?'🟢':'🔴';
    $('sig-dir').textContent=isCall?'🟢 做多':'🔴 做空';
    $('sig-dir').className='signal-dir '+(isCall?'up':'down');
    $('sig-price').textContent='$'+(sig.price||0).toFixed(2);
    $('sig-reason').textContent=sig.reason||'';
  }else{
    $('sig-icon').textContent='⏳';
    $('sig-dir').textContent='无信号';
    $('sig-dir').className='signal-dir dim';
    $('sig-price').textContent='';
    $('sig-reason').textContent='';
  }

  // 过滤器
  const fs=d.filter_status||{};
  const fmap=[['sma20','SMA20'],['volume','量能'],['momentum','动量'],['body','K线实体']];
  let fhtml='';
  fmap.forEach(([k,label])=>{
    const f=fs[k]||{};
    const ok=f.ok;
    const dotColor=ok===true?'var(--green)':ok===false?'var(--red)':'var(--fg3)';
    const valColor=ok===true?'up':ok===false?'down':'dim';
    fhtml+=`<div class="filter-item">
      <span class="filter-dot" style="background:${dotColor}"></span>
      <span class="filter-name">${label}</span>
      <span class="filter-val ${valColor}">${f.val||'--'}</span>
      <span class="filter-det">${f.detail||''}</span>
    </div>`;
    // 过滤器变化日志
    if(ok!==prevFilter[k]){
      prevFilter[k]=ok;
      if(ok===true)addLog('✅ '+label+': 通过 ('+(f.val||'')+')','ok');
      else if(ok===false)addLog('❌ '+label+': 不通过 ('+(f.val||'')+')','no');
    }
  });
  $('filters').innerHTML=fhtml;

  // 持仓
  const pos=d._positions||[];
  let phtml='';
  pos.forEach(p=>{
    const pnl=parseFloat((p.pnl+'').replace(/[,+$]/g,''))||0;
    phtml+=`<tr><td>${p.sym}</td><td>${p.qty}</td><td>${p.cost}</td><td>${p.cur}</td>
      <td class="${pnl>=0?'t-up':'t-down'}">${p.pnl}</td>
      <td class="${pnl>=0?'t-up':'t-down'}">${p.pct}</td></tr>`;
  });
  $('tb-pos').innerHTML=phtml||'<tr><td colspan="6" style="text-align:center;color:var(--fg3)">无持仓</td></tr>';

  // 交易
  let thtml='';
  (d._trades||[]).slice(-15).reverse().forEach(t=>{
    const pnl=t.pnl_usd||0;
    thtml+=`<tr><td>${t.time||'--'}</td><td>${t.dir||'--'}</td><td>${t.opt||'--'}</td>
      <td>${t.ep||'--'}</td><td>${t.exit_price||'--'}</td><td>${t.qty||0}</td>
      <td class="${pnl>0?'t-up':pnl<0?'t-down':''}">${pnl?'$'+pnl.toLocaleString('en-US',{signDisplay:'always',minimumFractionDigits:2}):'--'}</td></tr>`;
  });
  $('tb-trd').innerHTML=thtml||'<tr><td colspan="7" style="text-align:center;color:var(--fg3)">无交易记录</td></tr>';

  // 事件日志
  (d.events||[]).forEach(e=>{
    const key=e.time+'|'+e.msg;
    if(key!==prevEventKey){
      prevEventKey=key;
      const tagMap={signal:'sig',trade:'trade',error:'no',engine:'engine',info:'info'};
      let tag=tagMap[e.tag]||'info';
      if(tag==='sig'&&(e.msg||'').includes('做空'))tag='sig_r';
      addLog(e.msg,tag);
    }
  });
}

function addLog(msg,tag){
  const box=$('log-box');
  const now=new Date().toTimeString().slice(0,8);
  const div=document.createElement('div');
  div.className='log-line '+(tag||'info');
  div.textContent='['+now+'] '+msg;
  box.appendChild(div);
  if(box.children.length>200)box.removeChild(box.firstChild);
  box.scrollTop=box.scrollHeight;
}

async function poll(){
  try{
    const r=await fetch('/api/state');
    if(r.ok){
      const d=await r.json();
      render(d);
    }
  }catch(e){}
}

$('clock').textContent=new Date().toLocaleTimeString('zh-CN');
setInterval(()=>{$('clock').textContent=new Date().toLocaleTimeString('zh-CN')},1000);
poll();
setInterval(poll,5000);
</script>
</body>
</html>'''


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/' or self.path == '/index.html':
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(HTML.encode('utf-8'))
        elif self.path == '/api/state':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            data = self._read_state()
            self.wfile.write(json.dumps(data, ensure_ascii=False, default=str).encode('utf-8'))
        else:
            self.send_error(404)

    def _read_state(self):
        sd = str(BASE_DIR)
        state = {}
        try:
            sf = os.path.join(sd, 'state.json')
            if os.path.exists(sf):
                with open(sf, encoding='utf-8') as f:
                    state = json.load(f)
        except:
            pass

        # 持仓
        positions = []
        pf = os.path.join(sd, 'position_snapshot.json')
        if os.path.exists(pf):
            try:
                with open(pf, encoding='utf-8') as f:
                    raw = json.load(f)
                positions = raw if isinstance(raw, list) else raw.get('positions', [raw])
            except:
                pass

        # 交易记录
        trades = list(state.get('trades_today', []))
        lf = os.path.join(sd, 'longbridge_orders.json')
        if os.path.exists(lf):
            try:
                with open(lf, encoding='utf-8') as f:
                    lb = json.load(f)
                for o in lb.get('orders', []):
                    eq = float(o.get('executed_qty', 0))
                    ep = float(o.get('executed_price', 0))
                    if eq > 0 and ep > 0:
                        trades.append({
                            'time': (o.get('updated_at', '')[-8:]) if o.get('updated_at') else '--',
                            'dir': '做多' if o.get('side') == '买入' else '做空',
                            'opt': o.get('symbol', '--'),
                            'ep': f'${ep:.2f}',
                            'exit_price': '',
                            'qty': int(eq),
                            'pnl_usd': 0,
                        })
            except:
                pass

        state['_positions'] = positions
        state['_trades'] = trades
        return state

    def log_message(self, format, *args):
        pass  # 静默


_server = None


def start_server(port=8080):
    global _server
    _server = HTTPServer(('0.0.0.0', port), Handler)
    _server.serve_forever()


def main(port=8080):
    t = threading.Thread(target=start_server, args=(port,), daemon=True)
    t.start()
    time.sleep(0.5)
    webbrowser.open(f'http://127.0.0.1:{port}')
    print(f'仪表盘已启动: http://127.0.0.1:{port}')
    return t


if __name__ == '__main__':
    main()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
