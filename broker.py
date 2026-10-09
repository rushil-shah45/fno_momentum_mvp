"""Read-only Dhan market-data client. There is deliberately NO order code here."""

from __future__ import annotations

import csv
import io
import time
from datetime import date, datetime
from pathlib import Path

import requests

import auth
from settings import ROOT, Settings
from strategy import Bar, Contract, IST, OpeningRange, ENTRY_TIME, MARKET_OPEN, clock

BASE_URL = "https://api.dhan.co/v2"
MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"
HISTORY_PAUSE = 1.05   # Dhan rate-limits the historical-data endpoint


class Dhan:
    def __init__(self, cfg: Settings):
        has_login = auth.auto_enabled(cfg) or (cfg.access_token and
                                               not cfg.access_token.startswith("replace_with"))
        if not cfg.client_id or cfg.client_id.startswith("replace_with") or not has_login:
            raise SystemExit("Set DHAN_CLIENT_ID plus either DHAN_PIN + DHAN_TOTP_SECRET "
                             "(automatic) or DHAN_ACCESS_TOKEN (manual) in .env.")
        self.cfg = cfg
        self.http = requests.Session()
        self.http.trust_env = False        # ignore stale system proxy settings
        self.http.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
            "access-token": auth.get_access_token(cfg),
            "client-id": cfg.client_id,
        })
        self._last_history_call = 0.0

    def _post(self, path: str, payload: dict) -> dict:
        renewed = False
        for attempt in range(3):
            resp = self.http.post(f"{BASE_URL}{path}", json=payload, timeout=30)
            if resp.status_code == 401 and auth.auto_enabled(self.cfg) and not renewed:
                renewed = True                     # token expired mid-run: mint a new one once
                self.http.headers["access-token"] = auth.get_access_token(self.cfg, force=True)
                continue
            if resp.status_code == 429:               # rate limited: back off, retry
                time.sleep(2 * (attempt + 1))
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError("Dhan rate limit: too many retries")

    # ----------------------------------------------------------------- quotes
    def quotes(self, security_ids: list[str]) -> dict[str, dict]:
        """Live snapshot for many NSE stocks in one request."""
        data = self._post("/marketfeed/quote", {"NSE_EQ": [int(i) for i in security_ids]})
        return data.get("data", {}).get("NSE_EQ", {})

    # ---------------------------------------------------------------- candles
    def _candles(self, security_id: str, segment: str, instrument: str,
                 day: str, start: str, end: str) -> list[Bar]:
        wait = HISTORY_PAUSE - (time.monotonic() - self._last_history_call)
        if wait > 0:
            time.sleep(wait)
        body = {"securityId": security_id, "exchangeSegment": segment,
                "instrument": instrument, "interval": "5", "oi": False,
                "fromDate": f"{day} {start}", "toDate": f"{day} {end}"}
        data = self._post("/charts/intraday", body)
        self._last_history_call = time.monotonic()
        d = data.get("data", data)
        n = min(len(d.get(k, [])) for k in ("timestamp", "open", "high", "low", "close"))
        vol = d.get("volume", [])
        return [Bar(int(d["timestamp"][i]), float(d["open"][i]), float(d["high"][i]),
                    float(d["low"][i]), float(d["close"][i]),
                    float(vol[i]) if i < len(vol) else 0.0) for i in range(n)]

    def option_candles(self, security_id: str, day: str) -> list[Bar]:
        return self._candles(security_id, "NSE_FNO", "OPTSTK", day, "09:15:00", "15:01:00")

    def stock_opening_range(self, security_id: str, day: str) -> OpeningRange:
        """Fetch the 09:15 5-min candle as the opening range (v1.1: 5m TF)."""
        bars = [b for b in self._candles(security_id, "NSE_EQ", "EQUITY", day,
                                         "09:15:00", "09:20:59")
                if clock(b.ts) == MARKET_OPEN]
        if not bars:
            raise RuntimeError("09:15 5-min candle not available")
        b = bars[0]
        return OpeningRange(b.open, b.high, b.low, b.close, b.volume)


# ------------------------------------------------------- instrument master (public)
def load_master() -> list[dict[str, str]]:
    """Download Dhan's instrument master once per day and cache it on disk."""
    cache = ROOT / "data" / f"instrument_master_{datetime.now(IST):%Y-%m-%d}.csv"
    if not cache.exists():
        cache.parent.mkdir(exist_ok=True)
        resp = requests.Session()
        resp.trust_env = False
        text = resp.get(MASTER_URL, timeout=60).content.decode("utf-8-sig")
        cache.write_text(text, encoding="utf-8")
    with cache.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def build_universe(master: list[dict[str, str]]) -> list[dict[str, str]]:
    """NSE stocks that have stock options: symbol, security id, lot size."""
    equities = {r["SECURITY_ID"] for r in master
                if r.get("EXCH_ID") == "NSE" and r.get("SEGMENT") == "E"
                and r.get("INSTRUMENT") == "EQUITY"}
    found: dict[str, dict[str, str]] = {}
    for r in master:
        if r.get("EXCH_ID") == "NSE" and r.get("SEGMENT") == "D" and r.get("INSTRUMENT") == "OPTSTK":
            sym, sid = r.get("UNDERLYING_SYMBOL", ""), r.get("UNDERLYING_SECURITY_ID", "")
            if sym and sid in equities and sym not in found:
                found[sym] = {"symbol": sym, "security_id": sid, "lot_size": r.get("LOT_SIZE", "")}
    return sorted(found.values(), key=lambda x: x["symbol"])


def option_contracts(master: list[dict[str, str]], underlying_id: str,
                     option_type: str) -> list[Contract]:
    out = []
    for r in master:
        if (r.get("EXCH_ID") == "NSE" and r.get("SEGMENT") == "D"
                and r.get("INSTRUMENT") == "OPTSTK"
                and r.get("UNDERLYING_SECURITY_ID") == underlying_id
                and r.get("OPTION_TYPE") == option_type):
            try:
                out.append(Contract(r["SECURITY_ID"], option_type,
                                    date.fromisoformat(r["SM_EXPIRY_DATE"][:10]),
                                    float(r["STRIKE_PRICE"]),
                                    int(float(r["LOT_SIZE"]))))
            except (KeyError, ValueError):
                continue
    return out
