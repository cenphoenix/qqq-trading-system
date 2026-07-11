#!/usr/bin/env python
"""Replay the implemented v6.2 CALL pool filters on saved 1m candles.

This is a signal-level replay. It mirrors the current live_trader.py
v62_call_pool entry gates as closely as possible from historical candles, but
it does not replay option quotes, broker fills, or other strategy positions.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategy import StrategyRuntimeRules


DEFAULTS = {
    "start_time": "09:35",
    "enable_v62_call_pool": True,
    "v62_call_pool_end_min": 870,
    "v62_call_pool_cooldown_bars": 5,
    "v62_call_pool_lookback": 3,
    "v62_call_pool_vol_mult": 0.80,
    "v62_call_pool_max_gap": 0.0020,
    "v62_call_pool_min_body": 0.00015,
    "v62_call_pool_range_max_position": 0.40,
    "v62_call_pool_max_price_pos": 0.85,
    "v62_call_pool_trend_max_price_pos": 0.95,
    "v62_call_pool_require_vwap_above": True,
    "v62_call_pool_use_sma_trend_guard": True,
    "v62_call_pool_skip_lunch": True,
    "market_regime_enabled": True,
    "trend_day_min_bars": 30,
    "opening_range_filter_enabled": True,
    "opening_range_minutes": 30,
    "opening_range_call_block_pos": 0.90,
    "opening_range_inside_fade_start_min": 690,
    "opening_range_breakout_min_sma20_slope": 0.00008,
    "opening_range_breakout_min_recent_move_pct": 0.0012,
    "extreme_down_call_filter_enabled": True,
    "extreme_down_min_drop_pct": 0.012,
    "extreme_down_max_day_move_for_call": -0.008,
    "extreme_down_call_reclaim_recent_move_pct": 0.0015,
    "extreme_down_call_hard_block_until_min": 690,
    "timeout_stage3_bars": 20,
}


def load_cfg() -> dict:
    cfg = dict(DEFAULTS)
    path = ROOT / "settings.json"
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        for value in data.values():
            if isinstance(value, dict):
                cfg.update(value)
    return cfg


def parse_override(value: str):
    lowered = value.strip().lower()
    if lowered in {"true", "yes", "on"}:
        return True
    if lowered in {"false", "no", "off"}:
        return False
    try:
        if "." in value:
            return float(value)
        return int(value)
    except ValueError:
        return value


def apply_overrides(cfg: dict, overrides: list[str]) -> None:
    for item in overrides:
        if "=" not in item:
            raise SystemExit(f"Bad --set override, expected key=value: {item}")
        key, value = item.split("=", 1)
        cfg[key.strip()] = parse_override(value.strip())


def parse_minute(value: str) -> int | None:
    try:
        dt = datetime.fromisoformat(value)
        return dt.hour * 60 + dt.minute
    except Exception:
        return None


def avg(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def load_day(path: Path) -> list[dict]:
    rows = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            try:
                rows.append(
                    {
                        "timestamp": row["timestamp"],
                        "open": float(row["open"]),
                        "high": float(row["high"]),
                        "low": float(row["low"]),
                        "close": float(row["close"]),
                        "volume": float(row.get("volume") or 0),
                    }
                )
            except Exception:
                continue
    rows.sort(key=lambda item: item["timestamp"])
    return rows


@dataclass
class ReplayState:
    cfg: dict
    bars: list[dict] = field(default_factory=list)
    closes: list[float] = field(default_factory=list)
    volumes: list[float] = field(default_factory=list)
    session_high: float = 0.0
    session_low: float = 999999.0
    cum_pv: float = 0.0
    cum_vol: float = 0.0
    last_bar: int = -999999
    position_until: int = -1

    def add_bar(self, bar: dict) -> None:
        self.bars.append(bar)
        self.closes.append(float(bar["close"]))
        self.volumes.append(float(bar["volume"]))
        self.session_high = max(self.session_high or 0.0, float(bar["high"]))
        self.session_low = min(self.session_low, float(bar["low"]))
        typical = (float(bar["high"]) + float(bar["low"]) + float(bar["close"])) / 3.0
        volume = max(float(bar["volume"]), 0.0)
        self.cum_pv += typical * volume
        self.cum_vol += volume

    @property
    def vwap(self) -> float:
        if self.cum_vol > 0:
            return self.cum_pv / self.cum_vol
        return self.closes[-1] if self.closes else 0.0

    def regular_bars(self) -> list[dict]:
        return [
            bar
            for bar in self.bars
            if (minute := parse_minute(bar["timestamp"])) is not None and 570 <= minute <= 960
        ]

    def entry_context(self, price: float) -> dict:
        high = max(self.session_high or 0.0, price)
        low = min(self.session_low if self.session_low < 999999 else price, price)
        price_pos = (price - low) / (high - low) if high > low else 0.5
        vwap = self.vwap or price
        vwap_dist = (price - vwap) / vwap if vwap else 0.0
        sma20_slope = 0.0
        if len(self.closes) >= 25:
            sma20 = avg(self.closes[-20:])
            sma20_prev = avg(self.closes[-25:-5])
            sma20_slope = (sma20 - sma20_prev) / price if price else 0.0
        return {
            "price_pos": price_pos,
            "vwap_dist": vwap_dist,
            "sma20_slope": sma20_slope,
        }

    def opening_range_context(self, price: float, context: dict) -> dict:
        if not self.cfg.get("opening_range_filter_enabled", True):
            return {"enabled": False, "ready": False}
        minutes = int(self.cfg.get("opening_range_minutes", 30) or 30)
        if minutes <= 0:
            return {"enabled": False, "ready": False}
        regular = self.regular_bars()
        if regular:
            opening = [
                bar for bar in regular
                if (parse_minute(bar["timestamp"]) or 0) < 570 + minutes
            ]
            after_opening = [
                bar for bar in regular
                if (parse_minute(bar["timestamp"]) or 0) >= 570 + minutes
            ]
        else:
            opening = self.bars[:minutes]
            after_opening = self.bars[minutes:]
        if len(opening) < max(10, minutes // 2) or not after_opening:
            return {"enabled": True, "ready": False, "reason": "opening_range_warming"}
        high = max(float(bar["high"]) for bar in opening)
        low = min(float(bar["low"]) for bar in opening)
        width = max(high - low, 1e-9)
        recent_bars = regular or self.bars
        recent_move = 0.0
        if len(recent_bars) >= 6:
            prior = float(recent_bars[-6]["close"])
            if prior > 0:
                recent_move = (price - prior) / prior
        return {
            "enabled": True,
            "ready": True,
            "high": high,
            "low": low,
            "position": (price - low) / width,
            "current_minute": parse_minute(recent_bars[-1]["timestamp"]) if recent_bars else None,
            "vwap_dist": float(context.get("vwap_dist", 0.0) or 0.0),
            "sma20_slope": float(context.get("sma20_slope", 0.0) or 0.0),
            "recent_move": recent_move,
        }

    def classify_regime(self, context: dict) -> dict:
        if not self.cfg.get("market_regime_enabled", True):
            return {"type": "disabled", "direction": ""}
        if len(self.bars) < 15:
            return {"type": "warming_up", "direction": ""}
        current = float(self.bars[-1]["close"])
        first_open = float(self.bars[0]["open"])
        high = max(float(bar["high"]) for bar in self.bars)
        low = min(float(bar["low"]) for bar in self.bars)
        session_range = max(high - low, 1e-9)
        session_pos = (current - low) / session_range
        day_move = (current - first_open) / first_open if first_open else 0.0
        lookback = min(20, len(self.bars) - 1)
        recent_prior = float(self.bars[-lookback]["close"]) if lookback > 0 else current
        recent_move = (current - recent_prior) / recent_prior if recent_prior else 0.0
        recent = self.bars[-min(20, len(self.bars)):]
        up_bars = sum(1 for bar in recent if float(bar["close"]) >= float(bar["open"]))
        down_bars = len(recent) - up_bars
        vwap_dist = float(context.get("vwap_dist", 0.0) or 0.0)
        sma20_slope = float(context.get("sma20_slope", 0.0) or 0.0)
        opening = self.bars[:min(15, len(self.bars))]
        opening_high = max(float(bar["high"]) for bar in opening)
        opening_low = min(float(bar["low"]) for bar in opening)
        broke_open_high = current > opening_high
        broke_open_low = current < opening_low

        trend_up = (
            len(self.bars) >= int(self.cfg.get("trend_day_min_bars", 30) or 30)
            and vwap_dist >= 0.0006
            and sma20_slope >= 0.00005
            and session_pos >= 0.60
            and (day_move >= 0.0012 or recent_move >= 0.0010 or up_bars >= 12)
        )
        trend_down = (
            len(self.bars) >= int(self.cfg.get("trend_day_min_bars", 30) or 30)
            and vwap_dist <= -0.0006
            and sma20_slope <= -0.00005
            and session_pos <= 0.40
            and (day_move <= -0.0012 or recent_move <= -0.0010 or down_bars >= 12)
        )
        if trend_up:
            return {"type": "trend_up", "direction": "call", "pos": session_pos}
        if trend_down:
            return {"type": "trend_down", "direction": "put", "pos": session_pos}
        if len(self.bars) <= 75 and ((broke_open_high and day_move < 0) or (broke_open_low and day_move > 0)):
            return {
                "type": "opening_reversal",
                "direction": "put" if broke_open_high else "call",
                "pos": session_pos,
            }
        range_pct = session_range / current if current else 0.0
        if len(self.bars) >= 30 and abs(vwap_dist) <= 0.0015 and 0.25 <= session_pos <= 0.75 and range_pct <= 0.0065:
            return {"type": "range", "direction": "", "pos": session_pos}
        return {"type": "unclear", "direction": "", "pos": session_pos}

    def skip_opening_range_call(self, price: float, context: dict) -> bool:
        or_ctx = self.opening_range_context(price, context)
        if not or_ctx.get("enabled") or not or_ctx.get("ready"):
            return False
        block_pos = float(self.cfg.get("opening_range_call_block_pos", 0.90) or 0.90)
        inside_fade_start = int(self.cfg.get("opening_range_inside_fade_start_min", 690) or 690)
        high = float(or_ctx.get("high") or 0.0)
        above_opening_high = high > 0 and price > high
        weak_above_breakout = (
            above_opening_high
            and (
                or_ctx["sma20_slope"] < float(self.cfg.get("opening_range_breakout_min_sma20_slope", 0.00008) or 0.00008)
                or or_ctx["recent_move"] < float(self.cfg.get("opening_range_breakout_min_recent_move_pct", 0.0012) or 0.0012)
            )
        )
        inside_late_fade = (
            (or_ctx.get("current_minute") or 0) >= inside_fade_start
            and or_ctx.get("position", 0.0) >= block_pos
            and or_ctx["sma20_slope"] <= 0
            and or_ctx["recent_move"] <= 0
        )
        return bool(weak_above_breakout or inside_late_fade)

    def skip_extreme_down_call(self, price: float, context: dict) -> bool:
        if not self.cfg.get("extreme_down_call_filter_enabled", True):
            return False
        bars = self.regular_bars() or self.bars
        if len(bars) < 15:
            return False
        first_open = float(bars[0]["open"])
        if price <= 0 or first_open <= 0:
            return False
        session_low = min(float(bar["low"]) for bar in bars)
        day_move = (price - first_open) / first_open
        max_drop = (session_low - first_open) / first_open
        min_drop = -abs(float(self.cfg.get("extreme_down_min_drop_pct", 0.012) or 0.012))
        max_day_move = float(self.cfg.get("extreme_down_max_day_move_for_call", -0.008) or -0.008)
        if max_drop > min_drop and day_move > max_day_move:
            return False
        current_minute = parse_minute(bars[-1]["timestamp"])
        hard_until = int(self.cfg.get("extreme_down_call_hard_block_until_min", 690) or 690)
        if current_minute is not None and current_minute > hard_until:
            return False
        or_ctx = self.opening_range_context(price, context)
        or_mid = None
        if or_ctx.get("ready"):
            or_mid = (float(or_ctx.get("high") or 0.0) + float(or_ctx.get("low") or 0.0)) / 2.0
        recent_move = 0.0
        if len(bars) >= 6:
            prior = float(bars[-6]["close"])
            if prior > 0:
                recent_move = (price - prior) / prior
        min_recent = float(self.cfg.get("extreme_down_call_reclaim_recent_move_pct", 0.0015) or 0.0015)
        reversal_ok = (
            (or_mid is None or price >= or_mid)
            and float(context.get("vwap_dist", 0.0) or 0.0) > 0
            and float(context.get("sma20_slope", 0.0) or 0.0) > 0
            and recent_move >= min_recent
        )
        return not reversal_ok


def reject(reason: str, rejects: Counter) -> str:
    rejects[reason] += 1
    return reason


def evaluate_bar(state: ReplayState, bar: dict, minute: int, rejects: Counter, idx: int, lock_bars: int) -> tuple[bool, str, dict]:
    cfg = state.cfg
    if not cfg.get("enable_v62_call_pool", False):
        return False, reject("disabled", rejects), {}
    if lock_bars > 0 and idx <= state.position_until:
        return False, reject("position_lock", rejects), {}

    start_h, start_m = [int(part) for part in cfg.get("start_time", "09:35").split(":")]
    start_min = start_h * 60 + start_m
    end_min = int(cfg.get("v62_call_pool_end_min", 870) or 870)
    if not (start_min <= minute <= end_min):
        return False, reject("time_window", rejects), {}
    if cfg.get("v62_call_pool_skip_lunch", True) and 720 <= minute < 780:
        return False, reject("lunch", rejects), {}

    current_bar_index = len(state.bars)
    cooldown = int(cfg.get("v62_call_pool_cooldown_bars", 5) or 5)
    if current_bar_index - int(state.last_bar) < cooldown:
        return False, reject("cooldown", rejects), {}

    lb = int(cfg.get("v62_call_pool_lookback", 3) or 3)
    if len(state.bars) < lb + 1:
        return False, reject("not_enough_bars", rejects), {}

    entry = float(bar["close"])
    upper = max(float(item["high"]) for item in state.bars[-lb - 1:-1])
    gap = (entry - upper) / upper if upper else 0.0
    max_gap = float(cfg.get("v62_call_pool_max_gap", 0.0020) or 0.0020)
    if not (entry > upper and gap < max_gap):
        return False, reject("no_breakout", rejects), {}
    if float(bar["close"]) < float(bar["open"]):
        return False, reject("bearish_bar", rejects), {}

    vol_avg = avg(state.volumes[-20:]) if len(state.volumes) >= 20 else 0.0
    vol_mult = float(cfg.get("v62_call_pool_vol_mult", 0.80) or 0.80)
    if vol_avg > 0 and float(bar["volume"]) < vol_avg * vol_mult:
        return False, reject("low_volume", rejects), {}

    body = abs(float(bar["close"]) - float(bar["open"])) / float(bar["open"] or 1.0)
    min_body = float(cfg.get("v62_call_pool_min_body", 0.00015) or 0.00015)
    if body < min_body:
        return False, reject("small_body", rejects), {}

    if cfg.get("v62_call_pool_use_sma_trend_guard", True) and len(state.closes) >= 50:
        sma20 = avg(state.closes[-20:])
        sma50 = avg(state.closes[-50:])
        if sma20 < sma50 and entry < sma20:
            return False, reject("sma_downtrend", rejects), {}

    if cfg.get("v62_call_pool_require_vwap_above", True) and state.vwap > 0 and entry < state.vwap:
        return False, reject("below_vwap", rejects), {}

    context = state.entry_context(entry)
    regime = state.classify_regime(context)
    regime_type = regime.get("type", "")
    regime_dir = regime.get("direction", "")
    if regime_dir == "put" or regime_type == "trend_down":
        return False, reject("trend_down_regime", rejects), {}
    max_price_pos = float(cfg.get("v62_call_pool_max_price_pos", 0.85) or 0.85)
    if regime_type == "trend_up":
        max_price_pos = float(cfg.get("v62_call_pool_trend_max_price_pos", 0.95) or 0.95)
    if context.get("price_pos", 0.5) > max_price_pos:
        return False, reject("price_pos_high", rejects), {}
    if regime_type == "range":
        max_range_pos = float(cfg.get("v62_call_pool_range_max_position", 0.40) or 0.40)
        if float(regime.get("pos", context.get("price_pos", 0.5)) or context.get("price_pos", 0.5)) > max_range_pos:
            return False, reject("range_not_edge", rejects), {}

    if state.skip_opening_range_call(entry, context):
        return False, reject("opening_range_call_block", rejects), {}
    if state.skip_extreme_down_call(entry, context):
        return False, reject("extreme_down_call_block", rejects), {}

    state.last_bar = current_bar_index
    if lock_bars > 0:
        state.position_until = idx + lock_bars
    return True, "accepted", {"entry": entry, "regime": regime_type, "gap": gap, "price_pos": context["price_pos"]}


def replay(paths: list[Path], cfg: dict, lock_bars: int) -> tuple[list[dict], Counter]:
    events = []
    rejects = Counter()
    for path in paths:
        rows = load_day(path)
        state = ReplayState(cfg=cfg)
        for idx, bar in enumerate(rows):
            state.add_bar(bar)
            minute = parse_minute(bar["timestamp"])
            if minute is None:
                rejects["bad_time"] += 1
                continue
            accepted, _reason, meta = evaluate_bar(state, bar, minute, rejects, idx, lock_bars)
            if not accepted:
                continue
            event = {
                "date": path.stem,
                "time": bar["timestamp"],
                "idx": idx,
                "entry": meta["entry"],
                "regime": meta["regime"],
                "gap_pct": meta["gap"] * 100,
                "price_pos": meta["price_pos"],
            }
            for horizon in (5, 10, 20):
                if idx + horizon < len(rows):
                    future = float(rows[idx + horizon]["close"])
                    event[f"ret{horizon}"] = (future - meta["entry"]) / meta["entry"] * 100.0
                else:
                    event[f"ret{horizon}"] = None
            events.append(event)
    return events, rejects


def summarize(events: list[dict], rejects: Counter, title: str) -> None:
    print(f"\n== {title} ==")
    print(f"accepted: {len(events)}")
    for horizon in (5, 10, 20):
        vals = [event[f"ret{horizon}"] for event in events if event[f"ret{horizon}"] is not None]
        if vals:
            wr = sum(1 for value in vals if value > 0) / len(vals) * 100.0
            print(f"+{horizon}: n={len(vals)} win={wr:.1f}% avg={mean(vals):+.4f}%")
        else:
            print(f"+{horizon}: n=0")
    by_regime = defaultdict(list)
    for event in events:
        if event["ret20"] is not None:
            by_regime[event["regime"]].append(event["ret20"])
    if by_regime:
        print("by regime +20:")
        for regime, vals in sorted(by_regime.items()):
            wr = sum(1 for value in vals if value > 0) / len(vals) * 100.0
            print(f"  {regime or 'unknown'}: n={len(vals)} win={wr:.1f}% avg={mean(vals):+.4f}%")
    print("top rejects:")
    for name, count in rejects.most_common(12):
        print(f"  {name}: {count}")
    complete = [event for event in events if event["ret20"] is not None]
    if complete:
        worst = sorted(complete, key=lambda event: event["ret20"])[:5]
        best = sorted(complete, key=lambda event: event["ret20"], reverse=True)[:5]
        print("worst +20:")
        for event in worst:
            print(f"  {event['time']} entry={event['entry']:.2f} ret20={event['ret20']:+.4f}% regime={event['regime']} pos={event['price_pos']*100:.0f}%")
        print("best +20:")
        for event in best:
            print(f"  {event['time']} entry={event['entry']:.2f} ret20={event['ret20']:+.4f}% regime={event['regime']} pos={event['price_pos']*100:.0f}%")


def compact_metrics(events: list[dict]) -> str:
    vals5 = [event["ret5"] for event in events if event["ret5"] is not None]
    vals20 = [event["ret20"] for event in events if event["ret20"] is not None]
    if not vals20:
        return f"n={len(events):>3} +5=n/a +20=n/a"
    win5 = sum(1 for value in vals5 if value > 0) / len(vals5) * 100.0 if vals5 else 0.0
    win20 = sum(1 for value in vals20 if value > 0) / len(vals20) * 100.0
    avg20 = mean(vals20)
    return f"n={len(events):>3} +5wr={win5:>5.1f}% +20wr={win20:>5.1f}% +20avg={avg20:+.4f}%"


def run_matrix(paths: list[Path], base_cfg: dict) -> None:
    variants = [
        ("baseline", {}),
        ("volume_0.60", {"v62_call_pool_vol_mult": 0.60}),
        ("body_0.00015", {"v62_call_pool_min_body": 0.00015}),
        ("max_pos_0.95", {"v62_call_pool_max_price_pos": 0.95}),
        ("range_pos_0.70", {"v62_call_pool_range_max_position": 0.70}),
        ("no_or_filter", {"opening_range_filter_enabled": False}),
        ("no_extreme_down", {"extreme_down_call_filter_enabled": False}),
        ("no_sma_guard", {"v62_call_pool_use_sma_trend_guard": False}),
        ("no_vwap_guard", {"v62_call_pool_require_vwap_above": False}),
        ("allow_lunch", {"v62_call_pool_skip_lunch": False}),
        ("full_day_until_16", {"v62_call_pool_end_min": 960}),
        (
            "loose_combo_safe",
            {
                "v62_call_pool_vol_mult": 0.60,
                "v62_call_pool_min_body": 0.00015,
                "v62_call_pool_max_price_pos": 0.90,
            },
        ),
        (
            "loose_combo_aggressive",
            {
                "v62_call_pool_vol_mult": 0.50,
                "v62_call_pool_min_body": 0.00010,
                "v62_call_pool_max_price_pos": 0.95,
                "v62_call_pool_range_max_position": 0.70,
                "v62_call_pool_skip_lunch": False,
                "v62_call_pool_end_min": 960,
            },
        ),
    ]
    lock_bars = StrategyRuntimeRules.v62_lock_bars(base_cfg)
    print("variant                         independent                       lock20")
    print("-" * 92)
    for name, changes in variants:
        cfg = dict(base_cfg)
        cfg.update(changes)
        events, _rejects = replay(paths, cfg, lock_bars=0)
        locked, _locked_rejects = replay(paths, cfg, lock_bars=lock_bars)
        print(f"{name:<30} {compact_metrics(events):<34} {compact_metrics(locked)}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--glob", default="2026-06-*.csv", help="CSV glob under data/candles")
    parser.add_argument("--start", default="", help="First date to include, YYYY-MM-DD")
    parser.add_argument("--end", default="", help="Last date to include, YYYY-MM-DD")
    parser.add_argument("--set", action="append", default=[], help="Override config key=value")
    parser.add_argument("--matrix", action="store_true", help="Run a compact filter-ablation matrix")
    args = parser.parse_args()
    cfg = load_cfg()
    apply_overrides(cfg, args.set)
    paths = sorted((ROOT / "data" / "candles").glob(args.glob))
    if args.start:
        paths = [path for path in paths if path.stem >= args.start]
    if args.end:
        paths = [path for path in paths if path.stem <= args.end]
    if not paths:
        raise SystemExit(f"No files matched: {args.glob}")

    print(f"files: {len(paths)} ({paths[0].name} .. {paths[-1].name})")
    print("note: replay excludes option quotes, broker fills, other live strategy positions, and PriceActionFilter state.")

    if args.matrix:
        run_matrix(paths, cfg)
        return

    events, rejects = replay(paths, cfg, lock_bars=0)
    summarize(events, rejects, "independent signal quality")

    lock_bars = StrategyRuntimeRules.v62_lock_bars(cfg)
    locked_events, locked_rejects = replay(paths, cfg, lock_bars=lock_bars)
    summarize(locked_events, locked_rejects, f"self position lock {lock_bars} bars")


if __name__ == "__main__":
    main()
