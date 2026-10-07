"""Offline tests for the run-once scheduler (no network, no real sleeping)."""

import csv
import sys
import tempfile
import unittest
from datetime import date, datetime, time
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import run
import scheduler
from strategy import IST


class CalendarTests(unittest.TestCase):
    def test_weekend_and_holiday(self):
        hol = {date(2026, 10, 20)}
        self.assertTrue(scheduler.is_trading_day(date(2026, 10, 5), hol))     # Monday
        self.assertFalse(scheduler.is_trading_day(date(2026, 10, 3), hol))    # Saturday
        self.assertFalse(scheduler.is_trading_day(date(2026, 10, 20), hol))   # listed holiday

    def test_stale_quote_detection(self):
        day = datetime(2026, 9, 25, 9, 20, tzinfo=IST)
        self.assertTrue(run.quotes_are_stale({"1": {"last_trade_time": "24/09/2026 15:29:58"}}, day))
        self.assertFalse(run.quotes_are_stale({"1": {"last_trade_time": "25/09/2026 09:20:01"}}, day))
        self.assertFalse(run.quotes_are_stale({"1": {}}, day))                       # no field: ignore
        self.assertFalse(run.quotes_are_stale({"1": {"last_trade_time": "???"}}, day))  # unknown format


class AttemptTests(unittest.TestCase):
    def test_retries_then_succeeds(self):
        calls = {"n": 0}

        def flaky(cfg):
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("HTTP 401")
            return "ok"

        with mock.patch.object(scheduler, "now_ist", lambda: datetime(2026, 9, 25, 9, 20, tzinfo=IST)), \
             mock.patch.object(scheduler._time, "sleep"), \
             mock.patch.object(scheduler, "load_settings", lambda: None):
            self.assertEqual(scheduler.attempt("x", flaky, time(11, 0)), (True, "ok"))
        self.assertEqual(calls["n"], 3)

    def test_no_market_data_is_not_retried(self):
        def holiday(cfg):
            raise run.NoMarketData("holiday")

        with mock.patch.object(scheduler, "now_ist", lambda: datetime(2026, 9, 25, 9, 20, tzinfo=IST)), \
             mock.patch.object(scheduler, "load_settings", lambda: None):
            with self.assertRaises(run.NoMarketData):
                scheduler.attempt("scan", holiday, time(11, 0))


class ProcessDayTests(unittest.TestCase):
    def run_day(self, movers_after_scan):
        order = []
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(scheduler, "ROOT", Path(tmp)), \
             mock.patch.object(scheduler, "sleep_until", lambda t: None), \
             mock.patch.object(scheduler, "now_ist", lambda: datetime(2026, 9, 25, 9, 1, tzinfo=IST)), \
             mock.patch.object(run, "cmd_universe", lambda c: order.append("universe")), \
             mock.patch.object(run, "cmd_scan", lambda c, use_history: order.append("scan")), \
             mock.patch.object(run, "load_movers", lambda: movers_after_scan if "scan" in order else []), \
             mock.patch.object(run, "cmd_trade", lambda c, watch: order.append("trade") or []):
            scheduler.process_day(date(2026, 9, 25))
            summary = Path(tmp) / "data" / "daily_summary.csv"
            rows = list(csv.reader(summary.open())) if summary.exists() else []
        return order, rows

    def test_full_day_runs_in_order_and_writes_summary(self):
        order, rows = self.run_day([{"symbol": "X"}])
        self.assertEqual(order, ["universe", "scan", "trade"])
        self.assertEqual(rows[0], ["date", "trades", "wins", "losses", "net_pnl"])
        self.assertEqual(rows[1][0], "2026-09-25")

    def test_no_movers_skips_trading(self):
        order, rows = self.run_day([])
        self.assertEqual(order, ["universe", "scan"])
        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
