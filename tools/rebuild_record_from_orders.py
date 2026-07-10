"""Rebuild a daily trade record from Longbridge filled option orders.

The Longbridge dashboard export may contain several completed cycles for the
same 0DTE contract.  This tool pairs fills in chronological order and writes
one record per buy-to-flat cycle instead of aggregating by option symbol.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_MULTIPLIER = 100


def _expiry_date(symbol: str) -> str:
    code = symbol.replace(".US", "")
    if len(code) < 9:
        return ""
    value = code[3:9]
    try:
        return datetime.strptime(value, "%y%m%d").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _filled_orders(orders: list[dict[str, Any]], target_date: str) -> list[dict[str, Any]]:
    filled = [
        order
        for order in orders
        if str(order.get("status", "")) == "Filled"
        and _expiry_date(str(order.get("symbol", ""))) == target_date
    ]
    if filled and all(order.get("submitted_at") for order in filled):
        return sorted(filled, key=lambda order: str(order.get("submitted_at")))
    # Older exports are newest-first and do not include a timestamp.
    return list(reversed(filled))


def _is_buy(order: dict[str, Any]) -> bool:
    side = str(order.get("side", ""))
    return side in {"buy", "Buy", "OrderSide.Buy", "\u4e70\u5165"}


def _build_cycles(orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    open_lots: dict[str, deque[dict[str, Any]]] = defaultdict(deque)
    cycles: list[dict[str, Any]] = []

    for order in orders:
        symbol = str(order.get("symbol", ""))
        quantity = float(order.get("executed_qty") or order.get("quantity") or 0)
        price = float(order.get("executed_price") or 0)
        if not symbol or quantity <= 0 or price <= 0:
            continue
        if _is_buy(order):
            open_lots[symbol].append({
                "symbol": symbol,
                "entry_price": price,
                "remaining": quantity,
                "contracts": quantity,
                "entry_order_id": str(order.get("order_id", "")),
                "entry_time": str(order.get("submitted_at", "")),
                "partial_exits": [],
            })
            continue

        remaining_sell = quantity
        while remaining_sell > 0 and open_lots[symbol]:
            lot = open_lots[symbol][0]
            matched = min(remaining_sell, float(lot["remaining"]))
            pnl = matched * (price - float(lot["entry_price"])) * CONTRACT_MULTIPLIER
            lot["partial_exits"].append({
                "contracts": int(matched),
                "exit_opt_price": price,
                "pnl_usd": round(pnl, 2),
                "order_id": str(order.get("order_id", "")),
                "time": str(order.get("submitted_at") or order.get("updated_at") or ""),
            })
            lot["remaining"] = float(lot["remaining"]) - matched
            remaining_sell -= matched
            if lot["remaining"] > 1e-9:
                continue

            total_contracts = int(lot["contracts"])
            total_pnl = sum(item["pnl_usd"] for item in lot["partial_exits"])
            average_exit = sum(
                item["contracts"] * item["exit_opt_price"] for item in lot["partial_exits"]
            ) / total_contracts
            cycles.append({
                "opt_symbol": symbol,
                "entry_price": round(float(lot["entry_price"]), 4),
                "exit_price": round(average_exit, 4),
                "contracts": total_contracts,
                "pnl_usd": round(total_pnl, 2),
                "pnl_pct": round(total_pnl / (total_contracts * CONTRACT_MULTIPLIER * float(lot["entry_price"])) * 100, 2),
                "entry_time": lot["entry_time"],
                "exit_time": str(order.get("submitted_at") or order.get("updated_at") or ""),
                "entry_order_id": lot["entry_order_id"],
                "partial_exits": lot["partial_exits"],
            })
            open_lots[symbol].popleft()
    return cycles


def _match_metadata(cycle: dict[str, Any], legacy: list[dict[str, Any]]) -> dict[str, Any]:
    candidates = [
        trade
        for trade in legacy
        if trade.get("opt_symbol") == cycle["opt_symbol"]
        and int(float(trade.get("contracts") or 0)) == cycle["contracts"]
        and abs(float(trade.get("entry_price") or 0) - cycle["entry_price"]) < 0.03
    ]
    if not candidates:
        return {}
    metadata = candidates.pop(0)
    legacy.remove(metadata)
    return metadata


def rebuild(target_date: str, record_path: Path, order_path: Path) -> dict[str, Any]:
    legacy = json.loads(record_path.read_text(encoding="utf-8")) if record_path.exists() else {}
    order_data = json.loads(order_path.read_text(encoding="utf-8"))
    cycles = _build_cycles(_filled_orders(order_data.get("orders", []), target_date))
    unmatched_legacy = [
        dict(trade) for trade in legacy.get("trades", []) if isinstance(trade, dict) and trade.get("_source") != "broker_reconcile"
    ]
    trades = []
    for cycle in cycles:
        source = _match_metadata(cycle, unmatched_legacy)
        trades.append({
            "date": target_date,
            "entry_time": source.get("entry_time") or cycle["entry_time"],
            "exit_time": source.get("exit_time") or cycle["exit_time"],
            "dir": source.get("dir") or ("call" if "C" in cycle["opt_symbol"].replace(".US", "")[9:] else "put"),
            "entry_price": cycle["entry_price"],
            "exit_price": cycle["exit_price"],
            "qty": cycle["contracts"] * CONTRACT_MULTIPLIER,
            "contracts": cycle["contracts"],
            "pnl_pct": cycle["pnl_pct"],
            "pnl_usd": cycle["pnl_usd"],
            "result": "win" if cycle["pnl_usd"] > 0 else "lose" if cycle["pnl_usd"] < 0 else "flat",
            "reason": source.get("reason") or "broker fill cycle rebuild",
            "exit_reason": source.get("exit_reason") or "broker fill cycle rebuild",
            "opt_symbol": cycle["opt_symbol"],
            "partial_exits": cycle["partial_exits"],
            "entry_order_id": cycle["entry_order_id"],
            "_source": "broker_cycle_rebuilt",
            **{key: source[key] for key in ("regime", "day_market_regime", "day_market_label", "day_market_direction", "atr_at_entry", "macd_hist_entry", "vwap_entry") if key in source},
        })

    total_pnl = round(sum(trade["pnl_usd"] for trade in trades), 2)
    result = {
        "date": target_date,
        "trades": trades,
        "total": len(trades),
        "wins": sum(1 for trade in trades if trade["result"] == "win"),
        "win_rate": round(sum(1 for trade in trades if trade["result"] == "win") / len(trades) * 100, 1) if trades else 0.0,
        "pnl": total_pnl,
        "accounting_schema": 2,
        "rebuild_source": "longbridge filled order cycles",
        "signal_probes": legacy.get("signal_probes", []),
    }
    backup = record_path.with_suffix(".json.pre_cycle_backup")
    if record_path.exists() and not backup.exists():
        backup.write_text(record_path.read_text(encoding="utf-8"), encoding="utf-8")
    temp = record_path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    temp.replace(record_path)
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("date", help="US trading date, YYYY-MM-DD")
    parser.add_argument("--record", type=Path, default=None)
    parser.add_argument("--orders", type=Path, default=ROOT / "longbridge_orders.json")
    args = parser.parse_args()
    record = args.record or ROOT / "records" / f"{args.date}.json"
    rebuilt = rebuild(args.date, record, args.orders)
    print(f"rebuilt {record}: {rebuilt['total']} cycles, pnl=${rebuilt['pnl']:+,.2f}")


if __name__ == "__main__":
    main()
