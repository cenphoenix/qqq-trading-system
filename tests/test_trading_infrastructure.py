import json
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from trading import AccountSnapshotService, LifecycleController, LifecycleState, LongbridgeBroker, NotificationLog, NotificationService, OrderAuditLog, OrderExecution, OrderStateStore, PositionBook, PositionRiskPolicy, PositionSizer, QuoteQualityPolicy, ReviewSummaryScheduler, RuntimeHealth, RuntimeStateStore, SignalProbeStore, TradeLedger, TradingSessionPolicy, redact_config, resolve_secret, validate_config
from strategy.entry_policy import EntryPolicy


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

            def depth(self, symbol):
                return ["depth", symbol]

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
        self.assertEqual(broker.depth("QQQ.US"), ["depth", "QQQ.US"])
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

    def test_submit_and_wait_returns_partial_fill_and_cancels_remainder(self):
        order = type("Order", (), {
            "order_id": "7", "status": "OrderStatus.PartialFilled",
            "executed_quantity": 2, "executed_price": 1.25,
        })()

        class Broker:
            def submit_order(self, **kwargs):
                return type("Response", (), {"order_id": "7"})()

            def today_orders(self, *args, **kwargs):
                return [order]

            def cancel_order(self, order_id):
                self.canceled = order_id

        broker = Broker()
        execution = OrderExecution(broker, sleep_fn=lambda _: None)
        result = execution.submit_and_wait({"symbol": "TEST"}, 3, retries=1, interval=0)
        self.assertTrue(result.filled)
        self.assertFalse(result.complete)
        self.assertEqual(result.executed_quantity, 2)
        self.assertEqual(broker.canceled, "7")

    def test_submit_and_wait_preserves_rejection(self):
        order = type("Order", (), {
            "order_id": "8", "status": "OrderStatus.Rejected",
            "executed_quantity": 0, "executed_price": 0,
        })()

        class Broker:
            def submit_order(self, **kwargs):
                return type("Response", (), {"order_id": "8"})()

            def today_orders(self, *args, **kwargs):
                return [order]

        result = OrderExecution(Broker(), sleep_fn=lambda _: None).submit_and_wait(
            {"symbol": "TEST"}, 3, retries=1, interval=0,
        )
        self.assertTrue(result.rejected)
        self.assertFalse(result.filled)

    def test_restart_recovers_active_buy_order(self):
        order = type("Order", (), {
            "order_id": "active-1", "symbol": "QQQ260710C500000.US",
            "side": "OrderSide.Buy", "quantity": 2,
            "executed_quantity": 0, "executed_price": 0,
            "status": "OrderStatus.New",
        })()

        class Broker:
            def today_orders(self, *args, **kwargs):
                return [order]

        with tempfile.TemporaryDirectory() as temp_dir:
            store = OrderStateStore(temp_dir, TZ_ET)
            execution = OrderExecution(Broker(), sleep_fn=lambda _: None, state_store=store)
            active = execution.recover_active_orders()
            restored = OrderStateStore(temp_dir, TZ_ET)
        self.assertEqual(len(active), 1)
        self.assertEqual(restored.active(buy_only=True)[0]["order_id"], "active-1")


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


class PositionSizerTests(unittest.TestCase):
    def test_put_and_afternoon_caps_are_applied(self):
        result = PositionSizer.calculate(100000, 1, "put", 1, {
            "order_pct": 20, "put_order_pct": 8, "contract_multiplier": 100,
            "max_contracts_per_trade": 400, "max_afternoon_contracts": 30,
        }, afternoon=True)
        self.assertEqual(result.effective_pct, 4)
        self.assertEqual(result.contracts, 30)
        self.assertEqual(result.quantity, 3000)

    def test_blocked_late_session_size_is_zero(self):
        result = PositionSizer.calculate(100000, 1, "call", 1, {
            "order_pct": 20, "contract_multiplier": 100,
        }, blocked=True)
        self.assertEqual(result.contracts, 0)
        self.assertEqual(result.quantity, 0)


class QuoteQualityPolicyTests(unittest.TestCase):
    def test_depth_supplies_bid_and_ask_when_quote_has_only_last_price(self):
        quote = type("Quote", (), {
            "last_done": 1.0, "timestamp": datetime.now(timezone.utc),
        })()
        level = lambda price: type("Level", (), {"price": price})()
        depth = type("Depth", (), {"bids": [level(0.98)], "asks": [level(1.02)]})()
        result = QuoteQualityPolicy.evaluate(quote, depth=depth)
        self.assertTrue(result.allowed)
        self.assertAlmostEqual(result.spread_pct, 0.04)

    def test_wide_spread_is_rejected(self):
        quote = type("Quote", (), {
            "last_done": 1.0, "bid_price": 0.7, "ask_price": 1.3,
            "timestamp": datetime.now(timezone.utc),
        })()
        result = QuoteQualityPolicy.evaluate(quote, max_spread_pct=0.30)
        self.assertFalse(result.allowed)
        self.assertIn("wide spread", result.reason)

    def test_stale_quote_is_rejected_and_valid_quote_keeps_mid(self):
        stale = type("Quote", (), {
            "last_done": 1.0, "bid_price": 0.95, "ask_price": 1.05,
            "timestamp": datetime.now(timezone.utc) - timedelta(seconds=60),
        })()
        self.assertFalse(QuoteQualityPolicy.evaluate(stale, max_age_seconds=30).allowed)
        fresh = type("Quote", (), {
            "last_done": 1.01, "bid_price": 0.98, "ask_price": 1.02,
            "timestamp": datetime.now(timezone.utc),
        })()
        result = QuoteQualityPolicy.evaluate(fresh)
        self.assertTrue(result.allowed)
        self.assertAlmostEqual(result.mid, 1.0)


class PositionRiskPolicyTests(unittest.TestCase):
    def test_put_measurement_updates_option_and_stock_peaks(self):
        position = {
            "dir": "put", "entry_opt_price": 1, "entry_price": 500,
            "entry_bar": 10, "max_pnl_pct": 0, "stock_peak": 500,
            "half_closed": False,
        }
        result = PositionRiskPolicy.measure(position, 1.2, 495, 15, True)
        self.assertAlmostEqual(result.option_pnl_pct, 20)
        self.assertAlmostEqual(result.stock_pnl_pct, 1)
        self.assertEqual(result.bars_held, 5)
        self.assertEqual(position["stock_peak"], 495)
        self.assertAlmostEqual(position["max_stock_pnl_pct"], 1)

    def test_v62_trend_timeout_keeps_existing_profile(self):
        profile = PositionRiskPolicy.timeout_profile(
            {"reason": "v6.2", "timeout_bars": 10}, {
                "v62_call_pool_timeout_stage_bars": 12,
                "v62_call_pool_timeout_bars": 20,
                "v62_call_pool_trend_timeout_bars": 28,
                "trend_timeout_bonus_bars": 4,
            }, "VWAP_Breakout", True, True,
        )
        self.assertEqual(profile["stage"], 12)
        self.assertEqual(profile["hard"], 16)

    def test_profit_tiers_are_sorted_and_normalized(self):
        tiers = PositionRiskPolicy.profit_tiers({"profit_take_tiers": [
            {"profit_pct": 60, "close_pct": 0.3},
            {"profit_pct": 30, "close_pct": 0.3},
        ]})
        self.assertEqual([tier["profit_pct"] for tier in tiers], [30, 60])


class EntryPolicyTests(unittest.TestCase):
    def test_rejection_is_returned_as_structured_decision(self):
        policy = EntryPolicy(lambda signal: True, lambda: "range high", lambda: True)
        decision = policy.evaluate({"dir": "call"})
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "range high")
        self.assertTrue(decision.hard_rejection)


class LifecycleControllerTests(unittest.TestCase):
    def test_start_and_stop_are_idempotent(self):
        lifecycle = LifecycleController()
        self.assertTrue(lifecycle.begin_start())
        self.assertFalse(lifecycle.begin_start())
        lifecycle.mark_running()
        self.assertTrue(lifecycle.begin_stop())
        self.assertFalse(lifecycle.begin_stop())
        lifecycle.mark_stopped()
        self.assertEqual(lifecycle.state, LifecycleState.STOPPED)


class TradingSessionPolicyTests(unittest.TestCase):
    def test_loss_day_uses_extension_window(self):
        config = {"end_time": "14:30", "extended_end_time": "15:00"}
        self.assertEqual(TradingSessionPolicy.effective_end_time(config, -1), "15:00")
        self.assertTrue(TradingSessionPolicy.is_extension_window(config, 14 * 60 + 45))
        self.assertFalse(TradingSessionPolicy.is_extension_window(config, 15 * 60))


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


class OrderAuditLogTests(unittest.TestCase):
    def test_filled_order_is_appended_to_daily_log(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = OrderAuditLog(temp_dir, TZ_ET).append(
                "42", "QQQ260710C500000.US", "call", 2, "filled", 2, 1.25,
            )
            text = path.read_text(encoding="utf-8")
        self.assertIn("42 | QQQ260710C500000.US | call | 2张 | filled", text)
        self.assertIn("成交:2张 @1.25", text)


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
        self.assertFalse(rows[0]["cycle_closed"])

    def test_repeated_contract_orders_are_separate_trade_cycles(self):
        orders = [
            {"order_id": "b1", "status": "Filled", "symbol": "QQQ260710C720000.US",
             "side": "买入", "executed_qty": 1, "executed_price": 1.0, "submitted_at": "2026-07-10 10:00:00"},
            {"order_id": "s1", "status": "Filled", "symbol": "QQQ260710C720000.US",
             "side": "卖出", "executed_qty": 1, "executed_price": 1.5, "submitted_at": "2026-07-10 10:05:00"},
            {"order_id": "b2", "status": "Filled", "symbol": "QQQ260710C720000.US",
             "side": "买入", "executed_qty": 2, "executed_price": 2.0, "submitted_at": "2026-07-10 11:00:00"},
            {"order_id": "s2", "status": "Filled", "symbol": "QQQ260710C720000.US",
             "side": "卖出", "executed_qty": 2, "executed_price": 1.8, "submitted_at": "2026-07-10 11:04:00"},
        ]
        rows = TradeLedger(".", TZ_ET).reconcile_order_rows(orders)
        self.assertEqual(len(rows), 2)
        self.assertEqual([row["pnl_usd"] for row in rows], [50, -40])
        self.assertNotEqual(rows[0]["trade_cycle_id"], rows[1]["trade_cycle_id"])
        self.assertTrue(all(row["cycle_closed"] for row in rows))
        self.assertEqual(rows[0]["entry_order_ids"], ["b1"])
        self.assertEqual(rows[1]["entry_order_ids"], ["b2"])

    def test_daily_record_restores_runtime_trades_and_statistics(self):
        payload = {
            "date": "2026-07-10",
            "trades": [
                {"date": "2026-07-10", "entry_time": "10:00:00", "exit_time": "10:05:00",
                 "dir": "call", "entry_price": 1, "exit_price": 1.5, "contracts": 2,
                 "pnl_pct": 50, "pnl_usd": 100, "result": "win", "opt_symbol": "CALL.US"},
                {"date": "2026-07-10", "entry_time": "11:00:00", "exit_time": "11:03:00",
                 "dir": "put", "entry_price": 2, "exit_price": 1.8, "contracts": 1,
                 "pnl_pct": -10, "pnl_usd": -20, "result": "lose", "opt_symbol": "PUT.US"},
                {"date": "2026-07-09", "pnl_usd": 999, "result": "win"},
            ],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            records = Path(temp_dir) / "records"
            records.mkdir()
            (records / "2026-07-10.json").write_text(json.dumps(payload), encoding="utf-8")
            result = TradeLedger(temp_dir, TZ_ET).load_daily_record("2026-07-10")
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["wins"], 1)
        self.assertEqual(result["pnl"], 80)
        self.assertEqual(result["call_pnl"], 100)
        self.assertEqual(result["put_pnl"], -20)
        self.assertEqual(result["largest_win_pct"], 50)
        self.assertEqual(result["largest_loss_pct"], -10)
        self.assertEqual(result["trades"][0]["entry_time"].strftime("%H:%M:%S"), "10:00:00")

    def test_pending_record_is_recovered_from_broker_snapshot(self):
        orders = {"orders": [
            {"status": "Filled", "symbol": "QQQ260710C720000.US", "side": "买入",
             "executed_qty": 2, "executed_price": 1.0},
            {"status": "Filled", "symbol": "QQQ260710C720000.US", "side": "卖出",
             "executed_qty": 2, "executed_price": 1.5},
        ]}
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "longbridge_orders.json"
            path.write_text(json.dumps(orders, ensure_ascii=False), encoding="utf-8")
            ledger = TradeLedger(temp_dir, TZ_ET)
            result = ledger.recover_pending_record(path, "2026-07-11")
            payload = json.loads(Path(result["path"]).read_text(encoding="utf-8"))
            second = ledger.recover_pending_record(path, "2026-07-11")
        self.assertEqual(result["status"], "recovered")
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["pnl"], 100)
        self.assertEqual(second["status"], "complete")

    def test_broker_order_snapshot_is_serialized_and_saved(self):
        order = type("Order", (), {
            "order_id": "42",
            "symbol": "QQQ260710C720000.US",
            "side": "OrderSide.Buy",
            "quantity": 3,
            "executed_quantity": 2,
            "executed_price": 1.25,
            "status": "OrderStatus.PartialFilled",
            "submitted_at": "submit",
            "updated_at": "update",
        })()
        with tempfile.TemporaryDirectory() as temp_dir:
            result = TradeLedger(temp_dir, TZ_ET).save_broker_orders([order])
            payload = json.loads(Path(result["path"]).read_text(encoding="utf-8"))
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["buy_count"], 1)
        self.assertEqual(payload["orders"][0]["executed_qty"], 2)
        self.assertEqual(payload["orders"][0]["status"], "PartialFilled")

    def test_daily_record_merges_internal_details_and_broker_reconciliation(self):
        internal = [{
            "order_id": "open-1", "entry_time": datetime(2026, 7, 10, 10, 0, tzinfo=TZ_ET),
            "exit_time": datetime(2026, 7, 10, 10, 5, tzinfo=TZ_ET), "dir": "call",
            "opt_symbol": "QQQ260710C720000.US", "contracts": 2,
            "entry_opt_price": 1, "exit_opt_price": 1.5, "pnl_usd": 100,
            "pnl_pct": 50, "win": True, "reason": "VWAP_Breakout",
        }]
        broker = [{
            "date": "2026-07-10", "dir": "call", "opt_symbol": "QQQ260710C720000.US",
            "contracts": 2, "pnl_usd": 100, "result": "win",
        }]
        with tempfile.TemporaryDirectory() as temp_dir:
            result = TradeLedger(temp_dir, TZ_ET).save_daily_record(internal, broker, 100, [{"id": 1}])
            payload = json.loads(Path(result["path"]).read_text(encoding="utf-8"))
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["pnl"], 100)
        self.assertEqual(payload["trades"][0]["reason"], "VWAP_Breakout")
        self.assertEqual(payload["signal_probes"], [{"id": 1}])

    def test_daily_record_keeps_missing_repeat_cycle_for_same_symbol(self):
        symbol = "QQQ260710C720000.US"
        internal = [{
            "trade_cycle_id": "live-cycle", "order_id": "b1",
            "entry_time": datetime(2026, 7, 10, 10, 0, tzinfo=TZ_ET),
            "exit_time": datetime(2026, 7, 10, 10, 5, tzinfo=TZ_ET),
            "dir": "call", "opt_symbol": symbol, "contracts": 1,
            "pnl_usd": 50, "win": True,
        }]
        broker = [
            {"trade_cycle_id": "broker-1", "entry_order_ids": ["b1"], "date": "2026-07-10",
             "dir": "call", "opt_symbol": symbol, "contracts": 1, "pnl_usd": 50, "result": "win"},
            {"trade_cycle_id": "broker-2", "entry_order_ids": ["b2"], "date": "2026-07-10",
             "dir": "call", "opt_symbol": symbol, "contracts": 1, "pnl_usd": -20, "result": "lose"},
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            result = TradeLedger(temp_dir, TZ_ET).save_daily_record(internal, broker, 100, [])
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["trades"][0]["trade_cycle_id"], "live-cycle")
        self.assertEqual(result["trades"][1]["trade_cycle_id"], "broker-2")


class SignalProbeStoreTests(unittest.TestCase):
    def test_save_and_load_normalizes_milestone_keys(self):
        probe = {
            "id": 7, "entry_time": "2026-07-10 10:00:00", "entry_bar": 20,
            "signal": "VWAP_Breakout", "dir": "call", "entry_price": 500,
            "day_market_regime": "trend", "day_market_direction": "up",
            "opening_range": {"high": 501, "low": 498},
            "milestones": {5: {"pct": 0.2}, 10: None, 20: None},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SignalProbeStore(temp_dir, TZ_ET)
            saved = store.save([probe])
            date_str = saved["date"]
            restored = store.load(date_str)
        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0]["id"], 7)
        self.assertEqual(restored[0]["milestones"][5], {"pct": 0.2})
        self.assertIn(20, restored[0]["milestones"])
        self.assertEqual(restored[0]["day_market_regime"], "trend")
        self.assertEqual(restored[0]["opening_range"]["high"], 501)


class RuntimeStateStoreTests(unittest.TestCase):
    def test_checkpoint_restores_bookkeeping_but_preserves_broker_quantity(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = RuntimeStateStore(temp_dir)
            checkpoint = {
                "opt_symbol": "QQQ260710C720000.US", "contracts": 5,
                "quantity": 500, "partial_exits": [{"tier": "30"}],
                "realized_pnl_usd": 40,
            }
            store.save({"position_checkpoint": checkpoint}, checkpoint, 501)
            restored = store.restore_checkpoint({
                "opt_symbol": "QQQ260710C720000.US", "contracts": 2, "quantity": 200,
            }, 100)
        self.assertEqual(restored["contracts"], 2)
        self.assertEqual(restored["quantity"], 200)
        self.assertEqual(restored["partial_exits"], [{"tier": "30"}])
        self.assertEqual(restored["realized_pnl_usd"], 40)
        self.assertEqual(restored["order_status"], "restored")


class ConfigSafetyTests(unittest.TestCase):
    def test_sensitive_values_are_redacted_recursively(self):
        safe = redact_config({
            "telegram": {"bot_token": "123:secret", "chat_id": "42"},
            "nested": [{"api_key": "key"}],
        })
        self.assertEqual(safe["telegram"]["bot_token"], "***REDACTED***")
        self.assertEqual(safe["telegram"]["chat_id"], "42")
        self.assertEqual(safe["nested"][0]["api_key"], "***REDACTED***")

    def test_environment_secret_takes_precedence(self):
        with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "env-token"}):
            value = resolve_secret(
                {"telegram": {"bot_token": "file-token"}},
                "telegram", "bot_token", "TELEGRAM_BOT_TOKEN",
            )
        self.assertEqual(value, "env-token")

    def test_cross_field_validation_rejects_dangerous_config(self):
        errors = validate_config({
            "trading": {"start_time": "15:30", "end_time": "09:30"},
            "risk": {
                "order_pct": 80, "put_order_pct": 8, "daily_limit": 0,
                "max_contracts_per_trade": 0,
                "timeout_stage1_bars": 20, "timeout_stage2_bars": 10,
                "timeout_stage3_bars": 5,
                "profit_take_tiers": [
                    {"profit_pct": 60, "close_pct": 0.7},
                    {"profit_pct": 30, "close_pct": 0.7},
                ],
            },
        })
        self.assertGreaterEqual(len(errors), 6)


class RuntimeHealthTests(unittest.TestCase):
    def test_health_tracks_rejections_and_failures(self):
        health = RuntimeHealth(TZ_ET)
        health.beat("loop")
        health.beat("order_sync", ok=False, detail="timeout")
        health.reject("range high call blocked")
        health.reject("range high call blocked")
        snapshot = health.snapshot(running=True, market_open=False)
        self.assertEqual(snapshot["status"], "degraded")
        self.assertIn("order_sync_failed", snapshot["issues"])
        self.assertEqual(snapshot["top_rejections"][0]["count"], 2)

    def test_closed_market_does_not_require_quotes(self):
        health = RuntimeHealth(TZ_ET)
        health.beat("loop")
        snapshot = health.snapshot(running=True, market_open=False)
        self.assertEqual(snapshot["status"], "healthy")
        self.assertNotIn("market_quote_stale", snapshot["issues"])

    def test_open_market_reports_missing_data_after_startup_grace(self):
        health = RuntimeHealth(TZ_ET)
        health.started_at -= timedelta(minutes=5)
        health.beat("loop")
        snapshot = health.snapshot(running=True, market_open=True)
        self.assertEqual(snapshot["status"], "degraded")
        self.assertIn("market_quote_stale", snapshot["issues"])
        self.assertIn("market_candle_stale", snapshot["issues"])


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
