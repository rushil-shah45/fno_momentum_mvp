"""Read-only market-data client for Dhan and ICICI Direct. There is deliberately NO order code here."""

from __future__ import annotations

import csv
import io
import time
import zipfile
from datetime import date, datetime
from pathlib import Path

import requests

import auth
from settings import ROOT, Settings
from strategy import Bar, Contract, IST, OpeningRange, ENTRY_TIME, MARKET_OPEN, clock

DHAN_BASE_URL = "https://api.dhan.co/v2"
DHAN_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"
ICICI_MASTER_URL = "https://directlink.icicidirect.com/NewSecurityMaster/SecurityMaster.zip"
HISTORY_PAUSE = 1.05   # Dhan rate-limits the historical-data endpoint


class Dhan:
    def __init__(self, cfg: Settings):
        has_login = (
            auth.auto_enabled(cfg)
            or bool(auth._read_cache(cfg.client_id))
            or (cfg.access_token and not cfg.access_token.startswith("replace_with"))
        )
        if not cfg.client_id or cfg.client_id.startswith("replace_with") or not has_login:
            raise SystemExit("Set DHAN_CLIENT_ID plus either DHAN_PIN + DHAN_TOTP_SECRET "
                             "(automatic) or DHAN_ACCESS_TOKEN in .env, or run `python run.py --login`.")
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
            resp = self.http.post(f"{DHAN_BASE_URL}{path}", json=payload, timeout=30)
            if resp.status_code == 401:
                body_text = getattr(resp, "text", "")
                if "806" in body_text or "Data APIs not Subscribed" in body_text:
                    raise RuntimeError("Dhan 401: Data APIs not Subscribed (Error 806). "
                                       "Please activate the free Data API subscription on web.dhan.co -> DhanHQ APIs.")
                if auth.dhan_auto_enabled(self.cfg) and not renewed:
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


class ICICIDirect:
    """Read-only ICICI Direct Breeze API market-data client."""

    def __init__(self, cfg: Settings):
        token = auth.get_access_token(cfg)
        if not cfg.icici_api_key or not cfg.icici_api_secret or not token:
            raise SystemExit(
                "Set ICICI_API_KEY + ICICI_API_SECRET in .env and either configure "
                "ICICI_USER_ID + ICICI_PASSWORD + ICICI_TOTP_SECRET (automatic) or ICICI_SESSION_TOKEN, "
                "or run `python run.py --login`."
            )
        self.cfg = cfg
        self.token = token
        try:
            from breeze_connect import BreezeConnect
        except ImportError:
            raise SystemExit("breeze-connect is not installed. Run `pip install breeze-connect`.")

        self.breeze = BreezeConnect(api_key=cfg.icici_api_key)
        self.breeze.generate_session(api_secret=cfg.icici_api_secret, session_token=token)

    def quotes(self, security_ids: list[str]) -> dict[str, dict]:
        """Fetch live quotes for security_ids (ICICI stock codes)."""
        import concurrent.futures
        out: dict[str, dict] = {}

        def fetch_one(sid: str):
            try:
                res = self.breeze.get_quotes(stock_code=sid, exchange_code="NSE")
                if isinstance(res, dict) and res.get("Success"):
                    items = res["Success"] if isinstance(res["Success"], list) else [res["Success"]]
                    if items:
                        item = items[0]
                        last_price = float(item.get("ltp") or item.get("close") or 0.0)
                        prev_close = float(item.get("previous_close") or item.get("close") or 0.0)
                        open_p = float(item.get("open") or prev_close)
                        high_p = float(item.get("high") or max(open_p, last_price))
                        low_p = float(item.get("low") or min(open_p, last_price))
                        vol = float(item.get("total_quantity_traded") or item.get("volume") or 0.0)
                        dt = str(item.get("datetime") or "")
                        return sid, {
                            "last_price": last_price,
                            "ohlc": {
                                "open": open_p,
                                "high": high_p,
                                "low": low_p,
                                "close": prev_close,
                            },
                            "volume": vol,
                            "last_trade_time": dt,
                        }
            except Exception:
                pass
            return sid, {}

        # Fetch in thread pool
        workers = min(10, len(security_ids) or 1)
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            results = ex.map(fetch_one, security_ids)
            for sid, q in results:
                if q:
                    out[sid] = q
        return out

    def stock_opening_range(self, security_id: str, day: str) -> OpeningRange:
        """Fetch the 09:15 5-min candle as opening range."""
        res = self.breeze.get_historical_data_v2(
            interval="5minute",
            from_date=f"{day}T06:00:00.000Z",
            to_date=f"{day}T12:00:00.000Z",
            stock_code=security_id,
            exchange_code="NSE",
            product_type="cash",
        )
        if not isinstance(res, dict) or not res.get("Success"):
            err_msg = res.get("Error") if isinstance(res, dict) else res
            raise RuntimeError(f"09:15 5-min candle not available: {err_msg}")

        candles = res["Success"]
        for c in candles:
            dt_str = str(c.get("datetime", ""))
            if "09:15" in dt_str:
                return OpeningRange(
                    float(c["open"]), float(c["high"]), float(c["low"]),
                    float(c["close"]), float(c.get("volume", 0.0))
                )
        raise RuntimeError("09:15 5-min candle not available")

    def option_candles(self, security_id: str, day: str) -> list[Bar]:
        """Fetch 5-min intraday option bars."""
        parts = security_id.split(":")
        if len(parts) >= 5:
            _, stock_code, expiry_str, strike_str, opt_type = parts[:5]
        else:
            raise ValueError(f"Invalid ICICI option security_id format: {security_id}")

        try:
            exp_date = datetime.strptime(expiry_str.strip(), "%d-%b-%Y").strftime("%Y-%m-%dT06:00:00.000Z")
        except ValueError:
            exp_date = f"{expiry_str.strip()}T06:00:00.000Z"

        res = self.breeze.get_historical_data_v2(
            interval="5minute",
            from_date=f"{day}T06:00:00.000Z",
            to_date=f"{day}T15:30:00.000Z",
            stock_code=stock_code,
            exchange_code="NFO",
            product_type="options",
            expiry_date=exp_date,
            right="call" if opt_type.upper() == "CE" else "put",
            strike_price=str(strike_str),
        )
        if not isinstance(res, dict) or not res.get("Success"):
            return []

        bars = []
        for c in res["Success"]:
            try:
                dt = datetime.strptime(c["datetime"][:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
                bars.append(Bar(
                    ts=int(dt.timestamp()),
                    open=float(c["open"]),
                    high=float(c["high"]),
                    low=float(c["low"]),
                    close=float(c["close"]),
                    volume=float(c.get("volume", 0.0)),
                ))
            except Exception:
                continue
        return bars


# ------------------------------------------------------- instrument master (public)
def load_dhan_master() -> list[dict[str, str]]:
    """Download Dhan's instrument master once per day and cache it on disk."""
    cache = ROOT / "data" / f"instrument_master_{datetime.now(IST):%Y-%m-%d}.csv"
    if not cache.exists():
        cache.parent.mkdir(exist_ok=True)
        resp = requests.Session()
        resp.trust_env = False
        text = resp.get(DHAN_MASTER_URL, timeout=60).content.decode("utf-8-sig")
        cache.write_text(text, encoding="utf-8")
    with cache.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def load_icici_master() -> list[dict[str, str]]:
    """Download ICICI Direct Security Master zip once per day and extract FONSEScripMaster.txt."""
    cache_zip = ROOT / "data" / f"icici_security_master_{datetime.now(IST):%Y-%m-%d}.zip"
    if not cache_zip.exists():
        cache_zip.parent.mkdir(exist_ok=True)
        resp = requests.Session()
        resp.trust_env = False
        res = resp.get(ICICI_MASTER_URL, timeout=60)
        cache_zip.write_bytes(res.content)

    with zipfile.ZipFile(cache_zip) as zf:
        with zf.open("FONSEScripMaster.txt") as f:
            text = f.read().decode("utf-8", errors="ignore")
            reader = csv.reader(text.splitlines(), skipinitialspace=True)
            hdr = [h.strip() for h in next(reader)]
            return [dict(zip(hdr, r)) for r in reader]


def load_master(cfg: Settings | None = None) -> list[dict[str, str]]:
    if cfg and cfg.active_broker in ("icici", "icicidirect"):
        return load_icici_master()
    return load_dhan_master()


def build_dhan_universe(master: list[dict[str, str]]) -> list[dict[str, str]]:
    """NSE stocks that have stock options in Dhan master: symbol, security id, lot size."""
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


def build_icici_universe(master: list[dict[str, str]]) -> list[dict[str, str]]:
    """NSE stocks that have stock options in ICICI Security Master."""
    found: dict[str, dict[str, str]] = {}
    for r in master:
        if r.get("InstrumentName") == "OPTSTK":
            sym = r.get("ExchangeCode", "").strip()
            sid = r.get("ShortName", "").strip()
            lot = r.get("LotSize", "").strip()
            if sym and sid and sym not in found:
                found[sym] = {"symbol": sym, "security_id": sid, "lot_size": lot}
    return sorted(found.values(), key=lambda x: x["symbol"])


def build_universe(master: list[dict[str, str]], cfg: Settings | None = None) -> list[dict[str, str]]:
    if cfg and cfg.active_broker in ("icici", "icicidirect"):
        return build_icici_universe(master)
    if master and "Token" in master[0] and "ShortName" in master[0]:
        return build_icici_universe(master)
    return build_dhan_universe(master)


def dhan_option_contracts(master: list[dict[str, str]], underlying_id: str,
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


def icici_option_contracts(master: list[dict[str, str]], underlying_id: str,
                           option_type: str) -> list[Contract]:
    out = []
    for r in master:
        if (r.get("InstrumentName") == "OPTSTK"
                and (r.get("ShortName") == underlying_id or r.get("ExchangeCode") == underlying_id)
                and r.get("OptionType") == option_type):
            try:
                exp_date = datetime.strptime(r["ExpiryDate"].strip(), "%d-%b-%Y").date()
                token = r["Token"].strip()
                short_name = r["ShortName"].strip()
                expiry_str = r["ExpiryDate"].strip()
                strike = float(r["StrikePrice"])
                sec_id = f"{token}:{short_name}:{expiry_str}:{strike}:{option_type}"
                out.append(Contract(sec_id, option_type, exp_date, strike, int(float(r["LotSize"]))))
            except (KeyError, ValueError):
                continue
    return out


def option_contracts(master: list[dict[str, str]], underlying_id: str,
                     option_type: str, cfg: Settings | None = None) -> list[Contract]:
    if cfg and cfg.active_broker in ("icici", "icicidirect"):
        return icici_option_contracts(master, underlying_id, option_type)
    if master and "Token" in master[0] and "ShortName" in master[0]:
        return icici_option_contracts(master, underlying_id, option_type)
    return dhan_option_contracts(master, underlying_id, option_type)


def get_broker(cfg: Settings) -> Dhan | ICICIDirect:
    """Return configured broker instance (Dhan or ICICIDirect)."""
    if cfg.active_broker in ("icici", "icicidirect"):
        return ICICIDirect(cfg)
    return Dhan(cfg)
