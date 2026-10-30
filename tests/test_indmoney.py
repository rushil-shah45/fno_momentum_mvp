"""Unit tests for INDMoney broker integration, universe building, and quotes handling."""

import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock

import broker
from settings import Settings
from strategy import IST, Contract

IND_CFG = Settings(
    active_broker="indmoney",
    indmoney_client_id="test_client_id",
    indmoney_access_token="test_token",
)


class INDMoneyUniverseTests(unittest.TestCase):
    def test_build_indmoney_universe_handles_hyphenated_symbols(self):
        """Symbols with hyphens like BAJAJ-AUTO and NAM-INDIA must not be clipped to BAJAJ / NAM."""
        mock_master = [
            {
                "EXCH": "NSE",
                "INSTRUMENT_NAME": "OPTSTK",
                "TRADING_SYMBOL": "BAJAJ-AUTO-Oct2026-13400-CE",
                "CUSTOM_SYMBOL": "BAJAJ-AUTO 27 OCT 13400 CE",
                "SECURITY_ID": "56598",
                "LOT_UNITS": "75",
            },
            {
                "EXCH": "NSE",
                "INSTRUMENT_NAME": "OPTSTK",
                "TRADING_SYMBOL": "NAM-INDIA-Nov2026-820-PE",
                "CUSTOM_SYMBOL": "NAM-INDIA 23 NOV 820 PE",
                "SECURITY_ID": "138474",
                "LOT_UNITS": "625",
            },
            {
                "EXCH": "NSE",
                "INSTRUMENT_NAME": "OPTSTK",
                "TRADING_SYMBOL": "RELIANCE-Nov2026-1400-CE",
                "CUSTOM_SYMBOL": "RELIANCE 27 NOV 1400 CE",
                "SECURITY_ID": "99999",
                "LOT_UNITS": "250",
            },
            {
                # Unknown stock not in equity map: must be skipped, not corrupt universe with option token
                "EXCH": "NSE",
                "INSTRUMENT_NAME": "OPTSTK",
                "TRADING_SYMBOL": "UNKNOWN-Nov2026-100-CE",
                "CUSTOM_SYMBOL": "UNKNOWN 27 NOV 100 CE",
                "SECURITY_ID": "11111",
                "LOT_UNITS": "100",
            },
        ]
        mock_equity_map = {
            "BAJAJ-AUTO": "16669",
            "NAM-INDIA": "357",
            "RELIANCE": "2885",
        }

        with mock.patch("broker.load_indmoney_equity_map", return_value=mock_equity_map):
            universe = broker.build_indmoney_universe(mock_master, IND_CFG)

        symbols = [u["symbol"] for u in universe]
        self.assertIn("BAJAJ-AUTO", symbols)
        self.assertIn("NAM-INDIA", symbols)
        self.assertIn("RELIANCE", symbols)
        self.assertNotIn("BAJAJ", symbols)
        self.assertNotIn("NAM", symbols)
        self.assertNotIn("UNKNOWN", symbols)

        bajaj = next(u for u in universe if u["symbol"] == "BAJAJ-AUTO")
        self.assertEqual(bajaj["security_id"], "16669")
        self.assertEqual(bajaj["lot_size"], "75")

        nam = next(u for u in universe if u["symbol"] == "NAM-INDIA")
        self.assertEqual(nam["security_id"], "357")
        self.assertEqual(nam["lot_size"], "625")

    def test_build_indmoney_universe_fallback_when_no_custom_symbol(self):
        """When CUSTOM_SYMBOL is missing, rsplit parses root symbol."""
        mock_master = [
            {
                "EXCH": "NSE",
                "INSTRUMENT_NAME": "OPTSTK",
                "TRADING_SYMBOL": "BAJAJ-AUTO-Oct2026-13400-CE",
                "CUSTOM_SYMBOL": "",
                "SECURITY_ID": "56598",
                "LOT_UNITS": "75",
            },
        ]
        mock_equity_map = {"BAJAJ-AUTO": "16669"}

        with mock.patch("broker.load_indmoney_equity_map", return_value=mock_equity_map):
            universe = broker.build_indmoney_universe(mock_master, IND_CFG)

        self.assertEqual(len(universe), 1)
        self.assertEqual(universe[0]["symbol"], "BAJAJ-AUTO")
        self.assertEqual(universe[0]["security_id"], "16669")


class INDMoneyOptionContractsTests(unittest.TestCase):
    def test_indmoney_option_contracts_matching(self):
        mock_master = [
            {
                "EXCH": "NSE",
                "INSTRUMENT_NAME": "OPTSTK",
                "TRADING_SYMBOL": "BAJAJ-AUTO-Oct2026-13400-CE",
                "CUSTOM_SYMBOL": "BAJAJ-AUTO 27 OCT 13400 CE",
                "SECURITY_ID": "56598",
                "OPTION_TYPE": "CE",
                "EXPIRY_DATE": "10/27/2026 14:00",
                "STRIKE_PRICE": "13400.00",
                "LOT_UNITS": "75",
            },
            {
                "EXCH": "NSE",
                "INSTRUMENT_NAME": "OPTSTK",
                "TRADING_SYMBOL": "BAJAJ-AUTO-Oct2026-13000-PE",
                "CUSTOM_SYMBOL": "BAJAJ-AUTO 27 OCT 13000 PE",
                "SECURITY_ID": "56599",
                "OPTION_TYPE": "PE",
                "EXPIRY_DATE": "10/27/2026 14:00",
                "STRIKE_PRICE": "13000.00",
                "LOT_UNITS": "75",
            },
        ]
        mock_equity_map = {"BAJAJ-AUTO": "16669"}

        with mock.patch("broker.load_indmoney_equity_map", return_value=mock_equity_map):
            contracts = broker.indmoney_option_contracts(mock_master, "16669", "CE", IND_CFG)

        self.assertEqual(len(contracts), 1)
        c = contracts[0]
        self.assertEqual(c.security_id, "NFO_56598")
        self.assertEqual(c.option_type, "CE")
        self.assertEqual(c.strike, 13400.0)
        self.assertEqual(c.lot_size, 75)
        self.assertEqual(c.expiry, date(2026, 10, 27))


class INDMoneyQuotesTests(unittest.TestCase):
    @mock.patch("auth.get_access_token", return_value="test_token")
    def test_quotes_bisection_on_400_bad_request(self, mock_auth):
        """If a chunk fails with 400 Bad Request, bisection isolates bad scrip and salvages good ones."""
        client = broker.INDMoney(IND_CFG)

        def mock_get(path, params=None):
            codes = (params or {}).get("scrip-codes", "").split(",")
            if "NSE_BAD" in codes:
                resp = mock.MagicMock()
                resp.status_code = 400
                resp.ok = False
                resp.text = '{"message": "Invalid scrip codes: NSE_BAD"}'
                import requests
                raise requests.HTTPError("400 Client Error: Bad Request", response=resp)
            data = {}
            for c in codes:
                data[c] = {
                    "last_price": 100.0,
                    "ohlc": {"open": 98.0, "high": 102.0, "low": 97.0, "close": 100.0},
                    "volume": 5000,
                    "timestamp": 1793101741,
                }
            return {"data": data}

        with mock.patch.object(client, "_get", side_effect=mock_get):
            results = client.quotes(["GOOD1", "GOOD2", "BAD", "GOOD3"])

        self.assertIn("GOOD1", results)
        self.assertIn("GOOD2", results)
        self.assertIn("GOOD3", results)
        self.assertNotIn("BAD", results)
        self.assertEqual(results["GOOD1"]["last_price"], 100.0)


class AutoHealAndSchedulerTests(unittest.TestCase):
    def test_attempt_run_once_if_past(self):
        import scheduler
        from datetime import time
        calls = []

        def dummy_fn(cfg):
            calls.append(True)
            return "done"

        # Current time 10:30, cutoff 09:15
        with mock.patch.object(scheduler, "now_ist", lambda: datetime(2026, 10, 29, 10, 30, tzinfo=IST)), \
             mock.patch.object(scheduler, "load_settings", lambda: None):
            ok, res = scheduler.attempt("universe", dummy_fn, until=time(9, 15), run_once_if_past=True)

        self.assertTrue(ok)
        self.assertEqual(res, "done")
        self.assertEqual(len(calls), 1)

    def test_cmd_scan_auto_heals_legacy_universe(self):
        import csv
        import run
        with tempfile.TemporaryDirectory() as tmp:
            tmp_data = Path(tmp)
            legacy_file = tmp_data / "universe.csv"
            legacy_file.write_text("symbol,security_id,lot_size\nBAJAJ,74928,75\n", encoding="utf-8")

            regenerated = []
            def mock_cmd_universe(cfg):
                regenerated.append(True)
                legacy_file.write_text("symbol,security_id,lot_size\nBAJAJ-AUTO,16669,75\n", encoding="utf-8")

            with mock.patch.object(run, "DATA", tmp_data), \
                 mock.patch.object(run, "cmd_universe", side_effect=mock_cmd_universe), \
                 mock.patch.object(run, "get_broker") as mock_get_broker, \
                 mock.patch.object(run, "quotes_are_stale", return_value=False):
                broker_inst = mock.MagicMock()
                broker_inst.quotes.return_value = {
                    "16669": {"last_price": 10000.0, "ohlc": {"close": 10000.0}}
                }
                broker_inst.stock_opening_range.return_value = mock.MagicMock(open=10000, high=10100, low=9900, close=10050, volume=1000)
                mock_get_broker.return_value = broker_inst

                try:
                    run.cmd_scan(IND_CFG, use_history=True)
                except Exception:
                    pass

            self.assertTrue(regenerated)
            with legacy_file.open() as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(rows[0]["symbol"], "BAJAJ-AUTO")
            self.assertEqual(rows[0]["security_id"], "16669")


if __name__ == "__main__":
    unittest.main()

