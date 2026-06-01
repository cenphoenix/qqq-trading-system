#!/usr/bin/env python3
"""
QQQ 0DTE 期权回测引擎 v1.0
对齐 live_trader.py 的实际精简逻辑（7步过滤管线）
支持参数扫描、统计输出
"""
import os
import sys
import json
import math
import argparse
import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime, timezone


# ===== 期权定价（简化模型）=====
def estimate_option_price(S, K, T_minutes, option_type='call', entry_price=None):
    """
    0DTE 期权价格估算。
    回测中我们没有实时期权数据，所以用简化模型：
    - 入场时用 Black-Scholes 给一个合理的初始价格
    - 后续用 Delta * S_move + 时间衰减 来模拟
    """
    # 0DTE OTM 的典型参数
    sigma_annual = 0.15  # 年化波动率（QQQ 日内约 15%）
    r = 0.04  # 无风险利率
    T_years = max(T_minutes / (252 * 390), 0.0)  # 390分钟/交易日

    if option_type == 'call':
        d1 = (math.log(S / K) + (r + 0.5 * sigma_annual**2) * T_years) / (sigma_annual * math.sqrt(T_years)) if T_years > 0 else 0
        d2 = d1 - sigma_annual * math.sqrt(T_years) if T_years > 0 else 0
        if T_years > 0:
            import scipy.stats as stats
            price = S * stats.norm.cdf(d1) - K * math.exp(-r * T_years) * stats.norm.cdf(d2)
        else:
            price = max(S - K, 0)
    else:
        d1 = (math.log(S / K) + (r + 0.5 * sigma_annual**2) * T_years) / (sigma_annual * math.sqrt(T_years)) if T_years > 0 else 0
        d2 = d1 - sigma_annual * math.sqrt(T_years) if T_years > 0 else 0
        if T_years > 0:
            import scipy.stats as stats
            price = K * math.exp(-r * T_years) * stats.norm.cdf(-d2) - S * stats.norm.cdf(-d1)
        else:
            price = max(K - S, 0)

    return max(price, 0.01)


def get_option_delta(S, K, T_minutes, option_type='call'):
    """估算期权 Delta"""
    sigma_annual = 0.15
    r = 0.04
    T_years = max(T_minutes / (252 * 390), 0.0001)

    if option_type == 'call':
        d1 = (math.log(S / K) + (r + 0.5 * sigma_annual**2) * T_years) / (sigma_annual * math.sqrt(T_years))
    else:
        d1 = (math.log(S / K) + (r + 0.5 * sigma_annual**2) * T_years) / (sigma_annual * math.sqrt(T_years))

    try:
        import scipy.stats as stats
        if option_type == 'call':
            return stats.norm.cdf(d1)
        else:
            return stats.norm.cdf(d1) - 1
    except ImportError:
        # 无 scipy 用近似
        return 0.5 if option_type == 'call' else -0.5


# ===== 技术指标计算 =====
class IndicatorCalculator:
    """与 live_trader.py FilterEngine 对齐的指标计算器"""

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def compute_all(self, df: pd.DataFrame) -> pd.DataFrame:
        """在 df 上计算所有指标，返回增强后的 df"""
        df = df.copy()

        # SMA20, SMA50
        df['sma20'] = df['close'].rolling(20, min_periods=20).mean()
        df['sma50'] = df['close'].rolling(50, min_periods=50).mean()

        # VWAP
        df['vwap'] = (df['close'] * df['volume']).cumsum() / df['volume'].cumsum().clip(lower=1)

        # EMA12, EMA26, MACD
        df['ema12'] = df['close'].ewm(span=12, adjust=False).mean()
        df['ema26'] = df['close'].ewm(span=26, adjust=False).mean()
        df['macd_line'] = df['ema12'] - df['ema26']
        df['macd_signal'] = df['macd_line'].ewm(span=9, adjust=False).mean()
        df['macd_hist'] = df['macd_line'] - df['macd_signal']

        # 成交量均线
        df['vol_mean'] = df['volume'].rolling(20, min_periods=20).mean()

        # ATR (True Range 的均值)
        df['high_lag1'] = df['high'].shift(1)
        df['low_lag1'] = df['low'].shift(1)
        df['prev_close'] = df['close'].shift(1)
        df['tr'] = np.maximum(
            df['high'] - df['low'],
            np.maximum(
                abs(df['high'] - df['prev_close']),
                abs(df['low'] - df['prev_close'])
            )
        )
        df['atr'] = df['tr'].rolling(14, min_periods=14).mean()

        # RSI (Wilder 平滑)
        df['delta'] = df['close'].diff()
        up = df['delta'].clip(lower=0)
        down = (-df['delta'].clip(upper=0)).clip(lower=0)
        avg_up = up.ewm(alpha=1/14, adjust=False, min_periods=14).mean()
        avg_down = down.ewm(alpha=1/14, adjust=False, min_periods=14).mean()
        rs = avg_up / avg_down.clip(lower=1e-10)
        df['rsi'] = 100 - (100 / (1 + rs))

        # K 线实体比例
        df['body'] = abs(df['close'] - df['open'])
        df['body_pct'] = df['body'] / df['close']
        df['is_bullish'] = df['close'] > df['open']
        df['is_bearish'] = df['close'] < df['open']

        # 突破线 (LB3, LB5)
        df['high_lb3'] = df['high'].shift(1).rolling(3, min_periods=3).max()
        df['low_lb3'] = df['low'].shift(1).rolling(3, min_periods=3).min()
        df['high_lb5'] = df['high'].shift(1).rolling(5, min_periods=5).max()
        df['low_lb5'] = df['low'].shift(1).rolling(5, min_periods=5).min()

        # 盘中高低点 (reset daily)
        day_groups = df.groupby(df.index.date)
        df['session_high'] = day_groups['high'].cummax()
        df['session_low'] = day_groups['low'].cummin()

        return df


# ===== 信号检测（精简7步管线，对齐 live_trader.py _check_breakout）=====
def check_breakout(row, df_row_idx: int, df: pd.DataFrame, cfg: dict) -> dict:
    """
    检查当前行是否触发突破信号。
    返回 {'dir': 'call'/'put', 'price': float, 'reason': str} 或 None。
    """
    close = row['close']
    lookback = cfg.get('lookback', 3)  # Classic: 3
    lookback_accel = cfg.get('lookback_accel', 2)  # Accelerated: 2
    vol_mult = cfg.get('vol_mult', 0.8)
    min_body = cfg.get('min_body', 0.0003)
    max_gap = cfg.get('max_gap', 0.002)

    volume = row.get('volume', 0)
    vol_mean = row.get('vol_mean', 0) if not pd.isna(row.get('vol_mean', np.nan)) else 0
    body_pct = row.get('body_pct', 0) if not pd.isna(row.get('body_pct', np.nan)) else 0
    bullish = row.get('is_bullish', False)
    bearish = row.get('is_bearish', False)

    sma20 = row.get('sma20', 0) if not pd.isna(row.get('sma20', np.nan)) else 0
    sma50 = row.get('sma50', 0) if not pd.isna(row.get('sma50', np.nan)) else 0
    vwap = row.get('vwap', 0) if not pd.isna(row.get('vwap', np.nan)) else 0

    # 获取突破线
    high_lb = row.get(f'high_lb{lookback}', 0)
    low_lb = row.get(f'low_lb{lookback}', 0)
    if high_lb == 0 and lookback != lookback_accel:
        high_lb = row.get(f'high_lb{lookback_accel}', 0)
        low_lb = row.get(f'low_lb{lookback_accel}', 0)

    if high_lb == 0 or vol_mean == 0:
        return None

    # 1. 价格突破
    gap_up = (close - high_lb) / high_lb if high_lb > 0 else 0
    gap_down = (low_lb - close) / low_lb if low_lb > 0 else 0

    call_breakout = close > high_lb and gap_up <= max_gap
    put_breakout = close < low_lb and gap_down <= max_gap

    if not call_breakout and not put_breakout:
        return None

    # 2. 动量确认
    if call_breakout and not bullish:
        return None
    if put_breakout and not bearish:
        return None

    # 3. 量能确认
    vol_ok = volume >= vol_mean * vol_mult

    # 4. 实体确认
    body_ok = body_pct >= min_body

    if not vol_ok or not body_ok:
        return None

    # 5. 趋势方向 (SMA)
    trend_ok = True
    if call_breakout and sma20 > 0 and close < sma20:
        trend_ok = False
    if put_breakout and sma50 > 0 and close > sma50:
        trend_ok = False

    # 6. VWAP 硬过滤
    vwap_ok = True
    if call_breakout and vwap > 0 and close < vwap:
        vwap_ok = False
    if put_breakout and vwap > 0 and close > vwap:
        vwap_ok = False

    if not trend_ok or not vwap_ok:
        return None

    # 7. 价格位置
    session_high = row.get('session_high', close)
    session_low = row.get('session_low', close)
    range_val = session_high - session_low if session_high > session_low else 0.01
    price_pos = (close - session_low) / range_val

    if call_breakout and price_pos > 0.85:
        return None
    if put_breakout and price_pos < 0.15:
        return None

    # --- 信号触发 ---
    mode = f"LB{lookback}" if close > high_lb else f"LB{lookback_accel}"
    direction = 'call' if call_breakout else 'put'
    price = close
    reason = f"突破{price:.2f}做{'多' if call_breakout else '空'}(跳空{gap_up*100:.2f}%,{mode})"

    return {'dir': direction, 'price': price, 'reason': reason}


# ===== 回测主循环 =====
@dataclass
class BacktestResult:
    total_pnl: float = 0.0
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    breakeven: int = 0
    max_drawdown: float = 0.0
    max_drawdown_pct: float = 0.0
    avg_pnl: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    sharpe: float = 0.0
    trades_detail: list = field(default_factory=list)
    daily_pnl: dict = field(default_factory=dict)
    daily_pnl_series: list = field(default_factory=list)


def run_backtest(
    df: pd.DataFrame,
    cfg: dict,
    option_offset: float = 2.0,
    capital: float = 100000,
    contract_multiplier: int = 100,
    order_pct: float = 8,
    commission_per_contract: float = 0.50,  # 双边手续费
    verbose: bool = True,
) -> BacktestResult:
    """
    运行回测。
    df: 必须有 datetime, open, high, low, close, volume + 指标列
    """
    result = BacktestResult()
    daily_pnl = 0.0
    peak_capital = capital
    current_capital = capital

    position = None  # 当前持仓
    entry_time = None
    entry_bar_idx = 0

    # 风控计数器
    consecutive_losses = 0
    daily_limit = cfg.get('daily_limit', 25)  # % of capital

    # 时间过滤
    start_h, start_m = 9, 35
    end_h, end_m = 15, 50

    # 超时配置
    timeout_stage2_bars = cfg.get('timeout_stage2_bars', 10)
    timeout_stage2_min = cfg.get('timeout_stage2_min', 0.05)
    timeout_stage3_bars = cfg.get('timeout_stage3_bars', 15)

    for idx, row in df.iterrows():
        dt = row.name if hasattr(row, 'name') else idx
        hour = dt.hour
        minute = dt.minute
        ts_minutes = idx

        # 检查是否是新的一天
        if position is None:
            daily_pnl = 0.0

        # 收盘前强平
        if position and (hour > 15 or (hour == 15 and minute >= 50)):
            exit_price = row['close']
            exit_opt_price = simulate_option_exit(
                row['close'], position['K'], 0.1,  # ~1分钟到期
                position['dir'], position['entry_opt_price']
            )
            pnl = _calc_exit_pnl(position, exit_opt_price, capital, contract_multiplier, commission_per_contract)
            _record_trade(result, position, exit_opt_price, pnl, '收盘强平', current_capital, capital)
            current_capital += pnl
            position = None
            continue

        # --- 如果有持仓：风控检查 ---
        if position:
            bars_held = idx - entry_bar_idx
            entry_opt = position['entry_opt_price']

            # 正股价格
            stock_move_pct = (row['close'] - position['entry_price']) / position['entry_price']
            if position['dir'] == 'put':
                stock_move_pct = -stock_move_pct

            # 更新峰值
            if stock_move_pct > position.get('max_move', 0):
                position['max_move'] = stock_move_pct
                position['peak_pnl'] = stock_move_pct * position['contracts'] * contract_multiplier * entry_opt / 100

            # 止损
            sl_pct = cfg.get('sl', 0.25) / 100  # turn percent to fraction
            tp_pct = cfg.get('tp', 0.30) / 100

            # 检查正股价格止损/止盈（策略用的是正股价格百分比）
            if abs(stock_move_pct) >= sl_pct and stock_move_pct < 0:
                # 止损
                opt_loss_price = entry_opt * (1 - sl_pct * 4)  # 期权杠杆 ~4x
                opt_loss_price = max(opt_loss_price, 0.01)
                pnl = _calc_exit_pnl(position, opt_loss_price, capital, contract_multiplier, commission_per_contract)
                _record_trade(result, position, opt_loss_price, pnl, f'止损({stock_move_pct*100:.2f}%)', current_capital, capital)
                current_capital += pnl
                position = None
                continue

            # 止盈
            if stock_move_pct >= tp_pct:
                opt_tp_price = entry_opt * (1 + tp_pct * 4)
                pnl = _calc_exit_pnl(position, opt_tp_price, capital, contract_multiplier, commission_per_contract)
                _record_trade(result, position, opt_tp_price, pnl, f'止盈({stock_move_pct*100:.2f}%)', current_capital, capital)
                current_capital += pnl
                position = None
                continue

            # 跟踪止损
            trail_activate = cfg.get('trail_activate', 0.10) / 100
            trail_drop = cfg.get('trail_drop', 0.05) / 100
            if stock_move_pct >= trail_activate:
                pullback = position['max_move'] - stock_move_pct
                if pullback >= trail_drop:
                    opt_tp_price = entry_opt * (1 + stock_move_pct * 3)
                    pnl = _calc_exit_pnl(position, opt_tp_price, capital, contract_multiplier, commission_per_contract)
                    _record_trade(result, position, opt_tp_price, pnl,
                                  f'跟踪止损(盈利{position["max_move"]*100:.1f}%,回撤{pullback*100:.1f}%)',
                                  current_capital, capital)
                    current_capital += pnl
                    position = None
                    continue

            # 超时退出
            min_timeout_bars = cfg.get('timeout_min_bars', 6)
            if bars_held >= min_timeout_bars:
                # Stage 2: 盈利 < 5% 退出
                if stock_move_pct < timeout_stage2_min and bars_held >= timeout_stage2_bars:
                    opt_exit = entry_opt * (1 + stock_move_pct * 3)
                    pnl = _calc_exit_pnl(position, opt_exit, capital, contract_multiplier, commission_per_contract)
                    _record_trade(result, position, opt_exit, pnl,
                                  f'超时{int(bars_held)}min(盈利不足{timeout_stage2_min*100:.1f}%)',
                                  current_capital, capital)
                    current_capital += pnl
                    position = None
                    continue

                # Stage 3: 硬超时
                if bars_held >= timeout_stage3_bars:
                    opt_exit = entry_opt * (1 + stock_move_pct * 3)
                    pnl = _calc_exit_pnl(position, opt_exit, capital, contract_multiplier, commission_per_contract)
                    _record_trade(result, position, opt_exit, pnl,
                                  f'硬超时{int(bars_held)}min',
                                  current_capital, capital)
                    current_capital += pnl
                    position = None
                    continue

            # 日亏损熔断
            day_pnl_pct = (current_capital - capital) / capital * 100
            if day_pnl_pct <= -daily_limit:
                if position:
                    opt_exit = entry_opt * (1 + stock_move_pct * 3)
                    pnl = _calc_exit_pnl(position, opt_exit, capital, contract_multiplier, commission_per_contract)
                    _record_trade(result, position, opt_exit, pnl,
                                  f'日亏损熔断{day_pnl_pct:.1f}%',
                                  current_capital, capital)
                    current_capital += pnl
                    position = None
                # Stop trading for the day
                break

            continue  # 有持仓时不检测新信号

        # --- 无持仓：检测信号 ---
        signal = check_breakout(row, idx, df, cfg)
        if not signal:
            continue

        # 开盘冷却
        if hour == 9 and minute < 35:
            continue

        # 交易时间窗口
        if hour < start_h or (hour == start_h and minute < start_m):
            continue
        # 15:30 后减半仓位，15:45 后不开仓
        if hour == 15 and minute >= 50:
            break

        # --- 开仓 ---
        entry_price = row['close']
        K = entry_price + (option_offset if signal['dir'] == 'call' else -option_offset)
        # 四舍五入到最近的 $1
        K = round(K)

        minutes_to_close = max((16 - hour) * 60 - minute, 1)
        entry_opt_price = estimate_option_price(entry_price, K, minutes_to_close, signal['dir'])
        entry_opt_price = max(entry_opt_price, 0.01)

        # 仓位计算
        capital_pct = order_pct / 100
        contracts = int((current_capital * capital_pct) / (entry_opt_price * contract_multiplier))
        contracts = max(contracts, 1)

        position = {
            'dir': signal['dir'],
            'entry_price': entry_price,
            'entry_opt_price': entry_opt_price,
            'K': K,
            'contracts': contracts,
            'reason': signal['reason'],
            'max_move': 0,
            'peak_pnl': 0,
        }
        entry_time = dt
        entry_bar_idx = idx

    # 最终统计
    result.total_pnl = current_capital - capital
    result.total_trades = len(result.trades_detail)
    result.wins = sum(1 for t in result.trades_detail if t['pnl'] > 0)
    result.losses = sum(1 for t in result.trades_detail if t['pnl'] < 0)
    result.breakeven = sum(1 for t in result.trades_detail if t['pnl'] == 0)
    result.avg_pnl = result.total_pnl / max(result.total_trades, 1)
    result.avg_win = np.mean([t['pnl'] for t in result.trades_detail if t['pnl'] > 0]) if result.wins > 0 else 0
    result.avg_loss = np.mean([t['pnl'] for t in result.trades_detail if t['pnl'] < 0]) if result.losses > 0 else 0
    result.win_rate = result.wins / max(result.total_trades, 1) * 100

    wins_total = sum(t['pnl'] for t in result.trades_detail if t['pnl'] > 0)
    losses_total = abs(sum(t['pnl'] for t in result.trades_detail if t['pnl'] < 0))
    result.profit_factor = wins_total / max(losses_total, 1)

    return result


def simulate_option_exit(stock_price, K, T_minutes, direction, entry_opt_price):
    """模拟平仓时的期权价格"""
    try:
        return estimate_option_price(stock_price, K, T_minutes, direction)
    except:
        return entry_opt_price * 0.5  # fallback


def _calc_exit_pnl(position, exit_opt_price, capital, contract_multiplier, commission):
    """计算平仓盈亏（含手续费）"""
    contracts = position['contracts']
    entry_opt = position['entry_opt_price']
    gross_pnl = (exit_opt_price - entry_opt) * contracts * contract_multiplier
    commission_total = commission * contracts * 2  # 开仓+平仓
    return gross_pnl - commission_total


def _record_trade(result, position, exit_opt_price, pnl, reason, current_capital, initial_capital):
    result.trades_detail.append({
        'entry_time': position.get('entry_time', ''),
        'dir': position['dir'],
        'entry_price': round(position['entry_price'], 2),
        'entry_opt_price': round(position['entry_opt_price'], 2),
        'exit_price': round(pnl / (position['contracts'] * 100) + position['entry_price'], 2),
        'exit_opt_price': round(exit_opt_price, 2),
        'contracts': position['contracts'],
        'pnl': round(pnl, 2),
        'pnl_pct': round(pnl / (position['entry_opt_price'] * position['contracts'] * 100) * 100, 2) if position['entry_opt_price'] > 0 else 0,
        'reason': reason,
        'capital_after': round(current_capital + pnl, 2),
    })


# ===== 数据加载 =====
def load_csv(csv_path: str) -> pd.DataFrame:
    """加载 CSV 数据"""
    df = pd.read_csv(csv_path)
    df['datetime'] = pd.to_datetime(df['datetime'])
    df = df.set_index('datetime')
    df = df.rename(columns={
        'open': 'Open', 'high': 'High', 'low': 'Low',
        'close': 'Close', 'volume': 'Volume'
    })
    return df


def filter_rth(df: pd.DataFrame) -> pd.DataFrame:
    """只保留美东 09:30 - 16:00 的交易时段数据"""
    # 假设 CSV 的 datetime 是 UTC
    df = df.copy()
    # 转换到美东
    if df.index.tz is None:
        df.index = df.index.tz_localize('UTC')
    df.index = df.index.tz_convert('America/New_York')
    # 过滤 RTH
    mask = (
        ((df.index.hour == 9) & (df.index.minute >= 30)) |
        ((df.index.hour > 9) & (df.index.hour < 16)) |
        ((df.index.hour == 15) & (df.index.minute > 30))
    )
    return df[mask]


# ===== 主入口 =====
def main():
    parser = argparse.ArgumentParser(description='QQQ 0DTE 回测引擎')
    parser.add_argument('--data', type=str, default='data/qqq_1min_2025.csv', help='CSV 数据文件路径')
    parser.add_argument('--capital', type=float, default=100000, help='初始资金')
    parser.add_argument('--order-pct', type=float, default=8, help='单笔仓位百分比')
    parser.add_argument('--offset', type=float, default=2.0, help='期权行权价偏移($)')
    parser.add_argument('--sl', type=float, default=0.25, help='止损 (%)')
    parser.add_argument('--tp', type=float, default=0.30, help='止盈 (%)')
    parser.add_argument('--vol-mult', type=float, default=0.8, help='成交量倍数')
    parser.add_argument('--min-body', type=float, default=0.0003, help='最小实体比例')
    parser.add_argument('--lookback', type=int, default=3, help='突破窗口')
    parser.add_argument('--lookback-accel', type=int, default=2, help='加速突破窗口')
    parser.add_argument('--trail-activate', type=float, default=0.10, help='跟踪止损激活 (%)')
    parser.add_argument('--trail-drop', type=float, default=0.05, help='跟踪止损回撤 (%)')
    parser.add_argument('--daily-limit', type=float, default=25, help='日亏损熔断 (%)')
    parser.add_argument('--commission', type=float, default=0.50, help='单手佣金 ($)')
    parser.add_argument('--sweep', action='store_true', help='执行参数扫描模式')
    args = parser.parse_args()

    # 加载数据
    print(f"📂 加载数据: {args.data}")
    if not os.path.exists(args.data):
        print(f"❌ 文件不存在: {args.data}")
        print("   请先运行: python fetch_historical_kline.py")
        sys.exit(1)

    df = load_csv(args.data)
    print(f"   原始数据: {len(df)} 条")

    # 过滤交易时段
    df = filter_rth(df)
    print(f"   RTH 过滤后: {len(df)} 条")
    print(f"   日期范围: {df.index[0].strftime('%Y-%m-%d')} ~ {df.index[-1].strftime('%Y-%m-%d')}")

    # 构建指标
    cfg = {
        'sl': args.sl, 'tp': args.tp,
        'lookback': args.lookback, 'lookback_accel': args.lookback_accel,
        'vol_mult': args.vol_mult, 'min_body': args.min_body,
        'max_gap': 0.002,
        'trail_activate': args.trail_activate, 'trail_drop': args.trail_drop,
        'timeout_stage2_bars': 10, 'timeout_stage2_min': 0.05,
        'timeout_stage3_bars': 15, 'timeout_min_bars': 6,
        'daily_limit': args.daily_limit,
        'order_pct': args.order_pct,
    }

    calc = IndicatorCalculator(cfg)
    df = calc.compute_all(df)
    print(f"   指标计算完成")

    if args.sweep:
        # 参数扫描
        sweep_results = []
        vol_mults = [0.5, 0.8, 1.0, 1.2]
        min_bodies = [0.0001, 0.0003, 0.0005, 0.001]
        trail_activates = [0.08, 0.10, 0.15]

        for vm in vol_mults:
            for mb in min_bodies:
                for ta in trail_activates:
                    c = cfg.copy()
                    c['vol_mult'] = vm
                    c['min_body'] = mb
                    c['trail_activate'] = ta

                    res = run_backtest(df, c, args.offset, args.capital,
                                       commission_per_contract=args.commission,
                                       verbose=False)

                    sweep_results.append({
                        'vol_mult': vm,
                        'min_body': mb,
                        'trail_act': ta,
                        'total_pnl': res.total_pnl,
                        'total_trades': res.total_trades,
                        'win_rate': res.win_rate,
                        'profit_factor': res.profit_factor,
                        'avg_pnl': res.avg_pnl,
                    })

        sweep_df = pd.DataFrame(sweep_results)
        sweep_df = sweep_df.sort_values('total_pnl', ascending=False)
        print("\n📊 参数扫描结果 (Top 20):")
        print(sweep_df.head(20).to_string(index=False))

        # 保存完整结果
        sweep_df.to_csv('data/backtest_sweep_results.csv', index=False)
        print(f"   完整结果已保存至 data/backtest_sweep_results.csv")
    else:
        # 单次运行
        res = run_backtest(df, cfg, args.offset, args.capital,
                          commission_per_contract=args.commission, verbose=True)

        print(f"\n{'='*60}")
        print(f"📊 回测结果")
        print(f"{'='*60}")
        print(f"  交易次数: {res.total_trades}")
        print(f"  胜率: {res.win_rate:.1f}% ({res.wins}胜/{res.losses}负/{res.breakeven}平)")
        print(f"  总盈亏: ${res.total_pnl:+,.2f}")
        print(f"  平均盈亏: ${res.avg_pnl:+,.2f}")
        print(f"  平均盈利: ${res.avg_win:+,.2f}")
        print(f"  平均亏损: ${res.avg_loss:+,.2f}")
        print(f"   Profit Factor: {res.profit_factor:.2f}")
        print(f"{'='*60}")

        # 保存交易明细
        detail_df = pd.DataFrame(res.trades_detail)
        detail_df.to_csv('data/backtest_trades_detail.csv', index=False)
        print(f"  交易明细已保存至 data/backtest_trades_detail.csv")


if __name__ == '__main__':
    main()
