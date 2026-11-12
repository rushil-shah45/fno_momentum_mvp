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
import dataclasses
import logging
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
    is_live = (getattr(cfg, "trading_mode", "paper") == "live")

    for m in movers:
        contract = picks[m["symbol"]]
        bars = broker.option_candles(contract.security_id, today())
        pos, rest, why = open_position(m["symbol"], m["direction"], contract, bars,
                                       cfg.max_risk_per_trade, cfg.enforce_risk_limit)
        if pos is None:
            skipped.append((m["symbol"], why))
            continue

        if is_live:
            pos.mode = "live"
            # Check available margin for that particular script in F&O wallet
            has_margin, req_margin, avail_margin = broker.check_margin_available(contract, pos.entry, pos.lots)
            if not has_margin:
                msg = f"insufficient F&O margin (required: Rs {req_margin:,.2f}, available: Rs {avail_margin:,.2f})"
                print(f"[MARGIN NOT AVAILABLE] {m['symbol']} {contract.option_type} {contract.strike}: "
                      f"Required Margin = Rs {req_margin:,.2f}, Available F&O Margin = Rs {avail_margin:,.2f}. "
                      f"Trade not taken.")
                skipped.append((m["symbol"], msg))
                continue

            print(f"[MARGIN AVAILABLE] {m['symbol']} {contract.option_type} {contract.strike}: "
                  f"Required Margin = Rs {req_margin:,.2f}, Available F&O Margin = Rs {avail_margin:,.2f}. "
                  f"Taking live trade...")
            try:
                order_res = broker.place_order(m["symbol"], contract, action="BUY", qty=pos.qty,
                                               price=pos.entry, order_type="MARKET")
                order_id = str(order_res.get("order_id") or "").strip()
                error_msg = str(order_res.get("error") or order_res.get("status") or "")

                if not order_id or order_id == "None":
                    raw_err = order_res.get("raw")
                    err_detail = error_msg if error_msg and error_msg != "FAILED" else f"Broker response: {raw_err}"
                    print(f"\n[LIVE ORDER FAILED] {pos.symbol} BUY {pos.qty} {contract.option_type} {contract.strike} failed!")
                    print(f"    Broker Error: {err_detail}")
                    skipped.append((m["symbol"], f"order rejected by broker: {err_detail}"))
                    continue

                pos.order_id = order_id
                print(f"[LIVE ORDER PLACED] {pos.symbol} BUY {pos.qty} {contract.option_type} {contract.strike} (Order ID: {order_id})")

                # After taking trade, check trade list from broker so code can observe from it
                time.sleep(1)
                try:
                    trades = broker.get_trade_list(today())
                    matched_trade = None
                    for t in trades:
                        t_oid = str(t.get("order_id") or "")
                        t_sec = str(t.get("security_id") or "")
                        t_sym = str(t.get("symbol") or "")
                        if order_id and t_oid == order_id:
                            matched_trade = t
                            break
                        if t.get("action", "").upper() == "BUY" and (m["symbol"] in t_sym or t_sec in contract.security_id or t_sym in contract.security_id):
                            matched_trade = t
                            break

                    if matched_trade:
                        exec_px = float(matched_trade.get("price") or 0.0)
                        if exec_px > 0:
                            pos.entry = exec_px
                            pos.executed_entry = exec_px
                            pos.risk = abs(pos.entry - pos.initial_stop)
                            print(f"[LIVE TRADE CONFIRMED] Broker trade list verified: {pos.symbol} filled at Rs {exec_px:.2f} "
                                  f"(Trade ID: {matched_trade.get('trade_id', order_id)}, Qty: {pos.qty})")
                    else:
                        # Check order book status too
                        try:
                            orders = broker.get_order_list(today())
                            matched_order = next((o for o in orders if str(o.get("order_id") or o.get("order_reference") or "") == order_id), None)
                            if matched_order:
                                ord_status = str(matched_order.get("status") or matched_order.get("order_status") or "").upper()
                                avg_px = float(matched_order.get("average_price") or matched_order.get("price") or 0.0)
                                if avg_px > 0:
                                    pos.entry = avg_px
                                    pos.executed_entry = avg_px
                                    pos.risk = abs(pos.entry - pos.initial_stop)
                                print(f"[LIVE ORDER STATUS] Order {order_id} on broker: {ord_status} (Avg Price: Rs {pos.entry:.2f})")
                            else:
                                print(f"[LIVE TRADE OBSERVED] Watching live position {pos.symbol} from broker with entry Rs {pos.entry:.2f}")
                        except Exception:
                            print(f"[LIVE TRADE OBSERVED] Watching live position {pos.symbol} from broker with entry Rs {pos.entry:.2f}")
                except Exception as t_err:
                    print(f"[LIVE TRADE LIST] Could not verify broker trade list for {pos.symbol}: {t_err}")
            except Exception as e:
                err_msg = f"Order placement failed: {e}"
                print(f"[LIVE ORDER ERROR] {m['symbol']}: {err_msg}")
                skipped.append((m["symbol"], err_msg))
                continue
        else:
            pos.mode = "paper"

        positions.append(pos)
        later[pos.symbol] = rest

    note = run_session(positions, later, cfg.max_daily_loss, cfg.force_exit)
    write_csv(DATA / today() / "trades.csv", [trade_row(p) for p in positions])
    write_csv(DATA / today() / "skipped.csv", [{"symbol": s, "reason": w} for s, w in skipped])
    print_report(positions, skipped, note, mode=cfg.trading_mode)
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
            "r_multiple": round(p.r_multiple, 2), "trailing": p.trailing, "status": p.status,
            "mode": getattr(p, "mode", "paper"),
            "order_id": getattr(p, "order_id", ""),
            "exit_order_id": getattr(p, "exit_order_id", "")}


def print_report(positions: list[Position], skipped: list, note: str, mode: str = "paper") -> None:
    print(f"\n{'SYMBOL':<12}{'DIR':<5}{'STRIKE':>9}{'LOTS':>5}{'ENTRY':>9}{'STOP':>9}"
          f"{'NOW/EXIT':>10}{'P&L':>10}{'R':>7}  {'MODE':<6} STATUS")
    for p in positions:
        pmode = getattr(p, "mode", mode).upper()
        print(f"{p.symbol:<12}{p.direction:<5}{p.contract.strike:>9.1f}{p.lots:>5}{p.entry:>9.2f}"
              f"{p.stop:>9.2f}{p.price_now:>10.2f}{p.pnl:>10.0f}{p.r_multiple:>7.2f}  {pmode:<6} {p.status}")
    total = sum(p.pnl for p in positions)
    wins = sum(p.pnl > 0 for p in positions)
    print(f"\nTrades: {len(positions)}   Wins: {wins}   Net P&L: Rs {total:,.0f}"
          f"   Open: {sum(p.status == 'OPEN' for p in positions)}")
    for symbol, why in skipped:
        print(f"  skipped {symbol}: {why}")
    if note:
        print(f"  note: {note}")
    if mode == "live" or any(getattr(p, "mode", "") == "live" for p in positions):
        print("LIVE TRADING - Real broker orders were submitted.")
    else:
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
                    if getattr(pos, "mode", "paper") == "live":
                        try:
                            exit_res = self.broker.place_order(pos.symbol, pos.contract, action="SELL",
                                                               qty=pos.qty, price=price, order_type="MARKET")
                            pos.exit_order_id = str(exit_res.get("order_id", ""))
                            if not pos.exit_order_id:
                                print(f"\n[LIVE STOP ERROR] Broker square-off failed: {exit_res.get('error') or exit_res.get('raw')}")
                            else:
                                time.sleep(0.5)
                                exit_trades = self.broker.get_trade_list(today())
                                matched_exit = next((t for t in exit_trades if str(t.get("order_id")) == pos.exit_order_id or
                                                    (t.get("action") == "SELL" and pos.symbol in str(t.get("symbol", "")))), None)
                                if matched_exit and float(matched_exit.get("price") or 0) > 0:
                                    pos.exit_price = float(matched_exit["price"])
                                    pos.executed_exit = pos.exit_price
                                print(f"\n[LIVE STOP HIT] {pos.symbol} stop loss hit at Rs {price:.2f} -> Closed on broker "
                                      f"(Order ID: {pos.exit_order_id}, Exit: Rs {pos.exit_price:.2f})")
                        except Exception as e:
                            print(f"\n[LIVE STOP ERROR] Failed to close {pos.symbol} on broker: {e}")
                    else:
                        print(f"\n[WS STOP] {pos.symbol} stop loss hit at {price:.2f} (Stop: {pos.stop:.2f})")
                    self._persist()

            # Aggregate tick into 5-minute candle
            completed_bar = self.aggregator.add_tick(sym, price, ts)
            if completed_bar:
                if pos and pos.status == "OPEN":
                    prev_stop = pos.stop
                    pos.on_bar(completed_bar)
                    if pos.status != "OPEN":
                        if getattr(pos, "mode", "paper") == "live" and not pos.exit_order_id:
                            try:
                                exit_res = self.broker.place_order(pos.symbol, pos.contract, action="SELL",
                                                                   qty=pos.qty, price=pos.stop, order_type="MARKET")
                                pos.exit_order_id = str(exit_res.get("order_id", ""))
                                if not pos.exit_order_id:
                                    print(f"\n[LIVE STOP ERROR] Broker square-off failed: {exit_res.get('error') or exit_res.get('raw')}")
                                else:
                                    print(f"\n[LIVE BAR STOP] {pos.symbol} stopped out at Rs {pos.stop:.2f} -> Closed on broker "
                                          f"(Order ID: {pos.exit_order_id})")
                            except Exception as e:
                                print(f"\n[LIVE STOP ERROR] Failed to close {pos.symbol} on broker: {e}")
                    elif pos.status == "OPEN" and pos.trailing and pos.stop != prev_stop:
                        print(f"\n[TRAILING SL] {pos.symbol} trailing stop ratcheted to Rs {pos.stop:.2f}")
                    self._persist()

            # Shared daily loss limit check
            if sum(p.pnl for p in self.positions) <= -self.cfg.max_daily_loss:
                for p in self.positions:
                    if p.status == "OPEN":
                        p.close(p.last_price, ts, "EXIT_DAILY_LOSS")
                        if getattr(p, "mode", "paper") == "live" and not p.exit_order_id:
                            try:
                                res = self.broker.place_order(p.symbol, p.contract, action="SELL", qty=p.qty,
                                                              price=p.last_price, order_type="MARKET")
                                p.exit_order_id = str(res.get("order_id", ""))
                                print(f"\n[DAILY LOSS EXIT] {p.symbol} closed on broker (Order ID: {p.exit_order_id})")
                            except Exception as e:
                                print(f"\n[DAILY LOSS EXIT ERROR] Failed to close {p.symbol}: {e}")
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
                    if getattr(p, "mode", "paper") == "live" and not p.exit_order_id:
                        try:
                            res = self.broker.place_order(p.symbol, p.contract, action="SELL", qty=p.qty,
                                                          price=p.last_price, order_type="MARKET")
                            p.exit_order_id = str(res.get("order_id", ""))
                            print(f"\n[FORCE EXIT] {p.symbol} closed on broker at 15:00 (Order ID: {p.exit_order_id})")
                        except Exception as e:
                            print(f"\n[FORCE EXIT ERROR] Failed to close {p.symbol}: {e}")
            self._persist()
            print_report(self.positions, self.skipped, "force-exit time reached", mode=self.cfg.trading_mode)
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
    broker_name = "ICICI Direct" if cfg.active_broker in ("icici", "icicidirect") else "INDMoney"
    mode_str = cfg.trading_mode.upper()
    print(f"\n{'=' * 65}")
    print(f" Trading Mode : [{mode_str}] | Active Broker: {broker_name}")
    if cfg.trading_mode == "live":
        try:
            fno_avail = broker.get_fno_margin()
            print(f" F&O Margin   : Rs {fno_avail:,.2f} available in wallet")
        except Exception as e:
            print(f" F&O Margin   : Could not fetch wallet margin ({e})")
        print(" WARNING      : LIVE MODE ACTIVE. Real orders will be submitted to broker!")
    else:
        print(" Simulation   : Paper mode active. Virtual orders only (no capital risk).")
    print(f"{'=' * 65}\n")

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
    parser = argparse.ArgumentParser(description="F&O opening-range momentum - trading bot")
    parser.add_argument("--login", action="store_true", help="authenticate with INDMoney / active broker (MPIN + TOTP) and cache fresh access token")
    parser.add_argument("--force", action="store_true", help="force requesting a new token from broker even if cached/existing is valid")
    parser.add_argument("--pin", type=str, default=None, help="INDMoney MPIN (optional, overrides .env)")
    parser.add_argument("--totp", type=str, default=None, help="6-digit TOTP code or secret (optional, overrides .env)")
    parser.add_argument("--mode", type=str, choices=["paper", "live"], default=None, help="trading mode ('paper' or 'live')")
    parser.add_argument("--live", action="store_true", help="run in live trading mode with real broker orders")
    parser.add_argument("--paper", action="store_true", help="run in paper trading mode (simulation)")

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
    trade.add_argument("--mode", type=str, choices=["paper", "live"], default=None, help="trading mode ('paper' or 'live')")
    trade.add_argument("--live", action="store_true", help="run in live trading mode with real broker orders")
    trade.add_argument("--paper", action="store_true", help="run in paper trading mode (simulation)")

    daemon_cmd = sub.add_parser("daemon", help="run once; handles every trading day automatically")
    daemon_cmd.add_argument("--mode", type=str, choices=["paper", "live"], default=None, help="trading mode ('paper' or 'live')")
    daemon_cmd.add_argument("--live", action="store_true", help="run in live trading mode with real broker orders")
    daemon_cmd.add_argument("--paper", action="store_true", help="run in paper trading mode (simulation)")

    dash = sub.add_parser("dashboard", help="open the trade dashboard in your browser")
    dash.add_argument("--port", type=int, default=None)
    args = parser.parse_args()

    cfg = load_settings()
    mode_arg = getattr(args, "mode", None)
    if getattr(args, "live", False) or mode_arg == "live":
        cfg = dataclasses.replace(cfg, trading_mode="live")
        os.environ["TRADING_MODE"] = "live"
    elif getattr(args, "paper", False) or mode_arg == "paper":
        cfg = dataclasses.replace(cfg, trading_mode="paper")
        os.environ["TRADING_MODE"] = "paper"

    if getattr(args, "login", False) or args.cmd == "login":
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

    if args.cmd == "universe":
        cmd_universe(cfg)
    elif args.cmd == "scan":
        cmd_scan(cfg, args.history)
    else:
        cmd_trade(cfg, args.watch)


if __name__ == "__main__":
    main()
