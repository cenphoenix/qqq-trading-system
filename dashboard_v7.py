"""
v7 FastAPI WebSocket Dashboard
Merged dashboard for the v7 multi-engine trading system.
"""
import asyncio
import csv
import logging
import json
import os
import re
import sys
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
import uvicorn

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from signal_names import display_signal_name
from review_summary import build_review_summary, latest_review_date


app = FastAPI(title="QQQ 0DTE v7 Dashboard")


APP_DIR = os.path.dirname(os.path.abspath(__file__))
CANDLE_DIR = os.path.join(APP_DIR, "data", "candles")
SETTINGS_PATH = os.path.join(APP_DIR, "settings.json")
I18N_PATH = os.path.join(APP_DIR, "dashboard_i18n.json")
ORDERS_PATH = os.path.join(APP_DIR, "longbridge_orders.json")
DEFAULT_SYMBOL = "QQQ.US"
SYMBOL_ALIASES = {
    "QQQ": "QQQ.US",
    "QQQ.US": "QQQ.US",
}

# Browser websocket disconnects on Windows can surface as noisy low-level
# "data transfer failed" logs. They are harmless for the trading engine.
logging.getLogger("websockets").setLevel(logging.CRITICAL)
logging.getLogger("uvicorn.protocols.websockets.websockets_impl").setLevel(logging.CRITICAL)


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


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ''):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ''):
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _infer_option_dir(symbol: Any) -> str:
    match = re.search(r'\d{6}([CP])', str(symbol or '').upper())
    if not match:
        return ''
    return 'call' if match.group(1) == 'C' else 'put'


def _safe_symbol(symbol: Optional[str]) -> str:
    raw = str(symbol or DEFAULT_SYMBOL).strip().upper()
    normalized = SYMBOL_ALIASES.get(raw, raw)
    if not re.match(r"^[A-Z0-9._-]+$", normalized):
        return DEFAULT_SYMBOL
    return normalized


def _symbol_candle_dir(symbol: Optional[str]) -> str:
    safe = _safe_symbol(symbol)
    symbol_dir = os.path.join(CANDLE_DIR, safe)
    if os.path.isdir(symbol_dir):
        return symbol_dir
    return CANDLE_DIR


def _available_symbols() -> List[str]:
    symbols = {DEFAULT_SYMBOL}
    if os.path.isdir(CANDLE_DIR):
        for name in os.listdir(CANDLE_DIR):
            path = os.path.join(CANDLE_DIR, name)
            if os.path.isdir(path) and re.match(r"^[A-Za-z0-9._-]+$", name):
                symbols.add(_safe_symbol(name))
    return sorted(symbols)


def _available_candle_dates(symbol: Optional[str] = None) -> List[str]:
    candle_dir = _symbol_candle_dir(symbol)
    if not os.path.isdir(candle_dir):
        return []
    dates = [
        name.replace(".csv", "")
        for name in os.listdir(candle_dir)
        if re.match(r"\d{4}-\d{2}-\d{2}\.csv$", name)
    ]
    return sorted(dates)


def _latest_candle_file(symbol: Optional[str] = None) -> Optional[str]:
    candle_dir = _symbol_candle_dir(symbol)
    if not os.path.isdir(candle_dir):
        return None
    files = [
        os.path.join(candle_dir, name)
        for name in os.listdir(candle_dir)
        if re.match(r"\d{4}-\d{2}-\d{2}\.csv$", name)
    ]
    return max(files) if files else None


def _parse_candle_time(value: str) -> Optional[datetime]:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(str(value).split("+")[0].split("-04:00")[0], fmt)
        except ValueError:
            continue
    return None


def _candle_files_for_timeframe(day: Optional[str], timeframe: str, symbol: Optional[str] = None) -> List[str]:
    tf = (timeframe or "1m").lower()
    candle_dir = _symbol_candle_dir(symbol)
    if day:
        path = os.path.join(candle_dir, f"{day}.csv")
        return [path] if os.path.exists(path) else []
    if tf in ("1d", "1w", "1mth", "1mo", "1month"):
        if not os.path.isdir(candle_dir):
            return []
        return [
            os.path.join(candle_dir, name)
            for name in sorted(os.listdir(candle_dir))
            if re.match(r"\d{4}-\d{2}-\d{2}\.csv$", name)
        ]
    latest = _latest_candle_file(symbol)
    return [latest] if latest else []


def _bucket_key(dt: datetime, timeframe: str) -> str:
    tf = (timeframe or "1m").lower()
    if tf in ("1m", "1min"):
        bucket = dt.replace(second=0, microsecond=0)
        return bucket.strftime("%Y-%m-%d %H:%M:%S")
    if tf in ("5m", "5min"):
        bucket = dt.replace(minute=(dt.minute // 5) * 5, second=0, microsecond=0)
        return bucket.strftime("%Y-%m-%d %H:%M:%S")
    if tf in ("10m", "10min"):
        bucket = dt.replace(minute=(dt.minute // 10) * 10, second=0, microsecond=0)
        return bucket.strftime("%Y-%m-%d %H:%M:%S")
    if tf in ("1h", "60m"):
        return dt.replace(minute=0, second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
    if tf in ("2h", "120m"):
        return dt.replace(hour=(dt.hour // 2) * 2, minute=0, second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
    if tf in ("3h", "180m"):
        return dt.replace(hour=(dt.hour // 3) * 3, minute=0, second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
    if tf in ("4h", "240m"):
        return dt.replace(hour=(dt.hour // 4) * 4, minute=0, second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
    if tf == "1w":
        iso = dt.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"
    if tf in ("1mth", "1mo", "1month"):
        return dt.strftime("%Y-%m")
    return dt.strftime("%Y-%m-%d")


def _aggregate_candles(rows: List[Dict], timeframe: str) -> List[Dict]:
    grouped: Dict[str, Dict] = {}
    order: List[str] = []
    for row in rows:
        dt = row.get("_dt")
        if not isinstance(dt, datetime):
            continue
        key = _bucket_key(dt, timeframe)
        if key not in grouped:
            grouped[key] = {
                "time": key,
                "open": row["open"],
                "high": row["high"],
                "low": row["low"],
                "close": row["close"],
                "volume": row["volume"],
            }
            order.append(key)
        else:
            item = grouped[key]
            item["high"] = max(item["high"], row["high"])
            item["low"] = min(item["low"], row["low"])
            item["close"] = row["close"]
            item["volume"] += row["volume"]
    return [grouped[k] for k in order]


def _add_indicators(rows: List[Dict]) -> List[Dict]:
    cumulative_pv = 0.0
    cumulative_vol = 0.0
    ema9 = None
    ema21 = None
    closes: List[float] = []
    enriched = []
    for item in rows:
        h = item["high"]
        l = item["low"]
        c = item["close"]
        v = item["volume"]
        typical = (h + l + c) / 3
        cumulative_pv += typical * max(v, 0)
        cumulative_vol += max(v, 0)
        vwap = cumulative_pv / cumulative_vol if cumulative_vol > 0 else c
        ema9 = c if ema9 is None else c * (2 / 10) + ema9 * (1 - 2 / 10)
        ema21 = c if ema21 is None else c * (2 / 22) + ema21 * (1 - 2 / 22)
        closes.append(c)
        sma20 = sum(closes[-20:]) / min(len(closes), 20)
        enriched.append({
            **item,
            "vwap": round(vwap, 4),
            "ema9": round(ema9, 4),
            "ema21": round(ema21, 4),
            "sma20": round(sma20, 4),
        })
    return enriched


def _read_candles(day: Optional[str] = None, limit: int = 420, timeframe: str = "1m", symbol: Optional[str] = None) -> Dict:
    safe_symbol = _safe_symbol(symbol)
    paths = _candle_files_for_timeframe(day, timeframe, safe_symbol)
    if not paths:
        return {"symbol": safe_symbol, "date": day or "", "timeframe": timeframe, "candles": []}

    raw_rows: List[Dict] = []
    for path in paths:
        if not path or not os.path.exists(path):
            continue
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                try:
                    ts = str(row.get("timestamp", ""))
                    dt = _parse_candle_time(ts)
                    if not dt:
                        continue
                    raw_rows.append({
                        "_dt": dt,
                        "time": ts,
                        "open": float(row.get("open", 0)),
                        "high": float(row.get("high", 0)),
                        "low": float(row.get("low", 0)),
                        "close": float(row.get("close", 0)),
                        "volume": float(row.get("volume", 0) or 0),
                    })
                except (TypeError, ValueError):
                    continue

    raw_rows.sort(key=lambda x: x["_dt"])
    rows = _aggregate_candles(raw_rows, timeframe)
    rows = _add_indicators(rows)
    if limit and limit > 0:
        rows = rows[-limit:]
    latest_name = os.path.basename(paths[-1]).replace(".csv", "") if paths else ""
    return {"symbol": safe_symbol, "date": day or latest_name, "timeframe": timeframe, "candles": rows}


def _load_config_snapshot() -> Dict:
    if not os.path.exists(SETTINGS_PATH):
        return {}
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8-sig") as f:
            cfg = json.load(f)
    except Exception:
        return {}
    risk = cfg.get("risk", {}) if isinstance(cfg, dict) else {}
    signal = cfg.get("signal", {}) if isinstance(cfg, dict) else {}
    trading = cfg.get("trading", {}) if isinstance(cfg, dict) else {}
    keys = [
        "order_pct", "put_order_pct", "shadow_signal_live_orders",
        "shadow_live_order_pos_mult", "shadow_live_open_pos_mult",
        "quick_trail_activate_pct", "quick_trail_drop_pct",
        "trend_quick_trail_activate_pct", "trend_quick_trail_drop_pct",
        "timeout_stage1_bars", "timeout_stage2_bars", "timeout_stage3_bars",
        "put_time_stop_bars", "max_contracts_per_trade", "max_trades",
        "daily_limit", "enable_put_entries", "put_quality_filter",
        "price_action_filter", "brooks_priority_mode", "trend_day_filter_enabled",
        "market_regime_enabled", "max_afternoon_contracts",
    ]
    return {
        "symbol": signal.get("symbol", DEFAULT_SYMBOL),
        "trading": {
            "start_time": trading.get("start_time"),
            "end_time": trading.get("end_time"),
        },
        "risk": {key: risk.get(key) for key in keys if key in risk},
    }


def _load_order_snapshot(limit: int = 30) -> Dict:
    if not os.path.exists(ORDERS_PATH):
        return {"orders": [], "updated": ""}
    try:
        with open(ORDERS_PATH, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except Exception:
        return {"orders": [], "updated": ""}
    raw_orders = data.get("orders", []) if isinstance(data, dict) else []
    if not isinstance(raw_orders, list):
        raw_orders = []
    orders = []
    for item in raw_orders:
        if not isinstance(item, dict):
            continue
        orders.append({
            "symbol": item.get("symbol", ""),
            "side": item.get("side", ""),
            "quantity": _to_int(item.get("quantity")),
            "executed_qty": _to_float(item.get("executed_qty")),
            "executed_price": _to_float(item.get("executed_price")),
            "status": item.get("status", ""),
        })
    return {
        "orders": orders[:max(1, min(limit, 100))],
        "updated": datetime.fromtimestamp(os.path.getmtime(ORDERS_PATH)).isoformat() if os.path.exists(ORDERS_PATH) else "",
    }


def _load_i18n() -> Dict:
    if not os.path.exists(I18N_PATH):
        return {"default": "zh", "languages": {"en": {}, "zh": {}}}
    try:
        with open(I18N_PATH, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except Exception:
        return {"default": "zh", "languages": {"en": {}, "zh": {}}}
    if not isinstance(data, dict):
        return {"default": "zh", "languages": {"en": {}, "zh": {}}}
    data.setdefault("default", "zh")
    data.setdefault("languages", {})
    data["languages"].setdefault("en", {})
    data["languages"].setdefault("zh", {})
    return data


def _normalize_position(pos: Dict) -> Dict:
    """Keep broker and internal positions using the same fields for the UI."""
    if not isinstance(pos, dict):
        return {'symbol': str(pos), 'opt_symbol': str(pos), 'dir': '', 'contracts': 0, 'entry_opt_price': 0}

    item = dict(pos)
    symbol = item.get('opt_symbol') or item.get('symbol') or item.get('sym') or ''
    direction = item.get('dir') or item.get('direction') or _infer_option_dir(symbol)
    contracts = _to_int(item.get('contracts', item.get('qty', item.get('quantity', 0))))
    entry_opt_price = _to_float(item.get('entry_opt_price', item.get('cost', item.get('entry_price', 0))))

    item['symbol'] = symbol
    item['opt_symbol'] = symbol
    item['dir'] = direction
    item['dir_label'] = 'CALL' if direction == 'call' else 'PUT' if direction == 'put' else '--'
    item['contracts'] = contracts
    item['qty'] = contracts
    item['entry_opt_price'] = entry_opt_price
    item['cost'] = entry_opt_price
    return item


class DashboardState:
    def __init__(self):
        self.signal_manager: Optional[Any] = None
        self.connected_clients: List[WebSocket] = []
        
        # 璐︽埛淇℃伅
        self.account_info: Dict = {}
        self.current_position: Optional[Dict] = None
        self.broker_positions: List[Dict] = []
        self.daily_pnl: float = 0.0
        self.daily_trades: int = 0
        self.trades_today: List[Dict] = []
        self.signal_probes: List[Dict] = []
        
        # 琛屾儏
        self.current_price: float = 0.0
        self.candle_count: int = 0
        self.day_market_regime: Dict = {}
        
        # VIX
        self.vix_state: Dict = {}
        
        # 绯荤粺
        self.running: bool = False
        self.connected: bool = False
        self.events: List[Dict] = []
        self.filter_status: Dict = {}
        self.start_time: datetime = datetime.now()
        
    def to_dict(self) -> Dict:
        # Uptime
        uptime = datetime.now() - self.start_time
        hours = int(uptime.total_seconds() // 3600)
        minutes = int((uptime.total_seconds() % 3600) // 60)
        uptime_str = f"{hours}h{minutes}m"
        
        # 寮曟搸鐘舵€?
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
            
        # 鏈€鏂颁俊鍙?
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
            
        # 璐︽埛
        account = self.account_info
        net_assets = 0
        cash = 0
        buying_power = 0
        if isinstance(account, dict):
            net_assets = account.get('net_assets', 0)
            cash = account.get('cash', 0)
            buying_power = account.get('buying_power', 0)
            
        # 鎸佷粨
        positions = []
        if self.current_position:
            positions.append(self.current_position)
        positions.extend(self.broker_positions)
        positions = [_normalize_position(p) for p in positions]
        
        # 浜ゆ槗璁板綍 - 淇濇寔鍜屾棫dashboard涓€鏍风殑鏍煎紡
        trades = []
        for t in self.trades_today:
            opt = t.get('opt_symbol', '')
            # Prefer close time, then entry time, and render HH:MM:SS for the UI.
            raw_time = t.get('exit_time') or t.get('entry_time') or t.get('time', '')
            # 澶勭悊datetime瀵硅薄鎴朓SO瀛楃涓?
            if hasattr(raw_time, 'strftime'):
                # datetime瀵硅薄
                time_str = raw_time.strftime('%H:%M:%S')
            elif isinstance(raw_time, str) and 'T' in raw_time:
                # ISO datetime "2026-05-28T14:06:30-04:00" 鈫?鍙栨椂鍒嗙
                time_part = raw_time.split('T')[1]
                time_str = time_part[:8]  # "14:06:30"
            else:
                time_str = '--:--:--'
                
            trades.append({
                'id': len(trades) + 1,
                'time': time_str or '--:--:--',
                'dir': 'CALL' if t.get('dir') == 'call' else 'PUT' if t.get('dir') == 'put' else str(t.get('dir', '--')),
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
            'day_market_regime': self.day_market_regime,
            'uptime': uptime_str,
            
            # 璐︽埛
            'account': {
                'net_assets': net_assets,
                'cash': cash,
                'buying_power': buying_power,
            },
            
            # 鎸佷粨
            'positions': positions,
            
            # 浜ゆ槗
            'trades': trades,
            'signal_probes': self.signal_probes[-50:],
            'daily_pnl': self.daily_pnl,
            'daily_trades': self.daily_trades,
            
            # 淇″彿
            'signal': last_signal,
            
            # 寮曟搸
            'engines': engine_states,
            
            # VIX
            'vix': self.vix_state,
            
            # 杩囨护鍣?            'filters': self.filter_status,
            
            # 浜嬩欢
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
                for client in list(state.connected_clients):
                    try:
                        await client.send_json(data)
                    except Exception:
                        disconnected.append(client)
                for client in disconnected:
                    if client in state.connected_clients:
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
    except (WebSocketDisconnect, RuntimeError, OSError):
        pass
    finally:
        if websocket in state.connected_clients:
            state.connected_clients.remove(websocket)


@app.get("/api/state")
async def get_state():
    return state.to_dict()


@app.get("/api/candles")
async def get_candles(date: Optional[str] = None, limit: int = 420, timeframe: str = "1m", symbol: str = DEFAULT_SYMBOL):
    return _json_safe(_read_candles(date, limit, timeframe, symbol))


@app.get("/api/symbols")
async def get_symbols():
    return {"symbols": _available_symbols(), "default": DEFAULT_SYMBOL}


@app.get("/api/candle_dates")
async def get_candle_dates(symbol: str = DEFAULT_SYMBOL):
    safe_symbol = _safe_symbol(symbol)
    dates = _available_candle_dates(safe_symbol)
    return {"symbol": safe_symbol, "dates": dates, "latest": dates[-1] if dates else ""}


@app.get("/api/config")
async def get_config():
    return _json_safe(_load_config_snapshot())


@app.get("/api/orders")
async def get_orders(limit: int = 30):
    return _json_safe(_load_order_snapshot(limit))


@app.get("/api/i18n")
async def get_i18n():
    return _json_safe(_load_i18n())


@app.get("/api/review_summary")
async def get_review_summary(period: str = "day", date: Optional[str] = None):
    anchor = date or latest_review_date()
    return _json_safe(build_review_summary(period, anchor))


@app.get("/")
async def root():
    return HTMLResponse(content=DASHBOARD_HTML, status_code=200)


# 澶栭儴璋冪敤鎺ュ彛
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

def update_day_market_regime(regime: Dict):
    state.day_market_regime = regime or {}

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
    import importlib.util
    import socket
    import time
    global _server_started

    if _server_started:
        print(f"Dashboard already started in this process, skip duplicate start: http://localhost:{port}")
        return
    _server_started = True

    # Wait for port release after a Windows Ctrl+C/TIME_WAIT shutdown.
    for attempt in range(15):
        try:
            test_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            test_sock.settimeout(1)
            test_sock.bind((host, port))
            test_sock.close()
            print(f"Port {port} available, starting Dashboard...")
            break
        except OSError:
            if attempt < 14:
                print(f"Port {port} busy, waiting 2s... ({attempt + 1}/15)")
                time.sleep(2)
            else:
                _server_started = False
                print(f"Port {port} still busy, please close the process using it")
                return

    ws_impl = "wsproto" if importlib.util.find_spec("wsproto") else "websockets"
    try:
        uvicorn.run(app, host=host, port=port, log_level="warning", ws=ws_impl)
    except TypeError:
        # Older uvicorn versions may not accept explicit websocket protocol args.
        uvicorn.run(app, host=host, port=port, log_level="warning")

# Dashboard HTML
DASHBOARD_HTML = r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>QQQ 0DTE v7 Dashboard</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#0a0e1a;--surface:rgba(12,18,40,.88);--surface2:rgba(18,26,56,.92);--border:rgba(120,160,220,.16);--border2:rgba(0,240,255,.22);--text:#e6eeff;--muted:rgba(205,216,240,.62);--cyan:#00e5ff;--blue:#4a91ff;--green:#00d084;--red:#ff4f6d;--yellow:#f5c542;--orange:#ff8b3d;--mono:Consolas,"SFMono-Regular",monospace;--sans:"Microsoft YaHei",system-ui,sans-serif}
body{font-family:var(--sans);background:var(--bg);color:var(--text);min-height:100vh;-webkit-font-smoothing:antialiased}.wrap{max-width:1500px;margin:0 auto;padding:14px 22px 56px}.card{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:14px 16px;backdrop-filter:blur(10px)}.card-title{font-size:13px;font-weight:700;color:var(--cyan);letter-spacing:.5px;margin-bottom:10px}.kv{display:flex;justify-content:space-between;gap:12px;padding:6px 0;border-bottom:1px solid var(--border)}.kv:last-child{border-bottom:0}.kv-label{color:var(--muted);font-size:12px}.kv-val{font-family:var(--mono);font-size:13px;font-weight:700}.up,.t-up{color:var(--green)}.down,.t-down{color:var(--red)}.dim{color:var(--muted)}.status-bar{display:flex;gap:20px;align-items:center;padding:12px 16px;background:var(--surface);border:1px solid var(--border);border-radius:10px;margin-bottom:12px;font-size:13px}.status-dot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:6px}.lang-switch{display:flex;align-items:center;gap:8px;color:var(--muted);font-size:12px}.lang-switch select{height:28px;background:var(--surface2);color:var(--text);border:1px solid var(--border2);border-radius:6px;padding:0 8px}.dot-green{background:var(--green)}.dot-red{background:var(--red)}.dot-gray{background:var(--muted)}.grid-4{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:12px}.grid-2{display:grid;grid-template-columns:2fr 3fr;gap:12px;margin-bottom:12px}.grid-5{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin-bottom:12px}.signal-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px}.signal{text-align:center}.signal-icon{font-family:var(--mono);font-size:30px;font-weight:800;margin:8px 0}.signal-dir{font-size:17px;font-weight:800}.signal-price{font-family:var(--mono);font-size:15px;margin:6px 0}.strength-bar{height:6px;background:var(--surface2);border-radius:999px;margin-top:8px;overflow:hidden}.strength-fill{height:100%;border-radius:999px}.strength-high{background:var(--green)}.strength-mid{background:var(--cyan)}.strength-low{background:var(--red)}table{width:100%;border-collapse:collapse;font-size:12px}th{text-align:left;color:var(--muted);font-weight:700;padding:8px 10px;border-bottom:1px solid var(--border)}td{padding:8px 10px;border-bottom:1px solid var(--border);font-family:var(--mono);font-size:12px}tr:hover{background:rgba(255,255,255,.03)}.probe-row{cursor:pointer}.probe-row.selected,.strategy-row.selected{background:rgba(0,229,255,.12);outline:1px solid rgba(0,229,255,.32)}.strategy-row{cursor:pointer}.probe-controls{display:flex;gap:8px;align-items:center;margin-bottom:10px;flex-wrap:wrap}.probe-controls select{height:30px;background:var(--surface2);color:var(--text);border:1px solid var(--border2);border-radius:6px;padding:0 8px;font-family:var(--mono);font-size:12px}.probe-count{margin-left:auto;color:var(--muted);font-family:var(--mono);font-size:12px}.log-box{background:var(--surface2);border-radius:8px;padding:10px;max-height:210px;overflow-y:auto;font-family:var(--mono);font-size:12px;line-height:1.8}.log-line{white-space:nowrap}.log-line.sig{color:var(--green)}.log-line.err{color:var(--red)}.log-line.trade{color:var(--cyan)}.engine-card{position:relative}.engine-card .priority{position:absolute;top:10px;right:10px;background:var(--surface2);padding:2px 6px;border-radius:4px;font-size:10px;color:var(--muted)}.f-item{display:flex;align-items:center;gap:10px;padding:6px 0}.f-dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}.f-name{font-size:12px;color:var(--muted);min-width:110px}.f-val{font-family:var(--mono);font-size:12px;flex:1}
.chart-card{padding:0;margin-bottom:12px;overflow:hidden}.chart-head{display:flex;align-items:center;gap:10px;padding:12px 16px;border-bottom:1px solid var(--border);background:rgba(255,255,255,.025)}.brand{font-family:var(--mono);font-weight:900;font-size:16px;color:#fff}.live-pill{font-size:11px;font-weight:800;color:#07140f;background:var(--green);padding:2px 7px;border-radius:4px}.ticker-strip{display:flex;gap:16px;align-items:center;flex:1;white-space:nowrap;font-family:var(--mono);font-size:12px}.ticker-strip span{color:var(--muted)}.chart-toolbar{display:flex;gap:6px;padding:10px 16px;border-bottom:1px solid var(--border);align-items:center;background:rgba(255,255,255,.015);overflow-x:auto}.symbol-select{height:34px;background:var(--surface2);color:var(--text);border:1px solid var(--border2);border-radius:6px;padding:0 12px;font-family:var(--mono);min-width:130px}.tf-btn{height:30px;min-width:38px;padding:0 10px;border:1px solid var(--border);border-radius:6px;background:var(--surface2);color:var(--muted);font-family:var(--mono);font-weight:700;cursor:pointer}.tf-btn.active{background:rgba(0,229,255,.12);border-color:var(--cyan);color:var(--cyan)}.chart-wrap{position:relative;height:520px;background:#07101c;cursor:crosshair}#k-chart{display:block;width:100%;height:100%}.ohlc-box,.indicator-box,.review-box{position:absolute;left:16px;background:rgba(8,13,26,.78);border:1px solid rgba(255,255,255,.08);border-radius:6px;padding:8px 10px;font-family:var(--mono);font-size:12px;line-height:1.8;backdrop-filter:blur(8px);pointer-events:none}.ohlc-box{top:16px;display:flex;gap:14px;flex-wrap:wrap}.indicator-box{top:78px;min-width:250px}.review-box{left:auto;right:16px;bottom:16px;min-width:260px;display:none;border-color:rgba(0,229,255,.35)}.review-title{color:var(--cyan);font-weight:800}.review-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin-top:6px}.review-chip{background:rgba(255,255,255,.06);border-radius:5px;padding:4px 6px;text-align:center}.chart-tools{position:absolute;right:16px;top:16px;display:flex;gap:6px}.tool-btn{min-width:30px;height:30px;border:1px solid var(--border2);border-radius:6px;background:rgba(255,255,255,.08);color:var(--text);font-family:var(--mono);font-size:12px;cursor:pointer}.tool-btn.active{background:rgba(0,229,255,.18);border-color:var(--cyan);color:var(--cyan)}.chart-empty{position:absolute;inset:0;display:none;align-items:center;justify-content:center;color:var(--muted);font-family:var(--mono)}.terminal-grid{display:grid;grid-template-columns:2fr 1fr;gap:12px;margin-bottom:12px}.study-row{display:grid;grid-template-columns:repeat(6,1fr);gap:8px}.study-pill,.risk-box{background:var(--surface2);border:1px solid var(--border);border-radius:8px;padding:10px 12px;min-height:56px}.study-label,.risk-label{font-size:11px;color:var(--muted);text-transform:uppercase}.study-val,.risk-val{font-family:var(--mono);font-size:14px;margin-top:5px}.risk-panel{display:grid;grid-template-columns:1fr 1fr;gap:8px}.config-list{margin-top:10px;display:grid;grid-template-columns:1fr 1fr;gap:8px}.config-item{display:flex;justify-content:space-between;gap:8px;background:rgba(255,255,255,.03);border:1px solid var(--border);border-radius:6px;padding:7px 9px;font-size:11px}.config-item span:first-child{color:var(--muted)}.config-item b{font-family:var(--mono);font-weight:700;color:var(--text)}.review-controls{display:flex;gap:8px;align-items:center;margin-bottom:10px;flex-wrap:wrap}.review-summary-grid{display:grid;grid-template-columns:repeat(5,1fr);gap:8px;margin-bottom:10px}.review-metric{background:var(--surface2);border:1px solid var(--border);border-radius:8px;padding:10px}.review-metric span{display:block;color:var(--muted);font-size:11px}.review-metric b{display:block;font-family:var(--mono);font-size:15px;margin-top:5px}.review-lists{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px}.review-list-title{font-size:12px;color:var(--cyan);font-weight:800;margin:6px 0}.review-list{font-family:var(--mono);font-size:12px;line-height:1.7;color:var(--muted);min-height:64px}.ops-grid{display:grid;grid-template-columns:1.15fr .85fr;gap:12px;margin-bottom:12px}.ops-head{display:flex;justify-content:space-between;align-items:center;gap:10px}.ops-updated{font-family:var(--mono);font-size:11px;color:var(--muted)}.orders-scroll{max-height:260px;overflow:auto}.status-pill{display:inline-block;padding:2px 7px;border-radius:999px;background:rgba(255,255,255,.06);border:1px solid var(--border);font-size:11px}.status-filled{color:var(--green)}.status-live{color:var(--cyan)}.status-muted{color:var(--muted)}.quant-center{display:grid;gap:8px}.quant-row{display:flex;justify-content:space-between;gap:10px;border-bottom:1px solid var(--border);padding:6px 0;font-size:12px}.quant-row span{color:var(--muted)}.quant-row b{font-family:var(--mono);font-weight:700;text-align:right}
.study-panels{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px}.study-chart{height:160px;position:relative;padding:0;overflow:hidden}.study-chart-head{position:absolute;left:12px;top:10px;z-index:1;font-family:var(--mono);font-size:12px;color:var(--cyan);font-weight:800}.study-chart canvas{display:block;width:100%;height:100%;background:#07101c}.study-chart-note{position:absolute;right:12px;top:10px;font-family:var(--mono);font-size:11px;color:var(--muted)}
@media(max-width:1100px){.terminal-grid,.ops-grid,.study-panels{grid-template-columns:1fr}.study-row{grid-template-columns:repeat(2,1fr)}.review-lists{grid-template-columns:1fr}.review-summary-grid{grid-template-columns:repeat(2,1fr)}}@media(max-width:900px){.grid-4,.grid-5{grid-template-columns:repeat(2,1fr)}.grid-2,.signal-grid{grid-template-columns:1fr}.chart-wrap{height:420px}.ticker-strip{display:none}.ohlc-box{right:16px}.indicator-box{right:16px;top:112px}}
</style>
</head>
<body>
<div class="wrap">
  <div class="status-bar"><span><span class="status-dot dot-gray" id="dot-engine"></span><span data-i18n="engine">Engine</span>: <b id="s-engine">--</b></span><span><span class="status-dot dot-gray" id="dot-conn"></span><span data-i18n="connection">Connection</span>: <b id="s-conn">--</b></span><span style="flex:1"></span><label class="lang-switch"><span data-i18n="language">Language</span><select id="lang-select"><option value="zh">Chinese</option><option value="en">English</option></select></label><span id="clock" style="font-family:var(--mono)"></span></div>
  <div class="grid-4">
    <div class="card"><div class="card-title" data-i18n="daily_overview">Daily Overview</div><div class="kv"><span class="kv-label" data-i18n="today_pnl">Today's P&L</span><span class="kv-val" id="v-pnl">--</span></div><div class="kv"><span class="kv-label" data-i18n="trade_count">Trades</span><span class="kv-val" id="v-trades">--</span></div><div class="kv"><span class="kv-label" data-i18n="win_rate">Win Rate</span><span class="kv-val" id="v-wr">--</span></div><div class="kv"><span class="kv-label" data-i18n="holding">Position</span><span class="kv-val" id="v-hold">--</span></div></div>
    <div class="card"><div class="card-title" data-i18n="market">Market</div><div class="kv"><span class="kv-label">QQQ</span><span class="kv-val" id="v-qqq">--</span></div><div class="kv"><span class="kv-label">VIX</span><span class="kv-val" id="v-vix">--</span></div><div class="kv"><span class="kv-label" data-i18n="vix_regime">VIX Regime</span><span class="kv-val" id="v-vix-regime">--</span></div><div class="kv"><span class="kv-label" data-i18n="day_market">Day Market</span><span class="kv-val" id="v-day-regime">--</span></div></div>
    <div class="card"><div class="card-title" data-i18n="account">Account</div><div class="kv"><span class="kv-label" data-i18n="total_assets">Total Assets</span><span class="kv-val" id="v-equity">--</span></div><div class="kv"><span class="kv-label" data-i18n="cash">Cash</span><span class="kv-val" id="v-cash">--</span></div><div class="kv"><span class="kv-label" data-i18n="buying_power">Buying Power</span><span class="kv-val" id="v-power">--</span></div></div>
    <div class="card"><div class="card-title" data-i18n="system">System</div><div class="kv"><span class="kv-label">Uptime</span><span class="kv-val" id="v-uptime">--</span></div><div class="kv"><span class="kv-label" data-i18n="kline_count">K-line Count</span><span class="kv-val" id="v-candles">--</span></div><div class="kv"><span class="kv-label" data-i18n="position_mult">Position Mult</span><span class="kv-val" id="v-pos-mult">--</span></div></div>
  </div>
  <div class="card chart-card"><div class="chart-head"><div class="brand">FLOW TERMINAL</div><div class="live-pill">LIVE</div><div class="ticker-strip"><span>SPY <b class="dim" id="m-spy">--</b></span><span>QQQ <b class="up" id="m-qqq">--</b></span><span>IWM <b class="dim">--</b></span><span>DIA <b class="dim">--</b></span><span>VIX <b id="m-vix">--</b></span></div></div><div class="chart-toolbar"><select class="symbol-select" id="chart-symbol"><option>QQQ.US</option></select><select class="symbol-select" id="chart-date"><option value="">Latest</option></select><button class="tf-btn active" data-tf="1m">1m</button><button class="tf-btn" data-tf="5m">5m</button><button class="tf-btn" data-tf="10m">10m</button><button class="tf-btn" data-tf="1h">1H</button><button class="tf-btn" data-tf="2h">2H</button><button class="tf-btn" data-tf="3h">3H</button><button class="tf-btn" data-tf="4h">4H</button><button class="tf-btn" data-tf="1d">1D</button><button class="tf-btn" data-tf="1w">1W</button><button class="tf-btn" data-tf="1mo">1M</button><button class="tf-btn active" id="toggle-signals" type="button" data-i18n="signals">Signals</button><button class="tf-btn active" id="toggle-refresh" type="button" data-i18n="auto_refresh">Auto Refresh</button></div><div class="chart-wrap" id="chart-wrap"><canvas id="k-chart"></canvas><div class="ohlc-box" id="ohlc-box" data-i18n="kline_loading">Loading K-line...</div><div class="indicator-box" id="indicator-box"></div><div class="review-box" id="review-box"></div><div class="chart-tools"><button class="tool-btn active" id="tool-signals" type="button" title="Signal markers">S</button><button class="tool-btn active" id="tool-levels" type="button" title="Key levels">L</button><button class="tool-btn" id="tool-trend" type="button" title="Draw trend line">T</button><button class="tool-btn active" id="tool-ma" type="button" title="Moving averages">MA</button><button class="tool-btn active" id="tool-session" type="button" title="Session band">SE</button><button class="tool-btn" id="tool-undo-drawing" type="button" title="Undo drawing">U</button><button class="tool-btn" id="tool-clear-drawings" type="button" title="Clear drawings">X</button><button class="tool-btn" id="tool-reset" type="button" title="Reset view">R</button></div><div class="chart-empty" id="chart-empty" data-i18n="kline_empty">No K-line data</div></div></div>
  <div class="study-panels"><div class="card study-chart"><div class="study-chart-head">MACD Momentum</div><div class="study-chart-note" id="macd-note">--</div><canvas id="macd-chart"></canvas></div><div class="card study-chart"><div class="study-chart-head">RSI / Trend Quality</div><div class="study-chart-note" id="rsi-note">--</div><canvas id="rsi-chart"></canvas></div></div>
  <div class="terminal-grid"><div class="card"><div class="card-title" data-i18n="secondary_studies">Secondary Studies</div><div class="study-row"><div class="study-pill"><div class="study-label">Market Regime</div><div class="study-val" id="diag-regime">--</div></div><div class="study-pill"><div class="study-label" data-i18n="direction">Direction</div><div class="study-val" id="diag-direction">--</div></div><div class="study-pill"><div class="study-label">VIX Regime</div><div class="study-val" id="diag-vix">--</div></div><div class="study-pill"><div class="study-label">VIX Mult</div><div class="study-val" id="diag-vix-mult">--</div></div><div class="study-pill"><div class="study-label">Signals Today</div><div class="study-val" id="diag-signal-count">--</div></div><div class="study-pill"><div class="study-label">Last Signal</div><div class="study-val" id="diag-last-signal">--</div></div></div></div><div class="card"><div class="card-title" data-i18n="quant_risk">Quant Risk</div><div class="risk-panel"><div class="risk-box"><div class="risk-label">Equity</div><div class="risk-val" id="risk-equity">--</div></div><div class="risk-box"><div class="risk-label">Cash</div><div class="risk-val" id="risk-cash">--</div></div><div class="risk-box"><div class="risk-label">Buying Power</div><div class="risk-val" id="risk-power">--</div></div><div class="risk-box"><div class="risk-label">Position Mult</div><div class="risk-val" id="risk-pos-mult">--</div></div><div class="risk-box"><div class="risk-label">Status</div><div class="risk-val" id="risk-status">--</div></div><div class="risk-box"><div class="risk-label">Open Pos</div><div class="risk-val" id="risk-open-pos">--</div></div></div><div class="config-list" id="config-list"></div></div></div>
  <div class="signal-grid"><div class="card signal"><div class="card-title" data-i18n="latest_signal">Latest Signal</div><div class="signal-icon" id="sig-icon">--</div><div class="signal-dir dim" id="sig-dir" data-i18n="no_signal">No signal</div><div class="signal-price" id="sig-price"></div><div style="font-size:11px;color:var(--muted)" id="sig-reason"></div><div class="strength-bar"><div class="strength-fill" id="sig-strength"></div></div></div><div class="card"><div class="card-title" data-i18n="engine_status">v7 Engine Status</div><div id="engines"></div></div></div>
  <div class="grid-5" id="engines-grid"></div>
  <div class="grid-2"><div class="card"><div class="card-title" data-i18n="current_position">Current Position</div><table><thead><tr><th data-i18n="symbol">Symbol</th><th data-i18n="direction">Direction</th><th>Qty</th><th data-i18n="cost">Cost</th></tr></thead><tbody id="tb-pos"></tbody></table></div><div class="card"><div class="card-title" data-i18n="trade_records">Trade Records</div><table><thead><tr><th>Time</th><th data-i18n="direction">Direction</th><th data-i18n="option">Option</th><th data-i18n="open">Open</th><th data-i18n="exit_price">Exit</th><th>Qty</th><th data-i18n="pnl">P&L</th></tr></thead><tbody id="tb-trd"></tbody></table></div></div>
  <div class="ops-grid"><div class="card"><div class="ops-head"><div class="card-title" data-i18n="order_monitor">Order Monitor</div><span class="ops-updated" id="orders-updated">--</span></div><div class="orders-scroll"><table><thead><tr><th data-i18n="symbol">Symbol</th><th data-i18n="direction">Side</th><th>Qty</th><th data-i18n="filled">Filled</th><th data-i18n="price">Price</th><th data-i18n="status">Status</th></tr></thead><tbody id="tb-orders"></tbody></table></div></div><div class="card"><div class="card-title" data-i18n="quant_center">Quant Center</div><div class="quant-center" id="quant-center"></div></div></div>
  <div class="card" style="margin-bottom:12px"><div class="card-title" data-i18n="review_summary">Review Summary</div><div class="review-controls"><button class="tf-btn active" data-review-period="day" data-i18n="review_daily">Daily</button><button class="tf-btn" data-review-period="week" data-i18n="review_weekly">Weekly</button><button class="tf-btn" data-review-period="month" data-i18n="review_monthly">Monthly</button><span class="probe-count" id="review-label">--</span></div><div class="review-summary-grid"><div class="review-metric"><span data-i18n="trade_count">Trades</span><b id="review-trades">--</b></div><div class="review-metric"><span data-i18n="win_rate">Win Rate</span><b id="review-wr">--</b></div><div class="review-metric"><span data-i18n="pnl">P&L</span><b id="review-pnl">--</b></div><div class="review-metric"><span>CALL</span><b id="review-call">--</b></div><div class="review-metric"><span>PUT</span><b id="review-put">--</b></div></div><div class="review-lists"><div><div class="review-list-title" data-i18n="review_best_signals">Best Signals</div><div class="review-list" id="review-best-signals">--</div></div><div><div class="review-list-title" data-i18n="review_worst_signals">Weak Signals</div><div class="review-list" id="review-worst-signals">--</div></div><div><div class="review-list-title" data-i18n="review_actions">Actions</div><div class="review-list" id="review-actions">--</div></div></div><div class="review-lists" style="margin-top:10px"><div><div class="review-list-title" data-i18n="review_best_trades">Best Trades</div><div class="review-list" id="review-best-trades">--</div></div><div><div class="review-list-title" data-i18n="review_worst_trades">Worst Trades</div><div class="review-list" id="review-worst-trades">--</div></div><div><div class="review-list-title" data-i18n="review_days">Days</div><div class="review-list" id="review-days">--</div></div></div></div>
  <div class="card" style="margin-bottom:12px"><div class="card-title" data-i18n="records_5_10_20">Signal Follow-up 5/10/20 Bars</div><div class="probe-controls"><select id="probe-filter"><option value="all" data-i18n="filter_all">All</option><option value="loss" data-i18n="filter_loss">Losing</option><option value="win" data-i18n="filter_win">Winning</option><option value="call">CALL</option><option value="put">PUT</option></select><select id="probe-sort"><option value="time_desc" data-i18n="sort_time_desc">Newest</option><option value="m5_asc" data-i18n="sort_m5_worst">+5 Worst</option><option value="m5_desc" data-i18n="sort_m5_best">+5 Best</option><option value="m10_asc" data-i18n="sort_m10_worst">+10 Worst</option><option value="m10_desc" data-i18n="sort_m10_best">+10 Best</option><option value="m20_asc" data-i18n="sort_m20_worst">+20 Worst</option><option value="m20_desc" data-i18n="sort_m20_best">+20 Best</option></select><button class="tf-btn" id="clear-strategy-filter" type="button" data-i18n="clear_strategy">Clear Strategy</button><span class="probe-count" id="probe-count">--</span></div><table><thead><tr><th>#</th><th>Time</th><th data-i18n="signal">Signal</th><th data-i18n="direction">Direction</th><th data-i18n="entry_price">Entry Price</th><th>+5</th><th>+10</th><th>+20</th></tr></thead><tbody id="tb-probes"></tbody></table></div>
  <div class="card" style="margin-bottom:12px"><div class="card-title" data-i18n="strategy_stats">Strategy Stats</div><table><thead><tr><th data-i18n="signal">Signal</th><th>N</th><th>+5 WR/AVG</th><th>+10 WR/AVG</th><th>+20 WR/AVG</th><th data-i18n="diagnosis">Diagnosis</th></tr></thead><tbody id="tb-strategy-stats"></tbody></table></div>
  <div class="card"><div class="card-title" data-i18n="events">Live Events</div><div class="log-box" id="log-box"></div></div>
</div>
<script>
const $=id=>document.getElementById(id);
const fmtMoney=v=>Number.isFinite(Number(v))?'$'+Number(v).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2}):'--';
const cls=v=>Number(v)>=0?'up':'down';
let chartCandles=[],lastState=null,lastCandleDate='',chartTf='1m',chartSymbol='QQQ.US',chartDate='',configSnapshot=null,i18n={default:'zh',languages:{en:{},zh:{}}},currentLang='zh',showSignals=true,showMAs=true,showKeyLevels=true,showSession=true,showPercentAxis=true,autoRefresh=true,refreshTimer=null,selectedProbeKey='',selectedProbe=null,probeFilter='all',probeSort='time_desc',strategyFilter='',reviewPeriod='day',chartHoverIndex=-1,chartHoverY=0,chartSignalHitboxes=[],chartDrawMode=false,chartDrawStart=null,chartDrawings=[];
function t(key,fallback){const lang=i18n.languages&&i18n.languages[currentLang]||{};return lang[key]||fallback||key}
function applyI18n(){document.querySelectorAll('[data-i18n]').forEach(el=>{el.textContent=t(el.dataset.i18n,el.textContent)});document.documentElement.lang=currentLang==='zh'?'zh-CN':'en';const select=$('lang-select');if(select)select.value=currentLang;const dateSelect=$('chart-date');if(dateSelect&&dateSelect.options.length)dateSelect.options[0].textContent=t('latest','Latest');renderConfig();if(lastState)updateStatusText(lastState);drawChart()}
async function loadI18n(){try{const res=await fetch('/api/i18n',{cache:'no-store'});i18n=await res.json()}catch(e){console.warn('load i18n failed',e)}currentLang=localStorage.getItem('dashboard_lang')||i18n.default||'zh';if(!i18n.languages||!i18n.languages[currentLang])currentLang='zh';applyI18n()}
function updateStatusText(d){if(!d)return;$('s-engine').textContent=d.running?t('running','Running'):t('stopped','Stopped');$('s-conn').textContent=ws&&ws.readyState===1?t('connected','Connected'):t('offline','Offline');}
function minuteKey(v){const s=String(v||'');return s?s.replace('T',' ').slice(0,16):''}
function probeKey(p){return encodeURIComponent([p.id||'',p.entry_time||p.time||'',p.signal||p.regime||'',p.dir||'',p.entry_price||''].join('|'))}
function fmtProbePct(v){if(v===null||v===undefined||v==='')return '--';const n=Number(v);return Number.isFinite(n)?(n>=0?'+':'')+n.toFixed(2)+'%':'--'}
function pctClass(v){const n=Number(v);return n>0?'t-up':n<0?'t-down':''}
function escapeHtml(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function fmtReviewMoney(v){const n=Number(v);return Number.isFinite(n)?(n>=0?'+':'')+'$'+Math.abs(n).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2}):'--'}
function fmtReviewPct(v){const n=Number(v);return Number.isFinite(n)?n.toFixed(1)+'%':'--'}
function renderReviewList(id,items,mapper,emptyKey='no_trades'){const el=$(id);if(!el)return;el.innerHTML=(items||[]).length?(items||[]).map(mapper).join(''):t(emptyKey,'--')}
function renderReviewSummary(s){if(!s)return;$('review-label').textContent=s.label||'--';$('review-trades').textContent=`${s.trades||0} (${s.wins||0}/${s.losses||0})`;$('review-wr').textContent=fmtReviewPct(s.win_rate);$('review-pnl').textContent=fmtReviewMoney(s.pnl);$('review-pnl').className=Number(s.pnl)>=0?'t-up':'t-down';const call=(s.directions&&s.directions.call)||{},put=(s.directions&&s.directions.put)||{};$('review-call').textContent=`${call.trades||0} / ${fmtReviewPct(call.win_rate)} / ${fmtReviewMoney(call.pnl)}`;$('review-put').textContent=`${put.trades||0} / ${fmtReviewPct(put.win_rate)} / ${fmtReviewMoney(put.pnl)}`;renderReviewList('review-best-signals',s.best_signals,r=>`<div><b>${escapeHtml(r.signal)}</b> ${r.trades||0}T ${fmtReviewMoney(r.pnl)} <span class="${pctClass(r.m20_avg)}">+20 ${fmtProbePct(r.m20_avg)}</span></div>`,'no_signal');renderReviewList('review-worst-signals',s.worst_signals,r=>`<div><b>${escapeHtml(r.signal)}</b> ${r.trades||0}T ${fmtReviewMoney(r.pnl)} <span class="${pctClass(r.m20_avg)}">+20 ${fmtProbePct(r.m20_avg)}</span></div>`,'no_signal');renderReviewList('review-actions',s.recommendations,r=>`<div>${escapeHtml(r)}</div>`,'no_signal');renderReviewList('review-best-trades',s.best_trades,r=>`<div class="${Number(r.pnl_usd)>=0?'t-up':'t-down'}">${escapeHtml((r.dir||'').toUpperCase())} ${escapeHtml(r.signal||'Unknown')} ${fmtReviewMoney(r.pnl_usd)}</div>`,'no_trades');renderReviewList('review-worst-trades',s.worst_trades,r=>`<div class="${Number(r.pnl_usd)>=0?'t-up':'t-down'}">${escapeHtml((r.dir||'').toUpperCase())} ${escapeHtml(r.signal||'Unknown')} ${fmtReviewMoney(r.pnl_usd)}</div>`,'no_trades');renderReviewList('review-days',s.days,r=>`<div>${escapeHtml(r.date)} ${r.trades||0}T ${fmtReviewMoney(r.pnl)} ${fmtReviewPct(r.trades?((r.wins||0)/(r.trades||1)*100):0)}</div>`,'no_trades')}
async function loadReviewSummary(){try{const res=await fetch(`/api/review_summary?period=${encodeURIComponent(reviewPeriod)}`,{cache:'no-store'});renderReviewSummary(await res.json())}catch(e){console.warn('review summary failed',e)}}
function probeScore(p){const vals=[p.m20_pct,p.m10_pct,p.m5_pct].map(Number).filter(Number.isFinite);return vals.length?vals[0]:0}
function sortedFilteredProbes(probes){let rows=[...(probes||[])];rows=rows.filter(p=>{if(strategyFilter&&String(p.signal||p.regime||'Unknown')!==strategyFilter)return false;if(probeFilter==='call'||probeFilter==='put')return p.dir===probeFilter;const score=probeScore(p);if(probeFilter==='loss')return score<0;if(probeFilter==='win')return score>0;return true});const fieldMap={m5_asc:'m5_pct',m5_desc:'m5_pct',m10_asc:'m10_pct',m10_desc:'m10_pct',m20_asc:'m20_pct',m20_desc:'m20_pct'};if(probeSort==='time_desc')rows.reverse();else{const field=fieldMap[probeSort]||'m20_pct',dir=probeSort.endsWith('_asc')?1:-1;rows.sort((a,b)=>{const av=Number(a[field]),bv=Number(b[field]);const aOk=Number.isFinite(av),bOk=Number.isFinite(bv);if(!aOk&&!bOk)return 0;if(!aOk)return 1;if(!bOk)return -1;return (av-bv)*dir})}return rows}
function metricCell(items,field){const vals=items.map(p=>Number(p[field])).filter(Number.isFinite);if(!vals.length)return '--';const wins=vals.filter(v=>v>0).length,avg=vals.reduce((a,b)=>a+b,0)/vals.length;return `<span class="${pctClass(avg)}">${Math.round(wins/vals.length*100)}% / ${avg>=0?'+':''}${avg.toFixed(2)}%</span>`}
function strategyDiagnosis(items){const vals=items.map(p=>Number(p.m20_pct)).filter(Number.isFinite);if(vals.length<5)return {key:'diag_watch',cls:'dim'};const wr=vals.filter(v=>v>0).length/vals.length,avg=vals.reduce((a,b)=>a+b,0)/vals.length;if(wr>=0.58&&avg>=0.05)return {key:'diag_keep',cls:'t-up'};if(wr<=0.42&&avg<=-0.05)return {key:'diag_pause',cls:'t-down'};return {key:'diag_watch',cls:'dim'}}
function updateStrategyStats(probes){const tbody=$('tb-strategy-stats');if(!tbody)return;const groups=new Map();(probes||[]).forEach(p=>{const name=String(p.signal||p.regime||'Unknown');if(!groups.has(name))groups.set(name,[]);groups.get(name).push(p)});const rows=[...groups.entries()].map(([name,items])=>({name,items,m20:items.map(p=>Number(p.m20_pct)).filter(Number.isFinite)})).map(r=>({...r,m20Avg:r.m20.length?r.m20.reduce((a,b)=>a+b,0)/r.m20.length:-999})).sort((a,b)=>b.m20Avg-a.m20Avg).slice(0,12);tbody.innerHTML=rows.map(r=>{const diag=strategyDiagnosis(r.items);return `<tr class="strategy-row ${r.name===strategyFilter?'selected':''}" data-strategy="${encodeURIComponent(r.name)}"><td>${escapeHtml(r.name)}</td><td>${r.items.length}</td><td>${metricCell(r.items,'m5_pct')}</td><td>${metricCell(r.items,'m10_pct')}</td><td>${metricCell(r.items,'m20_pct')}</td><td class="${diag.cls}">${t(diag.key,diag.key)}</td></tr>`}).join('')||`<tr><td colspan="6" class="dim">${t('no_signal_probes','No signal tracking')}</td></tr>`;tbody.querySelectorAll('.strategy-row').forEach(row=>row.addEventListener('click',()=>{strategyFilter=decodeURIComponent(row.dataset.strategy||'');selectedProbeKey='';selectedProbe=null;updateProbes((lastState&&lastState.signal_probes)||[]);const table=$('tb-probes');if(table)table.scrollIntoView({behavior:'smooth',block:'center'})}))}
function selectProbeByKey(key){const probes=(lastState&&lastState.signal_probes)||[];selectedProbeKey=key;selectedProbe=probes.find(p=>probeKey(p)===key)||null;if(selectedProbe){showSignals=true;const sig=$('toggle-signals');if(sig)sig.classList.add('active');const wrap=$('chart-wrap');if(wrap)wrap.scrollIntoView({behavior:'smooth',block:'center'})}drawChart();document.querySelectorAll('.probe-row').forEach(row=>row.classList.toggle('selected',row.dataset.probeKey===selectedProbeKey))}
function selectChartSignalAt(clientX,clientY){const wrap=$('chart-wrap');if(!wrap||!chartSignalHitboxes.length)return false;const rect=wrap.getBoundingClientRect(),x=clientX-rect.left,y=clientY-rect.top;let best=null,bestDist=999;for(const h of chartSignalHitboxes){const d=Math.hypot(x-h.x,y-h.y);if(d<h.r&&d<bestDist){best=h;bestDist=d}}if(best){selectProbeByKey(best.key);return true}return false}
function chartBucketKey(v){const s=String(v||'').replace('T',' ');const m=s.match(/(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2})/);if(!m)return minuteKey(v);const y=m[1],mo=m[2],d=m[3],hh=Number(m[4]),mm=Number(m[5]);const pad=n=>String(n).padStart(2,'0');if(chartTf==='5m')return `${y}-${mo}-${d} ${pad(hh)}:${pad(Math.floor(mm/5)*5)}`;if(chartTf==='10m')return `${y}-${mo}-${d} ${pad(hh)}:${pad(Math.floor(mm/10)*10)}`;if(chartTf==='1h')return `${y}-${mo}-${d} ${pad(hh)}:00`;if(chartTf==='2h')return `${y}-${mo}-${d} ${pad(Math.floor(hh/2)*2)}:00`;if(chartTf==='3h')return `${y}-${mo}-${d} ${pad(Math.floor(hh/3)*3)}:00`;if(chartTf==='4h')return `${y}-${mo}-${d} ${pad(Math.floor(hh/4)*4)}:00`;if(chartTf==='1d')return `${y}-${mo}-${d}`;if(chartTf==='1mo')return `${y}-${mo}`;return `${y}-${mo}-${d} ${pad(hh)}:${pad(mm)}`}
function fmtTime(v){const s=String(v||'');if(/^\d{4}-W\d{2}$/.test(s))return s.replace(/^\d{4}-/,'');if(/^\d{4}-\d{2}$/.test(s))return s.slice(2);if(/^\d{4}-\d{2}-\d{2}$/.test(s))return s.slice(5);return s.includes(' ')?s.split(' ')[1].slice(0,5):s.slice(11,16)||s.slice(0,5)}
function limitForTf(){if(chartTf==='1m')return 420;if(chartTf==='5m'||chartTf==='10m'||chartTf==='1h')return 260;return 180}
async function loadCandles(){try{const day=chartDate?`&date=${encodeURIComponent(chartDate)}`:'';const res=await fetch(`/api/candles?symbol=${encodeURIComponent(chartSymbol)}&limit=${limitForTf()}&timeframe=${encodeURIComponent(chartTf)}${day}`,{cache:'no-store'});const data=await res.json();chartCandles=data.candles||[];lastCandleDate=data.date||'';chartTf=data.timeframe||chartTf;chartSymbol=data.symbol||chartSymbol;loadChartDrawings();drawChart()}catch(e){console.warn('load candles failed',e)}}
async function loadSymbols(){try{const res=await fetch('/api/symbols',{cache:'no-store'});const data=await res.json();const select=$('chart-symbol');if(!select)return;select.innerHTML=(data.symbols||['QQQ.US']).map(s=>`<option value="${s}">${s}</option>`).join('');select.value=chartSymbol;await loadCandleDates()}catch(e){}}
async function loadCandleDates(){try{const res=await fetch(`/api/candle_dates?symbol=${encodeURIComponent(chartSymbol)}`,{cache:'no-store'});const data=await res.json();const select=$('chart-date');if(!select)return;const dates=data.dates||[];select.innerHTML=`<option value="">${t('latest','Latest')}</option>`+dates.slice().reverse().map(d=>`<option value="${d}">${d}</option>`).join('');if(chartDate&&dates.includes(chartDate))select.value=chartDate;else{chartDate='';select.value=''}}catch(e){}}
async function loadConfig(){try{const res=await fetch('/api/config',{cache:'no-store'});configSnapshot=await res.json();renderConfig()}catch(e){}}
async function loadOrders(){try{const res=await fetch('/api/orders?limit=30',{cache:'no-store'});renderOrders(await res.json())}catch(e){console.warn('orders failed',e)}}
function setupControls(){document.querySelectorAll('.tf-btn[data-tf]').forEach(btn=>{btn.addEventListener('click',()=>{document.querySelectorAll('.tf-btn[data-tf]').forEach(x=>x.classList.remove('active'));btn.classList.add('active');chartTf=btn.dataset.tf||'1m';loadCandles()})});document.querySelectorAll('[data-review-period]').forEach(btn=>{btn.addEventListener('click',()=>{document.querySelectorAll('[data-review-period]').forEach(x=>x.classList.remove('active'));btn.classList.add('active');reviewPeriod=btn.dataset.reviewPeriod||'day';loadReviewSummary()})});const select=$('chart-symbol');if(select){select.addEventListener('change',async()=>{chartSymbol=select.value||'QQQ.US';chartDate='';await loadCandleDates();loadCandles()})}const dateSelect=$('chart-date');if(dateSelect){dateSelect.addEventListener('change',()=>{chartDate=dateSelect.value||'';loadCandles()})}const chartWrap=$('chart-wrap');if(chartWrap){chartWrap.addEventListener('mousemove',e=>{if(!chartCandles.length)return;const rect=chartWrap.getBoundingClientRect(),left=42,right=78,plotW=rect.width-left-right,n=chartCandles.length,step=plotW/Math.max(n,1);chartHoverIndex=Math.max(0,Math.min(n-1,Math.round((e.clientX-rect.left-left-step/2)/step)));chartHoverY=e.clientY-rect.top;drawChart()});chartWrap.addEventListener('click',e=>{selectChartSignalAt(e.clientX,e.clientY)});chartWrap.addEventListener('mouseleave',()=>{chartHoverIndex=-1;drawChart()})}const filter=$('probe-filter');if(filter){filter.addEventListener('change',()=>{probeFilter=filter.value||'all';updateProbes((lastState&&lastState.signal_probes)||[])})}const sort=$('probe-sort');if(sort){sort.addEventListener('change',()=>{probeSort=sort.value||'time_desc';updateProbes((lastState&&lastState.signal_probes)||[])})}const clearStrategy=$('clear-strategy-filter');if(clearStrategy){clearStrategy.addEventListener('click',()=>{strategyFilter='';updateProbes((lastState&&lastState.signal_probes)||[])})}const sig=$('toggle-signals');if(sig){sig.addEventListener('click',()=>{showSignals=!showSignals;sig.classList.toggle('active',showSignals);const tool=$('tool-signals');if(tool)tool.classList.toggle('active',showSignals);drawChart()})}const toolSignals=$('tool-signals');if(toolSignals){toolSignals.addEventListener('click',()=>{showSignals=!showSignals;toolSignals.classList.toggle('active',showSignals);const main=$('toggle-signals');if(main)main.classList.toggle('active',showSignals);drawChart()})}const toolLevels=$('tool-levels');if(toolLevels){toolLevels.addEventListener('click',()=>{showKeyLevels=!showKeyLevels;toolLevels.classList.toggle('active',showKeyLevels);drawChart()})}const toolMa=$('tool-ma');if(toolMa){toolMa.addEventListener('click',()=>{showMAs=!showMAs;toolMa.classList.toggle('active',showMAs);drawChart()})}const toolSession=$('tool-session');if(toolSession){toolSession.addEventListener('click',()=>{showSession=!showSession;toolSession.classList.toggle('active',showSession);drawChart()})}const toolReset=$('tool-reset');if(toolReset){toolReset.addEventListener('click',()=>{chartHoverIndex=-1;selectedProbeKey='';selectedProbe=null;document.querySelectorAll('.probe-row').forEach(row=>row.classList.remove('selected'));drawChart()})}const refresh=$('toggle-refresh');if(refresh){refresh.addEventListener('click',()=>{autoRefresh=!autoRefresh;refresh.classList.toggle('active',autoRefresh);setRefreshTimer()})}const lang=$('lang-select');if(lang){lang.addEventListener('change',()=>{currentLang=lang.value||'zh';localStorage.setItem('dashboard_lang',currentLang);applyI18n()})}}
const setupControlsBase=setupControls;setupControls=function(){setupControlsBase();document.querySelectorAll('.chart-tools .tool-btn').forEach(btn=>{if(!btn.dataset.stopBound){btn.dataset.stopBound='1';btn.addEventListener('click',e=>e.stopPropagation())}});const chartWrap=$('chart-wrap');if(chartWrap&&!chartWrap.dataset.drawControls){chartWrap.dataset.drawControls='1';chartWrap.addEventListener('click',e=>{if(e.target&&e.target.closest&&e.target.closest('.chart-tools'))return;if(!chartDrawMode)return;e.preventDefault();e.stopImmediatePropagation();handleChartDrawClick(e)},true)}const toolTrend=$('tool-trend');if(toolTrend&&!toolTrend.dataset.bound){toolTrend.dataset.bound='1';toolTrend.addEventListener('click',()=>{chartDrawMode=!chartDrawMode;chartDrawStart=null;toolTrend.classList.toggle('active',chartDrawMode);drawChart()})}const undoDrawing=$('tool-undo-drawing');if(undoDrawing&&!undoDrawing.dataset.bound){undoDrawing.dataset.bound='1';undoDrawing.addEventListener('click',()=>{chartDrawings.pop();chartDrawStart=null;saveChartDrawings();drawChart()})}const clearDrawings=$('tool-clear-drawings');if(clearDrawings&&!clearDrawings.dataset.bound){clearDrawings.dataset.bound='1';clearDrawings.addEventListener('click',()=>{chartDrawings=[];chartDrawStart=null;saveChartDrawings();drawChart()})}const toolReset=$('tool-reset');if(toolReset&&!toolReset.dataset.drawReset){toolReset.dataset.drawReset='1';toolReset.addEventListener('click',()=>{chartDrawStart=null;drawChart()})}}
function drawingStorageKey(){return `qqq_chart_drawings:${chartSymbol}:${lastCandleDate||chartDate||'latest'}:${chartTf}`}
function saveChartDrawings(){try{localStorage.setItem(drawingStorageKey(),JSON.stringify(chartDrawings.slice(-80)))}catch(e){}}
function loadChartDrawings(){try{const raw=localStorage.getItem(drawingStorageKey());chartDrawings=raw?JSON.parse(raw):[];if(!Array.isArray(chartDrawings))chartDrawings=[]}catch(e){chartDrawings=[]}chartDrawStart=null}
function setRefreshTimer(){if(refreshTimer){clearInterval(refreshTimer);refreshTimer=null}if(autoRefresh)refreshTimer=setInterval(loadCandles,15000)}
function drawLine(ctx,pts,color,width=1.2,dash=[]){if(!showMAs&&['#e4b822','#ff8b3d','#4a91ff','rgba(180,200,240,.55)'].includes(color))return;ctx.save();ctx.strokeStyle=color;ctx.lineWidth=width;ctx.setLineDash(dash);ctx.beginPath();let started=false;for(const p of pts){if(!Number.isFinite(p.x)||!Number.isFinite(p.y))continue;if(!started){ctx.moveTo(p.x,p.y);started=true}else ctx.lineTo(p.x,p.y)}ctx.stroke();ctx.restore()}
function drawLabel(ctx,text,x,y,color,above=true){const maxText=String(text||'').slice(0,34);ctx.save();ctx.font='11px Consolas,monospace';const padX=5,tw=ctx.measureText(maxText).width,bw=tw+padX*2,bh=18;let bx=x-bw/2,by=above?y-34:y+16;bx=Math.max(48,Math.min(bx,ctx.canvas.width/(window.devicePixelRatio||1)-bw-84));ctx.fillStyle='rgba(7,16,28,.78)';ctx.strokeStyle=color;ctx.lineWidth=1;ctx.beginPath();ctx.rect(bx,by,bw,bh);ctx.fill();ctx.stroke();ctx.fillStyle=color;ctx.fillText(maxText,bx+padX,by+13);ctx.restore()}
function compactSignalLabel(p){const dir=p.dir==='call'?'CALL':p.dir==='put'?'PUT':'';const source=p.source==='shadow_live'?'OPEN':p.source==='shadow'?'WATCH':'SIG';const sig=String(p.signal||p.regime||'').replace(/_/g,' ');return `${source} ${dir}${sig?` - ${sig}`:''}`}
function validStockPrice(v){const n=Number(v);return Number.isFinite(n)&&n>100&&n<2000}
function drawPriceLevel(ctx,y,label,color,w,left,right,dash=[6,5]){ctx.save();ctx.strokeStyle=color;ctx.fillStyle=color;ctx.lineWidth=1.2;ctx.setLineDash(dash);ctx.beginPath();ctx.moveTo(left,y);ctx.lineTo(w-right,y);ctx.stroke();ctx.setLineDash([]);ctx.fillRect(w-right+4,y-9,70,18);ctx.fillStyle='#07101c';ctx.font='11px Consolas,monospace';ctx.fillText(label,w-right+8,y+4);ctx.restore()}
function emaSeries(values,period){const k=2/(period+1);let prev=null;return values.map(v=>{prev=prev===null?v:v*k+prev*(1-k);return prev})}
function calcStudies(){const closes=chartCandles.map(c=>Number(c.close)||0),ema12=emaSeries(closes,12),ema26=emaSeries(closes,26),macd=closes.map((_,i)=>ema12[i]-ema26[i]),sig=emaSeries(macd,9),hist=macd.map((v,i)=>v-sig[i]);let gains=[],losses=[],rsi=[];for(let i=0;i<closes.length;i++){if(i===0){rsi.push(50);continue}const ch=closes[i]-closes[i-1];gains.push(Math.max(ch,0));losses.push(Math.max(-ch,0));const g=gains.slice(-14).reduce((a,b)=>a+b,0)/Math.min(gains.length,14),l=losses.slice(-14).reduce((a,b)=>a+b,0)/Math.min(losses.length,14);rsi.push(l===0?100:100-(100/(1+g/l)))}return {hist,macd,sig,rsi}}
function drawMiniChart(canvasId,values,opts={}){const canvas=$(canvasId);if(!canvas)return;const box=canvas.parentElement.getBoundingClientRect(),dpr=window.devicePixelRatio||1,w=Math.max(1,Math.floor(box.width)),h=Math.max(1,Math.floor(box.height));canvas.width=w*dpr;canvas.height=h*dpr;canvas.style.width=w+'px';canvas.style.height=h+'px';const ctx=canvas.getContext('2d');ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,w,h);ctx.fillStyle='#07101c';ctx.fillRect(0,0,w,h);ctx.strokeStyle='rgba(255,255,255,.06)';ctx.lineWidth=1;for(let i=1;i<4;i++){const y=i*h/4;ctx.beginPath();ctx.moveTo(0,y);ctx.lineTo(w,y);ctx.stroke()}const clean=(values||[]).filter(Number.isFinite);if(clean.length<2)return;const max=Math.max(...clean,opts.max??-Infinity),min=Math.min(...clean,opts.min??Infinity),pad=(max-min)*0.12||1,hi=opts.max??max+pad,lo=opts.min??min-pad,left=38,right=12,top=20,bottom=18,plotW=w-left-right,plotH=h-top-bottom,x=i=>left+i*plotW/Math.max(values.length-1,1),y=v=>top+(hi-v)/(hi-lo)*plotH;if(opts.zero){const zy=y(0);ctx.strokeStyle='rgba(255,255,255,.18)';ctx.beginPath();ctx.moveTo(left,zy);ctx.lineTo(w-right,zy);ctx.stroke()}ctx.font='11px Consolas,monospace';ctx.fillStyle='rgba(200,210,240,.55)';ctx.fillText(hi.toFixed(opts.fixed??1),6,top+4);ctx.fillText(lo.toFixed(opts.fixed??1),6,h-bottom);if(opts.bars){const step=plotW/Math.max(values.length,1),barW=Math.max(2,Math.min(7,step*.64)),zy=y(0);values.forEach((v,i)=>{if(!Number.isFinite(v))return;ctx.fillStyle=v>=0?'rgba(0,208,132,.82)':'rgba(255,79,109,.82)';ctx.fillRect(x(i)-barW/2,Math.min(zy,y(v)),barW,Math.max(1,Math.abs(zy-y(v))))})}else{ctx.strokeStyle=opts.color||'#4a91ff';ctx.lineWidth=1.4;ctx.beginPath();let started=false;values.forEach((v,i)=>{if(!Number.isFinite(v))return;if(!started){ctx.moveTo(x(i),y(v));started=true}else ctx.lineTo(x(i),y(v))});ctx.stroke();(opts.levels||[]).forEach(level=>{ctx.strokeStyle=level.color;ctx.setLineDash([5,5]);ctx.beginPath();ctx.moveTo(left,y(level.value));ctx.lineTo(w-right,y(level.value));ctx.stroke();ctx.setLineDash([])})}}
function drawStudyCharts(){if(!chartCandles.length){drawMiniChart('macd-chart',[]);drawMiniChart('rsi-chart',[]);return}const s=calcStudies(),lastHist=s.hist[s.hist.length-1]||0,lastRsi=s.rsi[s.rsi.length-1]||0;drawMiniChart('macd-chart',s.hist,{bars:true,zero:true,fixed:3});drawMiniChart('rsi-chart',s.rsi,{min:0,max:100,color:'#4a91ff',levels:[{value:70,color:'rgba(255,79,109,.5)'},{value:50,color:'rgba(245,197,66,.4)'},{value:30,color:'rgba(0,208,132,.5)'}]});const mn=$('macd-note'),rn=$('rsi-note');if(mn){mn.textContent=`hist ${lastHist>=0?'+':''}${lastHist.toFixed(3)}`;mn.className='study-chart-note '+(lastHist>=0?'t-up':'t-down')}if(rn){rn.textContent=`RSI ${lastRsi.toFixed(1)}`;rn.className='study-chart-note '+(lastRsi>=70?'t-up':lastRsi<=30?'t-down':'')}}
function drawChart(){const canvas=$('k-chart'),wrap=$('chart-wrap'),empty=$('chart-empty');if(!canvas||!wrap)return;const rect=wrap.getBoundingClientRect(),dpr=window.devicePixelRatio||1;canvas.width=Math.max(1,Math.floor(rect.width*dpr));canvas.height=Math.max(1,Math.floor(rect.height*dpr));canvas.style.width=rect.width+'px';canvas.style.height=rect.height+'px';const ctx=canvas.getContext('2d');ctx.setTransform(dpr,0,0,dpr,0,0);const w=rect.width,h=rect.height;chartSignalHitboxes=[];ctx.clearRect(0,0,w,h);ctx.fillStyle='#07101c';ctx.fillRect(0,0,w,h);if(!chartCandles.length){if(empty)empty.style.display='flex';return}if(empty)empty.style.display='none';const left=42,right=78,top=18,bottom=30,volH=Math.max(80,h*.22),gap=18,plotW=w-left-right,priceH=h-top-bottom-volH-gap;const extra=[];((lastState&&lastState.positions)||[]).forEach(p=>{['entry_price','sl','tp'].forEach(k=>{if(validStockPrice(p[k]))extra.push(Number(p[k]))})});const highs=chartCandles.flatMap(c=>[c.high,c.vwap,c.ema9,c.ema21,c.sma20].filter(Number.isFinite)).concat(extra);const lows=chartCandles.flatMap(c=>[c.low,c.vwap,c.ema9,c.ema21,c.sma20].filter(Number.isFinite)).concat(extra);let maxP=Math.max(...highs),minP=Math.min(...lows);const pad=(maxP-minP)*0.08||1;maxP+=pad;minP-=pad;const maxV=Math.max(...chartCandles.map(c=>Number(c.volume)||0),1),n=chartCandles.length,step=plotW/Math.max(n,1),candleW=Math.max(2,Math.min(8,step*.62));const x=i=>left+i*step+step/2,y=p=>top+(maxP-p)/(maxP-minP)*priceH,yv=v=>top+priceH+gap+volH-(v/maxV)*volH;ctx.strokeStyle='rgba(255,255,255,.055)';ctx.lineWidth=1;ctx.font='11px Consolas,monospace';ctx.fillStyle='rgba(200,210,240,.55)';for(let i=0;i<=6;i++){const yy=top+i*priceH/6;ctx.beginPath();ctx.moveTo(left,yy);ctx.lineTo(w-right,yy);ctx.stroke();ctx.fillText((maxP-(maxP-minP)*i/6).toFixed(2),8,yy+4)}for(let i=0;i<n;i+=Math.max(1,Math.floor(n/12))){const xx=x(i);ctx.beginPath();ctx.moveTo(xx,top);ctx.lineTo(xx,h-bottom);ctx.stroke();ctx.fillText(fmtTime(chartCandles[i].time),xx-18,h-10)}chartCandles.forEach((c,i)=>{const xx=x(i),up=c.close>=c.open,color=up?'#00c987':'#ff4f6d';ctx.strokeStyle=color;ctx.fillStyle=color;ctx.globalAlpha=.95;ctx.beginPath();ctx.moveTo(xx,y(c.high));ctx.lineTo(xx,y(c.low));ctx.stroke();const bodyTop=y(Math.max(c.open,c.close)),bodyBot=y(Math.min(c.open,c.close));ctx.fillRect(xx-candleW/2,bodyTop,candleW,Math.max(1,bodyBot-bodyTop));ctx.globalAlpha=.28;ctx.fillRect(xx-candleW/2,yv(c.volume),candleW,top+priceH+gap+volH-yv(c.volume));ctx.globalAlpha=1});drawLine(ctx,chartCandles.map((c,i)=>({x:x(i),y:y(c.vwap)})),'#e4b822');drawLine(ctx,chartCandles.map((c,i)=>({x:x(i),y:y(c.ema9)})),'#ff8b3d');drawLine(ctx,chartCandles.map((c,i)=>({x:x(i),y:y(c.ema21)})),'#4a91ff',1.3);drawLine(ctx,chartCandles.map((c,i)=>({x:x(i),y:y(c.sma20)})),'rgba(180,200,240,.55)');const last=chartCandles[chartCandles.length-1],lastY=y(last.close);drawPriceLevel(ctx,lastY,last.close.toFixed(2),last.close>=last.open?'#00c987':'#ff4f6d',w,left,right,[3,3]);((lastState&&lastState.positions)||[]).forEach(p=>{if(validStockPrice(p.entry_price))drawPriceLevel(ctx,y(Number(p.entry_price)),'ENTRY '+Number(p.entry_price).toFixed(2),'#f5c542',w,left,right,[7,5]);if(validStockPrice(p.sl))drawPriceLevel(ctx,y(Number(p.sl)),'SL '+Number(p.sl).toFixed(2),'#ff4f6d',w,left,right,[5,5]);if(validStockPrice(p.tp))drawPriceLevel(ctx,y(Number(p.tp)),'TP '+Number(p.tp).toFixed(2),'#00d084',w,left,right,[5,5])});const indexByMinute=new Map(chartCandles.map((c,i)=>[chartBucketKey(c.time),i]));((showSignals&&lastState&&lastState.signal_probes)||[]).slice(-80).forEach(p=>{const i=indexByMinute.get(chartBucketKey(p.entry_time||p.time));if(i===undefined)return;const isCall=p.dir==='call',xx=x(i),yy=y(Number(p.entry_price)||chartCandles[i].close),key=probeKey(p),selected=key===selectedProbeKey;chartSignalHitboxes.push({x:xx,y:yy,r:selected?22:17,key});ctx.fillStyle=isCall?'#f5c542':'#4a91ff';ctx.strokeStyle=selected?'#00e5ff':'rgba(7,16,28,.9)';ctx.lineWidth=selected?5:3;ctx.beginPath();if(isCall){ctx.moveTo(xx,yy-11);ctx.lineTo(xx-7,yy+4);ctx.lineTo(xx+7,yy+4)}else{ctx.moveTo(xx,yy+11);ctx.lineTo(xx-7,yy-4);ctx.lineTo(xx+7,yy-4)}ctx.closePath();ctx.stroke();ctx.fill();if(selected){ctx.strokeStyle='rgba(0,229,255,.7)';ctx.lineWidth=1.5;ctx.beginPath();ctx.arc(xx,yy,16,0,Math.PI*2);ctx.stroke()}ctx.fillStyle=isCall?'#f5c542':'#8ebcff';ctx.fillText(isCall?'CALL':'PUT',xx+8,yy+4);drawLabel(ctx,compactSignalLabel(p),xx,yy,isCall?'#f5c542':'#8ebcff',isCall)});$('ohlc-box').innerHTML=`<b>${chartSymbol} ${(chartTf||'1m').toUpperCase()} ${lastCandleDate||''}</b><span>&#x5F00; ${last.open.toFixed(2)}</span><span class="up">&#x9AD8; ${last.high.toFixed(2)}</span><span class="down">&#x4F4E; ${last.low.toFixed(2)}</span><span>&#x6536; ${last.close.toFixed(2)}</span><span>&#x91CF; ${(last.volume/1000).toFixed(1)}K</span>`;$('indicator-box').innerHTML=`<div><b>VWAP</b> <span style="color:#e4b822">${last.vwap.toFixed(3)}</span></div><div><b>EMA</b> <span style="color:#ff8b3d">EMA9:${last.ema9.toFixed(3)}</span> <span style="color:#4a91ff">EMA21:${last.ema21.toFixed(3)}</span></div><div><b>SMA20</b> <span>${last.sma20.toFixed(3)}</span></div><div><b>&#x4FE1;&#x53F7;</b> &#x5B9E;&#x76D8;/&#x5F71;&#x5B50;&#x5165;&#x573A;&#x6807;&#x8BB0;</div>`}
function drawSelectedProbe(){const box=$('review-box'),canvas=$('k-chart'),wrap=$('chart-wrap');if(!box||!canvas||!wrap)return;if(!selectedProbe||!chartCandles.length){box.style.display='none';return}const key=chartBucketKey(selectedProbe.entry_time||selectedProbe.time),i=chartCandles.findIndex(c=>chartBucketKey(c.time)===key);if(i<0){box.style.display='none';return}const rect=wrap.getBoundingClientRect(),ctx=canvas.getContext('2d'),w=rect.width,h=rect.height,left=42,right=78,top=18,bottom=30,volH=Math.max(80,h*.22),gap=18,priceH=h-top-bottom-volH-gap,plotW=w-left-right;const highs=chartCandles.flatMap(c=>[c.high,c.vwap,c.ema9,c.ema21,c.sma20].filter(Number.isFinite));const lows=chartCandles.flatMap(c=>[c.low,c.vwap,c.ema9,c.ema21,c.sma20].filter(Number.isFinite));let maxP=Math.max(...highs),minP=Math.min(...lows);const pad=(maxP-minP)*0.08||1;maxP+=pad;minP-=pad;const step=plotW/Math.max(chartCandles.length,1),x=left+i*step+step/2,y=top+(maxP-(Number(selectedProbe.entry_price)||chartCandles[i].close))/(maxP-minP)*priceH;ctx.save();ctx.strokeStyle='rgba(0,229,255,.9)';ctx.fillStyle='rgba(0,229,255,.14)';ctx.lineWidth=1.6;ctx.setLineDash([5,5]);ctx.beginPath();ctx.moveTo(x,top);ctx.lineTo(x,top+priceH+gap+volH);ctx.stroke();ctx.setLineDash([]);ctx.beginPath();ctx.arc(x,y,13,0,Math.PI*2);ctx.fill();ctx.stroke();ctx.fillStyle='#00e5ff';ctx.font='12px Consolas,monospace';ctx.fillText(t('selected','Selected'),Math.min(x+16,w-right-80),Math.max(top+16,y-16));ctx.restore();const dir=selectedProbe.dir==='call'?'CALL':'PUT';box.style.display='block';box.innerHTML=`<div class="review-title">${t('review_selected','Selected Signal')} - ${dir}</div><div>${selectedProbe.signal||selectedProbe.regime||'--'} @ ${selectedProbe.entry_price?'$'+Number(selectedProbe.entry_price).toFixed(2):'--'}</div><div class="review-grid"><div class="review-chip ${pctClass(selectedProbe.m5_pct)}">+5<br>${fmtProbePct(selectedProbe.m5_pct)}</div><div class="review-chip ${pctClass(selectedProbe.m10_pct)}">+10<br>${fmtProbePct(selectedProbe.m10_pct)}</div><div class="review-chip ${pctClass(selectedProbe.m20_pct)}">+20<br>${fmtProbePct(selectedProbe.m20_pct)}</div></div>`}
function drawChartCrosshair(){if(chartHoverIndex<0||!chartCandles.length)return;const canvas=$('k-chart'),wrap=$('chart-wrap');if(!canvas||!wrap)return;const rect=wrap.getBoundingClientRect(),ctx=canvas.getContext('2d'),w=rect.width,h=rect.height,left=42,right=78,top=18,bottom=30,volH=Math.max(80,h*.22),gap=18,priceH=h-top-bottom-volH-gap,plotW=w-left-right,n=chartCandles.length,step=plotW/Math.max(n,1),idx=Math.max(0,Math.min(n-1,chartHoverIndex)),x=left+idx*step+step/2,y=Math.max(top,Math.min(top+priceH+gap+volH,chartHoverY));ctx.save();ctx.strokeStyle='rgba(0,229,255,.38)';ctx.lineWidth=1;ctx.setLineDash([4,4]);ctx.beginPath();ctx.moveTo(x,top);ctx.lineTo(x,h-bottom);ctx.stroke();ctx.beginPath();ctx.moveTo(left,y);ctx.lineTo(w-right,y);ctx.stroke();ctx.setLineDash([]);ctx.fillStyle='rgba(0,229,255,.16)';ctx.fillRect(Math.max(left,x-step/2),top,Math.max(2,step),priceH+gap+volH);ctx.fillStyle='#00e5ff';ctx.font='11px Consolas,monospace';ctx.fillText(fmtTime(chartCandles[idx].time),Math.min(x+8,w-right-70),h-10);ctx.restore()}
function chartGeom(){const canvas=$('k-chart'),wrap=$('chart-wrap');if(!canvas||!wrap||!chartCandles.length)return null;const rect=wrap.getBoundingClientRect(),w=rect.width,h=rect.height,left=42,right=78,top=18,bottom=30,volH=Math.max(80,h*.22),gap=18,plotW=w-left-right,priceH=h-top-bottom-volH-gap;const highs=chartCandles.flatMap(c=>[c.high,c.vwap,c.ema9,c.ema21,c.sma20].filter(Number.isFinite));const lows=chartCandles.flatMap(c=>[c.low,c.vwap,c.ema9,c.ema21,c.sma20].filter(Number.isFinite));let maxP=Math.max(...highs),minP=Math.min(...lows);const pad=(maxP-minP)*0.08||1;maxP+=pad;minP-=pad;const step=plotW/Math.max(chartCandles.length,1);return {canvas,ctx:canvas.getContext('2d'),w,h,left,right,top,bottom,volH,gap,plotW,priceH,maxP,minP,step,x:i=>left+i*step+step/2,y:p=>top+(maxP-p)/(maxP-minP)*priceH}}
function minuteOfDay(v){const s=String(v||'');const m=s.match(/(\d{2}):(\d{2})/);return m?Number(m[1])*60+Number(m[2]):null}
function drawSessionBands(g){if(!showSession||!g||!['1m','5m','10m'].includes(chartTf))return;let start=null,end=null;chartCandles.forEach((c,i)=>{const m=minuteOfDay(c.time);if(m!==null&&m<570){if(start===null)start=i;end=i}});if(start===null)return;const x1=Math.max(g.left,g.x(start)-g.step/2),x2=Math.min(g.w-g.right,g.x(end)+g.step/2);g.ctx.save();g.ctx.fillStyle='rgba(74,145,255,.07)';g.ctx.fillRect(x1,g.top,x2-x1,g.priceH+g.gap+g.volH);g.ctx.fillStyle='rgba(200,210,240,.45)';g.ctx.font='11px Consolas,monospace';g.ctx.fillText('PRE',x1+8,g.top+g.priceH+g.gap+g.volH-10);g.ctx.restore()}
function drawLeftPriceTag(ctx,x,y,label,color){ctx.save();ctx.font='11px Consolas,monospace';const w=Math.max(78,ctx.measureText(label).width+14);ctx.fillStyle=color;ctx.fillRect(x,y-9,w,18);ctx.fillStyle='#07101c';ctx.fillText(label,x+6,y+4);ctx.restore()}
function drawKeyLevel(g,price,label,color,dash=[7,5]){if(!g||!Number.isFinite(price))return;const y=g.y(price);if(y<g.top||y>g.top+g.priceH)return;g.ctx.save();g.ctx.strokeStyle=color;g.ctx.lineWidth=1.2;g.ctx.setLineDash(dash);g.ctx.beginPath();g.ctx.moveTo(g.left,y);g.ctx.lineTo(g.w-g.right,y);g.ctx.stroke();g.ctx.setLineDash([]);drawLeftPriceTag(g.ctx,8,y,`${label} ${price.toFixed(2)}`,color);g.ctx.restore()}
function drawKeyLevels(g){if(!showKeyLevels||!g||!chartCandles.length)return;const first=chartCandles[0],last=chartCandles[chartCandles.length-1],high=Math.max(...chartCandles.map(c=>Number(c.high)||0)),low=Math.min(...chartCandles.map(c=>Number(c.low)||Infinity)),open=Number(first.open),mid=(high+low)/2,q25=low+(high-low)*0.25,q75=low+(high-low)*0.75;drawKeyLevel(g,open,'Open','#8ebcff',[4,4]);drawKeyLevel(g,high,'Day High','#00d084',[6,5]);drawKeyLevel(g,low,'Day Low','#ff4f6d',[6,5]);drawKeyLevel(g,mid,'Mid','#f5c542',[3,5]);drawKeyLevel(g,q75,'75%','#00d084',[2,6]);drawKeyLevel(g,q25,'25%','#ff4f6d',[2,6]);if(Number.isFinite(last.vwap))drawKeyLevel(g,Number(last.vwap),'VWAP','#e4b822',[7,4])}
function drawPercentAxis(g){if(!showPercentAxis||!g||!chartCandles.length)return;const base=Number(chartCandles[0].open)||Number(chartCandles[0].close)||0;if(!base)return;g.ctx.save();g.ctx.font='11px Consolas,monospace';g.ctx.fillStyle='rgba(200,210,240,.55)';g.ctx.strokeStyle='rgba(255,255,255,.08)';for(let i=0;i<=6;i++){const price=g.maxP-(g.maxP-g.minP)*i/6,pct=(price-base)/base*100,y=g.y(price),txt=(pct>=0?'+':'')+pct.toFixed(2)+'%';g.ctx.fillText(txt,g.w-g.right+10,y+4)}const zeroY=g.y(base);if(zeroY>=g.top&&zeroY<=g.top+g.priceH){g.ctx.strokeStyle='rgba(245,197,66,.35)';g.ctx.setLineDash([5,5]);g.ctx.beginPath();g.ctx.moveTo(g.left,zeroY);g.ctx.lineTo(g.w-g.right,zeroY);g.ctx.stroke()}g.ctx.restore()}
function chartPointFromEvent(e){const g=chartGeom(),wrap=$('chart-wrap');if(!g||!wrap)return null;const rect=wrap.getBoundingClientRect(),px=e.clientX-rect.left,py=e.clientY-rect.top;if(px<g.left||px>g.w-g.right||py<g.top||py>g.top+g.priceH)return null;const idx=Math.max(0,Math.min(chartCandles.length-1,Math.round((px-g.left-g.step/2)/g.step))),price=g.maxP-(py-g.top)/g.priceH*(g.maxP-g.minP);return {i:idx,price,time:chartCandles[idx]&&chartCandles[idx].time}}
function handleChartDrawClick(e){const p=chartPointFromEvent(e);if(!p)return;if(!chartDrawStart){chartDrawStart=p}else{chartDrawings.push({a:chartDrawStart,b:p});chartDrawStart=null;saveChartDrawings()}drawChart()}
function drawUserLines(g){if(!g)return;const lines=chartDrawings.slice();if(chartDrawStart&&chartHoverIndex>=0){const py=Math.max(g.top,Math.min(g.top+g.priceH,chartHoverY)),idx=Math.max(0,Math.min(chartCandles.length-1,chartHoverIndex)),price=g.maxP-(py-g.top)/g.priceH*(g.maxP-g.minP);lines.push({a:chartDrawStart,b:{i:idx,price,time:chartCandles[idx]&&chartCandles[idx].time},pending:true})}g.ctx.save();g.ctx.font='11px Consolas,monospace';lines.forEach(line=>{const x1=g.x(line.a.i),y1=g.y(line.a.price),x2=g.x(line.b.i),y2=g.y(line.b.price),pct=line.a.price?((line.b.price-line.a.price)/line.a.price*100):0;g.ctx.strokeStyle=line.pending?'rgba(0,229,255,.55)':'rgba(0,229,255,.9)';g.ctx.fillStyle='rgba(0,229,255,.12)';g.ctx.lineWidth=line.pending?1.2:1.8;g.ctx.setLineDash(line.pending?[5,5]:[]);g.ctx.beginPath();g.ctx.moveTo(x1,y1);g.ctx.lineTo(x2,y2);g.ctx.stroke();g.ctx.setLineDash([]);g.ctx.beginPath();g.ctx.arc(x1,y1,4,0,Math.PI*2);g.ctx.arc(x2,y2,4,0,Math.PI*2);g.ctx.fill();g.ctx.fillStyle='#00e5ff';g.ctx.fillText(`${pct>=0?'+':''}${pct.toFixed(2)}%`,Math.min(Math.max(x2+8,g.left),g.w-g.right-54),Math.max(g.top+14,Math.min(g.top+g.priceH-6,y2-8))) });g.ctx.restore()}
function drawChartExtras(){const g=chartGeom();if(!g)return;drawSessionBands(g);drawKeyLevels(g);drawPercentAxis(g);drawUserLines(g)}
const drawChartBase=drawChart;drawChart=function(){drawChartBase();drawChartExtras();drawStudyCharts();if(!chartCandles.length)return;drawChartCrosshair();const last=chartHoverIndex>=0?(chartCandles[Math.max(0,Math.min(chartCandles.length-1,chartHoverIndex))]||chartCandles[chartCandles.length-1]):chartCandles[chartCandles.length-1];$('ohlc-box').innerHTML=`<b>${chartSymbol} ${(chartTf||'1m').toUpperCase()} ${lastCandleDate||''} ${chartHoverIndex>=0?fmtTime(last.time):''}</b><span>${t('open','Open')} ${last.open.toFixed(2)}</span><span class="up">${t('high','High')} ${last.high.toFixed(2)}</span><span class="down">${t('low','Low')} ${last.low.toFixed(2)}</span><span>${t('close','Close')} ${last.close.toFixed(2)}</span><span>${t('volume','Volume')} ${(last.volume/1000).toFixed(1)}K</span>`;$('indicator-box').innerHTML=`<div><b>VWAP</b> <span style="color:#e4b822">${last.vwap.toFixed(3)}</span></div><div><b>EMA</b> <span style="color:#ff8b3d">EMA9:${last.ema9.toFixed(3)}</span> <span style="color:#4a91ff">EMA21:${last.ema21.toFixed(3)}</span></div><div><b>SMA20</b> <span>${last.sma20.toFixed(3)}</span></div><div><b>${t('signal','Signal')}</b> ${t('live_shadow_marks','Live/shadow entry markers')}</div>`;drawSelectedProbe()}
let ws=null,reconnectTimer=null;function connect(){const protocol=location.protocol==='https:'?'wss:':'ws:';ws=new WebSocket(`${protocol}//${location.host}/ws`);ws.onopen=()=>{$('s-conn').textContent=t('connected','Connected');$('dot-conn').className='status-dot dot-green';if(reconnectTimer){clearTimeout(reconnectTimer);reconnectTimer=null}};ws.onmessage=e=>update(JSON.parse(e.data));ws.onclose=()=>{$('s-conn').textContent=t('offline','Offline');$('dot-conn').className='status-dot dot-red';reconnectTimer=setTimeout(connect,3000)};setInterval(()=>{if(ws&&ws.readyState===1)ws.send('ping')},30000)}
function update(d){lastState=d;const trades=d.trades||[],positions=d.positions||[],account=d.account||{},vix=d.vix||{},regime=d.day_market_regime||{},pnl=d.daily_pnl||0;updateStatusText(d);$('dot-engine').className='status-dot '+(d.running?'dot-green':'dot-red');$('clock').textContent=new Date().toLocaleTimeString(currentLang==='zh'?'zh-CN':'en-US',{hour12:false});$('v-pnl').textContent=fmtMoney(pnl);$('v-pnl').className='kv-val '+cls(pnl);$('v-trades').textContent=d.daily_trades||0;const wins=trades.filter(t=>t.pnl_usd>0).length;$('v-wr').textContent=trades.length?Math.round(wins/trades.length*100)+'%':'--';$('v-hold').textContent=positions.length;$('v-qqq').textContent=d.current_price?'$'+d.current_price.toFixed(2):'--';$('m-qqq').textContent=d.current_price?'$'+d.current_price.toFixed(2):'--';$('m-spy').textContent='--';$('v-vix').textContent=vix.vix?vix.vix.toFixed(1):'--';$('m-vix').textContent=vix.vix?vix.vix.toFixed(1):'--';$('m-vix').className=vix.vix&&vix.vix>20?'down':'up';$('v-vix-regime').textContent=vix.regime||'--';$('v-day-regime').textContent=regime.label||regime.type||'--';$('v-equity').textContent=fmtMoney(account.net_assets);$('v-cash').textContent=fmtMoney(account.cash);$('v-power').textContent=fmtMoney(account.buying_power);$('v-uptime').textContent=d.uptime||'--';$('v-candles').textContent=d.candle_count||0;$('v-pos-mult').textContent=vix.position_mult?Number(vix.position_mult).toFixed(1)+'x':'1.0x';updateSignalBox(d.signal);updateEngineBoxes(d.engines||[]);updatePositions(positions);updateTrades(trades);updateProbes(d.signal_probes||[]);updateDiagnostics(d);updateRiskPanel(d);const logBox=$('log-box');logBox.innerHTML=(d.events||[]).map(e=>`<div class="log-line ${e.tag}">[${e.time}] ${e.msg}</div>`).join('');logBox.scrollTop=logBox.scrollHeight;drawChart()}
function updateSignalBox(sig){if(sig){$('sig-icon').textContent=sig.direction==='call'?'CALL':'PUT';$('sig-dir').textContent=sig.direction==='call'?'CALL':'PUT';$('sig-dir').className='signal-dir '+(sig.direction==='call'?'up':'down');$('sig-price').textContent=sig.entry_price?'$'+Number(sig.entry_price).toFixed(2):'--';$('sig-reason').textContent=(sig.engine||'')+': '+(sig.reason||'');const strength=sig.strength||0,el=$('sig-strength');el.style.width=strength+'%';el.className='strength-fill '+(strength>70?'strength-high':strength>40?'strength-mid':'strength-low');return}$('sig-icon').textContent='--';$('sig-dir').textContent=t('no_signal','No signal');$('sig-dir').className='signal-dir dim';$('sig-price').textContent='';$('sig-reason').textContent='';$('sig-strength').style.width='0%'}
function updateEngineBoxes(engines){$('engines').innerHTML=engines.map((e,i)=>`<div class="f-item"><div class="f-dot" style="background:${e.enabled?'var(--green)':'var(--red)'}"></div><div class="f-name">#${i+1} ${e.name}</div><div class="f-val">${t('bars','Bars')}: ${e.bars_count||0}</div></div>`).join('');$('engines-grid').innerHTML=engines.map((e,i)=>`<div class="card engine-card"><div class="priority">#${i+1}</div><div class="card-title">${e.name}</div><div style="font-size:12px;color:var(--muted)">${t('bars','Bars')}: ${e.bars_count||0}</div><div style="margin-top:8px;font-size:11px;color:var(--muted)">${e.enabled?t('enabled','Enabled'):t('disabled','Disabled')}</div></div>`).join('')}
function updatePositions(positions){$('tb-pos').innerHTML=positions.map(p=>`<tr><td>${p.opt_symbol||p.symbol||'--'}</td><td class="${p.dir==='call'?'t-up':'t-down'}">${p.dir==='call'?'CALL':'PUT'}</td><td>${p.contracts||p.quantity||0}</td><td>${p.entry_opt_price?'$'+Number(p.entry_opt_price).toFixed(2):'--'}</td></tr>`).join('')||`<tr><td colspan="4" class="dim">${t('no_position','No position')}</td></tr>`}
function updateTrades(trades){$('tb-trd').innerHTML=trades.map(t=>`<tr><td>${t.time}</td><td class="${t.dir_up?'t-up':'t-down'}">${t.dir}</td><td>${t.opt||'--'}</td><td>${t.ep}</td><td>${t.exit_price}</td><td>${t.qty}</td><td class="${t.pnl_usd>=0?'t-up':'t-down'}">${t.pnl_usd>=0?'+':''}$${Number(t.pnl_usd||0).toFixed(2)}</td></tr>`).join('')||`<tr><td colspan="7" class="dim">${t('no_trades','No trade records')}</td></tr>`}
function updateProbes(probes){updateStrategyStats(probes);const tbody=$('tb-probes'),rows=sortedFilteredProbes(probes).slice(0,40),count=$('probe-count');if(count)count.textContent=`${strategyFilter?strategyFilter+' ':''}${rows.length}/${(probes||[]).length}`;tbody.innerHTML=rows.map(p=>{const raw=p.time||p.entry_time||'',time=String(raw).includes(' ')?String(raw).split(' ').pop().slice(0,8):String(raw).slice(0,8),dirUp=p.dir==='call',src=p.source==='shadow'?'WATCH-':p.source==='shadow_live'?'TEST-':'',key=probeKey(p);const cell=v=>`<td class="${pctClass(v)}">${fmtProbePct(v)}</td>`;return `<tr class="probe-row ${key===selectedProbeKey?'selected':''}" data-probe-key="${key}"><td>${p.id||''}</td><td>${time||'--:--:--'}</td><td title="${p.rejection_reason||p.reason||''}">${src}${escapeHtml(p.signal||p.regime||'QQQ_Breakout')}</td><td class="${dirUp?'t-up':'t-down'}">${dirUp?'CALL':'PUT'}</td><td>${p.entry_price?'$'+Number(p.entry_price).toFixed(2):'--'}</td>${cell(p.m5_pct)}${cell(p.m10_pct)}${cell(p.m20_pct)}</tr>`}).join('')||`<tr><td colspan="8" class="dim">${t('no_signal_probes','No signal tracking')}</td></tr>`;tbody.querySelectorAll('.probe-row').forEach(row=>row.addEventListener('click',()=>selectProbeByKey(row.dataset.probeKey||'')))}
function updateDiagnostics(d){const r=d.day_market_regime||{},v=d.vix||{};$('diag-regime').textContent=r.label||r.type||'--';$('diag-direction').textContent=r.direction||'--';$('diag-vix').textContent=v.regime||'--';$('diag-vix-mult').textContent=v.position_mult?Number(v.position_mult).toFixed(1)+'x':'1.0x';$('diag-signal-count').textContent=(d.signal_probes||[]).length;$('diag-last-signal').textContent=d.signal?`${d.signal.engine} ${d.signal.direction}`:'--'}
function updateRiskPanel(d){const a=d.account||{},v=d.vix||{};$('risk-equity').textContent=fmtMoney(a.net_assets);$('risk-cash').textContent=fmtMoney(a.cash);$('risk-power').textContent=fmtMoney(a.buying_power);$('risk-pos-mult').textContent=v.position_mult?Number(v.position_mult).toFixed(1)+'x':'1.0x';$('risk-status').textContent=d.running?'active':'stopped';$('risk-open-pos').textContent=(d.positions||[]).length;renderConfig()}
function renderConfig(){const el=$('config-list');if(!el||!configSnapshot)return;const r=configSnapshot.risk||{},trading=configSnapshot.trading||{};const items=[[t('call_position','CALL Position'),r.order_pct!=null?r.order_pct+'%':'--'],[t('put_position','PUT Position'),r.put_order_pct!=null?r.put_order_pct+'%':'--'],[t('real_shadow_orders','Shadow Live Orders'),r.shadow_signal_live_orders?'ON':'OFF'],[t('shadow_position','Shadow Position'),r.shadow_live_order_pos_mult!=null?r.shadow_live_order_pos_mult+'x':'--'],[t('quick_trail','Quick Trail'),`${r.quick_trail_activate_pct||'--'}/${r.quick_trail_drop_pct||'--'}%`],[t('trend_quick_trail','Trend Quick Trail'),`${r.trend_quick_trail_activate_pct||'--'}/${r.trend_quick_trail_drop_pct||'--'}%`],[t('timeout','Timeout'),`${r.timeout_stage1_bars||'--'}/${r.timeout_stage2_bars||'--'}/${r.timeout_stage3_bars||'--'} bars`],[t('trading_time','Trading Time'),`${trading.start_time||'--'}-${trading.end_time||'--'}`]];el.innerHTML=items.map(([k,v])=>`<div class="config-item"><span>${k}</span><b>${v}</b></div>`).join('')}
function renderOrders(data){const tbody=$('tb-orders');if(!tbody)return;const orders=(data&&data.orders)||[];const updated=$('orders-updated');if(updated)updated.textContent=data&&data.updated?new Date(data.updated).toLocaleString(currentLang==='zh'?'zh-CN':'en-US',{hour12:false}):'--';tbody.innerHTML=orders.map(o=>{const status=String(o.status||''),cls=status.toLowerCase().includes('filled')?'status-filled':status.toLowerCase().includes('expire')?'status-muted':'status-live';return `<tr><td>${escapeHtml(o.symbol||'--')}</td><td>${escapeHtml(o.side||'--')}</td><td>${o.quantity||0}</td><td>${Number(o.executed_qty||0).toFixed(0)}</td><td>${Number(o.executed_price||0)>0?'$'+Number(o.executed_price).toFixed(2):'--'}</td><td><span class="status-pill ${cls}">${escapeHtml(status||'--')}</span></td></tr>`}).join('')||`<tr><td colspan="6" class="dim">${t('no_orders','No orders')}</td></tr>`}
function renderQuantCenter(){const el=$('quant-center');if(!el||!configSnapshot)return;const r=configSnapshot.risk||{},trading=configSnapshot.trading||{};const yes=v=>v?t('enabled','Enabled'):t('disabled','Disabled');const rows=[[t('trading_time','Trading Time'),`${trading.start_time||'--'}-${trading.end_time||'--'}`],[t('call_position','CALL Position'),r.order_pct!=null?r.order_pct+'%':'--'],[t('put_position','PUT Position'),r.put_order_pct!=null?r.put_order_pct+'%':'--'],[t('daily_limit','Daily Limit'),r.daily_limit!=null?r.daily_limit+'%':'--'],[t('max_trades','Max Trades'),r.max_trades??'--'],[t('max_contracts','Max Contracts'),r.max_contracts_per_trade??'--'],[t('afternoon_contracts','Afternoon Max'),r.max_afternoon_contracts??'--'],[t('put_enabled','PUT Enabled'),yes(r.enable_put_entries)],[t('brooks_mode','Brooks Priority'),yes(r.brooks_priority_mode)],[t('price_action','Price Action'),yes(r.price_action_filter)],[t('market_regime','Market Regime'),yes(r.market_regime_enabled)],[t('trend_day','Trend Day'),yes(r.trend_day_filter_enabled)]];el.innerHTML=rows.map(([k,v])=>`<div class="quant-row"><span>${k}</span><b>${v}</b></div>`).join('')}
const renderConfigBase=renderConfig;renderConfig=function(){renderConfigBase();renderQuantCenter()}
connect();setupControls();loadI18n();loadSymbols();loadConfig();loadOrders();loadCandles();loadReviewSummary();setRefreshTimer();setInterval(loadOrders,30000);window.addEventListener('resize',()=>drawChart());
</script>
</body>
</html>'''

if __name__ == "__main__":
    run_dashboard()
