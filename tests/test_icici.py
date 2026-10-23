"""Unit tests for ICICI Direct broker integration and authentication."""

import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

import auth
import broker
from settings import Settings
from strategy import IST, Bar, Contract, OpeningRange

ICICI_CFG = Settings(
    active_broker="icici",
    icici_api_key="test_api_key",
    icici_api_secret="test_api_secret",
    icici_user_id="test_user",
    icici_password="test_password",
    icici_totp_secret="GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ",
)

ICICI_MANUAL = Settings(
    active_broker="icici",
    icici_api_key="test_api_key",
    icici_api_secret="test_api_secret",
    icici_session_token="manual-session-token",
)


class ICICIAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = mock.patch.object(auth, "ICICI_CACHE", Path(self.tmp.name) / "icici_token.json")
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_auto_enabled(self):
        self.assertTrue(auth.icici_auto_enabled(ICICI_CFG))
        self.assertTrue(auth.auto_enabled(ICICI_CFG))

        incomplete = Settings(active_broker="icici", icici_api_key="key", icici_user_id="u")
        self.assertFalse(auth.icici_auto_enabled(incomplete))
        self.assertFalse(auth.auto_enabled(incomplete))

    def test_cache_write_and_read(self):
        expiry = datetime.now(IST) + timedelta(hours=12)
        auth._write_icici_cache("test_api_key", "session_12345", expiry)
        cached = auth._read_icici_cache("test_api_key")
        self.assertIsNotNone(cached)
        token, exp = cached
        self.assertEqual(token, "session_12345")
        self.assertEqual(int(exp.timestamp()), int(expiry.timestamp()))

    def test_manual_mode_returns_session_token(self):
        self.assertEqual(auth.get_access_token(ICICI_MANUAL), "manual-session-token")

    def test_uses_valid_cached_token(self):
        expiry = datetime.now(IST) + timedelta(hours=8)
        auth._write_icici_cache("test_api_key", "cached_token_abc", expiry)
        self.assertEqual(auth.get_access_token(ICICI_CFG), "cached_token_abc")

    def test_login_with_explicit_session_token(self):
        tok, exp = auth.login_icici(ICICI_CFG, session_token="direct_pasted_token")
        self.assertEqual(tok, "direct_pasted_token")
        cached = auth._read_icici_cache("test_api_key")
        self.assertEqual(cached[0], "direct_pasted_token")

    def test_check_icici_access_mocked(self):
        with mock.patch("breeze_connect.BreezeConnect") as mock_breeze_cls:
            instance = mock_breeze_cls.return_value
            instance.user_id = "test_user"
            ok, status, msg, profile = auth.check_icici_access("valid_token", "key", "secret")
            self.assertTrue(ok)
            self.assertEqual(status, 200)
            self.assertEqual(profile["user_id"], "test_user")


class ICICIBrokerTests(unittest.TestCase):
    def test_get_broker_factory(self):
        with mock.patch.object(auth, "get_access_token", return_value="tok"):
            with mock.patch("breeze_connect.BreezeConnect"):
                b_icici = broker.get_broker(ICICI_CFG)
                self.assertIsInstance(b_icici, broker.ICICIDirect)

            with mock.patch.object(auth, "_read_cache", return_value=("tok", datetime.now())):
                dhan_cfg = Settings(client_id="12345", access_token="tok")
                b_dhan = broker.get_broker(dhan_cfg)
                self.assertIsInstance(b_dhan, broker.Dhan)

    def test_build_icici_universe(self):
        mock_master = [
            {"InstrumentName": "OPTSTK", "ExchangeCode": "RELIANCE", "ShortName": "RELIND", "LotSize": "250"},
            {"InstrumentName": "OPTSTK", "ExchangeCode": "TCS", "ShortName": "TCS", "LotSize": "175"},
            {"InstrumentName": "FUTSTK", "ExchangeCode": "RELIANCE", "ShortName": "RELIND", "LotSize": "250"},
            {"InstrumentName": "OPTIDX", "ExchangeCode": "NIFTY", "ShortName": "NIFTY", "LotSize": "50"},
        ]
        uni = broker.build_icici_universe(mock_master)
        self.assertEqual(len(uni), 2)
        symbols = [u["symbol"] for u in uni]
        self.assertEqual(symbols, ["RELIANCE", "TCS"])
        self.assertEqual(uni[0]["security_id"], "RELIND")
        self.assertEqual(uni[0]["lot_size"], "250")

    def test_icici_option_contracts(self):
        mock_master = [
            {
                "Token": "1001",
                "InstrumentName": "OPTSTK",
                "ExchangeCode": "RELIANCE",
                "ShortName": "RELIND",
                "ExpiryDate": "27-Oct-2026",
                "StrikePrice": "2500",
                "OptionType": "CE",
                "LotSize": "250",
            },
            {
                "Token": "1002",
                "InstrumentName": "OPTSTK",
                "ExchangeCode": "RELIANCE",
                "ShortName": "RELIND",
                "ExpiryDate": "27-Oct-2026",
                "StrikePrice": "2500",
                "OptionType": "PE",
                "LotSize": "250",
            },
            {
                "Token": "1003",
                "InstrumentName": "OPTSTK",
                "ExchangeCode": "RELIANCE",
                "ShortName": "RELIND",
                "ExpiryDate": "27-Oct-2026",
                "StrikePrice": "2600",
                "OptionType": "CE",
                "LotSize": "250",
            },
        ]
        # Query by ShortName ("RELIND")
        ces = broker.icici_option_contracts(mock_master, "RELIND", "CE")
        self.assertEqual(len(ces), 2)
        self.assertEqual(ces[0].strike, 2500.0)
        self.assertEqual(ces[0].expiry, date(2026, 10, 27))
        self.assertEqual(ces[0].lot_size, 250)
        self.assertIn("1001:RELIND:27-Oct-2026:2500.0:CE", ces[0].security_id)

        # Query by ExchangeCode ("RELIANCE")
        pes = broker.icici_option_contracts(mock_master, "RELIANCE", "PE")
        self.assertEqual(len(pes), 1)
        self.assertEqual(pes[0].strike, 2500.0)

    def test_icici_quotes_normalized(self):
        with mock.patch.object(auth, "get_access_token", return_value="tok"):
            with mock.patch("breeze_connect.BreezeConnect") as mock_breeze_cls:
                instance = mock_breeze_cls.return_value
                instance.get_quotes.return_value = {
                    "Status": 200,
                    "Success": [
                        {
                            "stock_code": "RELIND",
                            "exchange_code": "NSE",
                            "ltp": 2505.5,
                            "open": 2500.0,
                            "high": 2515.0,
                            "low": 2495.0,
                            "previous_close": 2480.0,
                            "total_quantity_traded": 1000000,
                            "datetime": "23-Oct-2026 10:15:00",
                        }
                    ]
                }
                b = broker.ICICIDirect(ICICI_CFG)
                q = b.quotes(["RELIND"])
                self.assertIn("RELIND", q)
                self.assertEqual(q["RELIND"]["last_price"], 2505.5)
                self.assertEqual(q["RELIND"]["ohlc"]["open"], 2500.0)
                self.assertEqual(q["RELIND"]["ohlc"]["close"], 2480.0)
                self.assertEqual(q["RELIND"]["volume"], 1000000)

    def test_icici_stock_opening_range(self):
        with mock.patch.object(auth, "get_access_token", return_value="tok"):
            with mock.patch("breeze_connect.BreezeConnect") as mock_breeze_cls:
                instance = mock_breeze_cls.return_value
                instance.get_historical_data_v2.return_value = {
                    "Status": 200,
                    "Success": [
                        {
                            "datetime": "2026-10-23 09:15:00",
                            "open": 2500.0,
                            "high": 2520.0,
                            "low": 2490.0,
                            "close": 2515.0,
                            "volume": 50000,
                        },
                        {
                            "datetime": "2026-10-23 09:20:00",
                            "open": 2515.0,
                            "high": 2525.0,
                            "low": 2510.0,
                            "close": 2520.0,
                            "volume": 30000,
                        },
                    ]
                }
                b = broker.ICICIDirect(ICICI_CFG)
                rng = b.stock_opening_range("RELIND", "2026-10-23")
                self.assertIsInstance(rng, OpeningRange)
                self.assertEqual(rng.open, 2500.0)
                self.assertEqual(rng.high, 2520.0)
                self.assertEqual(rng.low, 2490.0)
                self.assertEqual(rng.close, 2515.0)

    def test_icici_option_candles(self):
        with mock.patch.object(auth, "get_access_token", return_value="tok"):
            with mock.patch("breeze_connect.BreezeConnect") as mock_breeze_cls:
                instance = mock_breeze_cls.return_value
                instance.get_historical_data_v2.return_value = {
                    "Status": 200,
                    "Success": [
                        {
                            "datetime": "2026-10-23 09:20:00",
                            "open": 45.0,
                            "high": 52.0,
                            "low": 44.0,
                            "close": 50.0,
                            "volume": 1200,
                        },
                        {
                            "datetime": "2026-10-23 09:25:00",
                            "open": 50.0,
                            "high": 55.0,
                            "low": 48.0,
                            "close": 54.0,
                            "volume": 1500,
                        },
                    ]
                }
                b = broker.ICICIDirect(ICICI_CFG)
                sec_id = "1001:RELIND:27-Oct-2026:2500:CE"
                bars = b.option_candles(sec_id, "2026-10-23")
                self.assertEqual(len(bars), 2)
                self.assertIsInstance(bars[0], Bar)
                self.assertEqual(bars[0].open, 45.0)
                self.assertEqual(bars[0].close, 50.0)


if __name__ == "__main__":
    unittest.main()
