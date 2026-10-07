"""Offline tests for the dashboard (temp data folder, no network to brokers)."""

import csv
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dashboard

FIELDS = ["symbol", "direction", "option", "strike", "expiry", "lots", "qty", "entry_time", "entry",
          "initial_stop", "stop_now", "exit_time", "exit_or_last", "pnl", "r_multiple", "trailing", "status"]


def trade(symbol, pnl, status="EXIT_STOP", r=1.0):
    return dict(symbol=symbol, direction="CALL", option="CE", strike=1400.0, expiry="2026-09-29",
                lots=1, qty=100, entry_time="09:20:00", entry=10.0, initial_stop=9.0, stop_now=9.0,
                exit_time="" if status == "OPEN" else "10:05:00", exit_or_last=11.0, pnl=pnl,
                r_multiple=r, trailing=False, status=status)


def write_day(root: Path, day: str, rows: list[dict]):
    (root / day).mkdir(parents=True)
    with (root / day / "trades.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)


class FormatTests(unittest.TestCase):
    def test_inr_grouping(self):
        self.assertEqual(dashboard.inr(1234567), "₹12,34,567")
        self.assertEqual(dashboard.inr(-950), "-₹950")
        self.assertEqual(dashboard.inr(0), "₹0")
        self.assertEqual(dashboard.inr(100000), "₹1,00,000")


class StatsTests(unittest.TestCase):
    def test_summary_numbers(self):
        rows = [dict(trade("A", 300), pnl=300.0), dict(trade("B", -100), pnl=-100.0),
                dict(trade("C", 100), pnl=100.0), dict(trade("D", 50, "OPEN"), pnl=50.0)]
        s = dashboard.summarize(rows)
        self.assertEqual((s["trades"], s["closed"], s["open"], s["wins"], s["losses"]), (4, 3, 1, 2, 1))
        self.assertAlmostEqual(s["win_rate"], 66.666, places=2)       # open trade excluded
        self.assertEqual((s["net"], s["realised"], s["unrealised"]), (350.0, 300.0, 50.0))
        self.assertAlmostEqual(s["profit_factor"], 4.0)

    def test_empty_summary(self):
        s = dashboard.summarize([])
        self.assertIsNone(s["win_rate"])
        self.assertIsNone(s["profit_factor"])


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patch = mock.patch.object(dashboard, "DATA", self.root)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_history_drawdown_and_curve(self):
        write_day(self.root, "2026-09-21", [trade("A", 500.0)])
        write_day(self.root, "2026-09-22", [trade("A", -800.0)])
        write_day(self.root, "2026-09-23", [trade("A", 200.0)])
        h = dashboard.history()
        self.assertEqual([round(v) for _, v in h["cumulative"]], [500, -300, -100])
        self.assertEqual(h["stats"]["max_drawdown"], 800.0)
        self.assertEqual((h["stats"]["days"], h["stats"]["winning_days"]), (3, 2))

    def test_render_shows_trade_and_totals(self):
        write_day(self.root, "2026-09-25", [trade("RELIANCE", 1234.0), trade("TCS", -500.0)])
        html = dashboard.render("2026-09-25")
        for text in ("RELIANCE", "TCS", "₹1,234", "-₹500", "50%", "09:20:00", "10:05:00", "PAPER ONLY"):
            self.assertIn(text, html)
        self.assertNotIn("<script", html.lower())                    # no JavaScript at all

    def test_html_is_escaped(self):
        write_day(self.root, "2026-09-25", [trade("<script>alert(1)</script>", 10.0)])
        html = dashboard.render("2026-09-25")
        self.assertNotIn("<script>alert", html)
        self.assertIn("&lt;script&gt;", html)

    def test_empty_data_renders(self):
        html = dashboard.render(None)
        self.assertIn("No trades recorded", html)

    def test_malformed_row_is_skipped(self):
        write_day(self.root, "2026-09-25", [trade("OK", 5.0), dict(trade("BAD", 1.0), pnl="oops")])
        self.assertEqual([t["symbol"] for t in dashboard.load_trades("2026-09-25")], ["OK"])

    def test_bad_date_param_is_ignored(self):
        html = dashboard.render("../../etc/passwd")
        self.assertNotIn("passwd", html)

    def test_http_server(self):
        write_day(self.root, "2026-09-25", [trade("INFY", 77.0)])
        server = dashboard.make_server(port=0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            body = urllib.request.urlopen(f"{base}/?date=2026-09-25").read().decode()
            self.assertIn("INFY", body)
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(f"{base}/secret")
            self.assertEqual(ctx.exception.code, 404)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
