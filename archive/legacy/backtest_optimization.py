#!/usr/bin/env python3
"""
QQQ 0DTE 回测优化引擎 v2.0
测试多个优化方向 vs 基线 (v6.5)
"""
import os, sys, json, math, argparse
import numpy as np
import pandas as pd
from dataclasses import dataclass, field

# ── 期权定价 ──
def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def bs_price(S, K, T_min, opt_type='call', sigma=0.20, r=0.04):
    T_y = max(T_min / (252 * 390), 0.0)
    if T_y <= 0:
        return max(S - K, 0) if opt_type == 'call' else max(K - S, 0)
    d1 = (math.log(S/K) + (r + 0.5*sigma*sigma)*T_y) / (sigma*math.sqrt(T_y))
    d2 = d1 - sigma*math.sqrt(T_y)
    if opt_type == 'call':
        return S*norm_cdf(d1) - K*math.exp(-r*T_y)*norm_cdf(d2)
    else:
        return K*math.exp(-r*T_y)*norm_cdf(-d2) - S*norm_cdf(-d1)

def entry_opt_price(S, K, T_min, opt_type):
    return max(bs_price(S, K, T_min, opt_type), 0.01)

def exit_opt_px(S, K, T_min, opt_type, entry_p):
    try:
        return max(bs_price(S, K, T_min, opt_type), 0.001)
    except:
        return entry_p * 0.5

# ── 7步信号 ──
def check_breakout(row, vol_avg, sma20, sma50, vwap, s_high, s_low, cfg):
    close = row['Close']
    lb = cfg.get('lookback', 3)
    max_gap = cfg.get('max_gap', 0.002)
    vol_mult = cfg.get('vol_mult', 0.8)
    min_body = cfg.get('min_body', 0.0003)

    upper = row['high_lb%d' % lb]
    lower = row['low_lb%d' % lb]
    if upper == 0:
        return None

    gap_up = (close - upper) / upper if upper > 0 else 999
    gap_dn = (lower - close) / lower if lower > 0 else 999
    call_brk = close > upper and gap_up <= max_gap
    put_brk = close < lower and gap_dn <= max_gap
    if not call_brk and not put_brk:
        return None

    bullish = close >= row['Open']
    bearish = close <= row['Open']
    if call_brk and not bullish:
        return None
    if put_brk and not bearish:
        return None

    vol_ok = row['Volume'] >= vol_avg * vol_mult
    body_pct = abs(close - row['Open']) / close if close else 0
    body_ok = body_pct >= min_body
    if not vol_ok or not body_ok:
        return None

    if call_brk and sma20 > 0 and close < sma20:
        return None
    if put_brk and sma50 > 0 and close > sma50:
        return None

    if call_brk and vwap > 0 and close < vwap:
        return None
    if put_brk and vwap > 0 and close > vwap:
        return None

    rng = max(s_high - s_low, 0.01)
    pct_pos = (close - s_low) / rng
    pos_strict = cfg.get('price_pos_strict', 0.85)
    if call_brk and pct_pos > pos_strict:
        return None
    if put_brk and pct_pos < (1 - pos_strict):
        return None

    direction = 'call' if call_brk else 'put'
    gap_pct = gap_up*100 if call_brk else gap_dn*100
    return {'dir': direction, 'price': close,
            'reason': 'break@%.2f dir=%s gap=%.2f%% LB%d' % (close, direction, gap_pct, lb)}


SCENARIOS = {
    'baseline': {
        'label': 'Baseline (v6.5)',
        'sl': 0.25, 'tp': 0.30,
        'trail_activate': 0.10, 'trail_drop': 0.05,
        'timeout_stage2_bars': 10, 'timeout_stage2_min': 0.05,
        'timeout_stage3_bars': 15, 'timeout_min_bars': 6,
        'daily_limit': 12, 'order_pct': 8,
        'price_pos_strict': 0.85, 'partial_tp': None,
        'time_risk_taper': False, 'tighten_sl': False,
        'trend_stop_enable': False,
    },
    'time_taper': {
        'label': 'Time Taper (13:30 half, 14:00 block)',
        'sl': 0.25, 'tp': 0.30,
        'trail_activate': 0.10, 'trail_drop': 0.05,
        'timeout_stage2_bars': 10, 'timeout_stage2_min': 0.05,
        'timeout_stage3_bars': 12, 'timeout_min_bars': 6,
        'daily_limit': 12, 'order_pct': 8,
        'price_pos_strict': 0.85, 'partial_tp': None,
        'time_risk_taper': True, 'tighten_sl': False,
        'trend_stop_enable': False,
    },
    'tight_sl_tp': {
        'label': 'Tight SL+Partial TP (SL18% TP15% half@10%)',
        'sl': 0.18, 'tp': 0.30,
        'trail_activate': 0.10, 'trail_drop': 0.05,
        'timeout_stage2_bars': 10, 'timeout_stage2_min': 0.05,
        'timeout_stage3_bars': 15, 'timeout_min_bars': 6,
        'daily_limit': 10, 'order_pct': 6,
        'price_pos_strict': 0.85, 'partial_tp': 0.10,
        'time_risk_taper': False, 'tighten_sl': True,
        'trend_stop_enable': False,
    },
    'pos_strict': {
        'label': 'Price Position 80%',
        'sl': 0.25, 'tp': 0.30,
        'trail_activate': 0.10, 'trail_drop': 0.05,
        'timeout_stage2_bars': 10, 'timeout_stage2_min': 0.05,
        'timeout_stage3_bars': 15, 'timeout_min_bars': 6,
        'daily_limit': 12, 'order_pct': 8,
        'price_pos_strict': 0.80, 'partial_tp': None,
        'time_risk_taper': False, 'tighten_sl': False,
        'trend_stop_enable': False,
    },
    'all_combined': {
        'label': 'ALL Combined (our optimizations)',
        'sl': 0.18, 'tp': 0.30,
        'trail_activate': 0.10, 'trail_drop': 0.05,
        'timeout_stage2_bars': 10, 'timeout_stage2_min': 0.05,
        'timeout_stage3_bars': 12, 'timeout_min_bars': 6,
        'daily_limit': 10, 'order_pct': 6,
        'price_pos_strict': 0.80, 'partial_tp': 0.10,
        'time_risk_taper': True, 'tighten_sl': True,
        'trend_stop_enable': False,
    },
    'v63_merged': {
        'label': 'v6.3 Merged (lb5 + tight SL/TP + trend + time taper)',
        'sl': 0.005, 'tp': 0.01,  # v6.3's tight stock SL/TP
        'trail_activate': 0.10, 'trail_drop': 0.05,
        'timeout_stage2_bars': 10, 'timeout_stage2_min': 0.005,
        'timeout_stage3_bars': 15, 'timeout_min_bars': 6,
        'daily_limit': 10, 'order_pct': 6,
        'price_pos_strict': 0.80, 'partial_tp': 0.10,
        'time_risk_taper': True, 'tighten_sl': False,
        'trend_stop_enable': True, 'trend_stop_threshold': 0.008,
        'trend_stop_min_hold_bars': 2,
        'lookback': 5,  # v6.3 wider window
    },
    'v63_pure': {
        'label': 'v6.3 Pure (lb5 + tight SL/TP only)',
        'sl': 0.005, 'tp': 0.01,
        'trail_activate': 0.10, 'trail_drop': 0.05,
        'timeout_stage2_bars': 10, 'timeout_stage2_min': 0.005,
        'timeout_stage3_bars': 15, 'timeout_min_bars': 6,
        'daily_limit': 10, 'order_pct': 6,
        'price_pos_strict': 0.85, 'partial_tp': None,
        'time_risk_taper': False, 'tighten_sl': False,
        'trend_stop_enable': True, 'trend_stop_threshold': 0.008,
        'trend_stop_min_hold_bars': 2,
        'lookback': 5,
    },
    'trend_stop': {
        'label': 'Trend Stop (0.8% drawdown)',
        'sl': 0.25, 'tp': 0.30,
        'trail_activate': 0.10, 'trail_drop': 0.05,
        'timeout_stage2_bars': 10, 'timeout_stage2_min': 0.05,
        'timeout_stage3_bars': 15, 'timeout_min_bars': 6,
        'daily_limit': 12, 'order_pct': 8,
        'price_pos_strict': 0.85, 'partial_tp': None,
        'time_risk_taper': False, 'tighten_sl': False,
        'trend_stop_enable': True, 'trend_stop_threshold': 0.008,
        'trend_stop_min_hold_bars': 2,
    },
}


@dataclass
class BTResult:
    total_pnl: float = 0.0
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    avg_pnl: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    trades: list = field(default_factory=list)


def run_backtest(df, cfg, initial_capital=100000, commission=0.50):
    capital = initial_capital
    trades = []
    pos = None
    entry_idx = 0
    day_pnl = 0.0
    current_date = None

    sl_pct = cfg['sl']
    tp_pct = cfg['tp']
    order_pct = cfg['order_pct']
    partial_tp = cfg.get('partial_tp')
    time_taper = cfg.get('time_risk_taper', False)
    trend_stop = cfg.get('trend_stop_enable', False)
    trend_dd = cfg.get('trend_stop_threshold', 0.008)
    trend_min_hold = cfg.get('trend_stop_min_hold_bars', 2)

    for idx, row in df.iterrows():
        dt = row['datetime']
        date_str = dt.strftime('%Y-%m-%d')
        hm = dt.hour * 60 + dt.minute

        if date_str != current_date:
            current_date = date_str
            day_pnl = 0.0

        # Close at 15:50
        if pos and hm >= 950:
            opt = exit_opt_px(row['Close'], pos['K'], 1, pos['dir'], pos['entry_opt'])
            pnl = (opt - pos['entry_opt']) * pos['contracts'] * 100 - commission * pos['contracts'] * 2
            _add_trade(trades, dt, pos['dir'], pos['entry_price'], row['Close'],
                       pos['contracts'], pnl, 'CloseEOD')
            capital += pnl
            day_pnl += pnl
            pos = None
            continue

        if pos is not None:
            bars_held = idx - entry_idx
            stock_move = (row['Close'] - pos['entry_price']) / pos['entry_price']
            if pos['dir'] == 'put':
                stock_move = -stock_move
            if stock_move > pos.get('max_move', 0):
                pos['max_move'] = stock_move

            # Trend stop
            if trend_stop and bars_held >= trend_min_hold:
                exit_reason = None
                if 'peak_price' not in pos:
                    pos['peak_price'] = row['Close']
                if pos['dir'] == 'call':
                    if row['Close'] > pos['peak_price']:
                        pos['peak_price'] = row['Close']
                    dd = (row['Close'] - pos['peak_price']) / pos['peak_price']
                    if dd <= -trend_dd:
                        exit_reason = 'TrendStop(%.1f%%)' % (dd*100)
                else:
                    if row['Close'] < pos['peak_price']:
                        pos['peak_price'] = row['Close']
                    bounce = (row['Close'] - pos['peak_price']) / pos['peak_price']
                    if bounce >= trend_dd:
                        exit_reason = 'TrendStop(%.1f%%)' % (bounce*100)
                if exit_reason:
                    opt = exit_opt_px(row['Close'], pos['K'], max(1, 960-hm), pos['dir'], pos['entry_opt'])
                    pnl = (opt - pos['entry_opt']) * pos['contracts'] * 100 - commission * pos['contracts'] * 2
                    _add_trade(trades, dt, pos['dir'], pos['entry_price'], row['Close'],
                               pos['contracts'], pnl, exit_reason)
                    capital += pnl
                    day_pnl += pnl
                    pos = None
                    continue

            # Partial TP
            if partial_tp and not pos.get('half_closed', False):
                curr_opt = exit_opt_px(row['Close'], pos['K'], max(1, 960-hm), pos['dir'], pos['entry_opt'])
                opt_pnl = (curr_opt - pos['entry_opt']) / pos['entry_opt']
                if opt_pnl >= partial_tp:
                    half = pos['contracts'] // 2
                    pnl = (curr_opt - pos['entry_opt']) * half * 100 - commission * half * 2
                    _add_trade(trades, dt, pos['dir'], pos['entry_price'], row['Close'],
                               half, pnl, 'PartialTP')
                    capital += pnl
                    pos['contracts'] -= half
                    pos['half_closed'] = True

            # SL
            if stock_move <= -sl_pct:
                opt_loss = pos['entry_opt'] * (1 - sl_pct * 3.5)
                opt_loss = max(opt_loss, 0.005)
                pnl = (opt_loss - pos['entry_opt']) * pos['contracts'] * 100 - commission * pos['contracts'] * 2
                _add_trade(trades, dt, pos['dir'], pos['entry_price'], row['Close'],
                           pos['contracts'], pnl, 'SL(%.1f%%)' % (stock_move*100))
                capital += pnl
                day_pnl += pnl
                pos = None
                continue

            # TP
            if stock_move >= tp_pct:
                opt_tp = pos['entry_opt'] * (1 + tp_pct * 3.5)
                pnl = (opt_tp - pos['entry_opt']) * pos['contracts'] * 100 - commission * pos['contracts'] * 2
                _add_trade(trades, dt, pos['dir'], pos['entry_price'], row['Close'],
                           pos['contracts'], pnl, 'TP(%.1f%%)' % (stock_move*100))
                capital += pnl
                day_pnl += pnl
                pos = None
                continue

            # Trailing stop
            trail_act = cfg.get('trail_activate', 0.10)
            trail_drop = cfg.get('trail_drop', 0.05)
            if stock_move >= trail_act:
                pullback = pos['max_move'] - stock_move
                if pullback >= trail_drop:
                    opt = exit_opt_px(row['Close'], pos['K'], max(1, 960-hm), pos['dir'], pos['entry_opt'])
                    pnl = (opt - pos['entry_opt']) * pos['contracts'] * 100 - commission * pos['contracts'] * 2
                    _add_trade(trades, dt, pos['dir'], pos['entry_price'], row['Close'],
                               pos['contracts'], pnl, 'Trail(%.1f%%)' % (pos['max_move']*100))
                    capital += pnl
                    day_pnl += pnl
                    pos = None
                    continue

            # Timeout
            min_to = cfg.get('timeout_min_bars', 6)
            s2_bars = cfg.get('timeout_stage2_bars', 10)
            s2_min = cfg.get('timeout_stage2_min', 0.05)
            s3_bars = cfg.get('timeout_stage3_bars', 15)

            if bars_held >= min_to:
                if stock_move < s2_min and bars_held >= s2_bars:
                    opt_ex = pos['entry_opt'] * (1 + stock_move * 3)
                    pnl = (opt_ex - pos['entry_opt']) * pos['contracts'] * 100 - commission * pos['contracts'] * 2
                    _add_trade(trades, dt, pos['dir'], pos['entry_price'], row['Close'],
                               pos['contracts'], pnl, 'Timeout%d(weak)' % bars_held)
                    capital += pnl
                    day_pnl += pnl
                    pos = None
                    continue
                if bars_held >= s3_bars:
                    opt_ex = pos['entry_opt'] * (1 + stock_move * 3)
                    pnl = (opt_ex - pos['entry_opt']) * pos['contracts'] * 100 - commission * pos['contracts'] * 2
                    _add_trade(trades, dt, pos['dir'], pos['entry_price'], row['Close'],
                               pos['contracts'], pnl, 'Timeout%d(hard)' % bars_held)
                    capital += pnl
                    day_pnl += pnl
                    pos = None
                    continue

            # Daily loss circuit
            day_pnl_pct = day_pnl / initial_capital * 100
            if day_pnl_pct <= -cfg.get('daily_limit', 25):
                if pos:
                    opt_ex = exit_opt_px(row['Close'], pos['K'], max(1, 960-hm), pos['dir'], pos['entry_opt'])
                    pnl = (opt_ex - pos['entry_opt']) * pos['contracts'] * 100 - commission * pos['contracts'] * 2
                    _add_trade(trades, dt, pos['dir'], pos['entry_price'], row['Close'],
                               pos['contracts'], pnl, 'CircuitBreak')
                    capital += pnl
                    pos = None
                break

            continue

        # ── Signal detection ──
        if hm < 575 or hm >= 950:
            continue

        # Time taper
        pos_mult = 1.0
        if time_taper:
            if hm >= 810:  # 13:30
                pos_mult = 0.5
            if hm >= 840:  # 14:00
                continue

        signal = check_breakout(row,
            row.get('vol_avg_20', row['Volume']),
            row.get('sma20', 0),
            row.get('sma50', 0),
            row.get('vwap', 0),
            row.get('session_high', row['Close']),
            row.get('session_low', row['Close']),
            cfg)
        if not signal:
            continue

        K = round(signal['price'] + (2.0 if signal['dir'] == 'call' else -2.0))
        mins_to_close = max(960 - hm, 1)
        entry_opt = entry_opt_price(signal['price'], K, mins_to_close, signal['dir'])

        pct = order_pct * pos_mult
        if cfg.get('tighten_sl', False):
            pct *= 0.75
        contracts = max(1, int((capital * pct / 100) / (entry_opt * 100)))

        pos = {
            'dir': signal['dir'], 'entry_price': signal['price'],
            'entry_opt': entry_opt, 'K': K,
            'contracts': contracts, 'max_move': 0,
            'reason': signal['reason'], 'half_closed': False,
        }
        entry_idx = idx

    # Stats
    closed = [t for t in trades if t.get('pnl') is not None]
    result = BTResult()
    result.total_pnl = capital - initial_capital
    result.total_trades = len(closed)
    result.wins = sum(1 for t in closed if t['pnl'] > 0)
    result.losses = sum(1 for t in closed if t['pnl'] < 0)

    pnls = [t['pnl'] for t in closed]
    if pnls:
        pos_pnls = [p for p in pnls if p > 0]
        neg_pnls = [p for p in pnls if p < 0]
        result.avg_win = np.mean(pos_pnls) if pos_pnls else 0
        result.avg_loss = abs(np.mean(neg_pnls)) if neg_pnls else 0
        result.avg_pnl = np.mean(pnls)
        result.win_rate = result.wins / max(result.total_trades, 1) * 100
        result.profit_factor = sum(pos_pnls) / max(sum(abs(p) for p in neg_pnls), 1)
    result.trades = closed
    return result


def _add_trade(trades, dt, direction, entry_px, exit_px, contracts, pnl, reason):
    trades.append({
        'time': str(dt), 'dir': direction,
        'entry': round(entry_px, 2), 'exit': round(exit_px, 2),
        'contracts': contracts, 'pnl': round(pnl, 2),
        'result': 'win' if pnl > 0 else 'lose', 'reason': reason,
    })


def load_data():
    print("Loading data/qqq_1min_regular.csv...")
    df = pd.read_csv('data/qqq_1min_regular.csv')
    df['datetime'] = pd.to_datetime(df['timestamp_et'])
    df = df.rename(columns={
        'open': 'Open', 'high': 'High', 'low': 'Low',
        'close': 'Close', 'volume': 'Volume'
    })
    df = df.sort_values('datetime').reset_index(drop=True)

    C = df['Close'].values
    df['sma20'] = pd.Series(C).rolling(20).mean().values
    df['sma50'] = pd.Series(C).rolling(50).mean().values
    df['vol_avg_20'] = pd.Series(df['Volume'].values).rolling(20).mean().values
    df['vwap'] = (df['Close'] * df['Volume']).cumsum() / df['Volume'].cumsum().clip(lower=1)

    for lb in [2, 3, 5]:
        df['high_lb%d' % lb] = df['High'].shift(1).rolling(lb).max()
        df['low_lb%d' % lb] = df['Low'].shift(1).rolling(lb).min()

    day_groups = df.groupby(df['datetime'].dt.date)
    df['session_high'] = day_groups['High'].cummax()
    df['session_low'] = day_groups['Low'].cummin()
    df = df.fillna(0)

    print("  %d bars | %s ~ %s" % (len(df),
        df['datetime'].iloc[0].strftime('%Y-%m-%d'),
        df['datetime'].iloc[-1].strftime('%Y-%m-%d')))
    return df


def format_result(name, label, res):
    return {
        'name': name, 'label': label,
        'trades': res.total_trades,
        'wins': res.wins, 'losses': res.losses,
        'win_rate': res.win_rate,
        'total_pnl': res.total_pnl,
        'avg_pnl': res.avg_pnl,
        'avg_win': res.avg_win,
        'avg_loss': res.avg_loss,
        'profit_factor': res.profit_factor,
    }


def main():
    print("=" * 70)
    print("QQQ 0DTE Optimization Backtest")
    print("=" * 70)
    df = load_data()

    results = []
    for name, cfg in SCENARIOS.items():
        label = cfg['label']
        print("\n>> %s ..." % label)
        res = run_backtest(df.copy(), cfg)
        results.append(format_result(name, label, res))
        print("  Trades: %d, WR: %.1f%%, PnL: $%+.0f, PF: %.2f" % (
            res.total_trades, res.win_rate, res.total_pnl, res.profit_factor))

    # Output table
    print("\n" + "=" * 90)
    print("%-30s %6s %7s %10s %9s %9s %9s %6s" % (
        'Scenario', 'Trades', 'WinRate', 'TotalPnL', 'AvgPnL',
        'AvgWin', 'AvgLoss', 'PF'))
    print("-" * 90)
    for r in results:
        print("%-30s %6d %6.1f%% %+8.0f  %+8.0f  %+8.0f  %8.0f  %5.2f" % (
            r['label'], r['trades'], r['win_rate'], r['total_pnl'],
            r['avg_pnl'], r['avg_win'], r['avg_loss'], r['profit_factor']))

    # Exit reason analysis for best scenario
    print("\n" + "=" * 70)
    print("Exit Reason Distribution (ALL Combined)")
    print("=" * 70)
    all_res = None
    for name, cfg in SCENARIOS.items():
        if name == 'all_combined':
            all_res = run_backtest(df.copy(), cfg)
            break
    if all_res:
        reasons = {}
        for t in all_res.trades:
            r = t.get('reason', '?')
            for prefix in ['SL', 'TP', 'Trail', 'Timeout', 'CloseEOD',
                           'PartialTP', 'TrendStop', 'CircuitBreak']:
                if r.startswith(prefix):
                    reasons[prefix] = reasons.get(prefix, 0) + 1
                    break
            else:
                reasons['Other'] = reasons.get('Other', 0) + 1
        for k, v in sorted(reasons.items(), key=lambda x: -x[1]):
            print("  %s: %d" % (k, v))

    print("\nDone. Results saved to data/backtest_optimization_results.json")
    with open('data/backtest_optimization_results.json', 'w') as f:
        json.dump(results, f, indent=2)


if __name__ == '__main__':
    main()
