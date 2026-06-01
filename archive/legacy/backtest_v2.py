#!/usr/bin/env python3
"""
QQQ 0DTE 策略完整回测 v2 - 优化版
修复: 信号冷却、开盘禁止、真实期权定价、v7反转逻辑
"""
import json
import math
import numpy as np
import pandas as pd
from datetime import datetime, date, time as dtime, timedelta
from pathlib import Path

# ===== 策略参数 =====
V6_5_CONFIG = {
    'name': 'v6.5',
    'option_offset': 2.0,
    'sl': 0.25,
    'tp': 0.30,
    'lookback': 5,
    'max_trades': 15,
    'daily_limit': 0.25,
    'start_h': 9, 'start_m': 40,
    'end_h': 14, 'end_m': 30,
    'post_open_cooldown': 10,      # 开盘后禁止分钟数
    'signal_cooldown': 15,          # 信号冷却分钟数
    'trail_activate': 0.10,
    'trail_drop': 0.05,
    'max_gap': 0.0020,
    'vol_mult': 0.8,
    'min_body': 0.0003,
    'capital': 100000,
    'order_pct': 8,
    'contract_multiplier': 100,
    'reversal_enabled': False,
    'reversal_drop': 0.003,
    'reversal_bounce': 0.001,
    'rsi_period': 14,
    'rsi_oversold': 25,
    'rsi_overbought': 75,
    'loss_cooldown': 3,
    'timeout_bars': 8,              # 超时分钟数
    'sigma': 0.25,                  # 默认波动率
}

V7_CONFIG = {
    'name': 'v7',
    'option_offset': 2.0,
    'sl': 0.25,
    'tp': 0.30,
    'lookback': 5,
    'max_trades': 15,
    'daily_limit': 0.25,
    'start_h': 9, 'start_m': 40,
    'end_h': 14, 'end_m': 30,
    'post_open_cooldown': 10,
    'signal_cooldown': 15,
    'trail_activate': 0.10,
    'trail_drop': 0.05,
    'max_gap': 0.0020,
    'vol_mult': 0.8,
    'min_body': 0.0003,
    'capital': 100000,
    'order_pct': 8,
    'contract_multiplier': 100,
    'reversal_enabled': True,       # v7启用反转信号
    'reversal_drop': 0.003,
    'reversal_bounce': 0.001,
    'rsi_period': 14,
    'rsi_oversold': 25,
    'rsi_overbought': 75,
    'loss_cooldown': 3,
    'timeout_bars': 8,
    'sigma': 0.25,
}

# 参数优化测试组合
PARAM_GRID = {
    'sl': [0.15, 0.20, 0.25, 0.30, 0.35],
    'tp': [0.20, 0.30, 0.40, 0.50],
    'lookback': [3, 5, 8],
    'signal_cooldown': [10, 15, 20],
}


# ===== Black-Scholes 定价 (含Theta衰减) =====
def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def norm_pdf(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)

def bs_option_price(S, K, T, r, sigma, option_type='call'):
    """Black-Scholes期权定价"""
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

def bs_greeks(S, K, T, r, sigma, option_type='call'):
    """计算Greeks (Delta, Gamma, Theta, Vega)"""
    if T <= 0:
        return {'delta': 0, 'gamma': 0, 'theta': 0, 'vega': 0}
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    
    if option_type == 'call':
        delta = norm_cdf(d1)
    else:
        delta = norm_cdf(d1) - 1
    
    gamma = norm_pdf(d1) / (S * sigma * math.sqrt(T))
    vega = S * norm_pdf(d1) * math.sqrt(T) / 100
    
    if option_type == 'call':
        theta = (-S * norm_pdf(d1) * sigma / (2 * math.sqrt(T)) - r * K * math.exp(-r * T) * norm_cdf(d2)) / 365
    else:
        theta = (-S * norm_pdf(d1) * sigma / (2 * math.sqrt(T)) + r * K * math.exp(-r * T) * norm_cdf(-d2)) / 365
    
    return {'delta': delta, 'gamma': gamma, 'theta': theta, 'vega': vega}

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
    return remaining_seconds / (252 * 6.5 * 3600)

def get_option_price(S, K, T, sigma, direction):
    """获取期权价格，带安全检查"""
    if T <= 0 or S <= 0 or K <= 0:
        return 0.0
    opt_type = 'call' if direction == 'call' else 'put'
    price = bs_option_price(S, K, T, 0.05, sigma, opt_type)
    return max(price, 0.01)  # 最低$0.01


# ===== 技术指标 =====
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

def calc_atr(highs, lows, closes, period=14):
    """计算ATR"""
    if len(closes) < period + 1:
        return 0
    trs = []
    for i in range(1, len(highs[-period-1:])):
        tr = max(
            highs[-period-1:][i] - lows[-period-1:][i],
            abs(highs[-period-1:][i] - closes[-period-1:][i-1]),
            abs(lows[-period-1:][i] - closes[-period-1:][i-1])
        )
        trs.append(tr)
    return np.mean(trs) if trs else 0


# ===== 数据加载 =====
def load_data():
    """加载并合并所有K线数据"""
    data_dir = Path(__file__).parent / 'data'
    dfs = []
    
    # 1. 加载 qqq_1min_cleaned.csv
    hist_file = data_dir / 'qqq_1min_cleaned.csv'
    if hist_file.exists():
        df_hist = pd.read_csv(hist_file)
        df_hist = df_hist.rename(columns={
            'timestamp_et': 'Datetime',
            'open': 'Open', 'high': 'High', 'low': 'Low',
            'close': 'Close', 'volume': 'Volume'
        })
        df_hist['Datetime'] = pd.to_datetime(df_hist['Datetime'])
        dfs.append(df_hist[['Datetime', 'Open', 'High', 'Low', 'Close', 'Volume']])
        print(f"📊 历史数据: {len(df_hist)} 条")
    
    # 2. 加载 candles 数据
    candles_dir = data_dir / 'candles'
    if candles_dir.exists():
        for csv_file in sorted(candles_dir.glob('*.csv')):
            df_day = pd.read_csv(csv_file)
            col_map = {}
            for col in df_day.columns:
                cl = col.lower().strip()
                if cl in ('timestamp', 'timestamp_et', 'timestamp_cst'):
                    col_map[col] = 'Datetime'
                elif cl == 'open': col_map[col] = 'Open'
                elif cl == 'high': col_map[col] = 'High'
                elif cl == 'low': col_map[col] = 'Low'
                elif cl == 'close': col_map[col] = 'Close'
                elif cl == 'volume': col_map[col] = 'Volume'
            df_day = df_day.rename(columns=col_map)
            if 'Datetime' not in df_day.columns:
                continue
            df_day['Datetime'] = pd.to_datetime(df_day['Datetime'])
            dfs.append(df_day[['Datetime', 'Open', 'High', 'Low', 'Close', 'Volume']])
    
    if not dfs:
        print("❌ 没有数据")
        return None
    
    df = pd.concat(dfs, ignore_index=True)
    df = df.drop_duplicates(subset=['Datetime']).sort_values('Datetime').reset_index(drop=True)
    df['Date'] = df['Datetime'].dt.date
    
    # 只保留美股交易时间 (09:30-16:00)
    df = df[(df['Datetime'].dt.time >= dtime(9, 30)) & (df['Datetime'].dt.time < dtime(16, 0))]
    df = df.reset_index(drop=True)
    
    print(f"📊 总数据: {len(df)} 条, {len(df['Date'].unique())}个交易日")
    print(f"   日期: {df['Date'].min()} ~ {df['Date'].max()}")
    return df


# ===== 信号检测 =====
def check_breakout_signal(df_day, idx, cfg, last_signal_time):
    """检测突破信号（带冷却检查）"""
    if idx < cfg['lookback']:
        return None
    
    current_time = df_day.iloc[idx]['Datetime']
    
    # 冷却检查
    if last_signal_time:
        elapsed = (current_time - last_signal_time).total_seconds() / 60
        if elapsed < cfg['signal_cooldown']:
            return None
    
    current = df_day.iloc[idx]
    lookback_bars = df_day.iloc[idx-cfg['lookback']:idx]
    
    high_lookback = lookback_bars['High'].max()
    low_lookback = lookback_bars['Low'].min()
    
    close = current['Close']
    volume = current['Volume']
    avg_volume = lookback_bars['Volume'].mean()
    
    # 成交量确认
    if avg_volume > 0 and volume < avg_volume * cfg['vol_mult']:
        return None
    
    # K线实体确认
    body = abs(close - current['Open']) / current['Open'] if current['Open'] > 0 else 0
    if body < cfg['min_body']:
        return None
    
    # 向上突破
    if close > high_lookback:
        # 确认是阳线
        if close > current['Open']:
            return {
                'dir': 'call',
                'price': close,
                'reason': f'突破{high_lookback:.2f}',
            }
    
    # 向下突破
    if close < low_lookback:
        # 确认是阴线
        if close < current['Open']:
            return {
                'dir': 'put',
                'price': close,
                'reason': f'跌破{low_lookback:.2f}',
            }
    
    return None


def check_reversal_signal(df_day, idx, cfg, session_high, session_low, last_signal_time):
    """检测反转信号"""
    if idx < 20 or not cfg.get('reversal_enabled'):
        return None
    
    current_time = df_day.iloc[idx]['Datetime']
    
    # 冷却检查
    if last_signal_time:
        elapsed = (current_time - last_signal_time).total_seconds() / 60
        if elapsed < cfg['signal_cooldown']:
            return None
    
    current = df_day.iloc[idx]
    prev = df_day.iloc[idx-1]
    close = current['Close']
    
    # 超跌反弹
    if session_high > 0:
        drop_from_high = (session_high - close) / session_high
        if drop_from_high >= cfg['reversal_drop']:
            closes = df_day['Close'].values[:idx+1]
            rsi = calc_rsi(closes, cfg['rsi_period'])
            if rsi < cfg['rsi_oversold']:
                # 结构确认：突破前3根高点
                if idx >= 4:
                    high_3 = max(df_day.iloc[idx-3:idx]['High'])
                    if close > high_3:
                        # 前一根收阳
                        if prev['Close'] >= prev['Open']:
                            bounce_body = abs(prev['Close'] - prev['Open']) / prev['Open'] if prev['Open'] > 0 else 0
                            if bounce_body >= cfg['reversal_bounce']:
                                return {
                                    'dir': 'call',
                                    'price': close,
                                    'reason': f'超跌反弹RSI{rsi:.0f}',
                                }
    
    # 超涨回调
    if session_low < 999999:
        rise_from_low = (close - session_low) / session_low
        if rise_from_low >= cfg['reversal_drop']:
            closes = df_day['Close'].values[:idx+1]
            rsi = calc_rsi(closes, cfg['rsi_period'])
            if rsi > cfg['rsi_overbought']:
                if idx >= 4:
                    low_3 = min(df_day.iloc[idx-3:idx]['Low'])
                    if close < low_3:
                        if prev['Close'] <= prev['Open']:
                            drop_body = abs(prev['Close'] - prev['Open']) / prev['Open'] if prev['Open'] > 0 else 0
                            if drop_body >= cfg['reversal_bounce']:
                                return {
                                    'dir': 'put',
                                    'price': close,
                                    'reason': f'超涨回调RSI{rsi:.0f}',
                                }
    
    return None


# ===== 回测引擎 =====
def backtest_day(df_day, cfg):
    """回测单日"""
    trades = []
    position = None
    daily_pnl = 0
    consecutive_losses = 0
    session_high = 0
    session_low = 999999
    loss_cooldown_until = None
    last_signal_time = None
    trade_count = 0
    
    start_time = dtime(cfg['start_h'], cfg['start_m'])
    end_time = dtime(cfg['end_h'], cfg['end_m'])
    post_open_until = None  # 开盘禁止期结束时间
    
    for idx in range(len(df_day)):
        row = df_day.iloc[idx]
        current_time = row['Datetime'].time()
        current_dt = row['Datetime']
        
        # 更新高低点
        session_high = max(session_high, row['High'])
        session_low = min(session_low, row['Low'])
        
        # 检查时间窗口
        if current_time < start_time or current_time > end_time:
            continue
        
        # 开盘禁止期
        if post_open_until is None:
            post_open_until = (datetime.combine(date.today(), start_time) + 
                             timedelta(minutes=cfg['post_open_cooldown'])).time()
        if current_time < post_open_until:
            continue
        
        # 冷却检查
        if loss_cooldown_until and current_time < loss_cooldown_until:
            continue
        
        # 最大交易数检查
        if trade_count >= cfg['max_trades']:
            continue
        
        # 日亏损熔断
        if daily_pnl <= -cfg['capital'] * cfg['daily_limit']:
            break
        
        # ===== 持仓管理 =====
        if position:
            T = time_to_expiry(current_dt)
            K = position['strike']
            S = row['Close']
            sigma = cfg['sigma']
            
            current_opt_price = get_option_price(S, K, T, sigma, position['dir'])
            
            if position['entry_price'] > 0:
                pnl_pct = (current_opt_price - position['entry_price']) / position['entry_price']
            else:
                pnl_pct = 0
            
            # 计算持仓时间
            entry_dt = datetime.combine(date.today(), position['entry_time'])
            bars_held = int((current_dt - entry_dt).total_seconds() / 60)
            
            exit_reason = None
            
            # 止损
            if pnl_pct <= -cfg['sl']:
                exit_reason = f'止损{pnl_pct*100:.1f}%'
            
            # 止盈
            elif pnl_pct >= cfg['tp']:
                exit_reason = f'止盈{pnl_pct*100:.1f}%'
            
            # 硬超时
            elif bars_held >= cfg['timeout_bars']:
                exit_reason = f'超时{bars_held}min'
            
            if exit_reason:
                pnl_usd = position['contracts'] * cfg['contract_multiplier'] * (current_opt_price - position['entry_price'])
                pnl_usd -= position['contracts'] * 0.67 * 2  # 手续费
                
                daily_pnl += pnl_usd
                trade_count += 1
                
                trades.append({
                    'entry_time': position['entry_time'],
                    'exit_time': current_time,
                    'dir': position['dir'],
                    'entry_price': round(position['entry_price'], 2),
                    'exit_price': round(current_opt_price, 2),
                    'contracts': position['contracts'],
                    'pnl_pct': round(pnl_pct * 100, 2),
                    'pnl_usd': round(pnl_usd, 2),
                    'reason': position['reason'],
                    'exit_reason': exit_reason,
                    'result': 'win' if pnl_pct > 0 else 'lose',
                    'bars_held': bars_held,
                })
                
                # 更新连亏和冷却
                if pnl_pct <= 0:
                    consecutive_losses += 1
                    if consecutive_losses >= 3:
                        cooldown_min = 5
                    else:
                        cooldown_min = cfg['loss_cooldown']
                    loss_cooldown_until = (datetime.combine(date.today(), current_time) + 
                                          timedelta(minutes=cooldown_min)).time()
                else:
                    consecutive_losses = 0
                
                last_signal_time = current_dt
                position = None
                continue
        
        # ===== 开仓信号检测 =====
        if position is None:
            # 突破信号
            signal = check_breakout_signal(df_day, idx, cfg, last_signal_time)
            
            # 反转信号 (v7)
            if signal is None and cfg.get('reversal_enabled'):
                signal = check_reversal_signal(df_day, idx, cfg, session_high, session_low, last_signal_time)
            
            if signal:
                # 计算期权行权价
                S = signal['price']
                if signal['dir'] == 'call':
                    K = math.floor(S + cfg['option_offset'])
                else:
                    K = math.ceil(S - cfg['option_offset'])
                
                T = time_to_expiry(current_dt)
                sigma = cfg['sigma']
                
                opt_price = get_option_price(S, K, T, sigma, signal['dir'])
                if opt_price <= 0:
                    continue
                
                # 计算合约数
                order_pct = cfg['order_pct']
                if signal['dir'] == 'put':
                    order_pct = min(order_pct, 5)  # PUT限制5%
                
                order_amount = cfg['capital'] * order_pct / 100
                contracts = max(1, int(order_amount / (opt_price * cfg['contract_multiplier'])))
                
                position = {
                    'dir': signal['dir'],
                    'entry_time': current_time,
                    'entry_price': opt_price,
                    'strike': K,
                    'contracts': contracts,
                    'reason': signal['reason']
                }
                last_signal_time = current_dt
    
    # 收盘强平
    if position:
        last_row = df_day.iloc[-1]
        T = 0.0001  # 几乎到期
        K = position['strike']
        S = last_row['Close']
        sigma = cfg['sigma']
        
        exit_price = get_option_price(S, K, T, sigma, position['dir'])
        pnl_pct = (exit_price - position['entry_price']) / position['entry_price'] if position['entry_price'] > 0 else 0
        pnl_usd = position['contracts'] * cfg['contract_multiplier'] * (exit_price - position['entry_price'])
        pnl_usd -= position['contracts'] * 0.67 * 2
        
        daily_pnl += pnl_usd
        trade_count += 1
        
        trades.append({
            'entry_time': position['entry_time'],
            'exit_time': last_row['Datetime'].time(),
            'dir': position['dir'],
            'entry_price': round(position['entry_price'], 2),
            'exit_price': round(exit_price, 2),
            'contracts': position['contracts'],
            'pnl_pct': round(pnl_pct * 100, 2),
            'pnl_usd': round(pnl_usd, 2),
            'reason': position['reason'],
            'exit_reason': '收盘强平',
            'result': 'win' if pnl_pct > 0 else 'lose',
            'bars_held': 0,
        })
    
    return {
        'trades': trades,
        'daily_pnl': round(daily_pnl, 2),
        'trade_count': trade_count,
    }


def run_backtest(df, cfg):
    """运行完整回测"""
    results = []
    dates = df['Date'].unique()
    
    for trade_date in dates:
        df_day = df[df['Date'] == trade_date].copy()
        if len(df_day) < 30:
            continue
        df_day = df_day.reset_index(drop=True)
        day_result = backtest_day(df_day, cfg)
        day_result['date'] = str(trade_date)
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
            'total_trades': 0, 'win_rate': 0, 'total_pnl': 0,
            'avg_pnl': 0, 'max_win': 0, 'max_loss': 0,
            'max_consecutive_losses': 0, 'max_drawdown': 0,
            'sharpe_ratio': 0, 'profit_factor': 0,
            'avg_win': 0, 'avg_loss': 0,
            'sl_pct': 0, 'tp_pct': 0, 'timeout_pct': 0,
            'avg_bars_held': 0,
            'trades_per_day': 0,
        }
    
    total_trades = len(all_trades)
    win_trades = [t for t in all_trades if t['result'] == 'win']
    lose_trades = [t for t in all_trades if t['result'] == 'lose']
    
    win_rate = len(win_trades) / total_trades * 100
    total_pnl = sum(t['pnl_usd'] for t in all_trades)
    avg_pnl = total_pnl / total_trades
    
    max_win = max(t['pnl_usd'] for t in all_trades)
    max_loss = min(t['pnl_usd'] for t in all_trades)
    
    # 最大连续亏损
    max_consecutive_losses = 0
    current_losses = 0
    for t in all_trades:
        if t['result'] == 'lose':
            current_losses += 1
            max_consecutive_losses = max(max_consecutive_losses, current_losses)
        else:
            current_losses = 0
    
    # 最大回撤
    cumulative = 0
    peak = 0
    max_drawdown = 0
    for t in all_trades:
        cumulative += t['pnl_usd']
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)
    
    # 夏普比率
    daily_returns = [r['daily_pnl'] for r in results if r['trade_count'] > 0]
    if len(daily_returns) > 1:
        sharpe = np.mean(daily_returns) / np.std(daily_returns) * np.sqrt(252) if np.std(daily_returns) > 0 else 0
    else:
        sharpe = 0
    
    # 盈亏比
    avg_win = np.mean([t['pnl_usd'] for t in win_trades]) if win_trades else 0
    avg_loss = abs(np.mean([t['pnl_usd'] for t in lose_trades])) if lose_trades else 1
    profit_factor = avg_win / avg_loss if avg_loss > 0 else 0
    
    # 平仓原因统计
    sl_count = sum(1 for t in all_trades if '止损' in t.get('exit_reason', ''))
    tp_count = sum(1 for t in all_trades if '止盈' in t.get('exit_reason', ''))
    timeout_count = sum(1 for t in all_trades if '超时' in t.get('exit_reason', ''))
    
    # 平均持仓时间
    avg_bars = np.mean([t.get('bars_held', 0) for t in all_trades])
    
    trade_days = len([r for r in results if r['trade_count'] > 0])
    
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
        'sharpe_ratio': round(sharpe, 2),
        'profit_factor': round(profit_factor, 2),
        'avg_win': round(avg_win, 2),
        'avg_loss': round(avg_loss, 2),
        'sl_pct': round(sl_count/total_trades*100, 1),
        'tp_pct': round(tp_count/total_trades*100, 1),
        'timeout_pct': round(timeout_count/total_trades*100, 1),
        'avg_bars_held': round(avg_bars, 1),
        'trades_per_day': round(total_trades / max(trade_days, 1), 1),
    }


def grid_search(df):
    """参数网格搜索"""
    results = []
    total = (len(PARAM_GRID['sl']) * len(PARAM_GRID['tp']) * 
             len(PARAM_GRID['lookback']) * len(PARAM_GRID['signal_cooldown']))
    current = 0
    
    for sl in PARAM_GRID['sl']:
        for tp in PARAM_GRID['tp']:
            for lb in PARAM_GRID['lookback']:
                for cd in PARAM_GRID['signal_cooldown']:
                    current += 1
                    if current % 10 == 0:
                        print(f"\r  进度: {current}/{total}", end="", flush=True)
                    
                    cfg = V7_CONFIG.copy()
                    cfg['sl'] = sl
                    cfg['tp'] = tp
                    cfg['lookback'] = lb
                    cfg['signal_cooldown'] = cd
                    
                    day_results = run_backtest(df, cfg)
                    stats = calculate_stats(day_results, f"s{sl}_t{tp}_l{lb}_c{cd}")
                    
                    stats['sl'] = sl
                    stats['tp'] = tp
                    stats['lookback'] = lb
                    stats['signal_cooldown'] = cd
                    
                    results.append(stats)
    
    print()
    return results


# ===== 主程序 =====
def main():
    print("=" * 60)
    print("QQQ 0DTE 策略回测 v2 (优化版)")
    print("=" * 60)
    
    output_dir = Path(__file__).parent / 'backtest_results'
    output_dir.mkdir(exist_ok=True)
    
    # 加载数据
    print("\n📊 加载数据...")
    df = load_data()
    if df is None:
        return
    
    # 1. v6.5 基准
    print("\n🔵 运行 v6.5 基准策略...")
    v6_5_results = run_backtest(df, V6_5_CONFIG)
    v6_5_stats = calculate_stats(v6_5_results, 'v6.5')
    print(f"  ✅ v6.5: {v6_5_stats['total_trades']}笔, 胜率{v6_5_stats['win_rate']}%, "
          f"盈亏${v6_5_stats['total_pnl']:+,.2f}, 连亏{v6_5_stats['max_consecutive_losses']}笔")
    print(f"     止损{v6_5_stats['sl_pct']}% 止盈{v6_5_stats['tp_pct']}% 超时{v6_5_stats['timeout_pct']}%")
    print(f"     日均{v6_5_stats['trades_per_day']}笔 持仓{v6_5_stats['avg_bars_held']}分钟")
    
    # 2. v7 策略
    print("\n🟢 运行 v7 策略...")
    v7_results = run_backtest(df, V7_CONFIG)
    v7_stats = calculate_stats(v7_results, 'v7')
    print(f"  ✅ v7: {v7_stats['total_trades']}笔, 胜率{v7_stats['win_rate']}%, "
          f"盈亏${v7_stats['total_pnl']:+,.2f}, 连亏{v7_stats['max_consecutive_losses']}笔")
    print(f"     止损{v7_stats['sl_pct']}% 止盈{v7_stats['tp_pct']}% 超时{v7_stats['timeout_pct']}%")
    print(f"     日均{v7_stats['trades_per_day']}笔 持仓{v7_stats['avg_bars_held']}分钟")
    
    # 3. 参数优化
    print("\n🟡 参数网格搜索...")
    param_results = grid_search(df)
    
    # 过滤有效结果（至少100笔交易）
    valid_results = [r for r in param_results if r['total_trades'] >= 100]
    
    if valid_results:
        best_pnl = max(valid_results, key=lambda x: x['total_pnl'])
        best_wr = max(valid_results, key=lambda x: x['win_rate'])
        best_sharpe = max(valid_results, key=lambda x: x['sharpe_ratio'])
        best_cl = min(valid_results, key=lambda x: x['max_consecutive_losses'])
        best_pf = max(valid_results, key=lambda x: x['profit_factor'])
        
        print(f"\n📊 最优参数 (按总盈亏):")
        print(f"  sl={best_pnl['sl']} tp={best_pnl['tp']} lb={best_pnl['lookback']} cd={best_pnl['signal_cooldown']}")
        print(f"  → 盈亏${best_pnl['total_pnl']:+,.2f} 胜率{best_pnl['win_rate']}% 连亏{best_pnl['max_consecutive_losses']}笔")
        
        print(f"\n📊 最优参数 (按胜率):")
        print(f"  sl={best_wr['sl']} tp={best_wr['tp']} lb={best_wr['lookback']} cd={best_wr['signal_cooldown']}")
        print(f"  → 胜率{best_wr['win_rate']}% 盈亏${best_wr['total_pnl']:+,.2f}")
        
        print(f"\n📊 最优参数 (按夏普):")
        print(f"  sl={best_sharpe['sl']} tp={best_sharpe['tp']} lb={best_sharpe['lookback']} cd={best_sharpe['signal_cooldown']}")
        print(f"  → 夏普{best_sharpe['sharpe_ratio']} 盈亏${best_sharpe['total_pnl']:+,.2f}")
        
        print(f"\n📊 最优参数 (按最低连亏):")
        print(f"  sl={best_cl['sl']} tp={best_cl['tp']} lb={best_cl['lookback']} cd={best_cl['signal_cooldown']}")
        print(f"  → 连亏{best_cl['max_consecutive_losses']}笔 盈亏${best_cl['total_pnl']:+,.2f}")
        
        print(f"\n📊 最优参数 (按盈亏比):")
        print(f"  sl={best_pf['sl']} tp={best_pf['tp']} lb={best_pf['lookback']} cd={best_pf['signal_cooldown']}")
        print(f"  → 盈亏比{best_pf['profit_factor']} 胜率{best_pf['win_rate']}%")
    
    # ===== 保存结果 =====
    print("\n💾 保存结果...")
    
    # 汇总
    summary_df = pd.DataFrame([v6_5_stats, v7_stats])
    summary_df.to_csv(output_dir / 'summary.csv', index=False)
    
    # v6.5 详细
    v6_5_trades = []
    for r in v6_5_results:
        for t in r['trades']:
            t['date'] = r['date']
            v6_5_trades.append(t)
    pd.DataFrame(v6_5_trades).to_csv(output_dir / 'v6.5_results.csv', index=False)
    
    # v7 详细
    v7_trades = []
    for r in v7_results:
        for t in r['trades']:
            t['date'] = r['date']
            v7_trades.append(t)
    pd.DataFrame(v7_trades).to_csv(output_dir / 'v7_results.csv', index=False)
    
    # 参数优化
    param_df = pd.DataFrame(param_results)
    param_df = param_df.sort_values('total_pnl', ascending=False)
    param_df.to_csv(output_dir / 'parameter_optimization.csv', index=False)
    
    # 报告
    report_lines = [
        "# QQQ 0DTE 策略回测报告 v2",
        "",
        f"## 数据范围",
        f"- 日期: {df['Date'].min()} ~ {df['Date'].max()}",
        f"- 交易日: {len(df['Date'].unique())}天",
        f"- 数据量: {len(df)}条1分钟K线",
        "",
        "## 策略对比",
        "",
        "| 指标 | v6.5 | v7 |",
        "|------|------|-----|",
        f"| 总交易 | {v6_5_stats['total_trades']} | {v7_stats['total_trades']} |",
        f"| 胜率 | {v6_5_stats['win_rate']}% | {v7_stats['win_rate']}% |",
        f"| 总盈亏 | ${v6_5_stats['total_pnl']:+,.2f} | ${v7_stats['total_pnl']:+,.2f} |",
        f"| 盈亏比 | {v6_5_stats['profit_factor']} | {v7_stats['profit_factor']} |",
        f"| 最大连亏 | {v6_5_stats['max_consecutive_losses']}笔 | {v7_stats['max_consecutive_losses']}笔 |",
        f"| 最大回撤 | ${v6_5_stats['max_drawdown']:+,.2f} | ${v7_stats['max_drawdown']:+,.2f} |",
        f"| 夏普比率 | {v6_5_stats['sharpe_ratio']} | {v7_stats['sharpe_ratio']} |",
        f"| 止损占比 | {v6_5_stats['sl_pct']}% | {v7_stats['sl_pct']}% |",
        f"| 止盈占比 | {v6_5_stats['tp_pct']}% | {v7_stats['tp_pct']}% |",
        f"| 超时占比 | {v6_5_stats['timeout_pct']}% | {v7_stats['timeout_pct']}% |",
        f"| 日均交易 | {v6_5_stats['trades_per_day']}笔 | {v7_stats['trades_per_day']}笔 |",
        f"| 平均持仓 | {v6_5_stats['avg_bars_held']}分钟 | {v7_stats['avg_bars_held']}分钟 |",
        "",
    ]
    
    if valid_results:
        report_lines.extend([
            "## 最优参数组合",
            "",
            f"### 按总盈亏",
            f"- 参数: sl={best_pnl['sl']}, tp={best_pnl['tp']}, lb={best_pnl['lookback']}, cd={best_pnl['signal_cooldown']}",
            f"- 结果: 盈亏${best_pnl['total_pnl']:+,.2f}, 胜率{best_pnl['win_rate']}%, 连亏{best_pnl['max_consecutive_losses']}笔",
            "",
            f"### 按胜率",
            f"- 参数: sl={best_wr['sl']}, tp={best_wr['tp']}, lb={best_wr['lookback']}, cd={best_wr['signal_cooldown']}",
            f"- 结果: 胜率{best_wr['win_rate']}%, 盈亏${best_wr['total_pnl']:+,.2f}",
            "",
            f"### 按最低连亏",
            f"- 参数: sl={best_cl['sl']}, tp={best_cl['tp']}, lb={best_cl['lookback']}, cd={best_cl['signal_cooldown']}",
            f"- 结果: 连亏{best_cl['max_consecutive_losses']}笔, 盈亏${best_cl['total_pnl']:+,.2f}",
            "",
        ])
    
    report_lines.append(f"---\n*生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*")
    
    with open(output_dir / 'report.md', 'w', encoding='utf-8') as f:
        f.write('\n'.join(report_lines))
    
    print(f"  ✅ backtest_results/ 目录已更新")
    print("\n" + "=" * 60)
    print("回测完成！")
    print("=" * 60)


if __name__ == '__main__':
    main()
