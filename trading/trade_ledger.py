"""Serialization and durable storage for closed live trades."""

from __future__ import annotations

import json
import os
import time
from collections import Counter, defaultdict
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping


class TradeLedger:
    def __init__(
        self,
        app_dir: str | os.PathLike[str],
        timezone: tzinfo,
        json_default: Callable[[Any], Any] | None = None,
    ) -> None:
        self._records_dir = Path(app_dir) / "records"
        self._timezone = timezone
        self._json_default = json_default

    @staticmethod
    def _format_time(value: Any) -> str:
        return value.strftime("%H:%M:%S") if isinstance(value, datetime) else str(value)

    def serialize_closed_trades(self, source: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        trades = []
        for trade in source:
            if trade.get("exit_time") is None:
                continue
            if not trade.get("opt_symbol") or int(trade.get("contracts") or 0) <= 0:
                continue
            trades.append(
                {
                    "entry_time": self._format_time(trade.get("entry_time")),
                    "exit_time": self._format_time(trade.get("exit_time")),
                    "dir": trade.get("dir", ""),
                    "entry_price": trade.get("entry_opt_price") or trade.get("entry_price", 0),
                    "exit_price": trade.get("exit_opt_price") or trade.get("exit_price", 0),
                    "contracts": trade.get("cycle_contracts") or trade.get("original_contracts") or trade.get("contracts", 0),
                    "pnl_pct": round(trade.get("pnl_pct", 0), 2),
                    "pnl_usd": round(trade.get("pnl_usd", 0), 2),
                    "result": "win" if trade.get("win") else ("lose" if trade.get("win") is False else ""),
                    "reason": trade.get("reason", ""),
                    "exit_reason": trade.get("exit_reason", ""),
                    "opt_symbol": trade.get("opt_symbol", ""),
                    "regime": trade.get("regime", "neutral"),
                    "atr_at_entry": trade.get("atr_at_entry", 0),
                    "macd_hist_entry": trade.get("macd_hist_entry", 0),
                    "vwap_entry": trade.get("vwap_entry", 0),
                    "sma20_entry": trade.get("sma20_entry", 0),
                    "final_exit_pnl_pct": round(trade.get("final_exit_pnl_pct", 0), 2),
                    "final_exit_pnl_usd": round(trade.get("final_exit_pnl_usd", 0), 2),
                    "partial_exits": list(trade.get("partial_exits") or []),
                    "half_closed": trade.get("half_closed", False),
                    "_source": "live",
                }
            )
        return trades

    def save_live_snapshot(
        self,
        source: Iterable[Mapping[str, Any]],
        signal_probes: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        trades = self.serialize_closed_trades(source)
        if not trades:
            return None
        date_str = datetime.now(self._timezone).strftime("%Y-%m-%d")
        wins = sum(1 for trade in trades if trade["result"] == "win")
        total_pnl = sum(trade["pnl_usd"] for trade in trades)
        payload = {
            "date": date_str,
            "trades": trades,
            "total": len(trades),
            "wins": wins,
            "win_rate": round(wins / len(trades) * 100, 1),
            "pnl": round(total_pnl, 2),
            "signal_probes": signal_probes,
            "updated": datetime.now(self._timezone).strftime("%Y-%m-%d %H:%M:%S"),
        }
        self._records_dir.mkdir(parents=True, exist_ok=True)
        path = self._records_dir / f"{date_str}.json"
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        with tmp_path.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, default=self._json_default)
        for attempt in range(5):
            try:
                os.replace(tmp_path, path)
                break
            except OSError:
                if attempt == 4:
                    raise
                time.sleep(0.2)
        return {"path": str(path), **payload}

    def reconcile_broker_orders(self, orders_file: str | os.PathLike[str]) -> list[dict[str, Any]]:
        """Build closed option cycles from filled broker orders using FIFO matching."""
        path = Path(orders_file)
        if not path.exists():
            return []
        try:
            with path.open(encoding="utf-8") as stream:
                data = json.load(stream)
        except (OSError, json.JSONDecodeError):
            return []
        filled = [order for order in data.get("orders", []) if order.get("status") == "Filled"]
        grouped: dict[str, dict[str, list[dict[str, float]]]] = defaultdict(lambda: {"buys": [], "sells": []})
        for order in filled:
            symbol = str(order.get("symbol") or "")
            quantity = float(order.get("executed_qty", 0) or order.get("quantity", 0) or 0)
            price = float(order.get("executed_price", 0) or 0)
            if not symbol or quantity <= 0 or price <= 0:
                continue
            target = "buys" if order.get("side") == "买入" else "sells" if order.get("side") == "卖出" else ""
            if target:
                grouped[symbol][target].append({"qty": quantity, "price": price})

        today = datetime.now(self._timezone).strftime("%Y-%m-%d")
        reconciled = []
        for symbol in sorted(grouped):
            buys = [dict(item) for item in grouped[symbol]["buys"]]
            sells = [dict(item) for item in grouped[symbol]["sells"]]
            buy_count, sell_count = len(buys), len(sells)
            total_buy = sum(item["qty"] for item in buys)
            total_sell = sum(item["qty"] for item in sells)
            if not buys or not sells or total_buy <= 0 or total_sell <= 0:
                continue
            avg_buy = sum(item["qty"] * item["price"] for item in buys) / total_buy
            avg_sell = sum(item["qty"] * item["price"] for item in sells) / total_sell
            unmatched = buys
            pnl = matched = 0.0
            for sell in sells:
                remaining = sell["qty"]
                while remaining > 0 and unmatched:
                    buy = unmatched[0]
                    quantity = min(remaining, buy["qty"])
                    pnl += quantity * (sell["price"] - buy["price"]) * 100
                    matched += quantity
                    buy["qty"] -= quantity
                    remaining -= quantity
                    if buy["qty"] <= 0:
                        unmatched.pop(0)
            if matched <= 0:
                continue
            code = symbol.replace(".US", "")
            try:
                date_part = code[3:9]
                trade_date = f"{2000 + int(date_part[:2])}-{int(date_part[2:4]):02d}-{int(date_part[4:6]):02d}"
                option_part = code[9:]
                direction = "call" if option_part[0] == "C" else "put"
            except (ValueError, IndexError):
                trade_date = today
                direction = ""
            contracts = int(matched)
            pnl_pct = (avg_sell - avg_buy) / avg_buy * 100 if avg_buy else 0.0
            reconciled.append({
                "date": trade_date,
                "entry_time": today,
                "exit_time": today,
                "dir": direction,
                "entry_price": round(avg_buy, 2),
                "exit_price": avg_sell,
                "qty": contracts * 100,
                "contracts": contracts,
                "pnl_pct": round(pnl_pct, 2),
                "pnl_usd": round(pnl, 2),
                "result": "win" if pnl > 0 else "lose" if pnl < 0 else "",
                "reason": f"broker对账({buy_count}买/{sell_count}卖,配对{contracts}张)",
                "exit_reason": "broker对账",
                "opt_symbol": symbol,
                "entry_opt_price": round(avg_buy, 2),
                "_source": "broker_reconcile",
            })
        return reconciled

    @staticmethod
    def serialize_broker_orders(orders: Iterable[Any]) -> list[dict[str, Any]]:
        rows = []
        for order in orders:
            try:
                side = "买入" if str(getattr(order, "side", "")) == "OrderSide.Buy" else "卖出"
                rows.append({
                    "order_id": str(getattr(order, "order_id", "") or ""),
                    "symbol": str(getattr(order, "symbol", "") or ""),
                    "side": side,
                    "quantity": int(float(getattr(order, "quantity", 0) or 0)),
                    "executed_qty": float(getattr(order, "executed_quantity", 0) or 0),
                    "executed_price": float(getattr(order, "executed_price", 0) or 0),
                    "status": str(getattr(order, "status", "")).replace("OrderStatus.", ""),
                    "submitted_at": str(getattr(order, "submitted_at", "") or ""),
                    "updated_at": str(getattr(order, "updated_at", "") or ""),
                })
            except (TypeError, ValueError):
                continue
        return rows

    def save_broker_orders(self, orders: Iterable[Any]) -> dict[str, Any]:
        rows = self.serialize_broker_orders(orders)
        payload = {
            "orders": rows,
            "total": len(rows),
            "buy_count": sum(row["side"] == "买入" for row in rows),
            "sell_count": sum(row["side"] == "卖出" for row in rows),
            "updated": datetime.now(self._timezone).strftime("%Y-%m-%d %H:%M:%S"),
        }
        path = self._records_dir.parent / "longbridge_orders.json"
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        with tmp_path.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, default=self._json_default)
        for attempt in range(5):
            try:
                os.replace(tmp_path, path)
                break
            except OSError:
                if attempt == 4:
                    raise
                time.sleep(0.2)
        return {"path": str(path), **payload}

    def _serialize_internal_trade(self, trade: Mapping[str, Any], multiplier: int) -> dict[str, Any] | None:
        symbol = str(trade.get("opt_symbol", "") or "")
        contracts = trade.get("cycle_contracts") or trade.get("original_contracts") or trade.get("contracts", 0)
        if not symbol or int(contracts or 0) <= 0:
            return None
        pnl_pct = trade.get("pnl_pct", trade.get("max_pnl_pct", 0))
        return {
            "date": datetime.now(self._timezone).strftime("%Y-%m-%d"),
            "entry_time": self._format_time(trade.get("entry_time"))[:8],
            "exit_time": self._format_time(trade.get("exit_time"))[:8],
            "dir": trade.get("dir", ""),
            "entry_price": trade.get("entry_opt_price") or trade.get("entry_price", 0),
            "exit_price": trade.get("exit_opt_price", 0),
            "qty": int(contracts) * multiplier,
            "contracts": contracts,
            "pnl_pct": round(float(pnl_pct or 0), 2),
            "pnl_usd": round(float(trade.get("pnl_usd", 0) or 0), 2),
            "final_exit_pnl_pct": round(float(trade.get("final_exit_pnl_pct", 0) or 0), 2),
            "final_exit_pnl_usd": round(float(trade.get("final_exit_pnl_usd", 0) or 0), 2),
            "partial_exits": list(trade.get("partial_exits") or []),
            "result": "win" if trade.get("win") else "lose" if trade.get("win") is False else "",
            "reason": trade.get("reason", ""),
            "exit_reason": trade.get("exit_reason", ""),
            "opt_symbol": symbol,
            "regime": trade.get("regime", "neutral"),
            "day_market_regime": trade.get("day_market_regime", ""),
            "day_market_label": trade.get("day_market_label", ""),
            "day_market_direction": trade.get("day_market_direction", ""),
            "atr_at_entry": trade.get("atr_at_entry", 0),
            "macd_hist_entry": trade.get("macd_hist_entry", 0),
            "vwap_entry": trade.get("vwap_entry", 0),
            "_source": "internal",
        }

    def save_daily_record(
        self,
        internal_trades: Iterable[Mapping[str, Any]],
        broker_trades: Iterable[Mapping[str, Any]],
        multiplier: int,
        signal_probes: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        broker_rows = [dict(trade) for trade in broker_trades]
        rows = []
        seen = set()
        for trade in internal_trades:
            key = str(trade.get("order_id") or f"{trade.get('opt_symbol', '')}_{trade.get('entry_time', '')}")
            if key in seen:
                continue
            serialized = self._serialize_internal_trade(trade, multiplier)
            if serialized:
                rows.append(serialized)
                seen.add(key)
        internal_symbols = {row["opt_symbol"] for row in rows}
        for broker_trade in broker_rows:
            symbol = str(broker_trade.get("opt_symbol", ""))
            if symbol and symbol not in internal_symbols:
                recovered = dict(broker_trade)
                recovered["_source"] = "broker_unmatched"
                recovered["reason"] = f"broker_unmatched: {broker_trade.get('reason', '')}"
                rows.append(recovered)
        if not rows:
            return None

        dates = [str(row.get("date", "")) for row in rows if row.get("date")]
        date_str = Counter(dates).most_common(1)[0][0] if dates else datetime.now(self._timezone).strftime("%Y-%m-%d")
        broker_pnl = sum(float(row.get("pnl_usd", 0) or 0) for row in broker_rows)
        internal_pnl = sum(float(row.get("pnl_usd", 0) or 0) for row in rows)
        total_pnl = broker_pnl if broker_rows else internal_pnl
        payload = {
            "date": date_str,
            "trades": rows,
            "total": len(rows),
            "wins": sum(row.get("result") == "win" for row in rows),
            "pnl": round(total_pnl, 2),
            "internal_pnl": round(internal_pnl, 2),
            "broker_reconciliation": broker_rows,
            "reconciliation_delta": round(total_pnl - internal_pnl, 2),
            "signal_probes": signal_probes,
        }
        self._records_dir.mkdir(parents=True, exist_ok=True)
        path = self._records_dir / f"{date_str}.json"
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        with tmp_path.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, default=self._json_default)
        for attempt in range(5):
            try:
                os.replace(tmp_path, path)
                break
            except OSError:
                if attempt == 4:
                    raise
                time.sleep(0.2)
        return {
            "path": str(path),
            "broker_count": len(broker_rows),
            "internal_count": sum(row.get("_source") == "internal" for row in rows),
            **payload,
        }
