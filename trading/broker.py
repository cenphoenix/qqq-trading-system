"""Thin gateway around Longbridge quote and trade contexts."""

from __future__ import annotations

from typing import Any


class LongbridgeBroker:
    """Centralize SDK calls without changing their return types."""

    def __init__(self, quote_context: Any, trade_context: Any) -> None:
        self.quote_context = quote_context
        self.trade_context = trade_context

    def quote(self, symbols):
        return self.quote_context.quote(symbols)

    def positions(self):
        return self.trade_context.stock_positions()

    def account_balance(self):
        return self.trade_context.account_balance()

    def submit_order(self, **kwargs):
        return self.trade_context.submit_order(**kwargs)

    def today_orders(self, *args, **kwargs):
        return self.trade_context.today_orders(*args, **kwargs)

    def cancel_order(self, order_id):
        return self.trade_context.cancel_order(order_id)
