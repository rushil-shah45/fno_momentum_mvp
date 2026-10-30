"""Command-line entry point.

    python run.py login             # authenticate with INDMoney (MPIN + TOTP) & cache token
    python run.py universe          # once a day (or week): list F&O stocks
    python run.py scan              # at ~09:20 IST: find setups + top movers
    python run.py trade             # after 09:20: paper-trade the movers' ATM options
    python run.py trade --watch     # same, refreshing every minute until force exit
    python run.py daemon            # run ONCE; does all of the above every trading day (08:55 login)
    python run.py dashboard         # web dashboard at http://127.0.0.1:8501
"""

from __future__ import annotations

import argparse
import csv
import os
import threading
import time
from datetime import datetime
from pathlib import Path

from broker import (INDMoney, ICICIDirect, build_universe, get_broker, get_stream,
                    load_master, option_contracts)
from settings import ROOT, Settings, load_settings
from strategy import (IST, CandleAggregator, OpeningRange, Position, classify, fmt_time,
                      open_position, pick_atm, rank_movers, run_session)

DATA = ROOT / "data"


class NoMarketData(Exception):
    """Raised when there is nothing usable to scan (market holiday / no feed)."""


def quotes_are_stale(quotes: dict, day: datetime) -> bool:
    """True only if quotes carry a readable trade date and none of them is today.
    Unknown or unparseable formats return False, so a format surprise can never
    make us skip a real trading day."""
    seen = []
    for q in quotes.values():
        raw = str(q.get("last_trade_time") or "").replace("T", " ").split(" ")[0]
        for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
            try:
                seen.append(datetime.strptime(raw, fmt).date())
                break
            except ValueError:
                continue
    return bool(seen) and day.date() not in seen


def today() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d")


def write_csv(path: Path, rows: list[dict]) -> None:
    """Write via a temp file + rename, so the dashboard never reads a half-written CSV."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as fh:
        if rows:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
    os.replace(tmp, path)


# --------------------------------------------------------------------- universe
def cmd_universe(cfg: Settings) -> None:
    broker_name = "ICICI Direct" if cfg.active_broker in ("icici", "icicidirect") else "INDMoney"
    rows = build_universe(load_master(cfg), cfg)
    write_csv(DATA / "universe.csv", rows)
    print(f"Universe ({broker_name}): {len(rows)} F&O stocks -> data/universe.csv")


# ------------------------------------------------------------------------- scan
def cmd_scan(cfg: Settings, use_history: bool) -> None:
    uni_file = DATA / "universe.csv"
    if not uni_file.exists():
        raise SystemExit("Run `python run.py universe` first.")
    with uni_file.open(encoding="utf-8") as fh:
        universe = list(csv.DictReader(fh))

    # Auto-heal legacy universe files containing truncated symbols (BAJAJ/NAM) or option tokens
    if any(u.get("symbol") in ("BAJAJ", "NAM") or u.get("security_id") in ("74928", "115385", "56598", "138474") for u in universe):
        print("Detected outdated universe file with legacy tokens. Re-generating data/universe.csv...")
        cmd_universe(cfg)
        with uni_file.open(encoding="utf-8") as fh:
            universe = list(csv.DictReader(fh))

    broker, day = get_broker(cfg), today()
    now = datetime.now(IST).time()
    # At 09:20-09:21 the 5-min OHLC is fully formed in one quote request.
    # Outside that window we fall back to ~1 request per stock (historical candles).
    live = not use_history and (now.hour, now.minute) in {(9, 20), (9, 21)}
    quotes = broker.quotes([u["security_id"] for u in universe])   # also gives prev close
    if quotes_are_stale(quotes, datetime.now(IST)):
        raise NoMarketData("quotes are not from today (market holiday?)")
    print(f"Scanning {len(universe)} stocks ({'live snapshot' if live else 'historical candles'})...")

    setups = []
    for u in universe:
        sid, q = u["security_id"], quotes.get(u["security_id"], {})
        try:
            if live and q.get("ohlc"):
                o = q["ohlc"]
                rng = OpeningRange(float(o["open"]), float(o["high"]), float(o["low"]),
                                   float(q.get("last_price") or o["close"]), float(q.get("volume", 0)))
            else:
                rng = broker.stock_opening_range(sid, day)
            direction, reason, range_pct = classify(rng, cfg.tolerance, cfg.close_zone, cfg.max_range_pct)
            prev_close = float(q.get("ohlc", {}).get("close") or 0)
            last = float(q.get("last_price") or 0)
            change = (last - prev_close) / prev_close * 100 if prev_close and last else 0.0
            setups.append({"symbol": u["symbol"], "security_id": sid, "lot_size": u["lot_size"],
                           "direction": direction, "open": rng.open, "high": rng.high,
                           "low": rng.low, "close": rng.close, "range_pct": round(range_pct, 2),
                           "change_pct": round(change, 2), "status": reason})
        except Exception as err:                       # one bad stock must not stop the scan
            setups.append({"symbol": u["symbol"], "security_id": sid, "lot_size": u["lot_size"],
                           "direction": "", "open": "", "high": "", "low": "", "close": "",
                           "range_pct": 0, "change_pct": 0, "status": f"data unavailable: {err}"})

    if all(str(s["status"]).startswith("data unavailable") for s in setups):
        raise NoMarketData("no usable market data for any stock")
    write_csv(DATA / day / "setups.csv", setups)
    qualified = [s for s in setups if s["direction"]]
    movers = rank_movers(qualified, cfg.top_n)
    write_csv(DATA / day / "movers.csv", movers)

    print(f"Qualified setups: {len(qualified)}   Selected movers: {len(movers)}")
    for m in movers:
        print(f"  {m['direction']:<4} {m['symbol']:<12} {m['change_pct']:>+6.2f}%  range {m['range_pct']}%")


# ------------------------------------------------------------------------ trade
def simulate_once(cfg: Settings, broker, movers: list[dict], picks: dict) -> list[Position]:
    positions, later, skipped = [], {}, []
    for m in movers:
        contract = picks[m["symbol"]]
        bars = broker.option_candles(contract.security_id, today())
        pos, rest, why = open_position(m["symbol"], m["direction"], contract, bars,
                                       cfg.max_risk_per_trade, cfg.enforce_risk_limit)
        if pos is None:
            skipped.append((m["symbol"], why))
            continue
        positions.append(pos)
        later[pos.symbol] = rest
    note = run_session(positions, later, cfg.max_daily_loss, cfg.force_exit)
    write_csv(DATA / today() / "trades.csv", [trade_row(p) for p in positions])
    write_csv(DATA / today() / "skipped.csv", [{"symbol": s, "reason": w} for s, w in skipped])
    print_report(positions, skipped, note)
    return positions


def trade_row(p: Position) -> dict:
    capital_used = round(p.entry * p.qty, 2)   # premium paid = entry price × qty
    return {"symbol": p.symbol, "direction": p.direction, "option": p.contract.option_type,
            "strike": p.contract.strike, "expiry": p.contract.expiry.isoformat(),
            "lots": p.lots, "qty": p.qty, "capital_used": capital_used,
            "entry_time": fmt_time(p.entry_ts),
            "entry": round(p.entry, 2), "initial_stop": round(p.initial_stop, 2),
            "stop_now": round(p.stop, 2), "exit_time": fmt_time(p.exit_ts),
            "exit_or_last": round(p.price_now, 2), "pnl": round(p.pnl, 2),
            "r_multiple": round(p.r_multiple, 2), "trailing": p.trailing, "status": p.status}


def print_report(positions: list[Position], skipped: list, note: str) -> None:
    print(f"\n{'SYMBOL':<12}{'DIR':<5}{'STRIKE':>9}{'LOTS':>5}{'ENTRY':>9}{'STOP':>9}"
          f"{'NOW/EXIT':>10}{'P&L':>10}{'R':>7}  STATUS")
    for p in positions:
        print(f"{p.symbol:<12}{p.direction:<5}{p.contract.strike:>9.1f}{p.lots:>5}{p.entry:>9.2f}"
              f"{p.stop:>9.2f}{p.price_now:>10.2f}{p.pnl:>10.0f}{p.r_multiple:>7.2f}  {p.status}")
    total = sum(p.pnl for p in positions)
    wins = sum(p.pnl > 0 for p in positions)
    print(f"\nTrades: {len(positions)}   Wins: {wins}   Net P&L: Rs {total:,.0f}"
          f"   Open: {sum(p.status == 'OPEN' for p in positions)}")
    for symbol, why in skipped:
        print(f"  skipped {symbol}: {why}")
    if note:
        print(f"  note: {note}")
    print("PAPER ONLY - no order was sent.")


def load_movers() -> list[dict]:
    """Today's selected movers, or [] if the scan has not run / found none."""
    path = DATA / today() / "movers.csv"
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as fh:
        return [{**r, "close": float(r["close"])} for r in csv.DictReader(fh)]


class LiveTradeSession:
    """Manages real-time WebSocket watching, tick-by-tick stops, candle aggregation, and live P&L updates."""

    def __init__(self, cfg: Settings, broker, movers: list[dict], picks: dict,
                 initial_positions: list[Position]):
        self.cfg = cfg
        self.broker = broker
        self.movers = movers
        self.picks = picks
        self.positions = initial_positions
        self.aggregator = CandleAggregator(interval_seconds=300)
        self.lock = threading.Lock()
        self.last_csv_write = 0.0
        self.last_tick_time = time.time()
        self.stream = None
        self.skipped: list[tuple[str, str]] = []

        # Map security_id variants to symbol
        self.sec_id_to_symbol: dict[str, str] = {}
        for sym, c in picks.items():
            sid = c.security_id
            self.sec_id_to_symbol[sid] = sym
            self.sec_id_to_symbol[sid.replace(":", "_")] = sym
            self.sec_id_to_symbol[sid.replace("_", ":")] = sym
            if ":" in sid:
                self.sec_id_to_symbol[sid.split(":")[0]] = sym
                self.sec_id_to_symbol[sid.split(":")[-1]] = sym
            if "_" in sid:
                self.sec_id_to_symbol[sid.split("_")[-1]] = sym

    def _get_position(self, symbol: str) -> Position | None:
        for p in self.positions:
            if p.symbol == symbol:
                return p
        return None

    def _persist(self) -> None:
        write_csv(DATA / today() / "trades.csv", [trade_row(p) for p in self.positions])

    def on_tick(self, sec_id: str, price: float, ts: int) -> None:
        with self.lock:
            self.last_tick_time = time.time()
            sym = self.sec_id_to_symbol.get(sec_id)
            if not sym:
                clean = sec_id.replace("NFO:", "").replace("NSE:", "").replace("NFO_", "").replace("NSE_", "")
                sym = self.sec_id_to_symbol.get(clean)
            if not sym and len(self.picks) == 1:
                sym = list(self.picks.keys())[0]
            if not sym:
                return

            pos = self._get_position(sym)
            if pos and pos.status == "OPEN":
                # Instant tick-level stop-loss check
                closed = pos.on_tick(price, ts)
                if closed:
                    print(f"\n[WS STOP] {pos.symbol} stop loss hit at {price:.2f} (Stop: {pos.stop:.2f})")
                    self._persist()

            # Aggregate tick into 5-minute candle
            completed_bar = self.aggregator.add_tick(sym, price, ts)
            if completed_bar:
                if pos and pos.status == "OPEN":
                    pos.on_bar(completed_bar)
                    self._persist()

            # Shared daily loss limit check
            if sum(p.pnl for p in self.positions) <= -self.cfg.max_daily_loss:
                for p in self.positions:
                    if p.status == "OPEN":
                        p.close(p.last_price, ts, "EXIT_DAILY_LOSS")
                self._persist()

            # Periodically write trades.csv so dashboard reflects live tick P&L
            now = time.time()
            if now - self.last_csv_write > 5.0:
                self._persist()
                self.last_csv_write = now

    def run(self) -> list[Position]:
        contracts = list(self.picks.values())
        try:
            self.stream = get_stream(self.broker, contracts, on_tick=self.on_tick)
            self.stream.start()
            print(f"\n[WS] WebSocket live stream active for {len(contracts)} contracts.")
            print("[WS] Watching positions in real-time (instant stop-loss active)...")
        except Exception as err:
            print(f"\n[WS] WebSocket streaming unavailable ({err}). Falling back to REST polling.")
            return self._fallback_polling()

        try:
            while datetime.now(IST).time() < self.cfg.force_exit:
                time.sleep(1)
                # If no ticks received for 90s, re-check or sync via REST
                if time.time() - self.last_tick_time > 90:
                    self.last_tick_time = time.time()
                    try:
                        self.positions = simulate_once(self.cfg, self.broker, self.movers, self.picks)
                    except Exception:
                        pass
        finally:
            if self.stream:
                self.stream.stop()

        with self.lock:
            now_ts = int(datetime.now(IST).timestamp())
            for p in self.positions:
                if p.status == "OPEN":
                    p.close(p.last_price, now_ts, "EXIT_FORCED")
            self._persist()
            print_report(self.positions, self.skipped, "force-exit time reached")
        return self.positions

    def _fallback_polling(self) -> list[Position]:
        while True:
            self.positions = simulate_once(self.cfg, self.broker, self.movers, self.picks)
            if datetime.now(IST).time() >= self.cfg.force_exit:
                return self.positions
            time.sleep(60)


def cmd_trade(cfg: Settings, watch: bool) -> list[Position]:
    movers = load_movers()
    if not movers:
        raise SystemExit("No movers for today - run `python run.py scan` first.")

    master, broker = load_master(cfg), get_broker(cfg)
    picks = {}
    for m in movers:     # choose each ATM contract once, using the 5-min close as spot
        kind = "CE" if m["direction"] == "CALL" else "PE"
        picks[m["symbol"]] = pick_atm(option_contracts(master, m["security_id"], kind, cfg),
                                      m["close"], datetime.now(IST).date(), cfg.min_days_to_expiry)

    # Initial state computed from available historical candles
    positions = simulate_once(cfg, broker, movers, picks)
    if not watch or datetime.now(IST).time() >= cfg.force_exit:
        return positions

    session = LiveTradeSession(cfg, broker, movers, picks, positions)
    return session.run()


def cmd_login(cfg: Settings, pin: str | None = None, totp: str | None = None, force: bool = False) -> None:
    from auth import login, check_access, _read_cache, _write_cache
    from datetime import timedelta
    is_icici = cfg.active_broker in ("icici", "icicidirect")
    broker_name = "ICICI Direct" if is_icici else "INDMoney"
    print(f"Authenticating with {broker_name}...")

    # For INDMoney, if not explicitly forcing a regeneration or overriding PIN/TOTP,
    # test whether current cached or .env token is already authenticated and active:
    if not is_icici and not force and not pin and not totp:
        cached = _read_cache(cfg.indmoney_client_id) if cfg.indmoney_client_id else None
        existing_token = (cached[0] if cached else None) or (cfg.indmoney_access_token if cfg else "")
        if existing_token:
            ok, status_code, msg, profile = check_access(existing_token, client_id=cfg.indmoney_client_id, active_broker="indmoney")
            if ok:
                expiry = cached[1] if cached else (datetime.now(IST) + timedelta(hours=23))
                _write_cache(cfg.indmoney_client_id, existing_token, expiry)
                masked = f"{existing_token[:8]}...{existing_token[-6:]}" if len(existing_token) > 14 else existing_token
                print(f"[✓] Existing {broker_name} access token is valid and active!")
                print(f"    Client ID : {cfg.indmoney_client_id or 'provided'}")
                print(f"    Token     : {masked}")
                print(f"    Expires   : {expiry.strftime('%Y-%m-%d %H:%M:%S %Z')}")
                print("    Cached in : data/indmoney_token.json")
                print(f"[✓] INDMoney API access verified.")
                if profile and isinstance(profile, dict) and profile.get("data", {}).get("name"):
                    print(f"    Account   : {profile['data']['name']}")
                elif profile and isinstance(profile, dict) and profile.get("name"):
                    print(f"    Account   : {profile['name']}")
                print("    (Pass `python run.py login --force` to request a new token via MPIN+TOTP)")
                return

    try:
        token, expiry = login(cfg, pin=pin, totp=totp)
    except Exception as e:
        raise SystemExit(f"Authentication failed: {e}")
    masked = f"{token[:8]}...{token[-6:]}" if len(token) > 14 else token
    print(f"[✓] {broker_name} authentication successful!")
    if is_icici:
        print(f"    User ID   : {cfg.icici_user_id or 'provided'}")
        print(f"    Token     : {masked}")
        print(f"    Expires   : {expiry.strftime('%Y-%m-%d %H:%M:%S %Z')}")
        print("    Cached in : data/icici_token.json")

        ok, status_code, msg, profile = check_access(token, api_key=cfg.icici_api_key, api_secret=cfg.icici_api_secret, active_broker="icici")
        if ok:
            print("[✓] ICICI Direct Breeze API access verified.")
        else:
            print(f"[!] Warning: Access check returned HTTP {status_code} ({msg}).")
    else:
        print(f"    Client ID : {cfg.indmoney_client_id or 'provided'}")
        print(f"    Token     : {masked}")
        print(f"    Expires   : {expiry.strftime('%Y-%m-%d %H:%M:%S %Z')}")
        print("    Cached in : data/indmoney_token.json")

        ok, status_code, msg, profile = check_access(token, client_id=cfg.indmoney_client_id, active_broker="indmoney")
        if ok:
            print("[✓] INDMoney API access verified.")
            if profile and isinstance(profile, dict) and profile.get("data", {}).get("name"):
                print(f"    Account   : {profile['data']['name']}")
            elif profile and isinstance(profile, dict) and profile.get("name"):
                print(f"    Account   : {profile['name']}")
        else:
            print(f"[!] Warning: Access check returned HTTP {status_code} ({msg}).")


def main() -> None:
    parser = argparse.ArgumentParser(description="F&O opening-range momentum - paper trading MVP")
    parser.add_argument("--login", action="store_true", help="authenticate with INDMoney / active broker (MPIN + TOTP) and cache fresh access token")
    parser.add_argument("--force", action="store_true", help="force requesting a new token from broker even if cached/existing is valid")
    parser.add_argument("--pin", type=str, default=None, help="INDMoney MPIN (optional, overrides .env)")
    parser.add_argument("--totp", type=str, default=None, help="6-digit TOTP code or secret (optional, overrides .env)")

    sub = parser.add_subparsers(dest="cmd", required=False)
    login_cmd = sub.add_parser("login", help="authenticate with INDMoney / active broker (MPIN + TOTP) and cache fresh access token")
    login_cmd.add_argument("--force", action="store_true", help="force requesting a new token from broker even if cached/existing is valid")
    login_cmd.add_argument("--pin", type=str, default=None, help="INDMoney MPIN (optional, overrides .env)")
    login_cmd.add_argument("--totp", type=str, default=None, help="6-digit TOTP code or secret (optional, overrides .env)")

    sub.add_parser("universe")
    scan = sub.add_parser("scan")
    scan.add_argument("--history", action="store_true", help="force historical candles")
    trade = sub.add_parser("trade")
    trade.add_argument("--watch", action="store_true", help="refresh every minute")
    sub.add_parser("daemon", help="run once; handles every trading day automatically")
    dash = sub.add_parser("dashboard", help="open the trade dashboard in your browser")
    dash.add_argument("--port", type=int, default=None)
    args = parser.parse_args()

    if getattr(args, "login", False) or args.cmd == "login":
        cfg = load_settings()
        pin = getattr(args, "pin", None)
        totp = getattr(args, "totp", None)
        force = getattr(args, "force", False)
        cmd_login(cfg, pin=pin, totp=totp, force=force)
        return

    if not args.cmd:
        parser.print_help()
        return

    if args.cmd == "daemon":
        from scheduler import run_forever      # imported here to avoid a circular import
        run_forever()
        return

    if args.cmd == "dashboard":
        import dashboard
        dashboard.serve(port=args.port or load_settings().dashboard_port)
        return

    cfg = load_settings()
    if args.cmd == "universe":
        cmd_universe(cfg)
    elif args.cmd == "scan":
        cmd_scan(cfg, args.history)
    else:
        cmd_trade(cfg, args.watch)


if __name__ == "__main__":
    main()
