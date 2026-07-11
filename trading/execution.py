"""Shared order lookup and fill normalization."""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any


@dataclass(frozen=True)
class ExecutionSnapshot:
    order: Any
    status: Any
    quantity: float
    price: float

    @property
    def rejected(self) -> bool:
        return str(self.status) == "OrderStatus.Rejected"


@dataclass(frozen=True)
class FillResult:
    order_id: str
    requested_quantity: int
    executed_quantity: int
    executed_price: float
    status: Any
    rejected: bool = False
    canceled: bool = False

    @property
    def filled(self) -> bool:
        return self.executed_quantity > 0

    @property
    def complete(self) -> bool:
        return self.executed_quantity >= self.requested_quantity


class OrderExecution:
    def __init__(self, broker, sleep_fn=time.sleep) -> None:
        self.broker = broker
        self._sleep = sleep_fn

    def find_order(self, order_id) -> Any | None:
        """Find an order through the filtered API, then the full-day list."""
        try:
            orders = self.broker.today_orders(order_id=order_id)
            if orders:
                for order in orders:
                    if str(getattr(order, "order_id", "")) == str(order_id):
                        return order
                return orders[0]
        except Exception:
            pass
        try:
            for order in self.broker.today_orders() or []:
                if str(getattr(order, "order_id", "")) == str(order_id):
                    return order
        except Exception:
            pass
        return None

    @staticmethod
    def snapshot(order: Any) -> ExecutionSnapshot:
        quantity = float(getattr(order, "executed_quantity", 0) or 0)
        price = float(getattr(order, "executed_price", 0) or 0)
        if quantity > 0 and price <= 0:
            price = float(getattr(order, "last_done", 0) or getattr(order, "price", 0) or 0)
        return ExecutionSnapshot(order, getattr(order, "status", None), quantity, price)

    def poll(self, order_id, retries: int = 5, interval: float = 3):
        """Yield one normalized snapshot per polling attempt."""
        for attempt in range(retries):
            self._sleep(interval)
            order = self.find_order(order_id)
            yield attempt, self.snapshot(order) if order is not None else None

    def submit_and_wait(
        self,
        submit_kwargs: dict[str, Any],
        requested_quantity: int,
        retries: int = 5,
        interval: float = 3,
        cancel_remainder: bool = True,
    ) -> FillResult:
        response = self.broker.submit_order(**submit_kwargs)
        order_id = str(response.order_id)
        latest = None
        for _, snapshot in self.poll(order_id, retries=retries, interval=interval):
            if snapshot is None:
                continue
            latest = snapshot
            if snapshot.rejected:
                return FillResult(
                    order_id, requested_quantity, int(snapshot.quantity),
                    snapshot.price, snapshot.status, rejected=True,
                )
            if snapshot.quantity >= requested_quantity:
                return FillResult(
                    order_id, requested_quantity, requested_quantity,
                    snapshot.price, snapshot.status,
                )
        quantity = min(requested_quantity, int(latest.quantity)) if latest else 0
        price = latest.price if latest else 0.0
        status = latest.status if latest else None
        canceled = False
        if cancel_remainder and quantity < requested_quantity:
            try:
                self.broker.cancel_order(order_id)
                canceled = True
            except Exception:
                canceled = False
        return FillResult(
            order_id, requested_quantity, quantity, price, status,
            rejected=False, canceled=canceled,
        )
