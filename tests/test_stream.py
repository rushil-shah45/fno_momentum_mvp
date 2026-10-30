"""Unit tests for WebSocket live streaming, candle aggregation, and real-time position management."""

import json
import tempfile
import time
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock

import broker
import run
from settings import Settings
from strategy import IST, Bar, CandleAggregator, Contract, Position


class INDMoneyLiveStreamTests(unittest.TestCase):
    def test_build_subscription_formats_instruments(self):
        stream = broker.INDMoneyLiveStream("test_token")
        sub = stream.build_subscription(["NFO_56598", "2885", "NSE:1234"])
        self.assertEqual(sub["action"], "subscribe")
        self.assertEqual(sub["mode"], "ltp")
        self.assertIn("NFO:56598", sub["instruments"])
        self.assertIn("NSE:2885", sub["instruments"])
        self.assertIn("NSE:1234", sub["instruments"])

    def test_parse_message_various_payloads(self):
        stream = broker.INDMoneyLiveStream("test_token")

        # Flat payload
        msg1 = json.dumps({"instrument": "NFO:56598", "ltp": 125.50, "timestamp": 1793100000})
        res1 = stream._parse_message(msg1)
        self.assertEqual(len(res1), 1)
        self.assertEqual(res1[0], ("NFO:56598", 125.50, 1793100000))

        # Nested data list with millisecond timestamp
        msg2 = json.dumps({
            "data": [
                {"scrip_code": "NFO_56598", "last_price": "128.00", "timestamp": 1793100000000},
                {"instrument": "NSE:2885", "ltp": 2500.0, "timestamp": 1793100000},
            ]
        })
        res2 = stream._parse_message(msg2)
        self.assertEqual(len(res2), 2)
        self.assertEqual(res2[0], ("NFO_56598", 128.00, 1793100000))
        self.assertEqual(res2[1], ("NSE:2885", 2500.0, 1793100000))

    def test_on_message_triggers_callback(self):
        ticks = []
        stream = broker.INDMoneyLiveStream("token", on_tick=lambda s, p, t: ticks.append((s, p, t)))
        msg = json.dumps({"instrument": "NFO:56598", "ltp": 130.0, "timestamp": 1000})
        stream._on_message(None, msg)
        self.assertEqual(ticks, [("NFO:56598", 130.0, 1000)])


class ICICILiveStreamTests(unittest.TestCase):
    def test_icici_token_mapping_and_tick_handling(self):
        contract = Contract("56598:RELIANCE:27-Nov-2026:1400:CE", "CE", date(2026, 11, 27), 1400.0, 250)
        ticks = []
        mock_breeze = mock.MagicMock()

        stream = broker.ICICILiveStream(mock_breeze, [contract], on_tick=lambda s, p, t: ticks.append((s, p, t)))

        # Simulate tick payload from Breeze socket
        tick_data = {
            "symbol": "4.1!56598",
            "last": "145.50",
            "stock_name": "RELIANCE",
        }
        stream._on_ticks_handler(tick_data)

        self.assertEqual(len(ticks), 1)
        self.assertEqual(ticks[0][0], "56598:RELIANCE:27-Nov-2026:1400:CE")
        self.assertEqual(ticks[0][1], 145.50)

    def test_icici_stream_start_subscribes_feeds(self):
        contract = Contract("56598:RELIANCE:27-Nov-2026:1400:CE", "CE", date(2026, 11, 27), 1400.0, 250)
        mock_breeze = mock.MagicMock()

        stream = broker.ICICILiveStream(mock_breeze, [contract])
        stream.start()

        mock_breeze.ws_connect.assert_called_once()
        mock_breeze.subscribe_feeds.assert_called_once_with(
            exchange_code="NFO",
            stock_code="RELIANCE",
            product_type="options",
            expiry_date="27-Nov-2026",
            strike_price="1400",
            right="call",
            get_exchange_quotes=True,
        )


class CandleAggregatorTests(unittest.TestCase):
    def test_aggregates_ticks_and_emits_on_bucket_boundary(self):
        agg = CandleAggregator(interval_seconds=300)

        # First tick at 09:20:10 (bucket 09:20 = 1793100000)
        base_ts = 1793100000
        b1 = agg.add_tick("X", 100.0, base_ts + 10)
        self.assertIsNone(b1)  # Bucket in progress

        # Subsequent ticks in same bucket
        agg.add_tick("X", 105.0, base_ts + 50)
        agg.add_tick("X", 98.0, base_ts + 100)
        agg.add_tick("X", 102.0, base_ts + 200)

        in_prog = agg.get_bar("X")
        self.assertIsNotNone(in_prog)
        self.assertEqual((in_prog.open, in_prog.high, in_prog.low, in_prog.close),
                         (100.0, 105.0, 98.0, 102.0))

        # Tick at 09:25:05 (bucket rolls to 1793100300)
        completed = agg.add_tick("X", 103.0, base_ts + 305)
        self.assertIsNotNone(completed)
        self.assertEqual(completed.ts, base_ts)
        self.assertEqual((completed.open, completed.high, completed.low, completed.close),
                         (100.0, 105.0, 98.0, 102.0))

        # New in-progress bucket
        new_in_prog = agg.get_bar("X")
        self.assertEqual(new_in_prog.ts, base_ts + 300)
        self.assertEqual(new_in_prog.close, 103.0)


class PositionOnTickTests(unittest.TestCase):
    def test_instant_stop_loss_trigger(self):
        contract = Contract("C1", "CE", date(2026, 11, 27), 100.0, 100)
        pos = Position(
            symbol="TEST",
            direction="CALL",
            contract=contract,
            lots=1,
            entry_ts=1000,
            entry=100.0,
            initial_stop=90.0,
            stop=90.0,
            risk=10.0,
            last_price=100.0,
        )

        # Price ticks above stop
        closed = pos.on_tick(95.0, 1010)
        self.assertFalse(closed)
        self.assertEqual(pos.status, "OPEN")
        self.assertEqual(pos.last_price, 95.0)

        # Price ticks at or below stop
        closed = pos.on_tick(89.5, 1020)
        self.assertTrue(closed)
        self.assertEqual(pos.status, "EXIT_STOP")
        self.assertEqual(pos.exit_price, 90.0)
        self.assertEqual(pos.exit_ts, 1020)

        # Further ticks do nothing
        closed2 = pos.on_tick(80.0, 1030)
        self.assertFalse(closed2)


class LiveTradeSessionTests(unittest.TestCase):
    def test_session_on_tick_triggers_stop_and_persists(self):
        cfg = Settings(active_broker="indmoney", max_daily_loss=10000)
        contract = Contract("NFO_56598", "CE", date(2026, 11, 27), 100.0, 50)
        pos = Position("RELIANCE", "CALL", contract, 1, 1000, 100.0, 90.0, 90.0, 10.0, last_price=100.0)

        movers = [{"symbol": "RELIANCE", "direction": "CALL", "security_id": "2885", "close": 100.0}]
        picks = {"RELIANCE": contract}

        with tempfile.TemporaryDirectory() as tmp:
            tmp_data = Path(tmp)
            with mock.patch.object(run, "DATA", tmp_data):
                session = run.LiveTradeSession(cfg, mock.MagicMock(), movers, picks, [pos])

                # Tick above stop
                session.on_tick("NFO:56598", 98.0, 1010)
                self.assertEqual(pos.status, "OPEN")
                self.assertEqual(pos.last_price, 98.0)

                # Tick hitting stop
                session.on_tick("NFO:56598", 89.0, 1020)
                self.assertEqual(pos.status, "EXIT_STOP")

                trades_csv = tmp_data / run.today() / "trades.csv"
                self.assertTrue(trades_csv.exists())
                content = trades_csv.read_text(encoding="utf-8")
                self.assertIn("EXIT_STOP", content)
                self.assertIn("RELIANCE", content)


if __name__ == "__main__":
    unittest.main()
