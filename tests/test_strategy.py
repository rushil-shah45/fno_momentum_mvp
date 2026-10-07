"""Offline tests for the strategy logic. Run:  python -m unittest discover -s tests"""

import sys
import unittest
from datetime import date, datetime, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from strategy import (IST, Bar, Contract, OpeningRange, classify, open_position,
                      pick_atm, rank_movers, run_session)


def ts(hhmm: str) -> int:
    h, m = map(int, hhmm.split(":"))
    return int(datetime(2026, 9, 25, h, m, tzinfo=IST).timestamp())


def bar(hhmm, o, h, l, c):
    return Bar(ts(hhmm), o, h, l, c)


ARGS = dict(tolerance=0.05, close_zone=0.20, max_range_pct=3.0)
CE = Contract("1", "CE", date(2026, 9, 29), 100.0, 100)


def opening_bars(low=90.0):
    return [bar(f"09:{15 + i}", 95, 100, low, 98) for i in range(5)]


class ClassifyTests(unittest.TestCase):
    def test_call(self):
        self.assertEqual(classify(OpeningRange(100, 102, 100, 101.9), **ARGS)[0], "CALL")

    def test_put(self):
        self.assertEqual(classify(OpeningRange(102, 102, 100, 100.1), **ARGS)[0], "PUT")

    def test_close_not_near_extreme(self):
        self.assertEqual(classify(OpeningRange(100, 102, 100, 101.0), **ARGS)[0], "")

    def test_range_too_large(self):
        self.assertEqual(classify(OpeningRange(100, 110, 100, 109.9), **ARGS)[1],
                         "opening range too large")

    def test_flat(self):
        self.assertEqual(classify(OpeningRange(100, 100, 100, 100), **ARGS)[0], "")


class SelectionTests(unittest.TestCase):
    def test_rank_aligns_direction_and_caps(self):
        rows = [{"direction": "CALL", "change_pct": c, "symbol": f"C{c}"} for c in (3, 2, 1, -1)]
        rows += [{"direction": "PUT", "change_pct": c, "symbol": f"P{c}"} for c in (-3, -2, 1)]
        got = [r["symbol"] for r in rank_movers(rows, top_n=2)]
        self.assertEqual(got, ["C3", "C2", "P-3", "P-2"])

    def test_atm_pick(self):
        cs = [Contract("a", "CE", date(2026, 9, 26), 100, 1),   # too close to expiry
              Contract("b", "CE", date(2026, 9, 29), 95, 1),
              Contract("c", "CE", date(2026, 9, 29), 105, 1),
              Contract("d", "CE", date(2026, 10, 27), 101, 1)]
        self.assertEqual(pick_atm(cs, 104, date(2026, 9, 25), 3).security_id, "c")


class PositionTests(unittest.TestCase):
    def make(self, later, **kw):
        bars = opening_bars() + [bar("09:20", 100, 101, 99, 100)] + later
        pos, rest, why = open_position("X", "CALL", CE, bars, kw.get("risk", 2000), kw.get("enf", True))
        return pos, rest, why

    def test_entry_and_stop_from_clock_not_index(self):
        # Option had no 09:15-09:17 candles; entry must still be the 09:20 open.
        bars = [bar("09:18", 95, 100, 92, 98), bar("09:19", 98, 99, 91, 95),
                bar("09:20", 100, 101, 99, 100)]
        pos, _, _ = open_position("X", "CALL", CE, bars, 5000, True)
        self.assertEqual((pos.entry, pos.initial_stop), (100, 91))

    def test_stop_hit(self):
        pos, rest, _ = self.make([bar("09:21", 100, 101, 89, 90)])
        run_session([pos], {"X": rest}, 10**9, time(15, 0))
        self.assertEqual((pos.status, pos.exit_price), ("EXIT_STOP", 90.0))
        self.assertAlmostEqual(pos.r_multiple, -1.0)

    def test_trailing_then_stop(self):
        # risk = 10. +1R at 110 -> stop to entry(100); then trails lows.
        pos, rest, _ = self.make([bar("09:21", 100, 111, 100.5, 110),
                                  bar("09:22", 110, 120, 108, 119),
                                  bar("09:23", 119, 119, 107, 108)])
        run_session([pos], {"X": rest}, 10**9, time(15, 0))
        self.assertTrue(pos.trailing)
        self.assertEqual((pos.status, pos.exit_price), ("EXIT_STOP", 108.0))
        self.assertGreater(pos.pnl, 0)

    def test_risk_limit_skips_or_sizes(self):
        _, _, why = self.make([], risk=500)            # 1 lot risks 10*100 = 1000
        self.assertIn("limit", why)
        pos, _, _ = self.make([], risk=2500)           # floor(2500/1000) = 2 lots
        self.assertEqual(pos.lots, 2)
        pos, _, _ = self.make([], risk=500, enf=False)
        self.assertEqual(pos.lots, 1)

    def test_force_exit(self):
        pos, rest, _ = self.make([bar("14:59", 100, 103, 99, 102), bar("15:00", 102, 103, 99, 100)])
        run_session([pos], {"X": rest}, 10**9, time(15, 0))
        self.assertEqual((pos.status, pos.exit_price), ("EXIT_FORCED", 102.0))

    def test_daily_loss_limit_closes_open_positions(self):
        pos, rest, _ = self.make([bar("09:21", 100, 100.5, 95, 95), bar("09:22", 95, 96, 94, 94)],
                                 risk=2000)
        note = run_session([pos], {"X": rest}, 400, time(15, 0))
        self.assertEqual(note, "daily loss limit hit")
        self.assertEqual(pos.status, "EXIT_DAILY_LOSS")

    def test_still_open(self):
        pos, rest, _ = self.make([bar("09:21", 100, 102, 99, 101)])
        run_session([pos], {"X": rest}, 10**9, time(15, 0))
        self.assertEqual(pos.status, "OPEN")


if __name__ == "__main__":
    unittest.main()
