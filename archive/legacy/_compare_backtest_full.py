#!/usr/bin/env python3
"""完整盈亏回测：比较 v6.3 (lookback=5) vs v6.5 (lookback=3)
基于 backtest_v6.py 引擎（BS定价 + 全退出逻辑）"""
import sys, os, json, math
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
import pandas as pd
from datetime import datetime, date

# ===== 数据加载 =====
CSV_PATH = 'data/qqq_1min_regular.csv'
print(f"📂 加载数据...")
df = pd.read_csv(CSV_PATH)
print(f"   {len(df)}行")
# 重命名列为脚本期望的格式
df = df.rename(columns={
    'open': 'Open', 'high': 'High', 'low': 'Low', 'close': 'Close',
    'volume': 'Volume', 'timestamp_et': 'Datetime'
})
df['Datetime'] = pd.to_datetime(df['Datetime'])
df = df.sort_values('Datetime').reset_index(drop=True)
dates = sorted(df['Datetime'].dt.date.unique())
print(f"   {len(dates)}个交易日 ({dates[0]} ~ {dates[-1]})")

# ===== Black-Scholes =====
def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def bs_option_price(S, K, T, r, sigma, option_type='call'):
    if T <= 0:
        return max(S - K, 0) if option_type == 'call' else max(K - S, 0)
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if option_type == 'call':
        return S * norm_cdf(d1) - K * math.exp(-r * T) * norm_cdf(d2)
    else:
        return K * math.exp(-r * T) * norm_cdf(-d2) - S * norm_cdf(-d1)

def run_backtest(cfg, label):
    """全流程回测"""
    n = len(df)
    C = df['Close'].values
    O = df['Open'].values
    H = df['High'].values
    L = df['Low'].values
    V = df['Volume'].values
    DT = df['Datetime'].values

    trades = []
    daily_pnl = {}
    in_position = False
    pos = {}

    lb = cfg['lookback']
    sl = cfg['sl']
    tp = cfg['tp']
    trail_activate = cfg.get('trail_activate', 0)
    trail_drop = cfg.get('trail_drop', 0)
    capital = cfg.get('capital', 100000)
    option_offset = cfg.get('option_offset', 2.0)

    for i in range(n):
        ts = pd.Timestamp(DT[i])
        hm = ts.hour * 60 + ts.minute
        if hm < 575 or hm > 950:
            continue

        if in_position:
            # 退出检查
            price = C[i]
            pnl_opt = 0
            exit_reason = None

            if pos['type'] == 'call':
                # 期权价格变化基于正股涨跌（delta近似=0.5）
                stock_move = (price - pos['entry_stock']) / pos['entry_stock']
                opt_move = stock_move * cfg.get('leverage', 4.0)
                opt_pnl = opt_move
                current_opt_price = pos['entry_opt'] * (1 + opt_move)
            else:
                stock_move = (pos['entry_stock'] - price) / pos['entry_stock']
                opt_move = stock_move * cfg.get('leverage', 4.0)
                opt_pnl = opt_move
                current_opt_price = pos['entry_opt'] * (1 + opt_move)

            # 止损
            if opt_pnl <= -sl:
                exit_reason = '止损'
                pnl_opt = -sl
            # 止盈
            elif opt_pnl >= tp:
                exit_reason = '止盈'
                pnl_opt = tp
            # 跟踪止损
            elif trail_activate > 0 and opt_pnl > trail_activate:
                peak_pnl = max(pos.get('peak_pnl', 0), opt_pnl)
                pos['peak_pnl'] = peak_pnl
                if opt_pnl < peak_pnl - trail_drop:
                    exit_reason = '追踪止损'
                    pnl_opt = opt_pnl
            # 超时退出
            elif (i - pos['entry_idx']) * 60 >= cfg.get('max_hold_seconds', 1800):
                exit_reason = '超时'
                pnl_opt = opt_pnl

            if exit_reason:
                pnl_usd = pnl_opt * capital * 0.05  # 每笔使用5%资金
                trades.append({
                    'date': str(pd.Timestamp(DT[pos['entry_idx']]).date()),
                    'entry_time': str(DT[pos['entry_idx']]),
                    'exit_time': str(DT[i]),
                    'dir': pos['type'],
                    'entry_stock': round(pos['entry_stock'], 2),
                    'exit_stock': round(price, 2),
                    'stock_pnl_pct': round(stock_move * 100, 4) if pos['type'] == 'call' else round(-stock_move * 100, 4),
                    'opt_pnl_pct': round(pnl_opt * 100, 2),
                    'pnl_usd': round(pnl_usd, 2),
                    'result': 'win' if pnl_opt > 0 else 'lose',
                    'exit_reason': exit_reason,
                })
                d = str(ts.date())
                daily_pnl[d] = daily_pnl.get(d, 0) + pnl_usd
                in_position = False
            continue

        # === 信号检测 ===
        if i < lb + 1:
            continue

        rsi_period = cfg.get('rsi_period', 14)
        if i >= rsi_period + 1:
            deltas = [C[j] - C[j-1] for j in range(i-rsi_period+1, i)]
            gains = [max(d, 0) for d in deltas]
            losses = [max(-d, 0) for d in deltas]
            avg_g = np.mean(gains) if gains else 0
            avg_l = np.mean(losses) if losses else 0
            rsi = 100 - 100 / (1 + avg_g / avg_l) if avg_l > 0 else 100
        else:
            rsi = 50
        if rsi > 80 or rsi < 20:
            continue

        entry_price = C[i]
        upper = max(H[i-lb:i])
        lower = min(L[i-lb:i])

        gap_up = (entry_price - upper) / upper if upper > 0 else 999
        gap_dn = (lower - entry_price) / lower if lower > 0 else 999
        max_gap = cfg.get('max_gap', 0.002)

        sig_dir = None
        if entry_price > upper and gap_up < max_gap:
            sig_dir = 'call'
        elif entry_price < lower and gap_dn < max_gap:
            sig_dir = 'put'
        if not sig_dir:
            continue

        # Momentum filter
        if (sig_dir == 'call' and C[i] < O[i]) or (sig_dir == 'put' and C[i] > O[i]):
            continue

        # Volume filter
        vol_avg = np.mean(V[max(0, i-20):i]) if i >= 20 else 0
        vol_mult = cfg.get('vol_mult', 0.8)
        if vol_avg > 0 and V[i] < vol_avg * vol_mult:
            continue

        # Body filter
        body = abs(C[i] - O[i]) / O[i] if O[i] > 0 else 0
        min_body = cfg.get('min_body', 0.0003)
        if body < min_body:
            continue

        # === 开仓 ===
        K = entry_price + option_offset if sig_dir == 'call' else entry_price - option_offset
        T = (16 * 3600 - (ts.hour * 3600 + ts.minute * 60 + ts.second)) / (252 * 6.5 * 3600)
        opt_price = bs_option_price(entry_price, K, max(T, 0.0001), 0.04, 0.15, sig_dir)

        in_position = True
        pos = {
            'type': sig_dir,
            'entry_stock': entry_price,
            'entry_opt': opt_price,
            'entry_idx': i,
            'peak_pnl': 0,
        }

    # 收盘平仓
    if in_position:
        last_price = C[-1]
        stock_move = (last_price - pos['entry_stock']) / pos['entry_stock']
        if pos['type'] == 'put':
            stock_move = -stock_move
        opt_pnl = stock_move * cfg.get('leverage', 4.0)
        pnl_usd = opt_pnl * capital * 0.05
        trades.append({
            'date': str(pd.Timestamp(DT[pos['entry_idx']]).date()),
            'entry_time': str(DT[pos['entry_idx']]),
            'exit_time': str(DT[-1]),
            'dir': pos['type'],
            'entry_stock': round(pos['entry_stock'], 2),
            'exit_stock': round(last_price, 2),
            'opt_pnl_pct': round(opt_pnl * 100, 2),
            'pnl_usd': round(pnl_usd, 2),
            'result': 'win' if opt_pnl > 0 else 'lose',
            'exit_reason': '收盘平仓',
        })
        d = str(pd.Timestamp(DT[-1]).date())
        daily_pnl[d] = daily_pnl.get(d, 0) + pnl_usd

    # === 统计 ===
    total = len(trades)
    wins = sum(1 for t in trades if t['result'] == 'win')
    wr = round(wins / total * 100, 1) if total > 0 else 0
    total_pnl = sum(t['pnl_usd'] for t in trades)
    total_pnl_pct = total_pnl / capital * 100

    # Profit Factor
    gross_win = sum(t['pnl_usd'] for t in trades if t['pnl_usd'] > 0)
    gross_loss = abs(sum(t['pnl_usd'] for t in trades if t['pnl_usd'] < 0))
    pf = round(gross_win / gross_loss, 2) if gross_loss > 0 else float('inf')

    # Max drawdown (cumulative PnL)
    cum = 0; peak = 0; max_dd = 0
    for t in trades:
        cum += t['pnl_usd']
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)

    print(f"\n{'='*60}")
    print(f"📊 {label}")
    print(f"{'='*60}")
    print(f"  总交易: {total}笔")
    print(f"  盈利: {wins}笔 | 亏损: {total-wins}笔")
    print(f"  胜率: {wr}%")
    print(f"  总盈亏: ${total_pnl:+,.0f} ({total_pnl_pct:+.1f}%)")
    print(f"  盈亏比(PF): {pf}")
    print(f"  最大回撤: ${max_dd:,.0f}")
    print(f"  日均盈亏: ${total_pnl/len(dates):+,.0f}")
    
    # 退出原因分析
    from collections import Counter
    reasons = Counter(t['exit_reason'] for t in trades)
    print(f"\n退出原因:")
    for r, c in reasons.most_common():
        sub = [t for t in trades if t['exit_reason'] == r]
        sw = sum(1 for t in sub if t['result'] == 'win')
        avg = np.mean([t['pnl_usd'] for t in sub])
        print(f"  {r}: {c}笔 ({c/total*100:.0f}%) 胜率{sw/len(sub)*100:.0f}% 均值${avg:+.0f}")

    return {
        'trades': total, 'wins': wins, 'win_rate': wr,
        'pnl_usd': total_pnl, 'pnl_pct': total_pnl_pct,
        'profit_factor': pf, 'max_drawdown': max_dd,
        'daily_avg': total_pnl / len(dates),
    }

# ===== v6.3 参数 =====
CFG_V63 = {
    'lookback': 5, 'sl': 0.25, 'tp': 0.30,
    'vol_mult': 0.8, 'min_body': 0.0003, 'max_gap': 0.002,
    'rsi_period': 14, 'trail_activate': 0.10, 'trail_drop': 0.05,
    'max_hold_seconds': 1800, 'capital': 100000,
    'option_offset': 2.0, 'leverage': 4.0,
}

# ===== v6.5 参数 (lookback=3) =====
CFG_V65 = {**CFG_V63, 'lookback': 3}

print("\n" + "=" * 60)
print("📋 完整盈亏回测对比")
print("=" * 60)

r63 = run_backtest(CFG_V63, "v6.3 (lookback=5)")
r65 = run_backtest(CFG_V65, "v6.5 当前 (lookback=3)")

print(f"\n{'='*60}")
print(f"📋 对比总结")
print(f"{'='*60}")
print(f"{'指标':<20} {'v6.3(原始)':<18} {'v6.5(当前)':<18} {'变化':<15}")
print("-"*70)
print(f"{'交易笔数':<20} {r63['trades']:<18} {r65['trades']:<18} {r65['trades']-r63['trades']:+d}")
print(f"{'胜率':<20} {r63['win_rate']:<18} {r65['win_rate']:<18} {r65['win_rate']-r63['win_rate']:+.1f}%")
print(f"{'总盈亏($)':<20} {r63['pnl_usd']:<+18,.0f} {r65['pnl_usd']:<+18,.0f} {r65['pnl_usd']-r63['pnl_usd']:<+15,.0f}")
print(f"{'盈亏比(PF)':<20} {r63['profit_factor']:<18} {r65['profit_factor']:<18}")
print(f"{'最大回撤($)':<20} {r63['max_drawdown']:<18,.0f} {r65['max_drawdown']:<18,.0f}")
print(f"{'日均盈亏($)':<20} {r63['daily_avg']:<+18,.0f} {r65['daily_avg']:<+18,.0f}")
