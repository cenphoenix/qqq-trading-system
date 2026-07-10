"""One-shot scheduling for daily, weekly, and monthly review notifications."""

from __future__ import annotations

import json
import os
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any, Callable


class ReviewSummaryScheduler:
    def __init__(
        self,
        app_dir: str | os.PathLike[str],
        timezone: tzinfo,
        notifier: Callable[..., bool],
        summary_builder: Callable[[str, str], dict[str, Any]],
        last_weekday_checker: Callable[[Any], bool],
        json_default: Callable[[Any], Any] | None = None,
    ) -> None:
        self._flags_path = Path(app_dir) / "records" / "review_summary_sent.json"
        self._timezone = timezone
        self._notifier = notifier
        self._summary_builder = summary_builder
        self._last_weekday_checker = last_weekday_checker
        self._json_default = json_default

    @property
    def flags_path(self) -> Path:
        return self._flags_path

    def load_flags(self) -> dict[str, Any]:
        try:
            if self._flags_path.exists():
                with self._flags_path.open(encoding="utf-8-sig") as stream:
                    data = json.load(stream)
                return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            pass
        return {}

    def save_flags(self, flags: dict[str, Any]) -> None:
        self._flags_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._flags_path.with_suffix(self._flags_path.suffix + ".tmp")
        with tmp_path.open("w", encoding="utf-8") as stream:
            json.dump(flags, stream, ensure_ascii=False, indent=2, default=self._json_default)
        os.replace(tmp_path, self._flags_path)

    def send_once(self, period: str, msg_type: str, now: datetime | None = None) -> bool:
        now = now or datetime.now(self._timezone)
        summary = self._summary_builder(period, now.strftime("%Y-%m-%d"))
        key = f"{period}:{summary.get('start_date')}:{summary.get('end_date')}"
        flags = self.load_flags()
        if flags.get(key):
            return False
        sent = self._notifier(f"📊 {summary.get('title', '复盘摘要')}", msg_type=msg_type)
        if sent:
            flags[key] = now.strftime("%Y-%m-%d %H:%M:%S")
            self.save_flags(flags)
        return bool(sent)

    def check(self, now: datetime | None = None) -> list[str]:
        """Send due summaries and return the periods successfully sent."""
        now = now or datetime.now(self._timezone)
        if not (16 <= now.hour < 17 and now.minute >= 5):
            return []
        sent = []
        if now.weekday() == 4 and self.send_once("week", "weekly_summary", now):
            sent.append("week")
        if now.weekday() < 5 and self._last_weekday_checker(now.date()):
            if self.send_once("month", "monthly_summary", now):
                sent.append("month")
        return sent
