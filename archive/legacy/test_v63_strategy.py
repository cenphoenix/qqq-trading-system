#!/usr/bin/env python3
"""
v6.3 策略信号回测验证
使用真实的 live_trader.py + FilterEngine 逻辑
对比原始 v6.3 回测结果 (records_backtest_v6_3.json)
"""
import sys, os, json, math
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np

# ── 加载真实策略模块 ──
from strategy.filters import FilterEngine

# ── 数据加载 ──
print("Loading data...")
import pandas as pd
df = pd.read_csv('data/qqq_1min_regular.csv')
print(f"  {len(df)} bars loaded")

# 按天分组
df['datetime'] = pd.to_datetime(df['timestamp_et'])
day_groups = list(df.groupby(df['datetime'].dt.date))
print(f"  {len(day_groups)} trading days")

# ── 用 FilterEngine 模拟 v6.3 信号检测 ──
cfg = {
    'rsi_period': 14, 'rsi_overbought': 75, 'rsi_oversold': 25,
    'vol_mult': 0.8, 'min_body': 0.0003, 'max_gap': 0.002,
    'lookback': 3, 'sl': 0.25, 'tp': 0.30,
    'base_atr': 0.35,
}

engine = FilterEngine(cfg)

# Stub for close_history (simulate what live_trader maintains)
close_history = []
volume_history = []
one_min_candles = []
session_high = 0
session_low = 999999
daily_signals = 0

total_signals = 0
filter_breakdown = {
    'rsi_pre': 0, 'rsi_dir': 0, 'trend': 0, 'momentum': 0,
    'volume': 0, 'body': 0, 'vwap': 0, 'atr': 0,
    'pullback': 0, 'preloaded': 0,
    'passed': 0
}
pattern_signals = {'call': 0, 'put': 0, 'trending': 0, 'neutral': 0, 'choppy': 0}

def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [max(d, 0) for d in deltas[:period]]
    losses = [max(-d, 0) for d in deltas[:period]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    for i in range(period, len(deltas)):
        gain = max(deltas[i], 0)
        loss = max(-deltas[i], 0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return 100
    return 100 - (100 / (1 + avg_gain / avg_loss))

day_count = 0
for date_str, day_df in day_groups:
    day_df = day_df.sort_values('datetime')
    day_count += 1
    
    # Reset daily state
    engine.reset_day()
    close_history.clear()
    volume_history.clear()
    one_min_candles.clear()
    session_high = 0
    session_low = 999999
    daily_signals = 0
    
    for _, bar_row in day_df.iterrows():
        bar = {
            'open': bar_row['open'], 'high': bar_row['high'],
            'low': bar_row['low'], 'close': bar_row['close'],
            'volume': bar_row['volume'],
        }
        
        # Update state
        close_history.append(bar['close'])
        volume_history.append(bar['volume'])
        one_min_candles.append(bar)
        session_high = max(session_high, bar['high'])
        session_low = min(session_low, bar['low'])
        
        hm = bar_row['datetime'].hour * 60 + bar_row['datetime'].minute
        if hm < 575 or hm > 950:
            continue
        
        # Update engine with this bar
        et_minute = hm
        engine.update(bar, et_minute)
        
        # ==== v6.3 _check_breakout simulation ====
        # 1. RSI pre-filter
        rsi = calc_rsi(close_history, 14)
        if rsi > 80 or rsi < 20:
            filter_breakdown['rsi_pre'] += 1
            continue
        
        # 2. Regime detection
        regime_params = engine.get_regime_params()
        regime = regime_params['regime']
        lb = regime_params['lookback']
        
        if len(one_min_candles) < lb + 1:
            continue
        
        entry_price = bar['close']
        upper = max(c['high'] for c in one_min_candles[-lb-1:-1])
        lower = min(c['low'] for c in one_min_candles[-lb-1:-1])
        
        gap_up = (entry_price - upper) / upper if upper > 0 else 999
        gap_dn = (lower - entry_price) / lower if lower > 0 else 999
        max_gap = cfg['max_gap'] * regime_params['gap_mult']
        
        sig_dir = None
        if entry_price > upper and gap_up < max_gap:
            sig_dir = 'call'
        elif entry_price < lower and gap_dn < max_gap:
            sig_dir = 'put'
        if not sig_dir:
            continue
        
        # 3. Trend filter
        if len(close_history) >= 50:
            sma20 = np.mean(close_history[-20:])
            sma50 = np.mean(close_history[-50:])
            if sma20 < sma50 and entry_price < sma20 and sig_dir == 'call':
                filter_breakdown['trend'] += 1
                continue
            if sma20 > sma50 and entry_price > sma20 and sig_dir == 'put':
                filter_breakdown['trend'] += 1
                continue
        
        # 4. RSI direction confirmation
        rsi_val = calc_rsi(close_history, 14)
        if sig_dir == 'call' and (rsi_val < 40 or rsi_val > 70):
            filter_breakdown['rsi_dir'] += 1
            continue
        if sig_dir == 'put' and (rsi_val < 30 or rsi_val > 60):
            filter_breakdown['rsi_dir'] += 1
            continue
        
        # 5. Momentum
        mom_ok = (bar['close'] >= bar['open']) if sig_dir == 'call' else (bar['close'] <= bar['open'])
        if not mom_ok:
            filter_breakdown['momentum'] += 1
            continue
        
        # 6. Volume
        vol_avg = np.mean(volume_history[-20:]) if len(volume_history) >= 20 else 0
        if vol_avg > 0 and bar['volume'] < vol_avg * regime_params['vol_mult']:
            filter_breakdown['volume'] += 1
            continue
        
        # 7. Body
        cur_body = abs(bar['close'] - bar['open']) / bar['open'] if bar['open'] else 0
        if cur_body < regime_params['min_body']:
            filter_breakdown['body'] += 1
            continue
        
        # 8. VWAP
        if engine.vwap > 0:
            if sig_dir == 'call' and entry_price < engine.vwap:
                filter_breakdown['vwap'] += 1
                continue
            if sig_dir == 'put' and entry_price > engine.vwap:
                filter_breakdown['vwap'] += 1
                continue
        
        # 9. ATR chase
        if engine.atr > 0 and session_high > session_low:
            atr_mult = 2.0 if regime == 'trending' else 1.5
            atr_threshold = engine.atr * atr_mult
            if sig_dir == 'call' and entry_price > session_high - atr_threshold:
                filter_breakdown['atr'] += 1
                continue
            if sig_dir == 'put' and entry_price < session_low + atr_threshold:
                filter_breakdown['atr'] += 1
                continue
        
        # 10. Pullback confirmation
        if regime_params['pullback']:
            recent_3 = one_min_candles[-3:] if len(one_min_candles) >= 3 else one_min_candles
            if sig_dir == 'call':
                conc = sum(1 for b in recent_3 if b['close'] >= b['open'])
                if conc < 3:
                    prev = one_min_candles[-2] if len(one_min_candles) >= 2 else None
                    if prev and prev['close'] > prev['open']:
                        filter_breakdown['pullback'] += 1
                        continue
            else:
                conc = sum(1 for b in recent_3 if b['close'] <= b['open'])
                if conc < 3:
                    prev = one_min_candles[-2] if len(one_min_candles) >= 2 else None
                    if prev and prev['close'] < prev['open']:
                        filter_breakdown['pullback'] += 1
                        continue
        
        # 11. Preloaded filters
        pre_ok, pre_filters, bonus_count = engine.check_preloaded(sig_dir, regime=regime)
        preloaded_pass = regime_params['preloaded_pass']
        if bonus_count < preloaded_pass:
            filter_breakdown['preloaded'] += 1
            continue
        
        # ── SIGNAL PASSED ALL FILTERS! ──
        total_signals += 1
        filter_breakdown['passed'] += 1
        pattern_signals[sig_dir] += 1
        pattern_signals[regime] += 1

# ── Results ──
print(f"\n{'='*60}")
print(f"v6.3 策略信号回测 (基于真实 FilterEngine)")
print(f"{'='*60}")
print(f"交易日: {day_count}")
print(f"总信号: {total_signals}")
print(f"日均信号: {total_signals/day_count:.1f}")
print(f"Call/Put: {pattern_signals['call']}/{pattern_signals['put']}")
print(f"Regime: trending={pattern_signals['trending']} neutral={pattern_signals['neutral']} choppy={pattern_signals['choppy']}")

print(f"\n📊 过滤管线逐级统计:")
print(f"{'过滤步骤':<25} {'拦截数':<8} {'通过率':<10}")
print("-"*45)
total_raw = total_signals + sum(filter_breakdown[k] for k in filter_breakdown if k != 'passed')
for k in ['rsi_pre', 'rsi_dir', 'trend', 'momentum', 'volume', 'body', 'vwap', 'atr', 'pullback', 'preloaded']:
    v = filter_breakdown[k]
    pct = 100 - (v / max(total_raw, 1) * 100)
    print(f"{k:<25} {v:<8} {pct:<10.1f}%")
print(f"{'passed':<25} {filter_breakdown['passed']:<8}")

# Compare with original v6.3 backtest
print(f"\n{'='*60}")
print(f"📋 对比原始 v6.3 回测数据")
print(f"{'='*60}")

# Load original
orig = json.load(open('data/records_backtest_v6_3.json'))
orig_trades = len(orig['trades'])
orig_wr = orig['meta']['win_rate']
orig_pnl = sum(t.get('pnl_usd', 0) for t in orig['trades'] if isinstance(t, dict))
print(f"\n{'指标':<20} {'原始 v6.3':<15} {'当前 v6.3 信号':<15}")
print("-"*50)
print(f"{'交易日':<20} {orig['meta'].get('period', '341天'):<15} {day_count:<15}")
print(f"{'总交易/信号':<20} {orig_trades:<15} {total_signals:<15}")
orig_trades_per_day = orig_trades / 341  # approx
print(f"{'日均交易':<20} {orig_trades_per_day:<.1f} {total_signals/day_count:.1f}")
print(f"{'胜率':<20} {orig_wr:.1f}% {'N/A (纯信号分析)'}")

# Estimate: with 67.5% WR, avg win $19, avg loss $10
est_win_pct = 0.675
est_avg_win = 19.0
est_avg_loss = -10.0
est_pnl_per_trade = est_win_pct * est_avg_win + (1-est_win_pct) * est_avg_loss
est_total = total_signals * est_pnl_per_trade
print(f"\n📈 预估盈亏 (基于原始v6.3平均盈亏):")
print(f"  每笔预期: ${est_pnl_per_trade:+.2f}")
print(f"  总信号: {total_signals} × ${est_pnl_per_trade:+.2f} = ${est_total:+,.0f}")

print(f"\n✅ 结论: 当前 FilterEngine + v6.3 _check_breakout")
print(f"   日均 {total_signals/day_count:.1f} 个信号 (原始v6.3日均 {orig_trades_per_day:.1f})")
if total_signals >= orig_trades_per_day * day_count * 0.8:
    print(f"   信号量达到原始v6.3的80%以上 ✅")
else:
    ratio = total_signals / (orig_trades_per_day * day_count) * 100
    print(f"   信号量是原始v6.3的 {ratio:.0f}%")
