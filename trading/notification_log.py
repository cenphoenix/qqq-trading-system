"""Persistent notification deduplication for the live trader."""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any, Callable, Mapping


class NotificationLog:
    """Store sent-notification fingerprints in daily JSON files."""

    def __init__(
        self,
        app_dir: str | os.PathLike[str],
        timezone: tzinfo,
        json_default: Callable[[Any], Any] | None = None,
    ) -> None:
        self._log_dir = Path(app_dir) / "logs"
        self._timezone = timezone
        self._json_default = json_default
        self._lock = threading.RLock()

    @staticmethod
    def trade_key(trade: Mapping[str, Any], source: str = "exit") -> str:
        """Build a stable key without collapsing repeat trades in one contract."""
        try:
            symbol = str(trade.get("opt_symbol", ""))
            direction = str(trade.get("dir", ""))
            contracts = int(float(trade.get("closed_contracts") or trade.get("contracts") or 0))
            entry = round(float(trade.get("entry_opt_price") or trade.get("entry_price") or 0), 4)
            exit_price = round(float(trade.get("exit_opt_price") or trade.get("exit_price") or 0), 4)
            pnl = round(float(trade.get("pnl_usd") or 0), 2)
            reason = str(trade.get("exit_reason") or trade.get("reason") or "")[:80]
            return f"{source}|{symbol}|{direction}|{contracts}|{entry}|{exit_price}|{pnl}|{reason}"
        except Exception:
            return f"{source}|{time.time()}"

    def path(self, date_str: str | None = None) -> Path:
        date_str = date_str or datetime.now(self._timezone).strftime("%Y-%m-%d")
        self._log_dir.mkdir(parents=True, exist_ok=True)
        return self._log_dir / f"notifications_{date_str}.json"

    @staticmethod
    def _read_items(path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        try:
            with path.open(encoding="utf-8") as stream:
                data = json.load(stream)
            items = data.get("items", []) if isinstance(data, dict) else []
            return items if isinstance(items, list) else []
        except (OSError, json.JSONDecodeError):
            return []

    def load_keys(self, include_recent_days: bool = False) -> set[str]:
        paths = [self.path()]
        if include_recent_days and self._log_dir.is_dir():
            recent = sorted(self._log_dir.glob("notifications_*.json"), reverse=True)[:5]
            paths = list(dict.fromkeys(paths + recent))
        return {
            str(item["key"])
            for path in paths
            for item in self._read_items(path)
            if item.get("key")
        }

    def load_items(self) -> list[dict[str, Any]]:
        return self._read_items(self.path())

    def recent_live_exit_exists(self, opt_symbol: str, seconds: int = 300) -> bool:
        if not opt_symbol:
            return False
        now = datetime.now(self._timezone)
        for item in reversed(self.load_items()):
            if item.get("type") not in ("exit", "partial"):
                continue
            key = str(item.get("key", ""))
            title = str(item.get("title", ""))
            if title != opt_symbol and f"|{opt_symbol}|" not in key:
                continue
            try:
                sent_at = datetime.strptime(str(item.get("time", "")), "%Y-%m-%d %H:%M:%S")
                sent_at = sent_at.replace(tzinfo=self._timezone)
            except (TypeError, ValueError):
                return True
            if 0 <= (now - sent_at).total_seconds() <= seconds:
                return True
        return False

    def mark_sent(self, key: str, msg_type: str, title: str = "") -> bool:
        """Persist a fingerprint. Return False when it already exists."""
        with self._lock:
            path = self.path()
            items = self._read_items(path)
            if any(str(item.get("key")) == str(key) for item in items):
                return False
            items.append(
                {
                    "key": key,
                    "type": msg_type,
                    "title": title,
                    "time": datetime.now(self._timezone).strftime("%Y-%m-%d %H:%M:%S"),
                }
            )
            tmp_path = path.with_suffix(path.suffix + ".tmp")
            with tmp_path.open("w", encoding="utf-8") as stream:
                json.dump(
                    {"items": items},
                    stream,
                    ensure_ascii=False,
                    indent=2,
                    default=self._json_default,
                )
            os.replace(tmp_path, path)
            return True
