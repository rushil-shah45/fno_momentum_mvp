"""Read-only market-data client for INDMoney and ICICI Direct. There is deliberately NO order code here."""

from __future__ import annotations

import csv
import io
import json
import logging
import time
import zipfile
from datetime import date, datetime
from pathlib import Path

import requests

import auth
from settings import ROOT, Settings
from strategy import Bar, Contract, IST, OpeningRange, ENTRY_TIME, MARKET_OPEN, clock

log = logging.getLogger("broker")

INDMONEY_BASE_URL = "https://api.indstocks.com"
INDMONEY_WS_URL = "wss://ws-prices.indstocks.com/api/v1/ws/prices"
ICICI_MASTER_URL = "https://directlink.icicidirect.com/NewSecurityMaster/SecurityMaster.zip"
HISTORY_PAUSE = 0.5   # Rate limiting pause between history calls


class INDMoney:
    """Read-only market data client for INDMoney (INDstocks API Suite)."""

    def __init__(self, cfg: Settings):
        token = auth.get_access_token(cfg)
        if not token:
            raise SystemExit(
                "Set INDMONEY_CLIENT_ID plus either INDMONEY_MPIN + INDMONEY_TOTP_SECRET "
                "(automatic) or INDMONEY_ACCESS_TOKEN in .env, or run `python run.py login`."
            )
        self.cfg = cfg
        self.token = token
        self.http = requests.Session()
        self.http.trust_env = False        # ignore stale system proxy settings
        self.http.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": token,
        })
        self._last_history_call = 0.0

    def _get(self, path: str, params: dict | None = None) -> dict:
        renewed = False
        for attempt in range(3):
            resp = self.http.get(f"{INDMONEY_BASE_URL}{path}", params=params, timeout=30)
            if resp.status_code == 401:
                if auth.indmoney_auto_enabled(self.cfg) and not renewed:
                    renewed = True                     # token expired mid-run: mint a new one once
                    self.token = auth.get_access_token(self.cfg, force=True)
                    self.http.headers["Authorization"] = self.token
                    continue
            if resp.status_code == 429:               # rate limited: back off, retry
                time.sleep(2 * (attempt + 1))
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError("INDMoney rate limit: too many retries")

    # ----------------------------------------------------------------- quotes
    def quotes(self, security_ids: list[str]) -> dict[str, dict]:
        """Live snapshot for many NSE stocks via INDMoney /market/quotes/full."""
        out: dict[str, dict] = {}
        if not security_ids:
            return out

        chunk_size = 100
        for i in range(0, len(security_ids), chunk_size):
            chunk = security_ids[i:i + chunk_size]
            formatted_scrips = [
                s if ("_" in str(s) or ":" in str(s)) else f"NSE_{s}"
                for s in chunk
            ]
            scrip_param = ",".join(formatted_scrips)
            try:
                res = self._get("/market/quotes/full", params={"scrip-codes": scrip_param})
            except Exception as err:
                log.warning("INDMoney quotes fetch error: %s", err)
                continue

            data = res.get("data", res) if isinstance(res, dict) else {}
            if not isinstance(data, dict):
                continue

            for raw_code, item in data.items():
                if not isinstance(item, dict):
                    continue
                ohlc = item.get("ohlc") or {}
                last_price = float(item.get("last_price") or ohlc.get("close") or 0.0)
                prev_close = float(ohlc.get("close") or item.get("previous_close") or last_price)
                open_p = float(ohlc.get("open") or prev_close)
                high_p = float(ohlc.get("high") or max(open_p, last_price))
                low_p = float(ohlc.get("low") or min(open_p, last_price))
                close_p = float(ohlc.get("close") or prev_close)
                vol = float(item.get("volume") or 0.0)

                ts = item.get("timestamp") or ""
                last_trade_time = ""
                if ts:
                    try:
                        ts_val = int(ts)
                        if ts_val > 10_000_000_000:
                            ts_val = ts_val // 1000
                        last_trade_time = datetime.fromtimestamp(ts_val, IST).strftime("%Y-%m-%d %H:%M:%S")
                    except Exception:
                        last_trade_time = str(ts)

                q_entry = {
                    "last_price": last_price,
                    "ohlc": {"open": open_p, "high": high_p, "low": low_p, "close": close_p},
                    "volume": vol,
                    "last_trade_time": last_trade_time,
                }
                out[raw_code] = q_entry
                if "_" in raw_code:
                    out[raw_code.split("_", 1)[1]] = q_entry
                if ":" in raw_code:
                    out[raw_code.split(":", 1)[1]] = q_entry

        return out

    # ---------------------------------------------------------------- candles
    def _candles(self, security_id: str, interval: str, start_dt: datetime, end_dt: datetime) -> list[Bar]:
        wait = HISTORY_PAUSE - (time.monotonic() - self._last_history_call)
        if wait > 0:
            time.sleep(wait)

        start_ms = int(start_dt.timestamp() * 1000)
        end_ms = int(end_dt.timestamp() * 1000)
        scrip_code = security_id if ("_" in security_id or ":" in security_id) else f"NSE_{security_id}"
        scrip_code = scrip_code.replace(":", "_")

        res = self._get(f"/market/historical/{interval}", params={
            "scrip-codes": scrip_code,
            "start_time": start_ms,
            "end_time": end_ms,
        })
        self._last_history_call = time.monotonic()

        data = res.get("data", res) if isinstance(res, dict) else res
        items = []
        if isinstance(data, dict):
            if scrip_code in data:
                items = data[scrip_code]
            elif "candles" in data:
                items = data["candles"]
            else:
                for k, v in data.items():
                    if isinstance(v, list):
                        items = v
                        break
        elif isinstance(data, list):
            items = data

        bars = []
        for c in items:
            try:
                if isinstance(c, dict):
                    ts_val = int(c.get("timestamp") or c.get("time") or c.get("t") or 0)
                    if ts_val > 10_000_000_000:
                        ts_val = ts_val // 1000
                    bars.append(Bar(
                        ts=ts_val,
                        open=float(c.get("open") or c.get("o") or 0.0),
                        high=float(c.get("high") or c.get("h") or 0.0),
                        low=float(c.get("low") or c.get("l") or 0.0),
                        close=float(c.get("close") or c.get("c") or 0.0),
                        volume=float(c.get("volume") or c.get("v") or 0.0),
                    ))
                elif isinstance(c, (list, tuple)) and len(c) >= 5:
                    ts_val = int(c[0])
                    if ts_val > 10_000_000_000:
                        ts_val = ts_val // 1000
                    bars.append(Bar(
                        ts=ts_val,
                        open=float(c[1]),
                        high=float(c[2]),
                        low=float(c[3]),
                        close=float(c[4]),
                        volume=float(c[5]) if len(c) > 5 else 0.0,
                    ))
            except Exception:
                continue

        return sorted(bars, key=lambda b: b.ts)

    def option_candles(self, security_id: str, day: str) -> list[Bar]:
        d = datetime.strptime(day, "%Y-%m-%d").date()
        start_dt = datetime.combine(d, datetime.strptime("09:15:00", "%H:%M:%S").time(), tzinfo=IST)
        end_dt = datetime.combine(d, datetime.strptime("15:30:00", "%H:%M:%S").time(), tzinfo=IST)
        return self._candles(security_id, "5minute", start_dt, end_dt)

    def stock_opening_range(self, security_id: str, day: str) -> OpeningRange:
        """Fetch the 09:15 5-min candle as the opening range (5m TF)."""
        d = datetime.strptime(day, "%Y-%m-%d").date()
        start_dt = datetime.combine(d, datetime.strptime("09:15:00", "%H:%M:%S").time(), tzinfo=IST)
        end_dt = datetime.combine(d, datetime.strptime("09:21:00", "%H:%M:%S").time(), tzinfo=IST)
        bars = self._candles(security_id, "5minute", start_dt, end_dt)
        if not bars:
            bars = self._candles(security_id, "1minute", start_dt, end_dt)
            if bars:
                o = bars[0].open
                h = max(b.high for b in bars[:5])
                l = min(b.low for b in bars[:5])
                c = bars[min(4, len(bars) - 1)].close
                v = sum(b.volume for b in bars[:5])
                return OpeningRange(o, h, l, c, v)
            raise RuntimeError(f"09:15 5-min candle not available for {security_id}")
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
class INDMoneyLiveStream:
    """WebSocket client helper for INDMoney live price streaming.
    Connects to wss://ws-prices.indstocks.com/api/v1/ws/prices with Authorization header.
    """

    def __init__(self, token: str):
        self.token = token
        self.ws_url = INDMONEY_WS_URL

    def build_subscription(self, instruments: list[str], mode: str = "ltp") -> dict:
        """Create subscription payload for WebSocket stream.
        instruments: e.g. ["NSE:2885", "NFO:51011"]
        mode: 'ltp' or 'quote'
        """
        formatted = []
        for inst in instruments:
            if ":" not in inst and "_" in inst:
                formatted.append(inst.replace("_", ":"))
            elif ":" not in inst:
                formatted.append(f"NSE:{inst}")
            else:
                formatted.append(inst)
        return {
            "action": "subscribe",
            "mode": mode,
            "instruments": formatted,
        }


def load_indmoney_equity_map(cfg: Settings | None = None) -> dict[str, str]:
    """Return map of NSE EQ symbol -> equity security_id (e.g. 'RELIANCE' -> '2885')."""
    cache = ROOT / "data" / f"indmoney_equity_{datetime.now(IST):%Y-%m-%d}.csv"
    if not cache.exists():
        cache.parent.mkdir(exist_ok=True)
        resp = requests.Session()
        resp.trust_env = False
        token = auth.get_access_token(cfg) if cfg else ""
        headers = {"Authorization": token} if token else {}
        try:
            res = resp.get(f"{INDMONEY_BASE_URL}/market/instruments?source=equity", headers=headers, timeout=60)
            if res.ok and len(res.content) > 50:
                cache.write_text(res.content.decode("utf-8-sig", errors="ignore"), encoding="utf-8")
        except Exception as e:
            log.warning("Could not download INDMoney equity instruments: %s", e)
    if cache.exists():
        with cache.open(encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            return {
                r["TRADING_SYMBOL"].strip(): r["SECURITY_ID"].strip()
                for r in reader
                if r.get("EXCH") == "NSE" and r.get("SERIES") == "EQ"
            }
    return {}


def load_indmoney_master(cfg: Settings | None = None) -> list[dict[str, str]]:
    """Download INDMoney's F&O instrument master once per day and cache it on disk."""
    cache = ROOT / "data" / f"indmoney_instruments_{datetime.now(IST):%Y-%m-%d}.csv"
    if not cache.exists():
        cache.parent.mkdir(exist_ok=True)
        resp = requests.Session()
        resp.trust_env = False
        token = auth.get_access_token(cfg) if cfg else ""
        headers = {"Authorization": token} if token else {}
        try:
            res = resp.get(f"{INDMONEY_BASE_URL}/market/instruments?source=fno", headers=headers, timeout=60)
            if res.ok and len(res.content) > 50:
                text = res.content.decode("utf-8-sig", errors="ignore")
                cache.write_text(text, encoding="utf-8")
        except Exception as e:
            log.warning("Could not download INDMoney instruments: %s", e)
    if cache.exists():
        with cache.open(encoding="utf-8") as fh:
            return list(csv.DictReader(fh))
    return []


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
    return load_indmoney_master(cfg)


def build_indmoney_universe(master: list[dict[str, str]], cfg: Settings | None = None) -> list[dict[str, str]]:
    """NSE stocks that have stock options in INDMoney F&O master."""
    equity_map = load_indmoney_equity_map(cfg)
    found: dict[str, dict[str, str]] = {}
    for r in master:
        inst = r.get("INSTRUMENT_NAME") or r.get("INSTRUMENT") or r.get("InstrumentName") or ""
        exch = r.get("EXCH") or ""
        if inst in ("OPTSTK", "FUTSTK", "") and exch in ("NSE", ""):
            tsym = (r.get("TRADING_SYMBOL") or r.get("UNDERLYING_SYMBOL") or r.get("SYMBOL") or
                   r.get("Symbol") or r.get("ShortName") or "").strip()
            sym = tsym.split("-")[0].strip() if "-" in tsym else tsym
            sid = equity_map.get(sym) or r.get("UNDERLYING_SECURITY_ID") or r.get("SECURITY_ID") or ""
            lot = (r.get("LOT_UNITS") or r.get("LOT_SIZE") or r.get("LotSize") or r.get("LOT") or "").strip()
            if sym and sid and sym not in found:
                found[sym] = {"symbol": sym, "security_id": sid, "lot_size": lot}
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
    return build_indmoney_universe(master, cfg=cfg)


def indmoney_option_contracts(master: list[dict[str, str]], underlying_id: str,
                              option_type: str, cfg: Settings | None = None) -> list[Contract]:
    out = []
    opt_type_upper = option_type.upper()
    equity_map = load_indmoney_equity_map(cfg)
    rev_map = {sid: sym for sym, sid in equity_map.items()}
    target_symbol = rev_map.get(underlying_id, underlying_id)

    for r in master:
        inst = r.get("INSTRUMENT_NAME") or r.get("INSTRUMENT") or r.get("InstrumentName") or ""
        exch = r.get("EXCH") or ""
        if inst and inst not in ("OPTSTK", "OPTIONS"):
            continue
        if exch and exch != "NSE":
            continue

        tsym = (r.get("TRADING_SYMBOL") or r.get("UNDERLYING_SYMBOL") or r.get("SYMBOL") or
                r.get("Symbol") or r.get("ShortName") or "").strip()
        sym = tsym.split("-")[0].strip() if "-" in tsym else tsym
        otype = (r.get("OPTION_TYPE") or r.get("OptionType") or r.get("RIGHT") or "").strip().upper()
        if not otype and tsym.endswith("CE"):
            otype = "CE"
        elif not otype and tsym.endswith("PE"):
            otype = "PE"

        if (sym == target_symbol or sym == underlying_id) and otype == opt_type_upper:
            try:
                exp_raw = r.get("EXPIRY_DATE") or r.get("EXPIRY_CODE") or r.get("ExpiryDate") or ""
                date_part = exp_raw.split(" ")[0].strip()
                if "/" in date_part:
                    exp_date = datetime.strptime(date_part, "%m/%d/%Y").date()
                elif "-" in date_part:
                    try:
                        exp_date = date.fromisoformat(date_part)
                    except ValueError:
                        exp_date = datetime.strptime(date_part, "%d-%b-%Y").date()
                elif r.get("EXPIRY_CODE"):
                    exp_date = datetime.strptime(r["EXPIRY_CODE"].strip(), "%d %b %Y").date()
                else:
                    continue

                sid = r.get("SECURITY_ID") or r.get("SCRIP_CODE") or r.get("Token") or ""
                strike = float(r.get("STRIKE_PRICE") or r.get("StrikePrice") or 0.0)
                lot = int(float(r.get("LOT_UNITS") or r.get("LOT_SIZE") or r.get("LotSize") or 1))
                out.append(Contract(sid, opt_type_upper, exp_date, strike, lot))
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
    return indmoney_option_contracts(master, underlying_id, option_type)


def get_broker(cfg: Settings) -> INDMoney | ICICIDirect:
    """Return configured broker instance (INDMoney or ICICIDirect)."""
    if cfg.active_broker in ("icici", "icicidirect"):
        return ICICIDirect(cfg)
    return INDMoney(cfg)
