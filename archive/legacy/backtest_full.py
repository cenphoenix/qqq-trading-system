#!/usr/bin/env python3
"""
QQQ 0DTE 策略完整回测 - 对比 v6.5 和 v7
测试不同参数组合，输出详细结果到文件
"""
import json
import math
import numpy as np
import pandas as pd
from datetime import datetime, date, time as dtime, timedelta
from pathlib import Path
import os

# ===== 策略参数 =====
V6_5_CONFIG = {
    'name': 'v6.5',
    'option_offset': 2.0,
    'sl': 0.25,
    'tp': 0.30,
    'lookback': 3,
    'max_trades': 999,
    'daily_limit': 0.25,
    'start_h': 9, 'start_m': 35,
    'end_h': 14, 'end_m': 30,
    'trail_activate': 0.10,
    'trail_drop': 0.05,
    'max_gap': 0.0020,
    'vol_mult': 0.8,
    'min_body': 0.0003,
    'capital': 100000,
    'order_pct': 8,
    'contract_multiplier': 100,
    'reversal_drop': 0.002,
    'reversal_bounce': 0.001,
    'rsi_period': 14,
    'rsi_overbought': 75,
    'rsi_oversold': 25,
    'loss_cooldown': 3,
    'tp_partial_pct': 1.0,
}

V7_CONFIG = {
    'name': 'v7',
    'option_offset': 2.0,
    'sl': 0.25,
    'tp': 0.30,
    'lookback': 3,
    'max_trades': 999,
    'daily_limit': 0.25,
    'start_h': 9, 'start_m': 35,
    'end_h': 14, 'end_m': 30,
    'trail_activate': 0.10,
    'trail_drop': 0.05,
    'max_gap': 0.0020,
    'vol_mult': 0.8,
    'min_body': 0.0003,
    'capital': 100000,
    'order_pct': 8,
    'contract_multiplier': 100,
    'reversal_drop': 0.002,
    'reversal_bounce': 0.001,
    'rsi_period': 14,
    'rsi_overbought': 75,
    'rsi_oversold': 25,
    'loss_cooldown': 3,
    'tp_partial_pct': 1.0,
    # v7 新增
    'vwap_enabled': True,
    'bollinger_enabled': True,
    'rsi_divergence_enabled': True,
    'ema_cross_enabled': True,
    'opening_breakout_enabled': True,
    'vix_filter_enabled': True,
}

# 参数优化测试组合
PARAM_GRID = {
    'sl': [0.15, 0.20, 0.25, 0.30],
    'tp': [0.25, 0.30, 0.40, 0.50],
    'order_pct': [5, 8, 10],
    'loss_cooldown': [2, 3, 5],
}


# ===== Black-Scholes 定价 =====
def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_option_price(S, K, T, r, sigma, option_type='call'):
    if T <= 0:
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
    return remaining_seconds / (252 * 6.5 * 3600)


# ===== 数据加载 =====
def load_data():
    """加载并合并所有K线数据"""
    data_dir = Path(__file__).parent / 'data'
    
    # 加载历史数据
    dfs = []
    
    # 1. 加载 qqq_1min_cleaned.csv
    hist_file = data_dir / 'qqq_1min_cleaned.csv'
    if hist_file.exists():
        df_hist = pd.read_csv(hist_file)
        df_hist = df_hist.rename(columns={
            'timestamp_et': 'Datetime',
            'open': 'Open',
            'high': 'High',
            'low': 'Low',
            'close': 'Close',
            'volume': 'Volume'
        })
        df_hist['Datetime'] = pd.to_datetime(df_hist['Datetime'])
        dfs.append(df_hist[['Datetime', 'Open', 'High', 'Low', 'Close', 'Volume']])
        print(f"📊 加载历史数据: {len(df_hist)} 条")
    
    # 2. 加载最近的 candles 数据
    candles_dir = data_dir / 'candles'
    if candles_dir.exists():
        for csv_file in sorted(candles_dir.glob('*.csv')):
            df_day = pd.read_csv(csv_file)
            # candles 文件列名: timestamp,open,high,low,close,volume,turnover
            col_map = {}
            for col in df_day.columns:
                col_lower = col.lower().strip()
                if col_lower in ('timestamp', 'timestamp_et', 'timestamp_cst'):
                    col_map[col] = 'Datetime'
                elif col_lower == 'open':
                    col_map[col] = 'Open'
                elif col_lower == 'high':
                    col_map[col] = 'High'
                elif col_lower == 'low':
                    col_map[col] = 'Low'
                elif col_lower == 'close':
                    col_map[col] = 'Close'
                elif col_lower == 'volume':
                    col_map[col] = 'Volume'
            df_day = df_day.rename(columns=col_map)
            if 'Datetime' not in df_day.columns:
                continue
            df_day['Datetime'] = pd.to_datetime(df_day['Datetime'])
            # 只保留美股交易时间 (09:30-16:00 ET)
            df_day = df_day[df_day['Datetime'].dt.time >= dtime(9, 30)]
            dfs.append(df_day[['Datetime', 'Open', 'High', 'Low', 'Close', 'Volume']])
    
    if not dfs:
        print("❌ 没有找到数据文件")
        return None
    
    # 合并并去重
    df = pd.concat(dfs, ignore_index=True)
    df = df.drop_duplicates(subset=['Datetime']).sort_values('Datetime').reset_index(drop=True)
    
    # 添加日期列
    df['Date'] = df['Datetime'].dt.date
    
    print(f"📊 总数据: {len(df)} 条, 日期范围: {df['Date'].min()} ~ {df['Date'].max()}")
    return df


# ===== 信号检测 =====
def calc_rsi(closes, period=14):
    """计算RSI"""
    if len(closes) < period + 1:
        return 50
    deltas = np.diff(closes[-period-1:])
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_gain = np.mean(gains)
    avg_loss = np.mean(losses)
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calc_ema(closes, period):
    """计算EMA"""
    if len(closes) < period:
        return closes[-1] if closes else 0
    multiplier = 2 / (period + 1)
    ema = closes[0]
    for price in closes[1:]:
        ema = (price - ema) * multiplier + ema
    return ema


def check_breakout_signal(df_day, idx, cfg):
    """检测突破信号"""
    if idx < cfg['lookback']:
        return None
    
    current = df_day.iloc[idx]
    lookback_bars = df_day.iloc[idx-cfg['lookback']:idx]
    
    high_lookback = lookback_bars['High'].max()
    low_lookback = lookback_bars['Low'].min()
    
    close = current['Close']
    volume = current['Volume']
    avg_volume = lookback_bars['Volume'].mean()
    
    # 向上突破
    if close > high_lookback and volume > avg_volume * cfg['vol_mult']:
        body = abs(close - current['Open']) / current['Open'] if current['Open'] > 0 else 0
        if body >= cfg['min_body']:
            return {
                'dir': 'call',
                'price': close,
                'reason': f'突破{high_lookback:.2f}',
                'strength': 1.0
            }
    
    # 向下突破
    if close < low_lookback and volume > avg_volume * cfg['vol_mult']:
        body = abs(close - current['Open']) / current['Open'] if current['Open'] > 0 else 0
        if body >= cfg['min_body']:
            return {
                'dir': 'put',
                'price': close,
                'reason': f'跌破{low_lookback:.2f}',
                'strength': 1.0
            }
    
    return None


def check_reversal_signal(df_day, idx, cfg, session_high, session_low):
    """检测反转信号"""
    if idx < 3:
        return None
    
    current = df_day.iloc[idx]
    prev = df_day.iloc[idx-1]
    close = current['Close']
    
    # 超跌反弹
    if session_high > 0:
        drop_from_high = (session_high - close) / session_high
        if drop_from_high >= cfg['reversal_drop']:
            rsi = calc_rsi(df_day['Close'].values[:idx+1], cfg['rsi_period'])
            if rsi < cfg['rsi_oversold']:
                if prev['Close'] >= prev['Open']:
                    bounce_body = abs(prev['Close'] - prev['Open']) / prev['Open'] if prev['Open'] > 0 else 0
                    if bounce_body >= cfg['reversal_bounce']:
                        return {
                            'dir': 'call',
                            'price': close,
                            'reason': f'超跌反弹RSI{rsi:.0f}',
                            'strength': 0.8
                        }
    
    # 超涨回调
    if session_low < 999999:
        rise_from_low = (close - session_low) / session_low
        if rise_from_low >= cfg['reversal_drop']:
            rsi = calc_rsi(df_day['Close'].values[:idx+1], cfg['rsi_period'])
            if rsi > cfg['rsi_overbought']:
                if prev['Close'] <= prev['Open']:
                    drop_body = abs(prev['Close'] - prev['Open']) / prev['Open'] if prev['Open'] > 0 else 0
                    if drop_body >= cfg['reversal_bounce']:
                        return {
                            'dir': 'put',
                            'price': close,
                            'reason': f'超涨回调RSI{rsi:.0f}',
                            'strength': 0.8
                        }
    
    return None


# ===== 回测引擎 =====
def backtest_day(df_day, cfg, date_str):
    """回测单日"""
    trades = []
    position = None
    daily_pnl = 0
    consecutive_losses = 0
    session_high = 0
    session_low = 999999
    loss_cooldown_until = None
    
    start_time = dtime(cfg['start_h'], cfg['start_m'])
    end_time = dtime(cfg['end_h'], cfg['end_m'])
    
    for idx in range(len(df_day)):
        row = df_day.iloc[idx]
        current_time = row['Datetime'].time()
        
        # 更新高低点
        session_high = max(session_high, row['High'])
        session_low = min(session_low, row['Low'])
        
        # 检查时间窗口
        if current_time < start_time or current_time > end_time:
            continue
        
        # 检查冷却
        if loss_cooldown_until and current_time < loss_cooldown_until:
            continue
        
        # 持仓管理
        if position:
            # 计算当前期权价格
            T = time_to_expiry(row['Datetime'])
            sigma = 0.3  # 假设波动率
            K = position['strike']
            S = row['Close']
            
            if position['dir'] == 'call':
                current_opt_price = bs_option_price(S, K, T, 0.05, sigma, 'call')
            else:
                current_opt_price = bs_option_price(S, K, T, 0.05, sigma, 'put')
            
            if position['entry_price'] > 0:
                pnl_pct = (current_opt_price - position['entry_price']) / position['entry_price']
            else:
                pnl_pct = 0
            
            # 止损
            if pnl_pct <= -cfg['sl']:
                pnl_usd = position['contracts'] * cfg['contract_multiplier'] * (current_opt_price - position['entry_price'])
                daily_pnl += pnl_usd
                trades.append({
                    'entry_time': position['entry_time'],
                    'exit_time': current_time,
                    'dir': position['dir'],
                    'entry_price': position['entry_price'],
                    'exit_price': current_opt_price,
                    'contracts': position['contracts'],
                    'pnl_pct': pnl_pct * 100,
                    'pnl_usd': pnl_usd,
                    'reason': position['reason'],
                    'exit_reason': f'止损{pnl_pct*100:.1f}%',
                    'result': 'lose'
                })
                consecutive_losses += 1
                # 冷却
                if consecutive_losses >= 3:
                    loss_cooldown_until = (datetime.combine(date.today(), current_time) + timedelta(minutes=5)).time()
                else:
                    loss_cooldown_until = (datetime.combine(date.today(), current_time) + timedelta(minutes=cfg['loss_cooldown'])).time()
                position = None
                continue
            
            # 止盈
            if pnl_pct >= cfg['tp']:
                pnl_usd = position['contracts'] * cfg['contract_multiplier'] * (current_opt_price - position['entry_price'])
                daily_pnl += pnl_usd
                trades.append({
                    'entry_time': position['entry_time'],
                    'exit_time': current_time,
                    'dir': position['dir'],
                    'entry_price': position['entry_price'],
                    'exit_price': current_opt_price,
                    'contracts': position['contracts'],
                    'pnl_pct': pnl_pct * 100,
                    'pnl_usd': pnl_usd,
                    'reason': position['reason'],
                    'exit_reason': f'止盈{pnl_pct*100:.1f}%',
                    'result': 'win'
                })
                consecutive_losses = 0
                position = None
                continue
            
            # 硬超时（8分钟）
            entry_dt = datetime.combine(date.today(), position['entry_time'])
            current_dt = datetime.combine(date.today(), current_time)
            if (current_dt - entry_dt).total_seconds() >= 480:
                pnl_usd = position['contracts'] * cfg['contract_multiplier'] * (current_opt_price - position['entry_price'])
                daily_pnl += pnl_usd
                trades.append({
                    'entry_time': position['entry_time'],
                    'exit_time': current_time,
                    'dir': position['dir'],
                    'entry_price': position['entry_price'],
                    'exit_price': current_opt_price,
                    'contracts': position['contracts'],
                    'pnl_pct': pnl_pct * 100,
                    'pnl_usd': pnl_usd,
                    'reason': position['reason'],
                    'exit_reason': '硬超时8分钟',
                    'result': 'win' if pnl_pct > 0 else 'lose'
                })
                if pnl_pct <= 0:
                    consecutive_losses += 1
                else:
                    consecutive_losses = 0
                position = None
                continue
        
        # 开仓信号检测
        if position is None:
            # 突破信号
            signal = check_breakout_signal(df_day, idx, cfg)
            
            # 反转信号
            if signal is None:
                signal = check_reversal_signal(df_day, idx, cfg, session_high, session_low)
            
            if signal:
                # 计算期权价格
                T = time_to_expiry(row['Datetime'])
                sigma = 0.3
                K = math.floor(signal['price'] + cfg['option_offset']) if signal['dir'] == 'call' else math.ceil(signal['price'] - cfg['option_offset'])
                
                if signal['dir'] == 'call':
                    opt_price = bs_option_price(signal['price'], K, T, 0.05, sigma, 'call')
                else:
                    opt_price = bs_option_price(signal['price'], K, T, 0.05, sigma, 'put')
                
                if opt_price <= 0:
                    continue
                
                # 计算合约数
                order_amount = cfg['capital'] * cfg['order_pct'] / 100
                contracts = max(1, int(order_amount / (opt_price * cfg['contract_multiplier'])))
                
                position = {
                    'dir': signal['dir'],
                    'entry_time': current_time,
                    'entry_price': opt_price,
                    'strike': K,
                    'contracts': contracts,
                    'reason': signal['reason']
                }
    
    # 收盘强平
    if position:
        last_row = df_day.iloc[-1]
        T = time_to_expiry(last_row['Datetime'])
        sigma = 0.3
        K = position['strike']
        S = last_row['Close']
        
        if position['dir'] == 'call':
            exit_price = bs_option_price(S, K, T, 0.05, sigma, 'call')
        else:
            exit_price = bs_option_price(S, K, T, 0.05, sigma, 'put')
        
        pnl_pct = (exit_price - position['entry_price']) / position['entry_price'] if position['entry_price'] > 0 else 0
        pnl_usd = position['contracts'] * cfg['contract_multiplier'] * (exit_price - position['entry_price'])
        daily_pnl += pnl_usd
        
        trades.append({
            'entry_time': position['entry_time'],
            'exit_time': last_row['Datetime'].time(),
            'dir': position['dir'],
            'entry_price': position['entry_price'],
            'exit_price': exit_price,
            'contracts': position['contracts'],
            'pnl_pct': pnl_pct * 100,
            'pnl_usd': pnl_usd,
            'reason': position['reason'],
            'exit_reason': '收盘强平',
            'result': 'win' if pnl_pct > 0 else 'lose'
        })
    
    return {
        'date': date_str,
        'trades': trades,
        'daily_pnl': daily_pnl,
        'trade_count': len(trades),
        'win_count': sum(1 for t in trades if t['result'] == 'win'),
        'lose_count': sum(1 for t in trades if t['result'] == 'lose'),
    }


def run_backtest(df, cfg):
    """运行完整回测"""
    results = []
    dates = df['Date'].unique()
    
    for trade_date in dates:
        df_day = df[df['Date'] == trade_date].copy()
        if len(df_day) < 30:  # 数据太少跳过
            continue
        
        df_day = df_day.reset_index(drop=True)
        day_result = backtest_day(df_day, cfg, str(trade_date))
        results.append(day_result)
    
    return results


def calculate_stats(results, cfg_name):
    """计算统计数据"""
    all_trades = []
    for r in results:
        all_trades.extend(r['trades'])
    
    if not all_trades:
        return {
            'strategy': cfg_name,
            'total_trades': 0,
            'win_rate': 0,
            'total_pnl': 0,
            'avg_pnl': 0,
            'max_win': 0,
            'max_loss': 0,
            'max_consecutive_losses': 0,
            'max_drawdown': 0,
            'sharpe_ratio': 0,
            'profit_factor': 0,
        }
    
    total_trades = len(all_trades)
    win_trades = [t for t in all_trades if t['result'] == 'win']
    lose_trades = [t for t in all_trades if t['result'] == 'lose']
    
    win_rate = len(win_trades) / total_trades * 100 if total_trades > 0 else 0
    total_pnl = sum(t['pnl_usd'] for t in all_trades)
    avg_pnl = total_pnl / total_trades if total_trades > 0 else 0
    
    max_win = max(t['pnl_usd'] for t in all_trades) if all_trades else 0
    max_loss = min(t['pnl_usd'] for t in all_trades) if all_trades else 0
    
    # 计算最大连续亏损
    max_consecutive_losses = 0
    current_losses = 0
    for t in all_trades:
        if t['result'] == 'lose':
            current_losses += 1
            max_consecutive_losses = max(max_consecutive_losses, current_losses)
        else:
            current_losses = 0
    
    # 计算最大回撤
    cumulative_pnl = 0
    peak = 0
    max_drawdown = 0
    for t in all_trades:
        cumulative_pnl += t['pnl_usd']
        peak = max(peak, cumulative_pnl)
        drawdown = peak - cumulative_pnl
        max_drawdown = max(max_drawdown, drawdown)
    
    # 计算夏普比率
    daily_returns = [r['daily_pnl'] for r in results]
    if len(daily_returns) > 1:
        sharpe_ratio = np.mean(daily_returns) / np.std(daily_returns) * np.sqrt(252) if np.std(daily_returns) > 0 else 0
    else:
        sharpe_ratio = 0
    
    # 计算盈亏比
    avg_win = np.mean([t['pnl_usd'] for t in win_trades]) if win_trades else 0
    avg_loss = abs(np.mean([t['pnl_usd'] for t in lose_trades])) if lose_trades else 1
    profit_factor = avg_win / avg_loss if avg_loss > 0 else 0
    
    return {
        'strategy': cfg_name,
        'total_trades': total_trades,
        'win_trades': len(win_trades),
        'lose_trades': len(lose_trades),
        'win_rate': round(win_rate, 2),
        'total_pnl': round(total_pnl, 2),
        'avg_pnl': round(avg_pnl, 2),
        'max_win': round(max_win, 2),
        'max_loss': round(max_loss, 2),
        'max_consecutive_losses': max_consecutive_losses,
        'max_drawdown': round(max_drawdown, 2),
        'sharpe_ratio': round(sharpe_ratio, 2),
        'profit_factor': round(profit_factor, 2),
        'avg_win': round(avg_win, 2),
        'avg_loss': round(avg_loss, 2),
    }


# ===== 参数优化 =====
def grid_search(df):
    """网格搜索最优参数"""
    results = []
    total_combos = len(PARAM_GRID['sl']) * len(PARAM_GRID['tp']) * len(PARAM_GRID['order_pct']) * len(PARAM_GRID['loss_cooldown'])
    current = 0
    
    for sl in PARAM_GRID['sl']:
        for tp in PARAM_GRID['tp']:
            for order_pct in PARAM_GRID['order_pct']:
                for loss_cooldown in PARAM_GRID['loss_cooldown']:
                    current += 1
                    print(f"\r  测试参数组合 {current}/{total_combos}: sl={sl}, tp={tp}, order_pct={order_pct}, cooldown={loss_cooldown}", end="", flush=True)
                    
                    cfg = V7_CONFIG.copy()
                    cfg['sl'] = sl
                    cfg['tp'] = tp
                    cfg['order_pct'] = order_pct
                    cfg['loss_cooldown'] = loss_cooldown
                    
                    day_results = run_backtest(df, cfg)
                    stats = calculate_stats(day_results, f"sl{sl}_tp{tp}_op{order_pct}_cd{loss_cooldown}")
                    
                    stats['sl'] = sl
                    stats['tp'] = tp
                    stats['order_pct'] = order_pct
                    stats['loss_cooldown'] = loss_cooldown
                    
                    results.append(stats)
    
    print()  # 换行
    return results


# ===== 主程序 =====
def main():
    print("=" * 60)
    print("QQQ 0DTE 策略完整回测")
    print("=" * 60)
    
    # 创建输出目录
    output_dir = Path(__file__).parent / 'backtest_results'
    output_dir.mkdir(exist_ok=True)
    
    # 加载数据
    print("\n📊 加载数据...")
    df = load_data()
    if df is None:
        return
    
    # 1. v6.5 基准测试
    print("\n🔵 运行 v6.5 基准策略...")
    v6_5_results = run_backtest(df, V6_5_CONFIG)
    v6_5_stats = calculate_stats(v6_5_results, 'v6.5')
    print(f"  ✅ v6.5: {v6_5_stats['total_trades']}笔交易, 胜率{v6_5_stats['win_rate']}%, 盈亏${v6_5_stats['total_pnl']:+,.2f}")
    
    # 2. v7 策略测试
    print("\n🟢 运行 v7 策略...")
    v7_results = run_backtest(df, V7_CONFIG)
    v7_stats = calculate_stats(v7_results, 'v7')
    print(f"  ✅ v7: {v7_stats['total_trades']}笔交易, 胜率{v7_stats['win_rate']}%, 盈亏${v7_stats['total_pnl']:+,.2f}")
    
    # 3. 参数优化
    print("\n🟡 运行参数优化...")
    param_results = grid_search(df)
    
    # 找出最优参数
    best_by_pnl = max(param_results, key=lambda x: x['total_pnl'])
    best_by_winrate = max(param_results, key=lambda x: x['win_rate'])
    best_by_sharpe = max(param_results, key=lambda x: x['sharpe_ratio'])
    lowest_consecutive_losses = min(param_results, key=lambda x: x['max_consecutive_losses'])
    
    print(f"\n📊 最优参数:")
    print(f"  最高盈亏: sl={best_by_pnl['sl']}, tp={best_by_pnl['tp']}, order_pct={best_by_pnl['order_pct']}, cooldown={best_by_pnl['loss_cooldown']}")
    print(f"    盈亏: ${best_by_pnl['total_pnl']:+,.2f}, 胜率: {best_by_pnl['win_rate']}%")
    print(f"  最高胜率: sl={best_by_winrate['sl']}, tp={best_by_winrate['tp']}, order_pct={best_by_winrate['order_pct']}, cooldown={best_by_winrate['loss_cooldown']}")
    print(f"    胜率: {best_by_winrate['win_rate']}%, 盈亏: ${best_by_winrate['total_pnl']:+,.2f}")
    print(f"  最高夏普: sl={best_by_sharpe['sl']}, tp={best_by_sharpe['tp']}, order_pct={best_by_sharpe['order_pct']}, cooldown={best_by_sharpe['loss_cooldown']}")
    print(f"    夏普: {best_by_sharpe['sharpe_ratio']}, 盈亏: ${best_by_sharpe['total_pnl']:+,.2f}")
    print(f"  最低连亏: sl={lowest_consecutive_losses['sl']}, tp={lowest_consecutive_losses['tp']}, order_pct={lowest_consecutive_losses['order_pct']}, cooldown={lowest_consecutive_losses['loss_cooldown']}")
    print(f"    连亏: {lowest_consecutive_losses['max_consecutive_losses']}笔, 盈亏: ${lowest_consecutive_losses['total_pnl']:+,.2f}")
    
    # ===== 保存结果 =====
    print("\n💾 保存结果...")
    
    # 1. 汇总对比
    summary_df = pd.DataFrame([v6_5_stats, v7_stats])
    summary_df.to_csv(output_dir / 'summary.csv', index=False)
    print(f"  ✅ summary.csv")
    
    # 2. v6.5 详细结果
    v6_5_trades = []
    for r in v6_5_results:
        for t in r['trades']:
            t['date'] = r['date']
            v6_5_trades.append(t)
    pd.DataFrame(v6_5_trades).to_csv(output_dir / 'v6.5_results.csv', index=False)
    print(f"  ✅ v6.5_results.csv ({len(v6_5_trades)}笔)")
    
    # 3. v7 详细结果
    v7_trades = []
    for r in v7_results:
        for t in r['trades']:
            t['date'] = r['date']
            v7_trades.append(t)
    pd.DataFrame(v7_trades).to_csv(output_dir / 'v7_results.csv', index=False)
    print(f"  ✅ v7_results.csv ({len(v7_trades)}笔)")
    
    # 4. 参数优化结果
    param_df = pd.DataFrame(param_results)
    param_df = param_df.sort_values('total_pnl', ascending=False)
    param_df.to_csv(output_dir / 'parameter_optimization.csv', index=False)
    print(f"  ✅ parameter_optimization.csv")
    
    # 5. 生成报告
    report = f"""# QQQ 0DTE 策略回测报告

## 数据范围
- 开始日期: {df['Date'].min()}
- 结束日期: {df['Date'].max()}
- 交易日数: {len(df['Date'].unique())}天

## 策略对比

| 指标 | v6.5 | v7 |
|------|------|-----|
| 总交易数 | {v6_5_stats['total_trades']} | {v7_stats['total_trades']} |
| 胜率 | {v6_5_stats['win_rate']}% | {v7_stats['win_rate']}% |
| 总盈亏 | ${v6_5_stats['total_pnl']:+,.2f} | ${v7_stats['total_pnl']:+,.2f} |
| 平均盈亏 | ${v6_5_stats['avg_pnl']:+,.2f} | ${v7_stats['avg_pnl']:+,.2f} |
| 最大单笔盈利 | ${v6_5_stats['max_win']:+,.2f} | ${v7_stats['max_win']:+,.2f} |
| 最大单笔亏损 | ${v6_5_stats['max_loss']:+,.2f} | ${v7_stats['max_loss']:+,.2f} |
| 最大连续亏损 | {v6_5_stats['max_consecutive_losses']}笔 | {v7_stats['max_consecutive_losses']}笔 |
| 最大回撤 | ${v6_5_stats['max_drawdown']:+,.2f} | ${v7_stats['max_drawdown']:+,.2f} |
| 夏普比率 | {v6_5_stats['sharpe_ratio']} | {v7_stats['sharpe_ratio']} |
| 盈亏比 | {v6_5_stats['profit_factor']} | {v7_stats['profit_factor']} |

## 参数优化结果

### 最优参数组合

**最高盈亏:**
- 止损: {best_by_pnl['sl']*100}%
- 止盈: {best_by_pnl['tp']*100}%
- 仓位: {best_by_pnl['order_pct']}%
- 冷却: {best_by_pnl['loss_cooldown']}分钟
- 结果: 盈亏${best_by_pnl['total_pnl']:+,.2f}, 胜率{best_by_pnl['win_rate']}%, 连亏{best_by_pnl['max_consecutive_losses']}笔

**最高胜率:**
- 止损: {best_by_winrate['sl']*100}%
- 止盈: {best_by_winrate['tp']*100}%
- 仓位: {best_by_winrate['order_pct']}%
- 冷却: {best_by_winrate['loss_cooldown']}分钟
- 结果: 胜率{best_by_winrate['win_rate']}%, 盈亏${best_by_winrate['total_pnl']:+,.2f}

**最低连亏:**
- 止损: {lowest_consecutive_losses['sl']*100}%
- 止盈: {lowest_consecutive_losses['tp']*100}%
- 仓位: {lowest_consecutive_losses['order_pct']}%
- 冷却: {lowest_consecutive_losses['loss_cooldown']}分钟
- 结果: 连亏{lowest_consecutive_losses['max_consecutive_losses']}笔, 盈亏${lowest_consecutive_losses['total_pnl']:+,.2f}

## 建议

1. **减少连续亏损**: 当前最大连续亏损{v7_stats['max_consecutive_losses']}笔，建议增加冷却时间或降低仓位
2. **参数调整**: 参考最优参数组合进行调整
3. **风险控制**: 最大回撤${v7_stats['max_drawdown']:+,.2f}，需要确保资金充足

---
*报告生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*
"""
    
    with open(output_dir / 'report.md', 'w', encoding='utf-8') as f:
        f.write(report)
    print(f"  ✅ report.md")
    
    print("\n" + "=" * 60)
    print("回测完成！结果保存在 backtest_results/ 目录")
    print("=" * 60)


if __name__ == '__main__':
    main()
