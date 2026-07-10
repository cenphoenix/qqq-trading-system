#!/usr/bin/env python3
"""Join one session's trade records to one-minute candles for loss review."""

from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime
from pathlib import Path


def load_candles(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    for row in rows:
        row["time"] = datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M:%S")
        for key in ("open", "high", "low", "close", "volume"):
            row[key] = float(row[key])
    return rows


def directional_pct(start: float, end: float, direction: str) -> float:
    raw = (end - start) / start * 100 if start else 0
    return -raw if direction == "put" else raw


def analyze(date_str: str, root: Path) -> list[dict]:
    record = json.loads((root / "records" / f"{date_str}.json").read_text(encoding="utf-8"))
    candles = load_candles(root / "data" / "candles" / f"{date_str}.csv")
    regular = [row for row in candles if row["time"].strftime("%H:%M") >= "09:30"]
    opening = [row for row in regular if row["time"].strftime("%H:%M") < "10:00"]
    opening_high = max(row["high"] for row in opening)
    opening_low = min(row["low"] for row in opening)

    results = []
    for trade in record.get("trades", []):
        if float(trade.get("pnl_usd", 0) or 0) >= 0:
            continue
        entry = datetime.strptime(f"{date_str} {trade['entry_time'][:8]}", "%Y-%m-%d %H:%M:%S")
        exit_time = datetime.strptime(f"{date_str} {trade['exit_time'][:8]}", "%Y-%m-%d %H:%M:%S")
        entry_index = min(range(len(candles)), key=lambda idx: abs((candles[idx]["time"] - entry).total_seconds()))
        exit_index = min(range(len(candles)), key=lambda idx: abs((candles[idx]["time"] - exit_time).total_seconds()))
        entry_candle = candles[entry_index]
        path = candles[entry_index : exit_index + 1]
        price = entry_candle["open"]
        direction = trade.get("dir", "")
        favorable_prices = [row["high"] if direction == "call" else row["low"] for row in path]
        adverse_prices = [row["low"] if direction == "call" else row["high"] for row in path]
        mfe = max(directional_pct(price, value, direction) for value in favorable_prices)
        mae = min(directional_pct(price, value, direction) for value in adverse_prices)
        or_position = (price - opening_low) / (opening_high - opening_low) * 100
        forward = {}
        for minutes in (3, 5, 10):
            target = min(entry_index + minutes, len(candles) - 1)
            forward[minutes] = directional_pct(price, candles[target]["close"], direction)
        results.append(
            {
                "entry": trade["entry_time"],
                "exit": trade["exit_time"],
                "dir": direction,
                "signal": str(trade.get("reason", "")).split(":", 1)[0],
                "pnl_usd": float(trade.get("pnl_usd", 0)),
                "pnl_pct": float(trade.get("pnl_pct", 0)),
                "exit_reason": trade.get("exit_reason", ""),
                "stock_entry": price,
                "or_position": or_position,
                "mfe": mfe,
                "mae": mae,
                "fwd3": forward[3],
                "fwd5": forward[5],
                "fwd10": forward[10],
            }
        )
    return sorted(results, key=lambda row: row["pnl_usd"])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("date")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    for row in analyze(args.date, args.root):
        print(
            f"{row['entry']}-{row['exit']} {row['dir'].upper():4} "
            f"${row['pnl_usd']:+8.2f} ({row['pnl_pct']:+6.2f}%) "
            f"OR={row['or_position']:5.1f}% MFE={row['mfe']:+.3f}% MAE={row['mae']:+.3f}% "
            f"+3={row['fwd3']:+.3f}% +5={row['fwd5']:+.3f}% +10={row['fwd10']:+.3f}% "
            f"| {row['signal']} | {row['exit_reason']}"
        )


if __name__ == "__main__":
    main()
