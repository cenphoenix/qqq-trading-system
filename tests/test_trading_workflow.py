import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from strategy import is_option_expiring_on
from strategy.entry_policy import EntryPolicy
from trading import OrderExecution, PositionSizer, TradeLedger


TZ_ET = ZoneInfo("America/New_York")


class FakeOrder:
    def __init__(self, order_id, quantity, price, status="OrderStatus.Filled"):
        self.order_id = order_id
        self.executed_quantity = quantity
        self.executed_price = price
        self.status = status


class FakeBroker:
    def __init__(self, fills):
        self._fills = list(fills)
        self._orders = {}
        self.canceled = []

    def submit_order(self, **kwargs):
        order_id = str(len(self._orders) + 1)
        quantity, price, status = self._fills.pop(0)
        self._orders[order_id] = FakeOrder(order_id, quantity, price, status)
        return type("Response", (), {"order_id": order_id})()

    def today_orders(self, order_id=None):
        if order_id is not None:
            order = self._orders.get(str(order_id))
            return [order] if order else []
        return list(self._orders.values())

    def cancel_order(self, order_id):
        self.canceled.append(str(order_id))


class SimulatedTradingWorkflowTests(unittest.TestCase):
    def test_option_expiry_parser_rejects_invalid_symbols(self):
        self.assertTrue(is_option_expiring_on("QQQ260710C500000.US", date(2026, 7, 10)))
        self.assertFalse(is_option_expiring_on("QQQ-invalid", date(2026, 7, 10)))

    def test_signal_to_partial_exit_and_daily_record(self):
        decision = EntryPolicy(lambda signal: False, lambda: "", lambda: False).evaluate({
            "dir": "call", "engine": "VWAP_Breakout",
        })
        self.assertTrue(decision.allowed)

        config = {
            "order_pct": 20, "put_order_pct": 8, "contract_multiplier": 100,
            "max_contracts_per_trade": 10,
        }
        size = PositionSizer.calculate(2000, 2, "call", 1, config)
        self.assertEqual(size.contracts, 2)

        broker = FakeBroker([
            (2, 2.00, "OrderStatus.Filled"),
            (1, 2.60, "OrderStatus.PartialFilled"),
            (1, 3.00, "OrderStatus.Filled"),
        ])
        execution = OrderExecution(broker, sleep_fn=lambda _: None)
        opened = execution.submit_and_wait({"symbol": "QQQ260710C500000.US"}, 2, retries=1, interval=0)
        partial = execution.submit_and_wait({"symbol": "QQQ260710C500000.US"}, 2, retries=1, interval=0)
        final = execution.submit_and_wait({"symbol": "QQQ260710C500000.US"}, 1, retries=1, interval=0)
        self.assertTrue(opened.complete)
        self.assertEqual(partial.executed_quantity, 1)
        self.assertEqual(broker.canceled, ["2"])
        self.assertTrue(final.complete)

        partial_pnl = (partial.executed_price - opened.executed_price) * 100
        final_pnl = (final.executed_price - opened.executed_price) * 100
        trade = {
            "order_id": opened.order_id,
            "entry_time": datetime(2026, 7, 10, 10, 0, tzinfo=TZ_ET),
            "exit_time": datetime(2026, 7, 10, 10, 20, tzinfo=TZ_ET),
            "dir": "call",
            "opt_symbol": "QQQ260710C500000.US",
            "original_contracts": 2,
            "entry_opt_price": opened.executed_price,
            "exit_opt_price": final.executed_price,
            "pnl_usd": partial_pnl + final_pnl,
            "pnl_pct": (partial_pnl + final_pnl) / 400 * 100,
            "win": True,
            "reason": "VWAP_Breakout",
            "partial_exits": [{
                "contracts": 1, "exit_opt_price": partial.executed_price,
                "pnl_usd": partial_pnl,
            }],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = TradeLedger(temp_dir, TZ_ET)
            saved = ledger.save_daily_record([trade], [], 100, [{"signal": "VWAP_Breakout"}])
            payload = json.loads(Path(saved["path"]).read_text(encoding="utf-8"))
            restored = ledger.load_daily_record(saved["date"])
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["trades"][0]["partial_exits"][0]["contracts"], 1)
        self.assertEqual(payload["pnl"], 160)
        self.assertEqual(restored["pnl"], 160)


if __name__ == "__main__":
    unittest.main()
