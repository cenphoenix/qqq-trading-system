"""Normalized access to Longbridge positions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class BrokerPosition:
    symbol: str
    quantity: int
    available: int
    cost_price: float
    channel: str
    raw: Any

    @property
    def option_direction(self) -> str:
        code = self.symbol.replace(".US", "")
        option_type = code[9:10] if len(code) > 9 else ""
        return "call" if option_type == "C" else "put" if option_type == "P" else ""

    @property
    def is_option(self) -> bool:
        return bool(self.option_direction)


class PositionBook:
    def __init__(self, broker) -> None:
        self.broker = broker

    def load(self) -> list[BrokerPosition]:
        response = self.broker.positions()
        rows = []
        for channel in getattr(response, "channels", []) or []:
            channel_name = str(
                getattr(channel, "name", "")
                or getattr(channel, "account_channel", "")
                or getattr(channel, "channel", "")
                or ""
            )
            for position in getattr(channel, "positions", []) or []:
                quantity = int(float(getattr(position, "quantity", 0) or 0))
                raw_available = getattr(position, "available_quantity", None)
                available = quantity if raw_available is None else int(float(raw_available or 0))
                rows.append(BrokerPosition(
                    symbol=str(getattr(position, "symbol", "") or ""),
                    quantity=quantity,
                    available=available,
                    cost_price=float(getattr(position, "cost_price", 0) or 0),
                    channel=channel_name,
                    raw=position,
                ))
        return rows

    def find(self, symbol: str) -> BrokerPosition | None:
        return next((position for position in self.load() if position.symbol == symbol), None)

    def total_quantity(self, predicate: Callable[[BrokerPosition], bool]) -> int:
        return sum(position.quantity for position in self.load() if position.quantity > 0 and predicate(position))
