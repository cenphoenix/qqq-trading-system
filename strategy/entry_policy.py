"""Structured boundary around the live entry filters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class EntryDecision:
    allowed: bool
    reason: str = ""
    hard_rejection: bool = False


class EntryPolicy:
    def __init__(
        self,
        evaluator: Callable[[dict], bool],
        reason_provider: Callable[[], str],
        hard_rejection_provider: Callable[[], bool],
    ) -> None:
        self._evaluator = evaluator
        self._reason_provider = reason_provider
        self._hard_rejection_provider = hard_rejection_provider

    def evaluate(self, signal: dict) -> EntryDecision:
        skipped = bool(self._evaluator(signal))
        return EntryDecision(
            allowed=not skipped,
            reason=self._reason_provider() if skipped else "",
            hard_rejection=self._hard_rejection_provider() if skipped else False,
        )
