#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Analyze saved 5/10/20-minute signal probes.

The live trader records signal_probes_YYYY-MM-DD.json files under records/.
This script turns those raw probes into daily and grouped quality reports so
strategy changes can be based on real post-entry behavior.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Tuple


ROOT = Path(__file__).resolve().parent
DEFAULT_RECORDS = ROOT / "records"
DEFAULT_REPORTS = ROOT / "reports"
HORIZONS = (5, 10, 20)


def _parse_date(value: str) -> str:
    return value.strip()[:10]


def _as_float(value, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def _load_probes(records_dir: Path, start: str = "", end: str = "", exclude_dates: set[str] | None = None) -> List[Dict]:
    probes: List[Dict] = []
    exclude_dates = exclude_dates or set()
    for path in sorted(records_dir.glob("signal_probes_*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        date = _parse_date(data.get("date") or path.stem.replace("signal_probes_", ""))
        if start and date < start:
            continue
        if end and date > end:
            continue
        if date in exclude_dates:
            continue
        for probe in data.get("probes", []):
            row = dict(probe)
            row["date"] = date
            row["day_market_label"] = row.get("day_market_label") or row.get("day_market_regime") or ""
            probes.append(row)
    return probes


def _detect_signal(reason: str) -> str:
    reason = reason or ""
    known = (
        "VWAP_Breakout",
        "Kline_Pattern",
        "EMA_Cross",
        "Granville_Pullback",
        "RSI_Reversal",
        "RSI_Overbought",
        "Chan_First_Buy",
        "Momentum_Death",
    )
    for name in known:
        if name in reason:
            return name
    if "neutral突破" in reason or "trending突破" in reason or "K线" in reason or "BB挤压" in reason:
        return "Kline_Pattern"
    return "Unknown"


def _load_trades(records_dir: Path, start: str = "", end: str = "", exclude_dates: set[str] | None = None) -> List[Dict]:
    trades: List[Dict] = []
    exclude_dates = exclude_dates or set()
    for path in sorted(records_dir.glob("20*.json")):
        if path.name.startswith("signal_probes_"):
            continue
        date = path.stem[:10]
        if start and date < start:
            continue
        if end and date > end:
            continue
        if date in exclude_dates:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for trade in data.get("trades", []):
            if trade.get("_source") == "broker_reconcile":
                continue
            if not trade.get("dir"):
                continue
            row = dict(trade)
            row["date"] = date
            row["signal"] = _detect_signal(str(row.get("reason", "")))
            row["pnl_usd_num"] = _as_float(row.get("pnl_usd"))
            row["pnl_pct_num"] = _as_float(row.get("pnl_pct"))
            trades.append(row)
    return trades


def _parse_dt(date: str, value) -> datetime | None:
    if hasattr(value, "strftime"):
        return value
    if not value:
        return None
    text = str(value).strip()
    try:
        if len(text) == 10 and text.count("-") == 2:
            return None
        if "T" in text:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
        if len(text) <= 8 and ":" in text:
            return datetime.fromisoformat(f"{date} {text}")
        if len(text) >= 19:
            return datetime.fromisoformat(text[:19])
    except Exception:
        return None
    return None


def _match_probe(trade: Dict, probes_by_date: Dict[str, List[Dict]]) -> Dict:
    date = trade.get("date", "")
    candidates = probes_by_date.get(date, [])
    if not candidates:
        return {}

    trade_time = _parse_dt(date, trade.get("entry_time"))
    opt_symbol = trade.get("opt_symbol", "")
    signal = trade.get("signal", "")
    direction = trade.get("dir", "")

    best = None
    best_score = 10**9
    for probe in candidates:
        score = 0
        if opt_symbol and probe.get("opt_symbol") == opt_symbol:
            score -= 100
        if direction and probe.get("dir") == direction:
            score -= 20
        if signal and probe.get("signal") == signal:
            score -= 20

        probe_time = _parse_dt(date, probe.get("entry_time"))
        if trade_time and probe_time:
            delta = abs((trade_time - probe_time).total_seconds())
            score += delta
        else:
            score += 999

        if score < best_score:
            best = probe
            best_score = score

    if best is None:
        return {}
    if best_score > 999 and opt_symbol and best.get("opt_symbol") != opt_symbol:
        return {}
    return best


def _signed_return(probe: Dict, minutes: int) -> float | None:
    value = probe.get(f"m{minutes}_pct")
    if value is None:
        return None
    return _as_float(value)


def _group_stats(items: List[Dict]) -> Dict:
    stats = {
        "n": len(items),
        "completed": sum(1 for item in items if item.get("completed")),
    }
    for minutes in HORIZONS:
        vals = [
            _signed_return(item, minutes)
            for item in items
            if _signed_return(item, minutes) is not None
        ]
        vals = [v for v in vals if v is not None]
        if vals:
            stats[f"m{minutes}_n"] = len(vals)
            stats[f"m{minutes}_win"] = sum(1 for v in vals if v > 0) / len(vals) * 100
            stats[f"m{minutes}_avg"] = sum(vals) / len(vals)
            stats[f"m{minutes}_sum"] = sum(vals)
        else:
            stats[f"m{minutes}_n"] = 0
            stats[f"m{minutes}_win"] = 0.0
            stats[f"m{minutes}_avg"] = 0.0
            stats[f"m{minutes}_sum"] = 0.0
    return stats


def _group_by(probes: Iterable[Dict], keys: Tuple[str, ...]) -> List[Dict]:
    groups: Dict[Tuple[str, ...], List[Dict]] = defaultdict(list)
    for probe in probes:
        key = tuple(str(probe.get(k) or "") for k in keys)
        groups[key].append(probe)

    rows = []
    for key, items in groups.items():
        row = {name: value for name, value in zip(keys, key)}
        row.update(_group_stats(items))
        rows.append(row)
    rows.sort(key=lambda r: (r.get("m20_avg", 0), r.get("m10_avg", 0)), reverse=True)
    return rows


def _fmt_pct(value: float) -> str:
    return f"{value:+.3f}%"


def _fmt_win(value: float) -> str:
    return f"{value:.1f}%"


def _write_csv(path: Path, rows: List[Dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = list(rows[0].keys())
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _markdown_table(title: str, rows: List[Dict], label_fields: Tuple[str, ...], limit: int = 30) -> List[str]:
    out = [f"\n## {title}", "", "| 分组 | 样本 | 5分钟胜率/均值 | 10分钟胜率/均值 | 20分钟胜率/均值 |", "|---|---:|---:|---:|---:|"]
    for row in rows[:limit]:
        label = " / ".join(str(row.get(field, "") or "-") for field in label_fields)
        out.append(
            "| "
            + " | ".join(
                [
                    label,
                    str(row["n"]),
                    f"{_fmt_win(row['m5_win'])} / {_fmt_pct(row['m5_avg'])}",
                    f"{_fmt_win(row['m10_win'])} / {_fmt_pct(row['m10_avg'])}",
                    f"{_fmt_win(row['m20_win'])} / {_fmt_pct(row['m20_avg'])}",
                ]
            )
            + " |"
        )
    return out


def _recommendations(signal_dir_rows: List[Dict]) -> List[str]:
    strong = [
        row for row in signal_dir_rows
        if row["n"] >= 5 and row["m10_win"] >= 55 and row["m10_avg"] > 0 and row["m20_avg"] > -0.05
    ]
    weak = [
        row for row in signal_dir_rows
        if row["n"] >= 5 and (row["m10_win"] < 45 or row["m10_avg"] < -0.03)
    ]
    out = ["\n## 优化提示", ""]
    if strong:
        out.append("适合放宽或保留的组合：")
        for row in strong[:10]:
            out.append(
                f"- {row.get('signal','-')} / {row.get('dir','-')}: "
                f"n={row['n']}, 10分钟 {_fmt_win(row['m10_win'])}/{_fmt_pct(row['m10_avg'])}, "
                f"20分钟 {_fmt_win(row['m20_win'])}/{_fmt_pct(row['m20_avg'])}"
            )
    else:
        out.append("暂时没有达到放宽条件的组合。")

    if weak:
        out.append("\n建议收紧或只做影子观察的组合：")
        for row in weak[:10]:
            out.append(
                f"- {row.get('signal','-')} / {row.get('dir','-')}: "
                f"n={row['n']}, 10分钟 {_fmt_win(row['m10_win'])}/{_fmt_pct(row['m10_avg'])}, "
                f"20分钟 {_fmt_win(row['m20_win'])}/{_fmt_pct(row['m20_avg'])}"
            )
    else:
        out.append("\n暂时没有明显需要收紧的组合。")
    return out


def _trade_diagnosis(trade: Dict, probe: Dict) -> str:
    pnl = _as_float(trade.get("pnl_usd"))
    m5 = _as_float(probe.get("m5_pct"), None) if probe else None
    m10 = _as_float(probe.get("m10_pct"), None) if probe else None
    m20 = _as_float(probe.get("m20_pct"), None) if probe else None
    exit_reason = str(trade.get("exit_reason", ""))
    signal = trade.get("signal", "")

    if not probe:
        return "未匹配到 5/10/20 信号追踪，先检查开仓记录与 probe 是否漏写。"

    forward = [v for v in (m5, m10, m20) if v is not None]
    if pnl < 0:
        if forward and all(v < 0 for v in forward):
            return "入场后 5/10/20 分钟都逆向，优先收紧该信号的入场条件。"
        if m10 is not None and m10 > 0 and ("止损" in exit_reason or "快退" in exit_reason):
            return "实际亏损但 10 分钟后转正，可能止损/快退过早，适合检查退出阈值。"
        if m20 is not None and m20 > 0:
            return "20 分钟后转正，方向可能不差但入场太早或持仓时间太短。"
        if signal in ("EMA_Cross", "RSI_Reversal", "Chan_First_Buy"):
            return "历史上偏弱的反转/交叉信号，建议降权或要求市场状态配合。"
        return "亏损单，结合拒绝原因和当日行情看是否需要降权。"

    if pnl > 0:
        if m20 is not None and m20 < 0:
            return "盈利后 20 分钟转弱，快速止盈是合理的，类似第一档止盈逻辑。"
        if m20 is not None and m10 is not None and m20 > m10 > 0:
            return "入场后持续顺向，适合保留并考虑趋势日延长持仓。"
        if m5 is not None and m5 > 0 and (m10 is None or m10 <= 0):
            return "盈利窗口偏短，适合快进快出，不宜放太久。"
        return "优质盈利样本，保留当前入场条件。"

    return "盈亏接近 0，更多用于观察信号方向质量。"


def _trade_review_rows(trades: List[Dict], probes: List[Dict]) -> List[Dict]:
    probes_by_date: Dict[str, List[Dict]] = defaultdict(list)
    for probe in probes:
        probes_by_date[probe.get("date", "")].append(probe)

    rows = []
    for trade in trades:
        probe = _match_probe(trade, probes_by_date)
        rows.append({
            "date": trade.get("date", ""),
            "entry_time": trade.get("entry_time", ""),
            "exit_time": trade.get("exit_time", ""),
            "signal": trade.get("signal", ""),
            "dir": trade.get("dir", ""),
            "opt_symbol": trade.get("opt_symbol", ""),
            "contracts": trade.get("contracts", trade.get("qty", "")),
            "pnl_usd": trade.get("pnl_usd", ""),
            "pnl_pct": trade.get("pnl_pct", ""),
            "exit_reason": trade.get("exit_reason", ""),
            "source": trade.get("_source", ""),
            "regime": trade.get("regime", ""),
            "day_market_label": trade.get("day_market_label", ""),
            "probe_source": probe.get("source", "") if probe else "",
            "rejection_reason": probe.get("rejection_reason", "") if probe else "",
            "m5_pct": probe.get("m5_pct", "") if probe else "",
            "m10_pct": probe.get("m10_pct", "") if probe else "",
            "m20_pct": probe.get("m20_pct", "") if probe else "",
            "diagnosis": _trade_diagnosis(trade, probe),
            "reason": trade.get("reason", ""),
        })
    rows.sort(key=lambda r: _as_float(r.get("pnl_usd")))
    return rows


def _trade_review_markdown(rows: List[Dict], top_n: int = 8) -> List[str]:
    if not rows:
        return ["\n## 最佳/最差订单逐单复盘", "", "没有匹配到实际交易记录。"]

    worst = rows[:top_n]
    best = list(reversed(rows[-top_n:]))

    def table(title: str, items: List[Dict]) -> List[str]:
        lines = [
            f"\n### {title}",
            "",
            "| 日期时间 | 信号/方向 | 盈亏 | 退出 | 5/10/20分钟 | 诊断 |",
            "|---|---|---:|---|---:|---|",
        ]
        for row in items:
            dt = f"{row.get('date','')} {row.get('entry_time','')}"
            sig = f"{row.get('signal','-')} / {row.get('dir','-')}"
            pnl = f"${_as_float(row.get('pnl_usd')):+.2f} ({_as_float(row.get('pnl_pct')):+.2f}%)"
            fwd = (
                f"{_fmt_pct(_as_float(row.get('m5_pct')))} / "
                f"{_fmt_pct(_as_float(row.get('m10_pct')))} / "
                f"{_fmt_pct(_as_float(row.get('m20_pct')))}"
            )
            lines.append(
                "| "
                + " | ".join([
                    dt,
                    sig,
                    pnl,
                    str(row.get("exit_reason", ""))[:36],
                    fwd,
                    str(row.get("diagnosis", ""))[:80],
                ])
                + " |"
            )
        return lines

    out = ["\n## 最佳/最差订单逐单复盘", ""]
    out += table("最差订单", worst)
    out += table("最好订单", best)
    return out


def build_report(probes: List[Dict], trades: List[Dict] | None = None, top_trades: int = 8) -> Tuple[str, List[Dict], List[Dict]]:
    by_date = _group_by(probes, ("date",))
    by_signal = _group_by(probes, ("signal",))
    by_signal_dir = _group_by(probes, ("signal", "dir"))
    by_market_signal = _group_by(probes, ("day_market_label", "signal", "dir"))
    by_rejection = _group_by(probes, ("rejection_reason", "signal", "dir"))

    lines = [
        "# 信号触发后 5/10/20 分钟表现复盘",
        "",
        f"- 生成时间: {datetime.now():%Y-%m-%d %H:%M:%S}",
        f"- 样本数: {len(probes)}",
        "- 说明: 当前系统使用 1 分钟 K 线，因此 5/10/20 根 K 线约等于 5/10/20 分钟。",
    ]
    lines += _markdown_table("按日期", by_date, ("date",), 40)
    lines += _markdown_table("按信号", by_signal, ("signal",), 40)
    lines += _markdown_table("按信号 + 方向", by_signal_dir, ("signal", "dir"), 60)
    lines += _markdown_table("按当日行情 + 信号 + 方向", by_market_signal, ("day_market_label", "signal", "dir"), 80)
    lines += _markdown_table("按拒绝原因 + 信号 + 方向", by_rejection, ("rejection_reason", "signal", "dir"), 80)
    lines += _recommendations(by_signal_dir)
    review_rows = _trade_review_rows(trades or [], probes)
    lines += _trade_review_markdown(review_rows, top_trades)

    flat_rows = []
    for probe in probes:
        row = {
            "date": probe.get("date", ""),
            "entry_time": probe.get("entry_time", ""),
            "signal": probe.get("signal", ""),
            "dir": probe.get("dir", ""),
            "source": probe.get("source", ""),
            "regime": probe.get("regime", ""),
            "day_market_label": probe.get("day_market_label", ""),
            "rejection_reason": probe.get("rejection_reason", ""),
            "entry_price": probe.get("entry_price", ""),
            "m5_pct": probe.get("m5_pct", ""),
            "m10_pct": probe.get("m10_pct", ""),
            "m20_pct": probe.get("m20_pct", ""),
            "completed": probe.get("completed", False),
            "reason": probe.get("reason", ""),
        }
        flat_rows.append(row)
    return "\n".join(lines) + "\n", flat_rows, review_rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze signal probe 5/10/20-minute returns.")
    parser.add_argument("--from-date", default="", help="Start date, YYYY-MM-DD")
    parser.add_argument("--to-date", default="", help="End date, YYYY-MM-DD")
    parser.add_argument("--records-dir", default=str(DEFAULT_RECORDS))
    parser.add_argument("--reports-dir", default=str(DEFAULT_REPORTS))
    parser.add_argument("--top-trades", type=int, default=8, help="Best/worst trades to include")
    parser.add_argument("--exclude-date", action="append", default=[], help="Date to exclude, YYYY-MM-DD. Can repeat.")
    args = parser.parse_args()

    records_dir = Path(args.records_dir)
    reports_dir = Path(args.reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)

    exclude_dates = {_parse_date(value) for value in args.exclude_date if value}
    probes = _load_probes(
        records_dir,
        _parse_date(args.from_date) if args.from_date else "",
        _parse_date(args.to_date) if args.to_date else "",
        exclude_dates,
    )
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    md_path = reports_dir / f"signal_probe_summary_{timestamp}.md"
    csv_path = reports_dir / f"signal_probe_rows_{timestamp}.csv"
    trade_csv_path = reports_dir / f"trade_review_rows_{timestamp}.csv"

    trades = _load_trades(
        records_dir,
        _parse_date(args.from_date) if args.from_date else "",
        _parse_date(args.to_date) if args.to_date else "",
        exclude_dates,
    )
    report, rows, review_rows = build_report(probes, trades, args.top_trades)
    md_path.write_text(report, encoding="utf-8")
    _write_csv(csv_path, rows)
    _write_csv(trade_csv_path, review_rows)

    print(f"probes={len(probes)}")
    print(f"trades={len(trades)}")
    print(f"markdown={md_path}")
    print(f"csv={csv_path}")
    print(f"trade_csv={trade_csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
