"""Candle-based per-trade review for entry and exit quality."""

from __future__ import annotations

import csv
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable


APP_DIR = Path(__file__).resolve().parent
CANDLE_DIR = APP_DIR / "data" / "candles"


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _trade_time(day: date, value: Any) -> datetime | None:
    text = str(value or "")
    if not text:
        return None
    if "T" in text or (len(text) >= 10 and text[4:5] == "-"):
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            pass
    try:
        return datetime.strptime(f"{day.isoformat()} {text[:8]}", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def load_session_candles(day: date, candle_dir: Path = CANDLE_DIR) -> list[dict[str, Any]]:
    path = candle_dir / f"{day.isoformat()}.csv"
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    candles = []
    for row in rows:
        try:
            candles.append({
                "time": datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M:%S"),
                "open": _number(row.get("open")),
                "high": _number(row.get("high")),
                "low": _number(row.get("low")),
                "close": _number(row.get("close")),
                "volume": _number(row.get("volume")),
            })
        except (KeyError, ValueError):
            continue
    return candles


def _directional_pct(start: float, end: float, direction: str) -> float:
    change = (end - start) / start * 100 if start else 0.0
    return -change if direction == "put" else change


def _nearest_index(candles: list[dict[str, Any]], target: datetime) -> int:
    return min(range(len(candles)), key=lambda index: abs((candles[index]["time"] - target).total_seconds()))


def _atr(candles: list[dict[str, Any]], index: int, period: int = 14) -> float:
    start = max(index - period, 0)
    ranges = [row["high"] - row["low"] for row in candles[start:index]]
    return sum(ranges) / len(ranges) if ranges else 0.0


def _entry_tags(
    direction: str,
    or_position: float,
    candle_range_atr: float,
    source_text: str,
) -> list[str]:
    tags = []
    if direction == "call" and or_position >= 80:
        tags.append("CALL接近开盘区间高位")
    if direction == "put" and or_position <= 20:
        tags.append("PUT接近开盘区间低位")
    if candle_range_atr >= 1.5:
        tags.append("超大K线后追价")
    if "unclear" in source_text or "range" in source_text:
        tags.append("震荡/方向不明")
    if "影子" in source_text or "scout" in source_text.lower():
        tags.append("影子/Scout信号")
    return tags


def review_trade(trade: dict[str, Any], day: date, candles: list[dict[str, Any]]) -> dict[str, Any]:
    item = dict(trade)
    entry_time = _trade_time(day, item.get("entry_time") or item.get("time"))
    exit_time = _trade_time(day, item.get("exit_time"))
    if not candles or not entry_time or not exit_time:
        item["review_available"] = False
        return item

    entry_index = _nearest_index(candles, entry_time)
    exit_index = max(entry_index, _nearest_index(candles, exit_time))
    entry_candle = candles[entry_index]
    entry_price = entry_candle["open"] or entry_candle["close"]
    direction = str(item.get("dir") or "").lower()
    path = candles[entry_index : exit_index + 1]
    favorable = [row["high"] if direction == "call" else row["low"] for row in path]
    adverse = [row["low"] if direction == "call" else row["high"] for row in path]
    mfe = max((_directional_pct(entry_price, price, direction) for price in favorable), default=0.0)
    mae = min((_directional_pct(entry_price, price, direction) for price in adverse), default=0.0)

    regular = [row for row in candles if "09:30" <= row["time"].strftime("%H:%M") < "10:00"]
    opening_high = max((row["high"] for row in regular), default=entry_price)
    opening_low = min((row["low"] for row in regular), default=entry_price)
    width = opening_high - opening_low
    or_position = (entry_price - opening_low) / width * 100 if width > 0 else 50.0
    atr = _atr(candles, entry_index)
    candle_range_atr = (entry_candle["high"] - entry_candle["low"]) / atr if atr > 0 else 0.0
    source_text = " ".join(str(item.get(key, "")) for key in ("reason", "regime", "day_market_regime"))
    entry_tags = _entry_tags(direction, or_position, candle_range_atr, source_text)

    forward = {}
    for minutes in (3, 5, 10, 20):
        target = min(entry_index + minutes, len(candles) - 1)
        forward[minutes] = _directional_pct(entry_price, candles[target]["close"], direction)
    post_exit = {}
    exit_price = candles[exit_index]["close"]
    for minutes in (5, 10):
        target = min(exit_index + minutes, len(candles) - 1)
        post_exit[minutes] = _directional_pct(exit_price, candles[target]["close"], direction)

    pnl_usd = _number(item.get("pnl_usd"))
    pnl_pct = _number(item.get("pnl_pct"))
    hold_seconds = max((exit_time - entry_time).total_seconds(), 0)
    exit_reason = str(item.get("exit_reason") or "")
    if pnl_usd > 0:
        if post_exit[10] >= 0.20:
            exit_verdict = "可能止盈过早，退出后方向继续"
        elif post_exit[5] <= -0.10:
            exit_verdict = "止盈合理，及时保护利润"
        else:
            exit_verdict = "止盈基本合理"
        entry_verdict = "入场质量良好" if not entry_tags and mfe > abs(mae) else "盈利但入场仍有改进空间"
    else:
        noise_stop = hold_seconds <= 30 and "止损" in exit_reason and abs(mae) < 0.15
        recovered = post_exit[10] >= 0.15 or forward[10] >= 0.15
        if noise_stop:
            exit_verdict = "疑似价差/报价噪声止损"
        elif pnl_pct <= -30:
            exit_verdict = "止损过宽，单笔风险过大"
        elif recovered:
            exit_verdict = "止损后方向恢复，需要结构确认"
        else:
            exit_verdict = "止损合理，限制进一步亏损"
        entry_verdict = "较可避免的亏损" if entry_tags else "信号失败，入场未见明显追价"

    item.update({
        "review_available": True,
        "entry_verdict": entry_verdict,
        "exit_verdict": exit_verdict,
        "entry_tags": entry_tags,
        "hold_seconds": round(hold_seconds),
        "or_position": round(or_position, 1),
        "entry_candle_atr": round(candle_range_atr, 2),
        "stock_mfe_pct": round(mfe, 4),
        "stock_mae_pct": round(mae, 4),
        "stock_fwd_3_pct": round(forward[3], 4),
        "stock_fwd_5_pct": round(forward[5], 4),
        "stock_fwd_10_pct": round(forward[10], 4),
        "stock_fwd_20_pct": round(forward[20], 4),
        "post_exit_5_pct": round(post_exit[5], 4),
        "post_exit_10_pct": round(post_exit[10], 4),
    })
    return item


def review_trades_for_day(
    trades: Iterable[dict[str, Any]],
    day: date,
    candle_dir: Path = CANDLE_DIR,
) -> list[dict[str, Any]]:
    candles = load_session_candles(day, candle_dir)
    return [review_trade(trade, day, candles) for trade in trades]
