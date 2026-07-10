import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from trading import NotificationLog, NotificationService, ReviewSummaryScheduler, TradeLedger


TZ_ET = ZoneInfo("America/New_York")


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
