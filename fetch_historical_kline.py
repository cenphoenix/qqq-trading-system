#!/usr/bin/env python3
"""
拉取 QQQ 2025 年全年 1 分钟 K 线数据
使用长桥 API，分批拉取，保存为 CSV
"""
import os
import sys
import csv
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

# 加载 .env
env_file = Path(__file__).parent / '.env'
if env_file.exists():
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if line and '=' in line and not line.startswith('#'):
                k, v = line.split('=', 1)
                v = v.strip('"').strip("'")
                if 'LONGPORT' in k or 'LONGBRIDGE' in k:
                    os.environ[k] = v
    print(f"[OK] 已加载 {env_file}")

# 兼容: LONGPORT_* → LONGBRIDGE_* (SDK 需要)
for key in list(os.environ.keys()):
    if key.startswith('LONGPORT_'):
        new_key = key.replace('LONGPORT', 'LONGBRIDGE', 1)
        if new_key not in os.environ:
            os.environ[new_key] = os.environ[key]

from longbridge.openapi import Config, QuoteContext, Period, AdjustType

# === 配置 ===
SYMBOL = "QQQ.US"
YEAR = 2025
BATCH_SIZE = 1000  # API 单次最大
SLEEP_SEC = 0.5    # 每次请求间隔（防限频）
OUTPUT_FILE = f"data/qqq_1min_{YEAR}.csv"

# 2025 年时间窗口（UTC）
END_DT_UTC = datetime(YEAR + 1, 1, 1)
START_DT_UTC = datetime(YEAR, 1, 1)


def main():
    print(f"🔌 连接长桥 API...")
    ctx = QuoteContext(Config.from_env())

    Path("data").mkdir(exist_ok=True)

    results = []
    skip_count = 0
    earliest_ts = None  # 已拉到的最早时间戳

    print(f"📥 开始拉取 {SYMBOL} {YEAR}年 1分钟K线...")
    print(f"   批次大小: {BATCH_SIZE}")
    print(f"   时间窗口: {START_DT_UTC.date()} ~ {END_DT_UTC.date()}")

    while True:
        try:
            candles = ctx.candlesticks(
                SYMBOL,
                Period.Min_1,
                BATCH_SIZE,
                AdjustType.NoAdjust,
            )
        except Exception as e:
            print(f"  ⚠️ API 失败: {e}")
            time.sleep(5)
            continue

        if not candles or len(candles) == 0:
            print(f"  ⚠️ 没有更多K线了")
            break

        # Candlestick 字段: timestamp, open, high, low, close, volume, turnover
        earliest = candles[0].timestamp
        latest = candles[-1].timestamp

        print(f"  📦 批次 #{skip_count + 1}: {len(candles)}根 "
              f"[{earliest.strftime('%Y-%m-%d %H:%M')} -> {latest.strftime('%Y-%m-%d %H:%M')}]")

        # 过滤 2025 年的数据
        done = False
        for c in candles:
            ts = c.timestamp  # 已经是 datetime 对象
            if START_DT_UTC <= ts <= END_DT_UTC:
                results.append({
                    'datetime': ts.strftime('%Y-%m-%d %H:%M:%S'),
                    'open': float(c.open),
                    'high': float(c.high),
                    'low': float(c.low),
                    'close': float(c.close),
                    'volume': float(c.volume),
                    'turnover': float(getattr(c, 'turnover', 0) or 0),
                })
            elif ts < START_DT_UTC:
                print(f"  ✅ 已覆盖到 {START_DT_UTC.date()}，停止拉取")
                done = True
                break

        if done:
            break

        # 检查是否已经拉到 2025-01-01 之前
        if earliest < START_DT_UTC:
            print(f"  ✅ 已覆盖到 {START_DT_UTC.date()}，停止拉取")
            break

        # 防止无限循环：无法继续回退
        if earliest_ts is not None and earliest >= earliest_ts:
            print(f"  ⚠️ API 无法继续回退 (已到 {earliest.date()})")
            break

        earliest_ts = earliest
        skip_count += 1
        time.sleep(SLEEP_SEC)

        if skip_count >= 120:
            print(f"  ⚠️ 达到最大批次限制 120（约 {skip_count * BATCH_SIZE} 根K线）")
            break

    print(f"\n📊 总计: {len(results)} 根K线")

    if not results:
        print("❌ 没有拉取到 2025 年数据")
        if candles:
            print(f"   最早可拉取日期: {candles[0].timestamp.date()}")
        print("   提示: 2025 年数据可能已超过 API 的历史回查窗口")
        return

    # 写入 CSV (按时间正序排列)
    results.sort(key=lambda x: x['datetime'])
    with open(OUTPUT_FILE, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=[
            'datetime', 'open', 'high', 'low', 'close', 'volume', 'turnover'
        ])
        writer.writeheader()
        for r in results:
            writer.writerow(r)

    total_mb = os.path.getsize(OUTPUT_FILE) / (1024 * 1024)
    print(f"✅ 已保存至 {OUTPUT_FILE}")
    print(f"   大小: {total_mb:.2f} MB")
    print(f"   日期范围: {results[0]['datetime'][:10]} ~ {results[-1]['datetime'][:10]}")
    num_trading_days = len(set(r['datetime'][:10] for r in results))
    print(f"   K线数: {len(results)} (约 {num_trading_days} 个交易日)")


if __name__ == '__main__':
    main()
