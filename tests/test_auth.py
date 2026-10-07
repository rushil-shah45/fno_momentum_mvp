"""Offline tests for automatic token handling."""

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import auth
from settings import Settings
from strategy import IST

AUTO = Settings(client_id="1", pin="123456", totp_secret="GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ")
MANUAL = Settings(client_id="1", access_token="manual-token")


class TotpTests(unittest.TestCase):
    def test_rfc6238_vector(self):
        # RFC 6238 appendix B: secret "12345678901234567890", T=59s, SHA-1 -> 94287082
        self.assertEqual(auth.totp_now(AUTO.totp_secret, at=59, digits=8), "94287082")
        self.assertEqual(auth.totp_now(AUTO.totp_secret, at=59), "287082")

    def test_tolerates_spaces_and_lowercase(self):
        spaced = "gezd gnbv gy3t qojq gezd gnbv gy3t qojq"
        self.assertEqual(auth.totp_now(spaced, at=59), "287082")


class TokenTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = mock.patch.object(auth, "CACHE", Path(self.tmp.name) / "token.json")
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def fresh(self, hours):
        return ("new-token", datetime.now(IST) + timedelta(hours=hours))

    def test_manual_mode_returns_static_token(self):
        self.assertEqual(auth.get_access_token(MANUAL), "manual-token")

    def test_generates_once_then_uses_cache(self):
        with mock.patch.object(auth, "generate_token", return_value=self.fresh(24)) as gen:
            self.assertEqual(auth.get_access_token(AUTO), "new-token")
            self.assertEqual(auth.get_access_token(AUTO), "new-token")
            self.assertEqual(gen.call_count, 1)

    def test_regenerates_when_less_than_8h_left(self):
        with mock.patch.object(auth, "generate_token", return_value=self.fresh(5)) as gen:
            auth.get_access_token(AUTO)
            auth.get_access_token(AUTO)
            self.assertEqual(gen.call_count, 2)

    def test_force_regenerates(self):
        with mock.patch.object(auth, "generate_token", return_value=self.fresh(24)) as gen:
            auth.get_access_token(AUTO)
            auth.get_access_token(AUTO, force=True)
            self.assertEqual(gen.call_count, 2)

    def test_falls_back_to_unexpired_cache_if_generation_fails(self):
        with mock.patch.object(auth, "generate_token", return_value=self.fresh(5)):
            auth.get_access_token(AUTO)                     # cached, 5h left
        with mock.patch.object(auth, "generate_token", side_effect=RuntimeError("down")):
            self.assertEqual(auth.get_access_token(AUTO), "new-token")

    def test_raises_if_generation_fails_and_no_cache(self):
        with mock.patch.object(auth, "generate_token", side_effect=RuntimeError("down")):
            with self.assertRaises(RuntimeError):
                auth.get_access_token(AUTO)

    def test_other_client_cache_is_ignored(self):
        with mock.patch.object(auth, "generate_token", return_value=self.fresh(24)):
            auth.get_access_token(AUTO)
        other = Settings(client_id="2", pin="1", totp_secret=AUTO.totp_secret)
        with mock.patch.object(auth, "generate_token", return_value=("other", datetime.now(IST) + timedelta(hours=24))):
            self.assertEqual(auth.get_access_token(other), "other")


class BrokerRenewTests(unittest.TestCase):
    def test_401_triggers_one_renewal_then_succeeds(self):
        import broker

        class Resp:
            def __init__(self, code, body=None):
                self.status_code, self._body = code, body or {}
            def raise_for_status(self):
                if self.status_code >= 400:
                    raise RuntimeError(self.status_code)
            def json(self):
                return self._body

        tokens = iter(["old", "renewed"])
        with mock.patch.object(auth, "get_access_token", lambda cfg, force=False: next(tokens)):
            dhan = broker.Dhan(AUTO)
            sent = []

            def fake_post(url, json=None, timeout=None):
                sent.append(dhan.http.headers["access-token"])
                return Resp(401) if len(sent) == 1 else Resp(200, {"data": {"NSE_EQ": {}}})

            dhan.http.post = fake_post
            self.assertEqual(dhan.quotes(["1"]), {})
        self.assertEqual(sent, ["old", "renewed"])


if __name__ == "__main__":
    unittest.main()
