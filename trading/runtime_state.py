"""Atomic runtime-state and position-checkpoint persistence."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Mapping


class RuntimeStateStore:
    def __init__(self, app_dir: str | os.PathLike[str], json_default: Callable[[Any], Any] | None = None) -> None:
        self.path = Path(app_dir) / "state.json"
        self._position_path = Path(app_dir) / "position_snapshot.json"
        self._json_default = json_default

    @staticmethod
    def _atomic_write(path: Path, payload: Any, json_default: Callable[[Any], Any] | None = None) -> None:
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        with tmp_path.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, default=json_default)
        for attempt in range(5):
            try:
                os.replace(tmp_path, path)
                return
            except OSError:
                if attempt == 4:
                    raise
                time.sleep(0.2)

    def save(self, state: Mapping[str, Any], position: Mapping[str, Any] | None, current_price: float) -> None:
        self._atomic_write(self.path, dict(state), self._json_default)
        if not position:
            self._position_path.unlink(missing_ok=True)
            return
        snapshot = [{
            "sym": position.get("opt_symbol", "QQQ"),
            "qty": position.get("contracts", 0),
            "cost": f"${float(position.get('entry_opt_price', 0) or 0):.2f}",
            "cur": f"${float(current_price or 0):.2f}",
            "pnl": f"${float(position.get('pnl_usd', 0) or 0):.2f}",
            "pct": f"{float(position.get('pnl_pct', 0) or 0):.1f}%",
        }]
        self._atomic_write(self._position_path, snapshot, self._json_default)

    def restore_checkpoint(
        self,
        position: Mapping[str, Any] | None,
        contract_multiplier: int,
    ) -> dict[str, Any] | None:
        if not position or not self.path.exists():
            return dict(position) if position else None
        restored = dict(position)
        try:
            with self.path.open(encoding="utf-8") as stream:
                checkpoint = (json.load(stream) or {}).get("position_checkpoint") or {}
        except (OSError, json.JSONDecodeError, TypeError):
            return restored
        if str(checkpoint.get("opt_symbol", "")) != str(restored.get("opt_symbol", "")):
            return restored
        broker_contracts = int(restored.get("contracts") or 0)
        broker_quantity = int(restored.get("quantity") or 0)
        restored.update(checkpoint)
        restored["contracts"] = broker_contracts
        restored["quantity"] = broker_quantity or broker_contracts * contract_multiplier
        restored["order_status"] = "restored"
        restored.setdefault("realized_pnl_usd", 0.0)
        restored.setdefault("partial_exits", [])
        return restored
