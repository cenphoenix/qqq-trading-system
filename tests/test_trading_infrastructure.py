import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from trading import NotificationLog, TradeLedger


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


if __name__ == "__main__":
    unittest.main()
