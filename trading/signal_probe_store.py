"""Durable storage and normalization for signal follow-up probes."""

from __future__ import annotations

import json
import os
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping


class SignalProbeStore:
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
    def serialize(probes: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        return [{
            "id": probe.get("id"),
            "entry_time": probe.get("entry_time", ""),
            "entry_bar": probe.get("entry_bar", 0),
            "signal": probe.get("signal", ""),
            "dir": probe.get("dir", ""),
            "entry_price": probe.get("entry_price", 0),
            "opt_symbol": probe.get("opt_symbol", ""),
            "contracts": probe.get("contracts", 0),
            "reason": probe.get("reason", ""),
            "regime": probe.get("regime", ""),
            "source": probe.get("source", "live"),
            "rejection_reason": probe.get("rejection_reason", ""),
            "m5_pct": probe.get("m5_pct"),
            "m10_pct": probe.get("m10_pct"),
            "m20_pct": probe.get("m20_pct"),
            "m5_price": probe.get("m5_price"),
            "m10_price": probe.get("m10_price"),
            "m20_price": probe.get("m20_price"),
            "completed": probe.get("completed", False),
            "milestones": probe.get("milestones", {}),
        } for probe in probes]

    def save(self, probes: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
        rows = self.serialize(probes)
        date_str = datetime.now(self._timezone).strftime("%Y-%m-%d")
        payload = {
            "date": date_str,
            "updated": datetime.now(self._timezone).strftime("%Y-%m-%d %H:%M:%S"),
            "probes": rows,
        }
        self._records_dir.mkdir(parents=True, exist_ok=True)
        path = self._records_dir / f"signal_probes_{date_str}.json"
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        with tmp_path.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, default=self._json_default)
        os.replace(tmp_path, path)
        return {"path": str(path), **payload}

    def load(self, date_str: str | None = None, default_entry_bar: int = 0) -> list[dict[str, Any]]:
        date_str = date_str or datetime.now(self._timezone).strftime("%Y-%m-%d")
        path = self._records_dir / f"signal_probes_{date_str}.json"
        if not path.exists():
            return []
        try:
            with path.open(encoding="utf-8") as stream:
                payload = json.load(stream)
        except (OSError, json.JSONDecodeError, TypeError):
            return []

        restored = []
        for probe in payload.get("probes", []):
            if not isinstance(probe, Mapping):
                continue
            milestones = {}
            for key, value in (probe.get("milestones") or {5: None, 10: None, 20: None}).items():
                try:
                    milestones[int(key)] = value
                except (TypeError, ValueError):
                    continue
            restored.append({
                "id": int(probe.get("id", len(restored) + 1)),
                "entry_time": probe.get("entry_time", ""),
                "entry_bar": int(probe.get("entry_bar", default_entry_bar)),
                "signal": probe.get("signal", ""),
                "dir": probe.get("dir", ""),
                "entry_price": float(probe.get("entry_price", 0) or 0),
                "opt_symbol": probe.get("opt_symbol", ""),
                "contracts": int(probe.get("contracts", 0) or 0),
                "reason": probe.get("reason", ""),
                "regime": probe.get("regime", ""),
                "source": probe.get("source", "live"),
                "rejection_reason": probe.get("rejection_reason", ""),
                "m5_pct": probe.get("m5_pct"),
                "m10_pct": probe.get("m10_pct"),
                "m20_pct": probe.get("m20_pct"),
                "m5_price": probe.get("m5_price"),
                "m10_price": probe.get("m10_price"),
                "m20_price": probe.get("m20_price"),
                "milestones": milestones or {5: None, 10: None, 20: None},
                "completed": bool(probe.get("completed", False)),
            })
        return restored
