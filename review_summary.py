"""
Review summary builder for daily, weekly, and monthly trading reviews.

The dashboard and Telegram notifications share this module so that review
numbers use one consistent calculation path.
"""
from __future__ import annotations

import html
import json
import os
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

from trade_review import review_trades_for_day


APP_DIR = os.path.dirname(os.path.abspath(__file__))
RECORDS_DIR = os.path.join(APP_DIR, "records")


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_date(value: Optional[str]) -> date:
    if value:
        try:
            return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
        except ValueError:
            pass
    return datetime.now().date()


def _read_json(path: str) -> Dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _record_dates() -> List[date]:
    if not os.path.isdir(RECORDS_DIR):
        return []
    dates = []
    for name in os.listdir(RECORDS_DIR):
        if not name.endswith(".json") or name.startswith("signal_probes_"):
            continue
        try:
            dates.append(datetime.strptime(name[:10], "%Y-%m-%d").date())
        except ValueError:
            continue
    return sorted(set(dates))


def period_range(period: str = "day", anchor: Optional[str] = None) -> Tuple[date, date]:
    anchor_date = _parse_date(anchor)
    period = (period or "day").lower()
    if period in ("week", "weekly"):
        start = anchor_date - timedelta(days=anchor_date.weekday())
        return start, start + timedelta(days=6)
    if period in ("month", "monthly"):
        start = anchor_date.replace(day=1)
        if start.month == 12:
            end = start.replace(year=start.year + 1, month=1, day=1) - timedelta(days=1)
        else:
            end = start.replace(month=start.month + 1, day=1) - timedelta(days=1)
        return start, end
    return anchor_date, anchor_date


def is_last_weekday_of_month(day: date) -> bool:
    nxt = day + timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    return nxt.month != day.month


def _date_iter(start: date, end: date) -> Iterable[date]:
    cur = start
    while cur <= end:
        yield cur
        cur += timedelta(days=1)


def _normalize_trade(trade: Dict, record_date: date) -> Dict:
    item = dict(trade)
    pnl = _to_float(item.get("pnl_usd"))
    result = str(item.get("result") or "").lower()
    if not result:
        result = "win" if pnl > 0 else "lose" if pnl < 0 else "flat"
    item["pnl_usd"] = pnl
    item["pnl_pct"] = _to_float(item.get("pnl_pct"))
    item["contracts"] = int(_to_float(item.get("contracts", item.get("qty", 0))))
    item["dir"] = str(item.get("dir") or "").lower()
    parsed_signal = _signal_from_reason(item.get("reason", ""))
    item["signal"] = item.get("signal") or (parsed_signal if parsed_signal != "Unknown" else None) or item.get("regime") or "Unknown"
    item["result"] = result
    item["date"] = str(item.get("date") or record_date.isoformat())[:10]
    item["entry_time"] = str(item.get("entry_time") or item.get("time") or "")
    item["exit_time"] = str(item.get("exit_time") or "")
    return item


def _signal_from_reason(reason: Any) -> str:
    text = str(reason or "")
    known = (
        "VWAP_Breakout", "Granville_Pullback", "Kline_Pattern", "EMA_Cross",
        "RSI_Reversal", "RSI_Overbought", "Chan_First_Buy", "Momentum_Death",
        "OR_High_Reversal", "OR_Low_Reversal", "Failed_Breakout", "VWAP_Reversion",
    )
    for name in known:
        if name in text:
            return name
    if "v6.2突破" in text:
        return "Kline_Pattern"
    if ":" in text:
        prefix = text.split(":", 1)[0].strip()
        if 2 <= len(prefix) <= 40 and "[" not in prefix:
            return prefix.split()[-1]
    return "Unknown"


def _load_trades_for_day(day: date) -> List[Dict]:
    path = os.path.join(RECORDS_DIR, f"{day.isoformat()}.json")
    data = _read_json(path)
    raw_trades = data.get("trades", [])
    if not isinstance(raw_trades, list):
        return []
    trades = [_normalize_trade(t, day) for t in raw_trades if isinstance(t, dict)]
    internal = [t for t in trades if t.get("_source") != "broker_reconcile"]
    if internal:
        return internal
    return trades


def _load_probes_for_day(day: date) -> List[Dict]:
    path = os.path.join(RECORDS_DIR, f"signal_probes_{day.isoformat()}.json")
    data = _read_json(path)
    raw = data.get("probes", [])
    if not isinstance(raw, list):
        return []
    rows = []
    for probe in raw:
        if not isinstance(probe, dict):
            continue
        item = dict(probe)
        item["date"] = day.isoformat()
        item["signal"] = item.get("signal") or item.get("regime") or "Unknown"
        item["dir"] = str(item.get("dir") or "").lower()
        for key in ("m5_pct", "m10_pct", "m20_pct"):
            item[key] = _to_float(item.get(key), default=None)
        rows.append(item)
    return rows


def _group_signal_stats(trades: List[Dict], probes: List[Dict]) -> List[Dict]:
    groups: Dict[str, Dict] = defaultdict(lambda: {
        "signal": "",
        "trades": 0,
        "wins": 0,
        "pnl": 0.0,
        "m20_values": [],
        "m10_values": [],
        "m5_values": [],
        "probes": 0,
    })
    for trade in trades:
        name = str(trade.get("signal") or "Unknown")
        g = groups[name]
        g["signal"] = name
        g["trades"] += 1
        g["wins"] += 1 if trade.get("result") == "win" or _to_float(trade.get("pnl_usd")) > 0 else 0
        g["pnl"] += _to_float(trade.get("pnl_usd"))
    for probe in probes:
        name = str(probe.get("signal") or "Unknown")
        g = groups[name]
        g["signal"] = name
        g["probes"] += 1
        for field, target in (("m5_pct", "m5_values"), ("m10_pct", "m10_values"), ("m20_pct", "m20_values")):
            value = probe.get(field)
            if isinstance(value, (int, float)):
                g[target].append(float(value))

    rows = []
    for g in groups.values():
        m20 = g["m20_values"]
        m20_avg = sum(m20) / len(m20) if m20 else 0.0
        m20_wr = len([v for v in m20 if v > 0]) / len(m20) * 100 if m20 else 0.0
        trade_wr = g["wins"] / g["trades"] * 100 if g["trades"] else 0.0
        rows.append({
            "signal": g["signal"],
            "trades": g["trades"],
            "wins": g["wins"],
            "win_rate": round(trade_wr, 1),
            "pnl": round(g["pnl"], 2),
            "probes": g["probes"],
            "m5_avg": round(sum(g["m5_values"]) / len(g["m5_values"]), 4) if g["m5_values"] else 0.0,
            "m10_avg": round(sum(g["m10_values"]) / len(g["m10_values"]), 4) if g["m10_values"] else 0.0,
            "m20_avg": round(m20_avg, 4),
            "m20_win_rate": round(m20_wr, 1),
        })
    rows.sort(key=lambda x: (x["pnl"], x["m20_avg"], x["trades"]), reverse=True)
    return rows


def _direction_stats(trades: List[Dict]) -> Dict[str, Dict]:
    out = {}
    for direction in ("call", "put"):
        rows = [t for t in trades if t.get("dir") == direction]
        wins = len([t for t in rows if t.get("result") == "win" or _to_float(t.get("pnl_usd")) > 0])
        pnl = sum(_to_float(t.get("pnl_usd")) for t in rows)
        out[direction] = {
            "trades": len(rows),
            "wins": wins,
            "losses": max(len(rows) - wins, 0),
            "win_rate": round(wins / len(rows) * 100, 1) if rows else 0.0,
            "pnl": round(pnl, 2),
        }
    return out


def _trade_issue_tags(trade: Dict) -> List[str]:
    text = " ".join(
        str(trade.get(key, "") or "")
        for key in ("reason", "exit_reason", "signal", "regime")
    ).lower()
    direction = str(trade.get("dir") or "").lower()
    pnl = _to_float(trade.get("pnl_usd"))
    tags = []

    if pnl >= 0:
        return tags

    if direction == "call" and any(k in text for k in ("openingrange", "or ", "breakout", "vwap_breakout", "追高", "突破")):
        tags.append("CALL chase / weak breakout")
    if any(k in text for k in ("timeout", "超时", "硬超时")):
        tags.append("Timeout loss")
    if any(k in text for k in ("stop", "止损", "快退", "fast")):
        tags.append("Stop / fast-fail")
    if any(k in text for k in ("trail", "移动止盈", "盈利回吐")):
        tags.append("Profit giveback")
    if direction == "put" and any(k in text for k in ("counter", "reversal", "反转", "逆势")):
        tags.append("PUT countertrend")
    if "broker" in text or "对账" in text:
        tags.append("Broker reconcile")
    if not tags:
        tags.append("Unclassified loss")
    return tags[:3]


def _issue_tag_stats(trades: List[Dict]) -> List[Dict]:
    groups: Dict[str, Dict] = defaultdict(lambda: {
        "tag": "",
        "trades": 0,
        "pnl": 0.0,
        "max_loss": 0.0,
        "directions": defaultdict(int),
    })
    for trade in trades:
        pnl = _to_float(trade.get("pnl_usd"))
        if pnl >= 0:
            continue
        for tag in _trade_issue_tags(trade):
            g = groups[tag]
            g["tag"] = tag
            g["trades"] += 1
            g["pnl"] += pnl
            g["max_loss"] = min(g["max_loss"], pnl)
            direction = str(trade.get("dir") or "").lower() or "unknown"
            g["directions"][direction] += 1

    rows = []
    for g in groups.values():
        direction = ""
        if g["directions"]:
            direction = max(g["directions"].items(), key=lambda item: item[1])[0]
        rows.append({
            "tag": g["tag"],
            "trades": g["trades"],
            "pnl": round(g["pnl"], 2),
            "max_loss": round(g["max_loss"], 2),
            "main_direction": direction,
        })
    rows.sort(key=lambda x: (x["pnl"], -x["trades"]))
    return rows


def _recommendations(signal_rows: List[Dict], trades: List[Dict], probes: List[Dict]) -> List[str]:
    recs = []
    losers = [s for s in signal_rows if s["trades"] >= 2 and s["pnl"] < 0]
    weak_probes = [s for s in signal_rows if s["probes"] >= 5 and s["m20_avg"] < -0.05 and s["m20_win_rate"] < 45]
    strong = [s for s in signal_rows if (s["trades"] >= 2 and s["pnl"] > 0) or (s["probes"] >= 5 and s["m20_avg"] > 0.05 and s["m20_win_rate"] >= 55)]
    if strong:
        names = ", ".join(s["signal"] for s in strong[:3])
        recs.append(f"优先保留/放大：{names}")
    if losers:
        names = ", ".join(s["signal"] for s in losers[:3])
        recs.append(f"重点复盘亏损信号：{names}")
    if weak_probes:
        names = ", ".join(s["signal"] for s in weak_probes[:3])
        recs.append(f"影子追踪偏弱，建议收紧或暂停：{names}")
    if trades:
        worst = min(trades, key=lambda t: _to_float(t.get("pnl_usd")))
        if _to_float(worst.get("pnl_usd")) < 0:
            recs.append(f"最大亏损来自 {worst.get('dir', '--').upper()} {worst.get('signal', 'Unknown')}，检查入场追价和止损速度")
    if not recs:
        recs.append("样本还不够，先继续积累 5/10/20K 跟踪数据")
    return recs[:4]


def build_review_summary(period: str = "day", anchor: Optional[str] = None) -> Dict:
    start, end = period_range(period, anchor)
    trades: List[Dict] = []
    probes: List[Dict] = []
    day_rows = []
    for day in _date_iter(start, end):
        day_trades = _load_trades_for_day(day)
        day_trades = review_trades_for_day(day_trades, day)
        day_probes = _load_probes_for_day(day)
        if day_trades or day_probes:
            wins = len([t for t in day_trades if t.get("result") == "win" or _to_float(t.get("pnl_usd")) > 0])
            pnl = sum(_to_float(t.get("pnl_usd")) for t in day_trades)
            day_rows.append({
                "date": day.isoformat(),
                "trades": len(day_trades),
                "wins": wins,
                "pnl": round(pnl, 2),
                "probes": len(day_probes),
            })
        trades.extend(day_trades)
        probes.extend(day_probes)

    wins = len([t for t in trades if t.get("result") == "win" or _to_float(t.get("pnl_usd")) > 0])
    total = len(trades)
    pnl = sum(_to_float(t.get("pnl_usd")) for t in trades)
    signal_rows = _group_signal_stats(trades, probes)
    winners_by_pnl = sorted(
        [t for t in trades if _to_float(t.get("pnl_usd")) > 0],
        key=lambda t: _to_float(t.get("pnl_usd")),
        reverse=True,
    )
    losers_by_pnl = sorted(
        [t for t in trades if _to_float(t.get("pnl_usd")) < 0],
        key=lambda t: _to_float(t.get("pnl_usd")),
    )
    best_trades = sorted(trades, key=lambda t: _to_float(t.get("pnl_usd")), reverse=True)[:5]
    worst_trades = sorted(trades, key=lambda t: _to_float(t.get("pnl_usd")))[:5]
    best_signals = signal_rows[:5]
    worst_signals = sorted(signal_rows, key=lambda x: (x["pnl"], x["m20_avg"]))[:5]
    issue_tags = _issue_tag_stats(trades)[:6]
    period_key = (period or "day").lower()
    title_map = {"day": "每日复盘", "daily": "每日复盘", "week": "每周复盘", "weekly": "每周复盘", "month": "每月复盘", "monthly": "每月复盘"}

    summary = {
        "period": period_key,
        "title": title_map.get(period_key, "复盘摘要"),
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "label": start.isoformat() if start == end else f"{start.isoformat()} ~ {end.isoformat()}",
        "days": day_rows,
        "trades": total,
        "wins": wins,
        "losses": max(total - wins, 0),
        "win_rate": round(wins / total * 100, 1) if total else 0.0,
        "pnl": round(pnl, 2),
        "directions": _direction_stats(trades),
        "best_trades": _compact_trades(best_trades),
        "worst_trades": _compact_trades(worst_trades),
        "winner_reviews": _compact_trades(winners_by_pnl[:5]),
        "loser_reviews": _compact_trades(losers_by_pnl[:5]),
        "best_signals": best_signals,
        "worst_signals": worst_signals,
        "issue_tags": issue_tags,
        "recommendations": _recommendations(signal_rows, trades, probes),
        "probe_count": len(probes),
    }
    summary["telegram_html"] = format_telegram_summary(summary)
    return summary


def _compact_trades(trades: List[Dict]) -> List[Dict]:
    rows = []
    for t in trades:
        rows.append({
            "date": t.get("date", ""),
            "time": t.get("entry_time", ""),
            "dir": t.get("dir", ""),
            "signal": t.get("signal", "Unknown"),
            "opt_symbol": t.get("opt_symbol", ""),
            "contracts": t.get("contracts", 0),
            "pnl_usd": round(_to_float(t.get("pnl_usd")), 2),
            "pnl_pct": round(_to_float(t.get("pnl_pct")), 2),
            "exit_reason": t.get("exit_reason", ""),
            "entry_verdict": t.get("entry_verdict", ""),
            "exit_verdict": t.get("exit_verdict", ""),
            "entry_tags": list(t.get("entry_tags") or []),
            "stock_mfe_pct": t.get("stock_mfe_pct"),
            "stock_mae_pct": t.get("stock_mae_pct"),
            "post_exit_5_pct": t.get("post_exit_5_pct"),
            "post_exit_10_pct": t.get("post_exit_10_pct"),
        })
    return rows


def _money(value: Any) -> str:
    return f"${_to_float(value):+,.2f}"


def _pct(value: Any) -> str:
    return f"{_to_float(value):.1f}%"


def _line_trade(trade: Dict) -> str:
    signal = html.escape(str(trade.get("signal") or "Unknown"))
    direction = html.escape(str(trade.get("dir") or "").upper())
    return f"{direction} {signal} {_money(trade.get('pnl_usd'))}"


def format_telegram_summary(summary: Dict) -> str:
    direction = summary.get("directions", {})
    call = direction.get("call", {})
    put = direction.get("put", {})
    lines = [
        f"<b>{html.escape(str(summary.get('title', '复盘摘要')))} {html.escape(str(summary.get('label', '')))}</b>",
        "────────────",
        f"交易 <b>{summary.get('trades', 0)}</b>笔 | 胜率 <b>{_pct(summary.get('win_rate'))}</b> | 盈亏 <b>{_money(summary.get('pnl'))}</b>",
        f"CALL {call.get('trades', 0)}笔 / 胜率 {_pct(call.get('win_rate'))} / {_money(call.get('pnl'))}",
        f"PUT  {put.get('trades', 0)}笔 / 胜率 {_pct(put.get('win_rate'))} / {_money(put.get('pnl'))}",
        f"信号追踪 <b>{summary.get('probe_count', 0)}</b>条",
    ]
    best_signals = summary.get("best_signals") or []
    worst_signals = summary.get("worst_signals") or []
    if best_signals:
        lines += ["────────────", "<b>强势信号</b>"]
        for item in best_signals[:3]:
            lines.append(f"{html.escape(item['signal'])}: {item['trades']}单 {_money(item['pnl'])} | +20K {item['m20_avg']:+.2f}%")
    if worst_signals:
        lines += ["────────────", "<b>弱势信号</b>"]
        for item in worst_signals[:3]:
            lines.append(f"{html.escape(item['signal'])}: {item['trades']}单 {_money(item['pnl'])} | +20K {item['m20_avg']:+.2f}%")
    if summary.get("best_trades") or summary.get("worst_trades"):
        lines += ["────────────", "<b>最好/最差订单</b>"]
        if summary.get("best_trades"):
            lines.append("最佳 " + _line_trade(summary["best_trades"][0]))
        if summary.get("worst_trades"):
            lines.append("最差 " + _line_trade(summary["worst_trades"][0]))
    winner_reviews = summary.get("winner_reviews") or []
    loser_reviews = summary.get("loser_reviews") or []
    if winner_reviews:
        winner = winner_reviews[0]
        lines += [
            "────────────",
            "<b>盈利单复盘</b>",
            _line_trade(winner),
            html.escape(str(winner.get("entry_verdict") or "")),
            html.escape(str(winner.get("exit_verdict") or "")),
        ]
    if loser_reviews:
        loser = loser_reviews[0]
        tags = " / ".join(loser.get("entry_tags") or []) or "无明显追价标签"
        lines += [
            "────────────",
            "<b>亏损单复盘</b>",
            _line_trade(loser),
            html.escape(str(loser.get("entry_verdict") or "")) + " | " + html.escape(tags),
            html.escape(str(loser.get("exit_verdict") or "")),
        ]
    issue_tags = summary.get("issue_tags") or []
    if issue_tags:
        lines += ["────────────", "<b>Loss Tags</b>"]
        for item in issue_tags[:4]:
            lines.append(
                f"{html.escape(str(item.get('tag', '')))}: "
                f"{item.get('trades', 0)}x / {_money(item.get('pnl'))}"
            )
    recs = summary.get("recommendations") or []
    if recs:
        lines += ["────────────", "<b>下一步建议</b>"]
        lines.extend(html.escape(str(r)) for r in recs[:4])
    return "\n".join(lines)


def latest_review_date() -> str:
    dates = _record_dates()
    return dates[-1].isoformat() if dates else datetime.now().date().isoformat()
