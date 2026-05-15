#!/usr/bin/env python3
"""
QQQ 交易记录 → Gist 同步脚本
合并回测数据 + 实盘记录 → 更新 Gist
"""
import os
import sys
import json
import glob
import requests
from datetime import datetime

# ===== 配置（从环境变量读取）=====
GIST_ID = os.environ.get("GIST_ID", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GIST_FILENAME = "qqq_records.json"

# 打包后exe目录 / 开发时脚本目录
if getattr(sys, 'frozen', False):
    SCRIPT_DIR = os.path.dirname(sys.executable)
else:
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
RECORDS_DIR = os.path.join(SCRIPT_DIR, "records")
DATA_DIR = os.path.join(SCRIPT_DIR, "data")


def load_account_info():
    """从长桥API获取实盘账户信息（OAuth2）"""
    try:
        import json
        from longbridge.openapi import Config, TradeContext, OAuthBuilder

        client_id = os.environ.get('LONGBRIDGE_CLIENT_ID', '555390a4-1348-49f4-9283-a300713bf50f')
        token_file = os.path.expanduser('~/.longbridge/openapi/tokens/' + client_id)

        if not os.path.exists(token_file):
            print("  ⚠️ OAuth token not found, skipping account info")
            return None

        with open(token_file) as tf:
            token_data = json.load(tf)
        if not token_data.get('access_token'):
            return None

        oauth = OAuthBuilder(client_id).build(lambda url: None)
        config = Config.from_oauth(oauth)
        tc = TradeContext(config)
        balances = tc.account_balance()

        total_assets = 0.0
        total_cash = 0.0
        buy_power = 0.0

        for b in balances:
            na = float(getattr(b, 'net_assets', 0) or 0)
            tc_val = float(getattr(b, 'total_cash', 0) or 0)
            bp = float(getattr(b, 'buy_power', 0) or 0)
            ccy = str(getattr(b, 'currency', 'USD') or 'USD')
            rate = 7.8 if ccy == 'HKD' else (7.2 if ccy == 'CNY' else 1.0)
            total_assets += na / rate
            total_cash += tc_val / rate
            buy_power += bp / rate

        return {
            'total_assets': round(total_assets, 2),
            'cash': round(total_cash, 2),
            'buy_power': round(buy_power, 2),
        }
    except Exception as e:
        print(f"  ⚠️ 获取账户信息失败: {e}")
        return None


def load_backtest_data():
    """加载回测数据（固定，不更新）"""
    cache_file = os.path.join(DATA_DIR, 'records_backtest.json')
    if os.path.exists(cache_file):
        with open(cache_file, encoding='utf-8') as f:
            data = json.load(f)
            # 兼容两种格式：带backtest键 或 直接meta键
            if 'backtest' in data:
                return data['backtest']
            elif 'meta' in data:
                m = data['meta']
                return {
                    'period': m.get('period', ''),
                    'total_trades': m.get('total_trades', 0),
                    'total_wins': m.get('total_wins', 0),
                    'total_losses': m.get('total_losses', 0),
                    'win_rate': m.get('win_rate', 0),
                    'total_pnl_pct': m.get('total_pnl_pct', 0),
                }
    return None


def save_backtest_cache(data):
    """保存回测数据缓存"""
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(os.path.join(DATA_DIR, 'records_backtest.json'), 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_all_records():
    """合并回测 + 实盘数据"""
    # 1. 先加载回测数据
    bt = load_backtest_data()

    # 2. 如果没有回测缓存，用当前 records.json 作为回测基准
    records_file = os.path.join(DATA_DIR, 'records.json')
    if bt is None and os.path.exists(records_file):
        with open(records_file, encoding='utf-8') as f:
            bt = json.load(f)
        save_backtest_cache(bt)

    all_trades = []
    daily_map = {}

    # 加载回测数据
    if bt:
        for t in bt.get('trades', []):
            all_trades.append(t)
        for d in bt.get('daily', []):
            daily_map[d['date']] = d

    # 3. 加载实盘记录（records/ 目录，覆盖回测同日期数据）
    if os.path.exists(RECORDS_DIR):
        for filepath in sorted(glob.glob(os.path.join(RECORDS_DIR, "*.json"))):
            try:
                with open(filepath, encoding='utf-8') as f:
                    day_data = json.load(f)
                    date = day_data.get("date", "")
                    trades = day_data.get("trades", [])

                    # 覆盖回测同日期数据
                    all_trades = [t for t in all_trades if t.get("date") != date]
                    all_trades.extend(trades)

                    total = len(trades)
                    wins = sum(1 for t in trades if t.get("result") == "win")
                    pnl = day_data.get("pnl", sum(t.get("pnl_pct", 0) for t in trades))
                    daily_map[date] = {
                        "date": date, "total": total,
                        "wins": wins, "losses": total - wins,
                        "pnl": round(pnl, 2),
                        "win_rate": round(wins / total * 100, 1) if total > 0 else 0,
                    }
            except Exception as e:
                print(f"⚠️ 读取 {filepath} 失败: {e}")

    all_trades.sort(key=lambda x: (x.get("date", ""), x.get("time", "")))

    total_trades = len(all_trades)
    total_wins = sum(1 for t in all_trades if t.get("result") == "win")
    total_pnl = round(sum(t.get("pnl_pct", 0) for t in all_trades), 2)
    dates = sorted(set(t.get("date", "") for t in all_trades if t.get("date")))

    return {
        "backtest": {
            "period": f"{dates[0]} ~ {dates[-1]}" if dates else "无数据",
            "total_trades": total_trades,
            "total_wins": total_wins,
            "total_losses": total_trades - total_wins,
            "win_rate": round(total_wins / total_trades * 100, 1) if total_trades > 0 else 0,
            "total_pnl_pct": total_pnl,
        },
        "daily": list(daily_map.values()),
        "trades": all_trades,
    }


def main():
    print("=" * 50)
    print("📤 QQQ 实盘数据 → Gist 同步")
    print("=" * 50)

    # 获取实盘账户
    account = load_account_info()
    if account:
        print(f"💰 实盘总资产: ${account['total_assets']:,.2f}")
        print(f"💵 现金: ${account['cash']:,.2f}")
    else:
        account = {'total_assets': 0, 'cash': 0, 'buy_power': 0}

    # 加载固定的回测数据（不更新）
    bt = load_backtest_data()
    bt_trades = []
    bt_daily = []
    cache_file = os.path.join(DATA_DIR, 'records_backtest.json')
    if os.path.exists(cache_file):
        with open(cache_file, encoding='utf-8') as f:
            raw = json.load(f)
            bt_trades = raw.get('trades', [])
            bt_daily = raw.get('daily', [])
    if bt:
        print(f"📊 回测(固定): {bt.get('total_trades',0)}笔 胜率{bt.get('win_rate',0)}% 得分{bt.get('total_pnl_pct',0):+.2f}%")
        print(f"📅 周期: {bt.get('period','')}")

    # 加载实盘记录（records/ 目录）
    live_trades = []
    live_daily = {}
    if os.path.exists(RECORDS_DIR):
        for filepath in sorted(glob.glob(os.path.join(RECORDS_DIR, "*.json"))):
            try:
                with open(filepath, encoding='utf-8') as f:
                    day_data = json.load(f)
                    date = day_data.get("date", "")
                    trades = day_data.get("trades", [])
                    live_trades.extend(trades)
                    total = len(trades)
                    wins = sum(1 for t in trades if t.get("result") == "win")
                    pnl = day_data.get("pnl", sum(t.get("pnl_pct", 0) for t in trades))
                    live_daily[date] = {
                        "date": date, "total": total,
                        "wins": wins, "losses": total - wins,
                        "pnl": round(pnl, 2),
                        "win_rate": round(wins / total * 100, 1) if total > 0 else 0,
                    }
            except Exception as e:
                print(f"⚠️ 读取 {filepath} 失败: {e}")

    # 合并回测+实盘trades（回测在前，实盘覆盖同日期）
    all_trades = list(bt_trades)
    all_daily = {d['date']: d for d in bt_daily}
    # 实盘覆盖
    for t in live_trades:
        all_trades = [x for x in all_trades if x.get('date') != t.get('date')]
    all_trades.extend(live_trades)
    all_daily.update(live_daily)
    all_trades.sort(key=lambda x: (x.get("date", ""), x.get("time", "")))

    # 今日数据
    today = datetime.now().strftime('%Y-%m-%d')
    today_daily = live_daily.get(today)
    today_pnl = today_daily['pnl'] if today_daily else 0
    today_trades = [t for t in live_trades if t.get('date') == today]

    print(f"📋 实盘记录: {len(live_trades)}笔 ({len(live_daily)}天)")

    # 组装最终数据
    output = {
        'meta': {
            'strategy': '全过滤v6',
            'symbol': 'QQQ',
            'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        },
        'account': account,
        'backtest': bt or {},
        'today': {
            'date': today,
            'pnl': today_pnl,
            'trades': len(today_trades),
        },
        'daily': list(all_daily.values()),
        'trades': all_trades,
    }

    # 保存本地
    os.makedirs(DATA_DIR, exist_ok=True)
    local_path = os.path.join(DATA_DIR, 'records.json')
    with open(local_path, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"💾 本地缓存: {local_path}")

    # 更新 Gist
    content = json.dumps(output, ensure_ascii=False, indent=2)
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }
    payload = {
        "description": f"QQQ Trading - {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "files": {GIST_FILENAME: {"content": content}},
    }
    resp = requests.patch(f"https://api.github.com/gists/{GIST_ID}", headers=headers, json=payload, timeout=30)
    if resp.status_code == 200:
        print(f"✅ Gist 更新成功！")
    else:
        print(f"❌ Gist 失败: {resp.status_code}")


if __name__ == "__main__":
    main()
