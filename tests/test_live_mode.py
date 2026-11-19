"""Unit tests for Live Trading Mode, Margin Checks, and Broker Order Execution."""

import io
import json
import tempfile
import unittest
import zipfile
from datetime import date, datetime
from pathlib import Path
from unittest import mock

# Mock urlopen before breeze_connect is imported
_zbuf = io.BytesIO()
with zipfile.ZipFile(_zbuf, "w") as _zf:
    pass
_zip_bytes = _zbuf.getvalue()

class _FakeResp:
    def read(self):
        return _zip_bytes

_p = mock.patch("urllib.request.urlopen", return_value=_FakeResp())
_p.start()
try:
    import breeze_connect
finally:
    _p.stop()

import auth
import broker
import run
import settings
from settings import Settings
from strategy import Bar, Contract, OpeningRange, Position


class TradingModeSettingsTests(unittest.TestCase):
    def test_settings_trading_mode_default_paper(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            with mock.patch("settings._read_env_file", return_value={}):
                cfg = settings.load_settings()
                self.assertEqual(cfg.trading_mode, "paper")

    def test_settings_trading_mode_live(self):
        with mock.patch.dict("os.environ", {"TRADING_MODE": "live"}):
            with mock.patch("settings._read_env_file", return_value={}):
                cfg = settings.load_settings()
                self.assertEqual(cfg.trading_mode, "live")

    def test_settings_trading_mode_case_insensitive(self):
        with mock.patch.dict("os.environ", {"MODE": "LIVE"}):
            with mock.patch("settings._read_env_file", return_value={}):
                cfg = settings.load_settings()
                self.assertEqual(cfg.trading_mode, "live")


class INDMoneyLiveMarginAndOrderTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Settings(
            active_broker="indmoney",
            trading_mode="live",
            indmoney_client_id="test_client",
            indmoney_access_token="test_token",
        )
        with mock.patch.object(auth, "get_access_token", return_value="test_token"):
            self.broker = broker.INDMoney(self.cfg)

    def test_get_funds_and_margin(self):
        fake_response = {
            "status": "Ok",
            "result": [
                {
                    "availableMargin": 75000.0,
                    "fnoDay": 0.0,
                    "fnoNrml": 50000.0,
                    "marginUsed": 10000.0,
                }
            ],
        }
        with mock.patch.object(self.broker, "_get", return_value=fake_response):
            funds = self.broker.get_funds()
            self.assertEqual(funds["available_margin"], 75000.0)
            self.assertEqual(funds["fno_margin"], 50000.0)
            self.assertEqual(self.broker.get_fno_margin(), 50000.0)

    def test_check_margin_available(self):
        contract = Contract("NFO_51011", "CE", date(2026, 11, 26), 2800.0, 250)
        # Required = 100 * 250 * 1 = 25,000
        with mock.patch.object(self.broker, "get_fno_margin", return_value=30000.0):
            avail, req, cur = self.broker.check_margin_available(contract, price=100.0, lots=1)
            self.assertTrue(avail)
            self.assertEqual(req, 25000.0)
            self.assertEqual(cur, 30000.0)

        with mock.patch.object(self.broker, "get_fno_margin", return_value=15000.0):
            avail, req, cur = self.broker.check_margin_available(contract, price=100.0, lots=1)
            self.assertFalse(avail)
            self.assertEqual(req, 25000.0)
            self.assertEqual(cur, 15000.0)

    def test_place_order(self):
        contract = Contract("NFO_51011", "CE", date(2026, 11, 26), 2800.0, 250)
        fake_res = {"status": "success", "order_id": "IND123456"}
        with mock.patch.object(self.broker, "_post", return_value=fake_res) as mock_post:
            res = self.broker.place_order("RELIANCE", contract, action="BUY", qty=250, price=105.0)
            self.assertEqual(res["order_id"], "IND123456")
            mock_post.assert_called_once()
            called_path, called_kwargs = mock_post.call_args[0][0], mock_post.call_args[1]
            self.assertEqual(called_path, "/order")
            payload = called_kwargs["payload"]
            self.assertEqual(payload["txn_type"], "BUY")
            self.assertEqual(payload["security_id"], "51011")
            self.assertEqual(payload["qty"], 250)
            self.assertEqual(payload["product"], "MARGIN")

    def test_get_trade_list(self):
        fake_trades = {
            "result": [
                {
                    "order_id": "IND123456",
                    "trade_id": "TRD999",
                    "security_id": "51011",
                    "trading_symbol": "RELIANCE26NOV2800CE",
                    "trade_price": 105.5,
                    "trade_qty": 250,
                    "txn_type": "BUY",
                    "status": "EXECUTED",
                }
            ]
        }
        with mock.patch.object(self.broker, "_get", return_value=fake_trades):
            trades = self.broker.get_trade_list("2026-11-09")
            self.assertEqual(len(trades), 1)
            self.assertEqual(trades[0]["order_id"], "IND123456")
            self.assertEqual(trades[0]["price"], 105.5)
            self.assertEqual(trades[0]["qty"], 250)


class ICICILiveMarginAndOrderTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Settings(
            active_broker="icici",
            trading_mode="live",
            icici_api_key="key",
            icici_api_secret="secret",
            icici_session_token="token",
        )
        with mock.patch.object(auth, "get_access_token", return_value="token"):
            with mock.patch("breeze_connect.BreezeConnect") as mock_breeze:
                self.broker = broker.ICICIDirect(self.cfg)
                self.mock_breeze = mock_breeze.return_value

    def test_get_funds_and_margin(self):
        self.mock_breeze.get_funds.return_value = {
            "Success": {
                "total_bank_balance": 100000.0,
                "allocated_fno": 60000.0,
                "block_by_trade_equity": 0.0,
            }
        }
        self.mock_breeze.get_margin.return_value = {
            "Success": {
                "cash_limit": 60000.0,
                "block_by_trade": 5000.0,
            }
        }
        funds = self.broker.get_funds()
        self.assertEqual(funds["fno_margin"], 60000.0)
        self.assertEqual(funds["available_margin"], 55000.0)

    def test_place_order(self):
        contract = Contract("51011:RELIANCE:26-Nov-2026:2800:CE", "CE", date(2026, 11, 26), 2800.0, 250)
        self.mock_breeze.place_order.return_value = {
            "Success": {"order_id": "ICI78901"}
        }
        res = self.broker.place_order("RELIANCE", contract, action="BUY", qty=250, price=105.0)
        self.assertEqual(res["order_id"], "ICI78901")
        self.mock_breeze.place_order.assert_called_once()


class LiveModeExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch_data = mock.patch.object(run, "DATA", Path(self.tmp.name))
        self.patch_data.start()

    def tearDown(self):
        self.patch_data.stop()
        self.tmp.cleanup()

    def test_live_mode_insufficient_margin_skips_trade(self):
        cfg = Settings(
            active_broker="indmoney",
            trading_mode="live",
            max_risk_per_trade=10000.0,
            enforce_risk_limit=False,
        )
        mock_broker = mock.MagicMock()
        # Option bars setup
        t0 = int(datetime(2026, 11, 9, 9, 15).timestamp())
        t1 = int(datetime(2026, 11, 9, 9, 20).timestamp())
        bars = [
            Bar(t0, open=100.0, high=105.0, low=95.0, close=100.0, volume=1000),
            Bar(t1, open=100.0, high=110.0, low=98.0, close=108.0, volume=2000),  # Breakout close > 105
        ]
        mock_broker.option_candles.return_value = bars

        # Margin: Required is 108 * 250 * 1 = 27,000; Available is 5,000 -> insufficient!
        mock_broker.check_margin_available.return_value = (False, 27000.0, 5000.0)

        contract = Contract("NFO_51011", "CE", date(2026, 11, 26), 2800.0, 250)
        movers = [{"symbol": "RELIANCE", "direction": "CALL", "security_id": "2885", "close": 2800.0}]
        picks = {"RELIANCE": contract}

        with mock.patch("run.today", return_value="2026-11-09"):
            positions = run.simulate_once(cfg, mock_broker, movers, picks)

        # Trade must be skipped, no order placed
        self.assertEqual(len(positions), 0)
        mock_broker.place_order.assert_not_called()

        skipped_file = Path(self.tmp.name) / "2026-11-09" / "skipped.csv"
        self.assertTrue(skipped_file.exists())
        content = skipped_file.read_text()
        self.assertIn("insufficient F&O margin", content)

    def test_live_mode_sufficient_margin_takes_trade_and_checks_trade_list(self):
        cfg = Settings(
            active_broker="indmoney",
            trading_mode="live",
            max_risk_per_trade=10000.0,
            enforce_risk_limit=False,
        )
        mock_broker = mock.MagicMock()
        t0 = int(datetime(2026, 11, 9, 9, 15).timestamp())
        t1 = int(datetime(2026, 11, 9, 9, 20).timestamp())
        bars = [
            Bar(t0, open=100.0, high=105.0, low=95.0, close=100.0, volume=1000),
            Bar(t1, open=100.0, high=110.0, low=98.0, close=108.0, volume=2000),
        ]
        mock_broker.option_candles.return_value = bars

        # Margin: Sufficient
        mock_broker.check_margin_available.return_value = (True, 27000.0, 50000.0)
        mock_broker.place_order.return_value = {"order_id": "ORD9988", "status": "PLACED"}
        mock_broker.get_trade_list.return_value = [
            {
                "order_id": "ORD9988",
                "trade_id": "TRD123",
                "price": 108.5,
                "qty": 250,
                "action": "BUY",
                "symbol": "RELIANCE",
            }
        ]

        contract = Contract("NFO_51011", "CE", date(2026, 11, 26), 2800.0, 250)
        movers = [{"symbol": "RELIANCE", "direction": "CALL", "security_id": "2885", "close": 2800.0}]
        picks = {"RELIANCE": contract}

        with mock.patch("run.today", return_value="2026-11-09"):
            positions = run.simulate_once(cfg, mock_broker, movers, picks)

        self.assertEqual(len(positions), 1)
        pos = positions[0]
        self.assertEqual(pos.mode, "live")
        self.assertEqual(pos.order_id, "ORD9988")
        self.assertEqual(pos.entry, 108.5)  # updated from broker trade list
        mock_broker.place_order.assert_called_once_with(
            "RELIANCE", contract, action="BUY", qty=250, price=108.0, order_type="MARKET"
        )
        mock_broker.get_trade_list.assert_called_once()

    def test_paper_mode_does_not_place_real_order(self):
        cfg = Settings(
            active_broker="indmoney",
            trading_mode="paper",
            max_risk_per_trade=10000.0,
            enforce_risk_limit=False,
        )
        mock_broker = mock.MagicMock()
        t0 = int(datetime(2026, 11, 9, 9, 15).timestamp())
        t1 = int(datetime(2026, 11, 9, 9, 20).timestamp())
        bars = [
            Bar(t0, open=100.0, high=105.0, low=95.0, close=100.0, volume=1000),
            Bar(t1, open=100.0, high=110.0, low=98.0, close=108.0, volume=2000),
        ]
        mock_broker.option_candles.return_value = bars

        contract = Contract("NFO_51011", "CE", date(2026, 11, 26), 2800.0, 250)
        movers = [{"symbol": "RELIANCE", "direction": "CALL", "security_id": "2885", "close": 2800.0}]
        picks = {"RELIANCE": contract}

        with mock.patch("run.today", return_value="2026-11-09"):
            positions = run.simulate_once(cfg, mock_broker, movers, picks)

        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0].mode, "paper")
        mock_broker.check_margin_available.assert_not_called()
        mock_broker.place_order.assert_not_called()

    def test_live_session_tick_stop_loss_places_sell_order(self):
        cfg = Settings(active_broker="indmoney", trading_mode="live")
        mock_broker = mock.MagicMock()
        mock_broker.place_order.return_value = {"order_id": "EXIT123", "status": "PLACED"}
        mock_broker.get_trade_list.return_value = [
            {"order_id": "EXIT123", "action": "SELL", "price": 94.0, "symbol": "RELIANCE"}
        ]

        contract = Contract("NFO_51011", "CE", date(2026, 11, 26), 2800.0, 250)
        pos = Position(
            symbol="RELIANCE",
            direction="CALL",
            contract=contract,
            lots=1,
            entry_ts=1700000000,
            entry=100.0,
            initial_stop=95.0,
            stop=95.0,
            risk=5.0,
            mode="live",
            order_id="BUY123",
            last_price=100.0,
        )

        session = run.LiveTradeSession(cfg, mock_broker, [], {"RELIANCE": contract}, [pos])
        # Incoming tick hits stop loss (price 94 <= stop 95)
        with mock.patch("run.today", return_value="2026-11-09"):
            session.on_tick("NFO_51011", price=94.0, ts=1700000060)

        self.assertEqual(pos.status, "EXIT_STOP")
        self.assertEqual(pos.exit_order_id, "EXIT123")
        self.assertEqual(pos.exit_price, 94.0)
        mock_broker.place_order.assert_called_once_with(
            "RELIANCE", contract, action="SELL", qty=250, price=94.0, order_type="MARKET"
        )


class PositionStatusAndRefreshTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch_data = mock.patch.object(run, "DATA", Path(self.tmp.name))
        self.patch_data.start()
        self.contract = Contract("NFO_51011", "CE", date(2026, 11, 26), 2800.0, 250)

    def tearDown(self):
        self.patch_data.stop()
        self.tmp.cleanup()

    def test_is_script_open_in_memory(self):
        pos_open = Position("RELIANCE", "CALL", self.contract, 1, 1000, 100.0, 95.0, 95.0, 5.0, status="OPEN")
        pos_closed = Position("TCS", "CALL", self.contract, 1, 1000, 100.0, 95.0, 95.0, 5.0, status="EXIT_STOP")

        self.assertTrue(run.is_script_open("RELIANCE", self.contract, [pos_open]))
        self.assertFalse(run.is_script_open("TCS", self.contract, [pos_closed]))
        self.assertFalse(run.is_script_open("INFY", self.contract, [pos_open]))

    def test_is_script_open_in_trades_csv(self):
        day = "2026-11-09"
        trades_dir = Path(self.tmp.name) / day
        trades_dir.mkdir(parents=True, exist_ok=True)
        trades_file = trades_dir / "trades.csv"

        with mock.patch("run.today", return_value=day):
            # No file yet
            self.assertFalse(run.is_script_open("RELIANCE", self.contract))

            # Write trades.csv with OPEN position
            run.write_csv(trades_file, [{
                "symbol": "RELIANCE", "direction": "CALL", "option": "CE", "strike": 2800.0,
                "expiry": "2026-11-26", "lots": 1, "qty": 250, "capital_used": 25000.0,
                "entry_time": "09:20:00", "entry": 100.0, "initial_stop": 95.0, "stop_now": 95.0,
                "exit_time": "", "exit_or_last": 102.0, "pnl": 500.0, "r_multiple": 0.4,
                "trailing": "False", "status": "OPEN", "mode": "live", "order_id": "ORD1",
                "exit_order_id": ""
            }])
            self.assertTrue(run.is_script_open("RELIANCE", self.contract))
            self.assertFalse(run.is_script_open("INFY", self.contract))

            # Update to EXIT_STOP
            run.write_csv(trades_file, [{
                "symbol": "RELIANCE", "direction": "CALL", "option": "CE", "strike": 2800.0,
                "expiry": "2026-11-26", "lots": 1, "qty": 250, "capital_used": 25000.0,
                "entry_time": "09:20:00", "entry": 100.0, "initial_stop": 95.0, "stop_now": 95.0,
                "exit_time": "10:00:00", "exit_or_last": 95.0, "pnl": -1250.0, "r_multiple": -1.0,
                "trailing": "False", "status": "EXIT_STOP", "mode": "live", "order_id": "ORD1",
                "exit_order_id": "EXIT1"
            }])
            self.assertFalse(run.is_script_open("RELIANCE", self.contract))

    def test_is_script_open_broker_positions(self):
        mock_broker = mock.MagicMock()
        mock_broker.get_positions.return_value = [
            {"security_id": "51011", "symbol": "RELIANCE26NOV2800CE", "net_qty": 250}
        ]

        infy_contract = Contract("NFO_52000", "CE", date(2026, 11, 26), 1800.0, 400)
        with mock.patch("run.today", return_value="2026-11-09"):
            self.assertTrue(run.is_script_open("RELIANCE", self.contract, broker=mock_broker))
            self.assertFalse(run.is_script_open("INFY", infy_contract, broker=mock_broker))

            # Broker reports 0 net quantity (flat/closed)
            mock_broker.get_positions.return_value = [
                {"security_id": "51011", "symbol": "RELIANCE26NOV2800CE", "net_qty": 0}
            ]
            self.assertFalse(run.is_script_open("RELIANCE", self.contract, broker=mock_broker))

    def test_simulate_once_skips_script_already_in_open_position(self):
        cfg = Settings(active_broker="indmoney", trading_mode="live")
        mock_broker = mock.MagicMock()
        mock_broker.check_margin_available.return_value = (True, 25000.0, 50000.0)

        movers = [{"symbol": "RELIANCE", "direction": "CALL", "security_id": "2885", "close": 2800.0}]
        picks = {"RELIANCE": self.contract}

        pos_existing = Position("RELIANCE", "CALL", self.contract, 1, 1000, 100.0, 95.0, 95.0, 5.0, status="OPEN")

        with mock.patch("run.today", return_value="2026-11-09"):
            # Provide existing open position
            positions = run.simulate_once(cfg, mock_broker, movers, picks, existing_positions=[pos_existing])

            # Must NOT place another order
            mock_broker.place_order.assert_not_called()
            mock_broker.option_candles.assert_not_called()
            # Existing position preserved
            self.assertEqual(len(positions), 1)
            self.assertEqual(positions[0], pos_existing)

    def test_10s_status_check_reports_open_position_and_syncs_broker(self):
        cfg = Settings(active_broker="indmoney", trading_mode="live")
        mock_broker = mock.MagicMock()
        mock_broker.get_order_list.return_value = [
            {"order_id": "ORD99", "status": "EXECUTED", "average_price": 105.0}
        ]
        mock_broker.get_positions.return_value = [
            {"security_id": "51011", "symbol": "RELIANCE26NOV2800CE", "net_qty": 250}
        ]

        pos = Position(
            symbol="RELIANCE", direction="CALL", contract=self.contract, lots=1,
            entry_ts=1000, entry=100.0, initial_stop=95.0, stop=95.0, risk=5.0,
            status="OPEN", mode="live", order_id="ORD99", last_price=108.0
        )
        movers = [{"symbol": "RELIANCE", "direction": "CALL", "security_id": "2885", "close": 2800.0}]
        picks = {"RELIANCE": self.contract}

        with mock.patch("run.today", return_value="2026-11-09"):
            session = run.LiveTradeSession(cfg, mock_broker, movers, picks, [pos])
            report = session.check_status_10s()

            self.assertIn("RELIANCE", report)
            self.assertIn("ORDER_EXECUTED", report["RELIANCE"]["trade_status"])
            self.assertIn("OPEN", report["RELIANCE"]["position_status"])
            self.assertEqual(pos.entry, 105.0)  # synced from broker order avg price

    def test_10s_status_check_reports_waiting_breakout_when_no_position(self):
        cfg = Settings(active_broker="indmoney", trading_mode="paper")
        mock_broker = mock.MagicMock()
        movers = [{"symbol": "TCS", "direction": "CALL", "security_id": "11536", "close": 3500.0}]
        picks = {"TCS": self.contract}

        with mock.patch("run.today", return_value="2026-11-09"):
            session = run.LiveTradeSession(cfg, mock_broker, movers, picks, [])
            report = session.check_status_10s()

            self.assertIn("TCS", report)
            self.assertEqual(report["TCS"]["trade_status"], "WAITING_BREAKOUT")
            self.assertEqual(report["TCS"]["position_status"], "NONE")

    def test_30s_refresh_does_not_take_script_again_when_in_open_position(self):
        cfg = Settings(active_broker="indmoney", trading_mode="live")
        mock_broker = mock.MagicMock()

        pos = Position(
            symbol="RELIANCE", direction="CALL", contract=self.contract, lots=1,
            entry_ts=1000, entry=100.0, initial_stop=95.0, stop=95.0, risk=5.0,
            status="OPEN", mode="live", order_id="ORD99", last_price=108.0
        )
        movers = [{"symbol": "RELIANCE", "direction": "CALL", "security_id": "2885", "close": 2800.0}]
        picks = {"RELIANCE": self.contract}

        with mock.patch("run.today", return_value="2026-11-09"):
            session = run.LiveTradeSession(cfg, mock_broker, movers, picks, [pos])
            new_trades = session.refresh_30s()

            # Script is in open position -> MUST NOT take that script again!
            self.assertEqual(len(new_trades), 0)
            mock_broker.place_order.assert_not_called()
            mock_broker.option_candles.assert_not_called()
            self.assertEqual(len(session.positions), 1)

    def test_30s_refresh_takes_trade_when_new_confirmation_bar_arrives(self):
        cfg = Settings(active_broker="indmoney", trading_mode="live")
        mock_broker = mock.MagicMock()
        t0 = int(datetime(2026, 11, 9, 9, 15).timestamp())
        t1 = int(datetime(2026, 11, 9, 9, 20).timestamp())
        bars = [
            Bar(t0, open=100.0, high=105.0, low=95.0, close=100.0, volume=1000),
            Bar(t1, open=100.0, high=110.0, low=98.0, close=108.0, volume=2000),  # Breakout confirmation
        ]
        mock_broker.option_candles.return_value = bars
        mock_broker.check_margin_available.return_value = (True, 27000.0, 50000.0)
        mock_broker.place_order.return_value = {"order_id": "ORD101", "status": "PLACED"}
        mock_broker.get_trade_list.return_value = [
            {"order_id": "ORD101", "trade_id": "TRD101", "price": 108.0, "qty": 250, "action": "BUY", "symbol": "RELIANCE"}
        ]

        movers = [{"symbol": "RELIANCE", "direction": "CALL", "security_id": "2885", "close": 2800.0}]
        picks = {"RELIANCE": self.contract}

        with mock.patch("run.today", return_value="2026-11-09"):
            # Start session with NO open positions
            session = run.LiveTradeSession(cfg, mock_broker, movers, picks, [])
            self.assertEqual(len(session.positions), 0)

            # 30-second refresh runs
            new_trades = session.refresh_30s()

            self.assertEqual(len(new_trades), 1)
            self.assertEqual(len(session.positions), 1)
            pos = session.positions[0]
            self.assertEqual(pos.symbol, "RELIANCE")
            self.assertEqual(pos.status, "OPEN")
            self.assertEqual(pos.order_id, "ORD101")
            mock_broker.place_order.assert_called_once()

            # Second 30-second refresh runs: now that it is in OPEN position, MUST NOT take that script again!
            mock_broker.place_order.reset_mock()
            new_trades_second = session.refresh_30s()
            self.assertEqual(len(new_trades_second), 0)
            mock_broker.place_order.assert_not_called()
            self.assertEqual(len(session.positions), 1)

    def test_refresh_30s_with_spot_candles_confirmation(self):
        cfg = Settings(active_broker="indmoney", trading_mode="paper")
        mock_broker = mock.MagicMock()
        t0 = int(datetime(2026, 11, 9, 9, 15).timestamp())
        t1 = int(datetime(2026, 11, 9, 9, 20).timestamp())

        # Spot stock candles: 09:15 range (high=2520), 09:20 breakout close=2525
        spot_bars = [
            Bar(t0, open=2500.0, high=2520.0, low=2490.0, close=2515.0, volume=10000),
            Bar(t1, open=2515.0, high=2530.0, low=2510.0, close=2525.0, volume=20000),
        ]
        mock_broker.stock_candles.return_value = spot_bars

        # Option candles matching confirmation
        opt_bars = [
            Bar(t0, open=50.0, high=55.0, low=45.0, close=52.0, volume=1000),
            Bar(t1, open=52.0, high=65.0, low=50.0, close=62.0, volume=2000),
        ]
        mock_broker.option_candles.return_value = opt_bars

        movers = [{"symbol": "RELIANCE", "direction": "CALL", "security_id": "2885", "close": 2515.0}]
        picks = {"RELIANCE": self.contract}

        with mock.patch("run.today", return_value="2026-11-09"):
            session = run.LiveTradeSession(cfg, mock_broker, movers, picks, [])
            new_trades = session.refresh_30s()
            self.assertEqual(len(new_trades), 1)
            pos = new_trades[0]
            self.assertEqual(pos.symbol, "RELIANCE")
            self.assertEqual(pos.entry, 62.0)
            self.assertEqual(pos.initial_stop, 45.0)


if __name__ == "__main__":
    unittest.main()
