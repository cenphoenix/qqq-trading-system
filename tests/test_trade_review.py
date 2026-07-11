import csv
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

from trade_review import review_trades_for_day


class TradeReviewTests(unittest.TestCase):
    def _write_candles(self, directory: Path, day: date, closes: list[float]) -> None:
        directory.mkdir(parents=True)
        path = directory / f"{day.isoformat()}.csv"
        start = datetime.combine(day, datetime.strptime("09:30", "%H:%M").time())
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(
                stream,
                fieldnames=["timestamp", "open", "high", "low", "close", "volume", "turnover"],
            )
            writer.writeheader()
            previous = closes[0]
            for index, close in enumerate(closes):
                writer.writerow({
                    "timestamp": (start + timedelta(minutes=index)).strftime("%Y-%m-%d %H:%M:%S"),
                    "open": previous,
                    "high": max(previous, close) + 0.05,
                    "low": min(previous, close) - 0.05,
                    "close": close,
                    "volume": 1000,
                    "turnover": 0,
                })
                previous = close

    def test_winner_detects_continuation_after_exit(self):
        day = date(2026, 7, 10)
        closes = [100 + index * 0.1 for index in range(40)]
        trade = {
            "entry_time": "09:35:00",
            "exit_time": "09:40:00",
            "dir": "call",
            "pnl_usd": 100,
            "pnl_pct": 20,
            "reason": "VWAP_Breakout",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            candle_dir = Path(temp_dir) / "candles"
            self._write_candles(candle_dir, day, closes)
            reviewed = review_trades_for_day([trade], day, candle_dir)[0]
        self.assertTrue(reviewed["review_available"])
        self.assertIn("止盈过早", reviewed["exit_verdict"])
        self.assertGreater(reviewed["post_exit_10_pct"], 0)

    def test_fast_loss_is_classified_as_quote_noise(self):
        day = date(2026, 7, 10)
        closes = [100.0] * 40
        trade = {
            "entry_time": "09:35:00",
            "exit_time": "09:35:10",
            "dir": "put",
            "pnl_usd": -500,
            "pnl_pct": -20,
            "exit_reason": "止损(-20%)",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            candle_dir = Path(temp_dir) / "candles"
            self._write_candles(candle_dir, day, closes)
            reviewed = review_trades_for_day([trade], day, candle_dir)[0]
        self.assertIn("报价噪声", reviewed["exit_verdict"])


if __name__ == "__main__":
    unittest.main()
