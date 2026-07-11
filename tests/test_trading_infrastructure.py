import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from trading import AccountSnapshotService, LongbridgeBroker, NotificationLog, NotificationService, OrderExecution, PositionBook, ReviewSummaryScheduler, TradeLedger


TZ_ET = ZoneInfo("America/New_York")


class AccountSnapshotServiceTests(unittest.TestCase):
    def test_cash_and_buying_power_are_not_swapped_or_double_counted(self):
        usd = type("Balance", (), {
            "currency": "USD", "net_assets": 10000,
            "total_cash": 2000, "buy_power": 5000,
        })()
        snapshot = AccountSnapshotService.normalize([usd])
        self.assertEqual(snapshot["net_assets"], 10000)
        self.assertEqual(snapshot["cash"], 2000)
        self.assertEqual(snapshot["buying_power"], 5000)

    def test_hkd_is_converted_to_usd(self):
        hkd = type("Balance", (), {
            "currency": "HKD", "net_assets": 7800,
            "total_cash": 780, "buy_power": 1560,
        })()
        snapshot = AccountSnapshotService.normalize([hkd])
        self.assertEqual(snapshot["net_assets"], 1000)
        self.assertEqual(snapshot["cash"], 100)
        self.assertEqual(snapshot["buying_power"], 200)


class BrokerGatewayTests(unittest.TestCase):
    def test_gateway_delegates_without_altering_results(self):
        class QuoteContext:
            def quote(self, symbols):
                return ["quote", symbols]

        class TradeContext:
            def stock_positions(self):
                return "positions"

            def account_balance(self):
                return "balance"

            def submit_order(self, **kwargs):
                return kwargs

            def today_orders(self, *args, **kwargs):
                return args, kwargs

            def cancel_order(self, order_id):
                return f"cancel:{order_id}"

        broker = LongbridgeBroker(QuoteContext(), TradeContext())
        self.assertEqual(broker.quote(["QQQ.US"]), ["quote", ["QQQ.US"]])
        self.assertEqual(broker.positions(), "positions")
        self.assertEqual(broker.account_balance(), "balance")
        self.assertEqual(broker.submit_order(symbol="QQQ.US"), {"symbol": "QQQ.US"})
        self.assertEqual(broker.today_orders(order_id="1"), ((), {"order_id": "1"}))
        self.assertEqual(broker.cancel_order("1"), "cancel:1")


class OrderExecutionTests(unittest.TestCase):
    def test_lookup_falls_back_and_normalizes_fill_price(self):
        class Order:
            order_id = "42"
            status = "OrderStatus.Filled"
            executed_quantity = 2
            executed_price = 0
            last_done = 1.25

        class Broker:
            def today_orders(self, *args, **kwargs):
                if kwargs:
                    raise RuntimeError("filtered lookup unsupported")
                return [Order()]

        execution = OrderExecution(Broker())
        snapshot = execution.snapshot(execution.find_order("42"))
        self.assertEqual(snapshot.quantity, 2)
        self.assertEqual(snapshot.price, 1.25)
        self.assertFalse(snapshot.rejected)

    def test_poll_preserves_retry_count_and_missing_orders(self):
        sleeps = []

        class Broker:
            calls = 0

            def today_orders(self, *args, **kwargs):
                self.calls += 1
                return []

        execution = OrderExecution(Broker(), sleep_fn=sleeps.append)
        results = list(execution.poll("missing", retries=3, interval=2))
        self.assertEqual(results, [(0, None), (1, None), (2, None)])
        self.assertEqual(sleeps, [2, 2, 2])


class PositionBookTests(unittest.TestCase):
    def test_positions_are_normalized_and_searchable(self):
        class RawPosition:
            symbol = "QQQ260710C720000.US"
            quantity = "3"
            available_quantity = "2"
            cost_price = "1.25"

        class Channel:
            account_channel = "lb"
            positions = [RawPosition()]

        class Response:
            channels = [Channel()]

        class Broker:
            def positions(self):
                return Response()

        book = PositionBook(Broker())
        position = book.find("QQQ260710C720000.US")
        self.assertEqual(position.quantity, 3)
        self.assertEqual(position.available, 2)
        self.assertEqual(position.cost_price, 1.25)
        self.assertEqual(position.option_direction, "call")
        self.assertTrue(position.is_option)
        self.assertEqual(book.total_quantity(lambda row: "QQQ" in row.symbol), 3)

    def test_zero_available_quantity_is_not_replaced_by_total(self):
        class RawPosition:
            symbol = "QQQ260710P720000.US"
            quantity = 4
            available_quantity = 0
            cost_price = 1

        class Broker:
            def positions(self):
                return type("Response", (), {"channels": [type("Channel", (), {"positions": [RawPosition()]})()]})()

        self.assertEqual(PositionBook(Broker()).load()[0].available, 0)

    def test_put_direction_uses_option_type_field(self):
        class RawPosition:
            symbol = "QQQ260710P720000.US"
            quantity = 1
            cost_price = 1

        class Broker:
            def positions(self):
                return type("Response", (), {"channels": [type("Channel", (), {"positions": [RawPosition()]})()]})()

        position = PositionBook(Broker()).load()[0]
        self.assertEqual(position.option_direction, "put")
        self.assertTrue(position.is_option)


class NotificationLogTests(unittest.TestCase):
    def test_trade_key_distinguishes_repeat_contract_exits(self):
        base = {
            "opt_symbol": "QQQ260710C500000.US",
            "dir": "call",
            "contracts": 2,
            "entry_opt_price": 1.0,
            "exit_opt_price": 1.3,
            "pnl_usd": 60,
            "exit_reason": "take profit",
        }
        changed = dict(base, exit_opt_price=1.4, pnl_usd=80)
        self.assertNotEqual(NotificationLog.trade_key(base), NotificationLog.trade_key(changed))

    def test_mark_sent_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            log = NotificationLog(temp_dir, TZ_ET)
            self.assertTrue(log.mark_sent("exit|one", "exit", "QQQ option"))
            self.assertFalse(log.mark_sent("exit|one", "exit", "QQQ option"))
            self.assertEqual(log.load_keys(), {"exit|one"})
            self.assertEqual(len(log.load_items()), 1)


class TradeLedgerTests(unittest.TestCase):
    def test_snapshot_only_contains_closed_real_trades(self):
        trades = [
            {
                "entry_time": datetime(2026, 7, 10, 10, 0, tzinfo=TZ_ET),
                "exit_time": datetime(2026, 7, 10, 10, 5, tzinfo=TZ_ET),
                "dir": "put",
                "entry_opt_price": 1.2,
                "exit_opt_price": 1.5,
                "contracts": 2,
                "pnl_pct": 25,
                "pnl_usd": 60,
                "win": True,
                "opt_symbol": "QQQ260710P500000.US",
            },
            {"exit_time": None, "contracts": 1, "opt_symbol": "OPEN.US"},
            {"exit_time": datetime.now(TZ_ET), "contracts": 0, "opt_symbol": "SHADOW.US"},
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            result = TradeLedger(temp_dir, TZ_ET).save_live_snapshot(trades, [{"id": 1}])
            self.assertIsNotNone(result)
            self.assertEqual(result["total"], 1)
            self.assertEqual(result["wins"], 1)
            self.assertEqual(result["pnl"], 60)
            payload = json.loads(Path(result["path"]).read_text(encoding="utf-8"))
            self.assertEqual(payload["trades"][0]["entry_time"], "10:00:00")
            self.assertEqual(payload["trades"][0]["_source"], "live")
            self.assertEqual(payload["signal_probes"], [{"id": 1}])

    def test_broker_orders_are_fifo_reconciled(self):
        orders = {
            "orders": [
                {"status": "Filled", "symbol": "QQQ260710P720000.US", "side": "买入", "executed_qty": 2, "executed_price": 1.0},
                {"status": "Filled", "symbol": "QQQ260710P720000.US", "side": "买入", "executed_qty": 1, "executed_price": 2.0},
                {"status": "Filled", "symbol": "QQQ260710P720000.US", "side": "卖出", "executed_qty": 2, "executed_price": 1.5},
            ]
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "orders.json"
            path.write_text(json.dumps(orders, ensure_ascii=False), encoding="utf-8")
            rows = TradeLedger(temp_dir, TZ_ET).reconcile_broker_orders(path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["dir"], "put")
        self.assertEqual(rows[0]["contracts"], 2)
        self.assertEqual(rows[0]["pnl_usd"], 100)


class NotificationServiceTests(unittest.TestCase):
    def test_disabled_transports_return_false(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = NotificationService(
                temp_dir,
                TZ_ET,
                lambda: {"telegram": {"enabled": False}, "feishu": {"enabled": False}},
                lambda message, msg_type, **kwargs: message,
            )
            self.assertFalse(service.notify("test"))

    def test_repeated_error_is_throttled(self):
        sent = []
        with tempfile.TemporaryDirectory() as temp_dir:
            service = NotificationService(
                temp_dir,
                TZ_ET,
                lambda: {},
                lambda message, msg_type, **kwargs: message,
            )
            service.notify = lambda message, msg_type="info", **kwargs: sent.append((msg_type, kwargs)) or True
            self.assertTrue(service.handle_error(TimeoutError("network timeout"), "quotes"))
            self.assertFalse(service.handle_error(TimeoutError("network timeout"), "quotes"))
            self.assertEqual(len(sent), 1)
            self.assertEqual(sent[0][0], "network")


class ReviewSummarySchedulerTests(unittest.TestCase):
    def test_friday_close_sends_weekly_summary_once(self):
        notifications = []

        def notify(message, msg_type="info", **kwargs):
            notifications.append((message, msg_type))
            return True

        def build(period, date_str):
            return {
                "title": f"{period} review",
                "start_date": "2026-07-06",
                "end_date": "2026-07-10",
            }

        with tempfile.TemporaryDirectory() as temp_dir:
            scheduler = ReviewSummaryScheduler(
                temp_dir, TZ_ET, notify, build, lambda date: False,
            )
            friday_close = datetime(2026, 7, 10, 16, 5, tzinfo=TZ_ET)
            self.assertEqual(scheduler.check(friday_close), ["week"])
            self.assertEqual(scheduler.check(friday_close), [])
            self.assertEqual(len(notifications), 1)

    def test_last_weekday_can_send_week_and_month(self):
        notifications = []

        def notify(message, msg_type="info", **kwargs):
            notifications.append(msg_type)
            return True

        def build(period, date_str):
            return {"title": period, "start_date": period, "end_date": date_str}

        with tempfile.TemporaryDirectory() as temp_dir:
            scheduler = ReviewSummaryScheduler(
                temp_dir, TZ_ET, notify, build, lambda date: True,
            )
            friday_close = datetime(2026, 7, 31, 16, 10, tzinfo=TZ_ET)
            self.assertEqual(scheduler.check(friday_close), ["week", "month"])
            self.assertEqual(notifications, ["weekly_summary", "monthly_summary"])


if __name__ == "__main__":
    unittest.main()
