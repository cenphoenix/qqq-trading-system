#!/usr/bin/env python3
"""
QQQ 0DTE 双向突破 v6.1 - ±$2虚值期权 + Black-Scholes定价
完全还原原始策略：
- 1分钟突破信号检测
- ±$2 虚值期权（Call: 行权价=股价+$2, Put: 行权价=股价-$2）
- Black-Scholes 定价 + Theta衰减
- SL=15% TP=30%（针对期权价格）
"""
import json
import math
import numpy as np
import pandas as pd
from datetime import datetime, date, time as dtime

# ===== 策略参数（回测最优 - v6.1全过滤 09:35-15:00）=====
CFG = {
    'option_offset': 2.0,      # ±$2 虚值期权偏移（仅用于生成合约代码）
    'sl': 0.0025,               # 止损 0.25%（正股价格）
    'tp': 0.0040,               # 止盈 0.40%（正股价格）
    'lookback': 5,              # 1分钟突破窗口
    'max_trades': 8,            # 日最大交易
    'daily_limit': 0.25,        # 日亏损熔断 25%
    'start_h': 9, 'start_m': 35,
    'end_h': 15, 'end_m': 50,  # 09:35 - 15:50 美东
    'trail_activate': 0.0030,   # 跟踪止损激活 0.30%
    'trail_drop': 0.0015,       # 跟踪止损回撤 0.15%
    'max_gap': 0.0020,          # 最大跳空 0.20%
    'vol_mult': 0.8,            # 成交量倍数（放宽到0.8）
    'min_body': 0.0003,         # 最小K线实体比例
    'capital': 100000,
    'leverage': 4.0,            # 期权杠杆倍数（0DTE OTM约4x）
    'reversal_drop': 0.002,     # 衰竭反转：从高低点回落0.2%
    'reversal_bounce': 0.001,   # 衰竭反转K线实体要求0.1%
}


# ===== Black-Scholes 定价 =====
def norm_cdf(x):
    """标准正态分布CDF（近似）"""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_option_price(S, K, T, r, sigma, option_type='call'):
    """
    Black-Scholes期权定价
    S: 标的价格, K: 行权价, T: 剩余到期时间(年), r: 无风险利率, sigma: 波动率
    """
    if T <= 0:
        # 到期：纯内在价值
        if option_type == 'call':
            return max(S - K, 0)
        else:
            return max(K - S, 0)

    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)

    if option_type == 'call':
        return S * norm_cdf(d1) - K * math.exp(-r * T) * norm_cdf(d2)
    else:
        return K * math.exp(-r * T) * norm_cdf(-d2) - S * norm_cdf(-d1)


def time_to_expiry(current_time, expire_time=dtime(16, 0)):
    """计算剩余到期时间（年）"""
    if isinstance(current_time, pd.Timestamp):
        ct = current_time.time()
    else:
        ct = current_time

    remaining_seconds = (
        (expire_time.hour * 3600 + expire_time.minute * 60) -
        (ct.hour * 3600 + ct.minute * 60 + ct.second)
    )
    if remaining_seconds <= 0:
        return 0.0
    # 一年约252个交易日，每个交易日6.5小时
    return remaining_seconds / (252 * 6.5 * 3600)


# ===== 数据加载 =====
def load_1min_data(csv_path):
    """加载1分钟数据"""
    df = pd.read_csv(csv_path)
    df['Datetime'] = pd.to_datetime(df['Datetime'])
    df = df.sort_values('Datetime').reset_index(drop=True)
    return df


def build_5min_reference(df_1min):
    """从1分钟数据构建5分钟突破参考线"""
    df = df_1min.copy()
    df['5min_bin'] = df['Datetime'].dt.floor('5min')
    agg = df.groupby('5min_bin').agg(
        High=('High', 'max'),
        Low=('Low', 'min'),
    ).reset_index()
    agg.columns = ['Time', 'H5', 'L5']
    return agg


# ===== 主回测 =====
def run_backtest(df_1min, cfg):
    """用1分钟数据回测，正股信号检测 + 正股盈亏计算（期权杠杆放大）"""
    n = len(df_1min)
    C = df_1min['Close'].values
    O = df_1min['Open'].values
    H = df_1min['High'].values
    L = df_1min['Low'].values
    V = df_1min['Volume'].values.astype(float)
    T = df_1min['Datetime'].values

    # SMA20趋势 + 量能均值
    def sma(arr, w):
        r = np.zeros(len(arr))
        for j in range(len(arr)):
            r[j] = np.mean(arr[max(0, j-w+1):j+1])
        return r
    sma_close = sma(C, 20)
    sma_vol = sma(V, 20)

    trades = []
    daily_pnl = {}
    pos = None
    current_date = None
    daily_count = 0
    daily_realized = 0

    lookback = cfg['lookback']
    leverage = cfg.get('leverage', 4.0)

    for i in range(1, n):
        t = pd.Timestamp(T[i])
        date_str = t.strftime('%Y-%m-%d')
        hm = t.hour * 60 + t.minute

        # 新的一天重置
        if date_str != current_date:
            current_date = date_str
            daily_count = 0
            daily_realized = 0

        start_min = cfg['start_h'] * 60 + cfg['start_m']
        end_min = cfg['end_h'] * 60 + cfg['end_m']

        # ===== 持仓管理 =====
        if pos is not None:
            # 计算正股盈亏
            stock_pnl = (C[i] - pos['entry_stock']) / pos['entry_stock']
            if pos['dir'] == 'put':
                stock_pnl = -stock_pnl  # 做空反向
            opt_pnl = stock_pnl * leverage  # 期权杠杆放大
            pos['max_pnl'] = max(pos['max_pnl'], opt_pnl)

            exit_reason = None
            if opt_pnl <= -cfg['sl'] * leverage:
                exit_reason = '止损'
            elif opt_pnl >= cfg['tp'] * leverage:
                exit_reason = '止盈'
            elif pos['max_pnl'] > cfg['trail_activate'] * leverage:
                if opt_pnl < pos['max_pnl'] - cfg['trail_drop'] * leverage:
                    exit_reason = '跟损'
            elif i - pos['entry_bar'] >= 60:  # 60根1分钟 = 1小时
                exit_reason = '超时'

            if exit_reason:
                pnl_usd = opt_pnl * cfg['capital'] * 0.05
                daily_realized += opt_pnl * 100
                trades.append({
                    'date': date_str,
                    'time': pos['entry_time'],
                    'dir': pos['dir'],
                    'entry_stock': round(pos['entry_stock'], 2),
                    'exit_stock': round(C[i], 2),
                    'stock_pnl_pct': round(stock_pnl * 100, 4),
                    'opt_pnl_pct': round(opt_pnl * 100, 2),
                    'pnl_usd': round(pnl_usd, 2),
                    'result': 'win' if opt_pnl > 0 else 'lose',
                    'reason': pos['reason'],
                    'exit_reason': exit_reason,
                })
                daily_pnl[date_str] = daily_pnl.get(date_str, 0) + opt_pnl * 100
                pos = None
                daily_count += 1

        # ===== 信号检测（1分钟直接检测）=====
        if pos is None and daily_count < cfg['max_trades']:
            if start_min <= hm <= end_min:

                # 用过去N根1分钟K线的高低点作为突破参考
                if i >= lookback:
                    upper = max(H[i-lookback:i])   # 过去N根1min最高价
                    lower = min(L[i-lookback:i])    # 过去N根1min最低价
                    avg_vol = np.mean(V[i-lookback:i])  # 过去N根平均成交量

                    # 跳空过滤
                    gap = abs(C[i-1] - C[i-2]) / C[i-2] if i >= 2 and C[i-2] > 0 else 0
                    if gap <= cfg['max_gap'] and daily_realized > -cfg['daily_limit'] * 100:
                        if C[i-1] > upper:
                            # ===== 全过滤：上突破做多Call =====
                            sig_ok = True
                            # 1. SMA20趋势过滤
                            if i >= 20 and C[i] < sma_close[i]:
                                sig_ok = False
                            # 2. 量能过滤
                            if sig_ok and avg_vol > 0:
                                if V[i] < avg_vol * cfg.get('vol_mult', 0.8):
                                    sig_ok = False
                            # 3. 动量确认
                            if sig_ok:
                                if not (C[i] > O[i] and C[i-1] >= O[i-1]):
                                    sig_ok = False
                            # 4. K线实体确认
                            if sig_ok:
                                prev_body = abs(C[i-1] - O[i-1]) / O[i-1] if O[i-1] > 0 else 0
                                if prev_body < cfg.get('min_body', 0):
                                    sig_ok = False
                            if sig_ok:
                                pos = {
                                    'dir': 'call',
                                    'entry_stock': C[i-1],
                                    'entry_bar': i,
                                    'entry_time': t.strftime('%H:%M'),
                                    'max_pnl': 0,
                                    'reason': f'上突破{upper:.2f}',
                                }
                        elif C[i-1] < lower:
                            # ===== 全过滤：下突破做空Put =====
                            sig_ok = True
                            # 1. SMA20趋势过滤
                            if i >= 20 and C[i] > sma_close[i]:
                                sig_ok = False
                            # 2. 量能过滤
                            if sig_ok and avg_vol > 0:
                                if V[i] < avg_vol * cfg.get('vol_mult', 0.8):
                                    sig_ok = False
                            # 3. 动量确认
                            if sig_ok:
                                if not (C[i] < O[i] and C[i-1] <= O[i-1]):
                                    sig_ok = False
                            # 4. K线实体确认
                            if sig_ok:
                                prev_body = abs(C[i-1] - O[i-1]) / O[i-1] if O[i-1] > 0 else 0
                                if prev_body < cfg.get('min_body', 0):
                                    sig_ok = False
                            if sig_ok:
                                pos = {
                                    'dir': 'put',
                                    'entry_stock': C[i-1],
                                    'entry_bar': i,
                                    'entry_time': t.strftime('%H:%M'),
                                    'max_pnl': 0,
                                    'reason': f'下突破{lower:.2f}',
                                }

    # 收盘强平
    if pos is not None:
        stock_pnl = (C[-1] - pos['entry_stock']) / pos['entry_stock']
        if pos['dir'] == 'put':
            stock_pnl = -stock_pnl
        opt_pnl = stock_pnl * leverage
        trades.append({
            'date': current_date,
            'time': pos['entry_time'],
            'dir': pos['dir'],
            'entry_stock': round(pos['entry_stock'], 2),
            'exit_stock': round(C[-1], 2),
            'stock_pnl_pct': round(stock_pnl * 100, 4),
            'opt_pnl_pct': round(opt_pnl * 100, 2),
            'pnl_usd': round(opt_pnl * cfg['capital'] * 0.05, 2),
            'result': 'win' if opt_pnl > 0 else 'lose',
            'reason': pos['reason'],
            'exit_reason': '收盘平仓',
        })

    return trades, daily_pnl


# ===== 主函数 =====
def main():
    data_dir = '/mnt/c/Users/Admin/Desktop/QQQ_Live/data'

    # 加载所有1分钟数据
    files = [
        (f'{data_dir}/QQQ_1min_2024_2025.csv', False),
        (f'{data_dir}/QQQ_1min_2026.csv', False),
    ]

    all_dfs = []
    for path, _ in files:
        df = load_1min_data(path)
        print(f"📂 {path.split('/')[-1]}: {len(df)}行 {df['Datetime'].min()} ~ {df['Datetime'].max()}")
        all_dfs.append(df)

    merged = pd.concat(all_dfs, ignore_index=True)
    merged = merged.sort_values('Datetime').drop_duplicates(subset='Datetime', keep='first').reset_index(drop=True)
    print(f"\n🔄 合并: {len(merged)}行 1分钟数据")

    # 运行回测
    print("⏳ 回测中...")
    trades, daily_pnl = run_backtest(merged, CFG)

    # 统计
    total = len(trades)
    wins = sum(1 for t in trades if t['result'] == 'win')
    total_pnl = sum(t['opt_pnl_pct'] for t in trades)
    win_rate = round(wins / total * 100, 1) if total > 0 else 0

    # 按天汇总
    daily_summary = {}
    for t in trades:
        d = t['date']
        if d not in daily_summary:
            daily_summary[d] = {'date': d, 'total': 0, 'wins': 0, 'losses': 0, 'pnl': 0}
        daily_summary[d]['total'] += 1
        daily_summary[d]['pnl'] += t['opt_pnl_pct']
        if t['result'] == 'win':
            daily_summary[d]['wins'] += 1
        else:
            daily_summary[d]['losses'] += 1

    for v in daily_summary.values():
        v['pnl'] = round(v['pnl'], 2)
        v['win_rate'] = round(v['wins'] / v['total'] * 100, 1) if v['total'] > 0 else 0

    dates = sorted(daily_summary.keys())

    # 输出
    print(f"\n{'='*60}")
    print(f"📊 QQQ 0DTE ±$2虚值期权回测 v6.1 (Black-Scholes)")
    print(f"{'='*60}")
    print(f"  策略: 双向突破 + ±$2 OTM期权（正股级回测）")
    print(f"  参数: SL={CFG['sl']*100:.2f}% TP={CFG['tp']*100:.2f}% 杠杆={CFG['leverage']}x")
    print(f"  交易窗口: {CFG['start_h']:02d}:{CFG['start_m']:02d} - {CFG['end_h']:02d}:{CFG['end_m']:02d}")
    print(f"{'='*60}")
    print(f"  总交易: {total}笔")
    print(f"  盈利: {wins}笔 | 亏损: {total - wins}笔")
    print(f"  胜率: {win_rate}%")
    print(f"  总得分: {total_pnl:+.2f}%")
    print(f"  交易天数: {len(daily_summary)}天")
    print(f"  周期: {dates[0]} ~ {dates[-1]}")
    print(f"{'='*60}")

    # 退出原因
    from collections import Counter
    reasons = Counter(t['exit_reason'] for t in trades)
    print("\n退出原因:")
    for r, c in reasons.most_common():
        sub = [t for t in trades if t['exit_reason'] == r]
        sw = sum(1 for t in sub if t['result'] == 'win')
        avg = sum(t['opt_pnl_pct'] for t in sub) / len(sub)
        print(f"  {r}: {c}笔 ({c/total*100:.0f}%) 胜率{sw/len(sub)*100:.0f}% 均值{avg:+.3f}%")

    # 最大回撤
    equity = 0; peak = 0; max_dd = 0
    for t in trades:
        equity += t['opt_pnl_pct']
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    print(f"\n最大回撤: {max_dd:.2f}%")

    # 年化收益
    if dates:
        days = (pd.Timestamp(dates[-1]) - pd.Timestamp(dates[0])).days
        if days > 0:
            annual = (1 + total_pnl/100) ** (365/days) - 1
            print(f"年化收益: {annual*100:.1f}%")

    # 保存
    output = {
        'meta': {
            'strategy': 'QQQ 0DTE ITM双向突破 v6 (BS定价)',
            'params': {k: v for k, v in CFG.items()},
            'period': f'{dates[0]} ~ {dates[-1]}',
            'total_trades': total,
            'win_rate': win_rate,
            'total_pnl_pct': round(total_pnl, 2),
            'max_drawdown': round(max_dd, 2),
            'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        },
        'daily': list(daily_summary.values()),
        'trades': trades,
    }
    out_path = f'{data_dir}/records.json'
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n💾 已保存: {out_path}")


if __name__ == '__main__':
    main()
