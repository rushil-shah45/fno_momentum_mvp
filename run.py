"""Command-line entry point.

    python run.py login             # authenticate with INDMoney (MPIN + TOTP) & cache token
    python run.py universe          # once a day (or week): list F&O stocks
    python run.py scan              # at ~09:20 IST: find setups + top movers
    python run.py trade             # after 09:20: paper-trade the movers' ATM options
    python run.py trade --watch     # same, 10s status check and 30s signal refresh until force exit
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
from strategy import (IST, Bar, CandleAggregator, OpeningRange, Position, classify, fmt_time,
                      open_position, pick_atm, rank_movers, run_session)

DATA = ROOT / "data"
log = logging.getLogger("run")


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
def load_existing_positions(day_str: str, picks: dict[str, Contract]) -> list[Position]:
    """Load existing trades for the day from trades.csv to preserve open/closed positions.
    Deduplicates rows so only the latest/authoritative position per symbol is kept."""
    trades_path = DATA / day_str / "trades.csv"
    if not trades_path.exists():
        return []
    positions = []
    seen_symbols = set()
    try:
        with trades_path.open(encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
            # Sort with OPEN first so if duplicate rows exist, OPEN takes precedence
            rows.sort(key=lambda r: 0 if r.get("status") == "OPEN" else 1)
            for r in rows:
                sym = r.get("symbol")
                if not sym or sym not in picks or sym in seen_symbols:
                    continue
                seen_symbols.add(sym)
                contract = picks[sym]
                try:
                    lots = int(r.get("lots") or 1)
                    entry = float(r.get("entry") or 0.0)
                    initial_stop = float(r.get("initial_stop") or 0.0)
                    stop = float(r.get("stop_now") or initial_stop)
                    last_px = float(r.get("exit_or_last") or entry)
                    risk = abs(entry - initial_stop) if abs(entry - initial_stop) > 0 else 1.0
                    status = r.get("status") or "OPEN"
                    pos = Position(
                        symbol=sym,
                        direction=r.get("direction", "CALL"),
                        contract=contract,
                        lots=lots,
                        entry_ts=int(datetime.now(IST).timestamp()),
                        entry=entry,
                        initial_stop=initial_stop,
                        stop=stop,
                        risk=risk,
                        trailing=(r.get("trailing") == "True"),
                        status=status,
                        last_price=last_px,
                        mode=r.get("mode", "paper"),
                        order_id=r.get("order_id", ""),
                        exit_order_id=r.get("exit_order_id", ""),
                    )
                    if status != "OPEN":
                        pos.exit_price = last_px
                        pos.exit_ts = int(datetime.now(IST).timestamp())
                    positions.append(pos)
                except (ValueError, TypeError):
                    continue
    except Exception:
        pass
    return positions


def is_script_open(symbol: str, contract: Contract | None = None,
                   positions: list[Position] | None = None,
                   broker = None, day_str: str | None = None) -> bool:
    """Return True if the script is currently in an open position in:
    1. The in-memory positions list (status == 'OPEN')
    2. Today's trades.csv file (status == 'OPEN')
    3. The broker's open portfolio/derivative positions (net_qty > 0)
    """
    # 1. Check in-memory positions list
    if positions:
        for p in positions:
            if p.symbol == symbol and p.status == "OPEN":
                return True

    # 2. Check today's trades.csv file
    day_str = day_str or today()
    trades_path = DATA / day_str / "trades.csv"
    if trades_path.exists():
        try:
            with trades_path.open(encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    if row.get("symbol") == symbol and row.get("status") == "OPEN":
                        return True
        except Exception:
            pass

    # 3. Check broker's open positions if broker is provided and has get_positions()
    if broker and hasattr(broker, "get_positions"):
        try:
            b_positions = broker.get_positions()
            if isinstance(b_positions, list):
                for bp in b_positions:
                    if not isinstance(bp, dict):
                        continue
                    sec_id = str(bp.get("security_id") or bp.get("securityId") or "")
                    sym = str(bp.get("symbol") or bp.get("trading_symbol") or bp.get("tradingSymbol") or bp.get("stock_code") or "")
                    net_qty = float(bp.get("net_qty") or bp.get("netQty") or bp.get("net_quantity") or bp.get("quantity") or bp.get("qty") or 0.0)
                    if net_qty > 0:
                        matched = False
                        if symbol and (symbol in sym or symbol in sec_id):
                            matched = True
                        elif contract and (contract.security_id in sec_id or sec_id in contract.security_id):
                            if not sym or (symbol and symbol in sym):
                                matched = True
                        if matched:
                            return True
        except Exception:
            pass

    return False


def is_script_traded_today(symbol: str, contract: Contract | None = None,
                           positions: list[Position] | None = None,
                           broker = None, day_str: str | None = None) -> bool:
    """Return True if the script was already traded today in ANY state (OPEN or CLOSED):
    Guarantees the rule of exactly 1 trade per stock per day.
    1. In-memory positions list (any status)
    2. Today's trades.csv file (any status)
    3. The broker's open portfolio/derivative positions (net_qty > 0)
    """
    # 1. Check in-memory positions list
    if positions:
        for p in positions:
            if p.symbol == symbol:
                return True

    # 2. Check today's trades.csv file
    day_str = day_str or today()
    trades_path = DATA / day_str / "trades.csv"
    if trades_path.exists():
        try:
            with trades_path.open(encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    if row.get("symbol") == symbol:
                        return True
        except Exception:
            pass

    # 3. Check broker's open positions
    if broker and hasattr(broker, "get_positions"):
        try:
            b_positions = broker.get_positions()
            if isinstance(b_positions, list):
                for bp in b_positions:
                    if not isinstance(bp, dict):
                        continue
                    sec_id = str(bp.get("security_id") or bp.get("securityId") or "")
                    sym = str(bp.get("symbol") or bp.get("trading_symbol") or bp.get("tradingSymbol") or bp.get("stock_code") or "")
                    net_qty = float(bp.get("net_qty") or bp.get("netQty") or bp.get("net_quantity") or bp.get("quantity") or bp.get("qty") or 0.0)
                    if net_qty > 0:
                        matched = False
                        if symbol and (symbol in sym or symbol in sec_id):
                            matched = True
                        elif contract and (contract.security_id in sec_id or sec_id in contract.security_id):
                            if not sym or (symbol and symbol in sym):
                                matched = True
                        if matched:
                            return True
        except Exception:
            pass

    return False


def simulate_once(cfg: Settings, broker, movers: list[dict], picks: dict,
                  existing_positions: list[Position] | None = None) -> list[Position]:
    positions = list(existing_positions) if existing_positions is not None else []

    # If no existing_positions was explicitly passed, load existing today's positions from trades.csv
    if existing_positions is None:
        for p in load_existing_positions(today(), picks):
            if not any(ep.symbol == p.symbol for ep in positions):
                positions.append(p)

    # Deduplicate positions list by symbol
    deduped = []
    seen = set()
    for p in positions:
        if p.symbol not in seen:
            seen.add(p.symbol)
            deduped.append(p)
    positions = deduped
    traded_symbols = {p.symbol for p in positions}

    later, skipped = {}, []
    is_live = (getattr(cfg, "trading_mode", "paper") == "live")

    for m in movers:
        sym = m["symbol"]
        contract = picks[sym]

        # Check if the script was already traded today (1 trade per stock per day rule)
        if sym in traded_symbols or is_script_traded_today(sym, contract, positions, broker if is_live else None):
            print(f"[TRADE EXISTS TODAY] Script {sym} was already traded today (1 trade per stock per day rule). Skipping duplicate entry.")
            skipped.append((sym, "already traded today"))
            continue

        bars = broker.option_candles(contract.security_id, today())
        stock_sid = m.get("security_id", sym)
        spot_bars = None
        if hasattr(broker, "stock_candles"):
            try:
                res = broker.stock_candles(stock_sid, today())
                if isinstance(res, list) and res and all(isinstance(b, Bar) for b in res):
                    spot_bars = res
            except Exception as e:
                print(f"[SPOT CANDLES ERROR] Could not fetch spot candles for {sym}: {e}")

        pos, rest, why = open_position(sym, m["direction"], contract, bars,
                                       cfg.max_risk_per_trade, cfg.enforce_risk_limit,
                                       spot_bars=spot_bars)
        if pos is None:
            skipped.append((sym, why))
            continue

        if is_live:
            pos.mode = "live"
            # Check available margin for that particular script in F&O wallet
            has_margin, req_margin, avail_margin = broker.check_margin_available(contract, pos.entry, pos.lots)
            if not has_margin:
                msg = f"insufficient F&O margin (required: Rs {req_margin:,.2f}, available: Rs {avail_margin:,.2f})"
                print(f"[MARGIN NOT AVAILABLE] {sym} {contract.option_type} {contract.strike}: "
                      f"Required Margin = Rs {req_margin:,.2f}, Available F&O Margin = Rs {avail_margin:,.2f}. "
                      f"Trade not taken.")
                skipped.append((sym, msg))
                continue

            print(f"[MARGIN AVAILABLE] {sym} {contract.option_type} {contract.strike}: "
                  f"Required Margin = Rs {req_margin:,.2f}, Available F&O Margin = Rs {avail_margin:,.2f}. "
                  f"Taking live trade...")
            try:
                order_res = broker.place_order(sym, contract, action="BUY", qty=pos.qty,
                                               price=pos.entry, order_type="MARKET")
                order_id = str(order_res.get("order_id") or "").strip()
                error_msg = str(order_res.get("error") or order_res.get("status") or "")

                if not order_id or order_id == "None":
                    raw_err = order_res.get("raw")
                    err_detail = error_msg if error_msg and error_msg != "FAILED" else f"Broker response: {raw_err}"
                    print(f"\n[LIVE ORDER FAILED] {pos.symbol} BUY {pos.qty} {contract.option_type} {contract.strike} failed!")
                    print(f"    Broker Error: {err_detail}")
                    skipped.append((sym, f"order rejected by broker: {err_detail}"))
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
                        if t.get("action", "").upper() == "BUY" and (sym in t_sym or t_sec in contract.security_id or t_sym in contract.security_id):
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
                print(f"[LIVE ORDER ERROR] {sym}: {err_msg}")
                skipped.append((sym, err_msg))
                continue
        else:
            pos.mode = "paper"

        positions.append(pos)
        traded_symbols.add(pos.symbol)
        later[pos.symbol] = rest

    note = run_session(positions, later, cfg.max_daily_loss, cfg.force_exit)
    # Deduplicate positions by symbol before writing to trades.csv
    deduped = []
    seen = set()
    for p in positions:
        if p.symbol not in seen:
            seen.add(p.symbol)
            deduped.append(p)
    positions = deduped

    write_csv(DATA / today() / "trades.csv", [trade_row(p) for p in positions])
    write_csv(DATA / today() / "skipped.csv", [{"symbol": s, "reason": w} for s, w in skipped])
    print_report(positions, skipped, note, mode=cfg.trading_mode)
    return positions


def trade_row(p: Position) -> dict:
    capital_used = round(p.entry * p.qty, 2)   # premium paid = entry price × qty
    ltp = round(p.price_now, 2)
    entry = round(p.entry, 2)
    diff = round(ltp - entry, 2)
    diff_pct = round((diff / entry * 100) if entry else 0.0, 2)
    trail_sl = round(p.stop, 2)
    return {"symbol": p.symbol, "direction": p.direction, "option": p.contract.option_type,
            "strike": p.contract.strike, "expiry": p.contract.expiry.isoformat(),
            "lots": p.lots, "qty": p.qty, "capital_used": capital_used,
            "entry_time": fmt_time(p.entry_ts),
            "entry": entry, "initial_stop": round(p.initial_stop, 2),
            "stop_now": trail_sl, "trail_sl": trail_sl,
            "exit_time": fmt_time(p.exit_ts),
            "exit_or_last": ltp, "ltp": ltp,
            "price_change": diff, "price_change_pct": diff_pct,
            "pnl": round(p.pnl, 2),
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
    """Manages real-time WebSocket watching, tick-by-tick stops, 10s trade & position status checking,
    30s candidate signal refreshes, candle aggregation, and live P&L updates.
    """

    def __init__(self, cfg: Settings, broker, movers: list[dict], picks: dict,
                 initial_positions: list[Position]):
        self.cfg = cfg
        self.broker = broker
        self.movers = movers
        self.picks = picks
        # Deduplicate initial_positions by symbol
        deduped = []
        seen = set()
        for p in initial_positions:
            if p.symbol not in seen:
                seen.add(p.symbol)
                deduped.append(p)
        self.positions = deduped
        self.aggregator = CandleAggregator(interval_seconds=300)
        self.lock = threading.RLock()
        self.last_csv_write = 0.0
        self.last_tick_time = time.time()
        self.last_10s_check = 0.0
        self.last_30s_refresh = 0.0
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
        # Deduplicate positions by symbol before writing
        seen = set()
        deduped = []
        for p in self.positions:
            if p.symbol not in seen:
                seen.add(p.symbol)
                deduped.append(p)
        self.positions = deduped
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
                prev_stop = pos.stop
                prev_trailing = pos.trailing
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
                else:
                    if not prev_trailing and pos.trailing:
                        print(f"\n[TRAILING SL ACTIVATED] {pos.symbol} LTP Rs {price:.2f} >= entry + 1.0 -> TSL active (Stop: Rs {pos.stop:.2f})")
                        self._persist()
                    elif pos.trailing and pos.stop != prev_stop:
                        print(f"\n[TRAILING SL] {pos.symbol} trailing stop ratcheted to Rs {pos.stop:.2f}")
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
            else:
                # In-progress candle: if trailing is active and candle low advances, ratchet stop
                if pos and pos.status == "OPEN" and pos.trailing:
                    curr_bar = self.aggregator.get_bar(sym)
                    if curr_bar and (curr_bar.low - 1.0) > pos.stop:
                        pos.stop = round(curr_bar.low - 1.0, 2)
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

            # Periodically write trades.csv so dashboard reflects live tick P&L and trailing stops
            now = time.time()
            has_open_pos = any(p.status == "OPEN" for p in self.positions)
            persist_interval = 2.0 if has_open_pos else 5.0
            if now - self.last_csv_write > persist_interval:
                self._persist()
                self.last_csv_write = now

    def check_status_10s(self) -> dict[str, dict]:
        """Check trade status or open position status for each selected script every 10 seconds.
        Queries broker orders/trades/positions (in live mode or when available),
        updates local position state, and logs real-time status.
        """
        now_ts = int(time.time())
        status_report = {}
        is_live = (getattr(self.cfg, "trading_mode", "paper") == "live")

        broker_orders = []
        broker_trades = []
        broker_positions = []
        if is_live and self.broker:
            try:
                broker_orders = self.broker.get_order_list(today()) or []
            except Exception as e:
                log.debug("10s check: error fetching order list: %s", e)
            try:
                broker_trades = self.broker.get_trade_list(today()) or []
            except Exception as e:
                log.debug("10s check: error fetching trade list: %s", e)
            try:
                broker_positions = self.broker.get_positions() or []
            except Exception as e:
                log.debug("10s check: error fetching positions: %s", e)

        time_str = datetime.now(IST).strftime("%H:%M:%S")
        print(f"\n[10s STATUS CHECK] Checking trade & open position status ({time_str}):")

        # Refresh latest broker quotes so LTP and trailing stop are up to date
        self._poll_quotes()

        with self.lock:
            for m in self.movers:
                sym = m["symbol"]
                contract = self.picks.get(sym)
                pos = self._get_position(sym)

                trade_status = "NO_ORDER"
                position_status = "NO_POSITION"

                if pos:
                    trade_status = f"ORDER_PLACED ({pos.order_id})" if pos.order_id else "PAPER_ACTIVE"
                    position_status = pos.status

                    # Sync live orders if live mode
                    if is_live and pos.order_id:
                        matched_order = next((o for o in broker_orders if str(o.get("order_id") or o.get("order_reference") or "") == pos.order_id), None)
                        if matched_order:
                            ord_st = str(matched_order.get("status") or matched_order.get("order_status") or "").upper()
                            trade_status = f"ORDER_{ord_st} ({pos.order_id})"
                            avg_px = float(matched_order.get("average_price") or matched_order.get("price") or 0.0)
                            if avg_px > 0 and pos.executed_entry is None:
                                pos.executed_entry = avg_px
                                pos.entry = avg_px
                                pos.risk = abs(pos.entry - pos.initial_stop)

                            if ord_st in ("REJECTED", "CANCELLED") and pos.status == "OPEN":
                                pos.close(0.0, now_ts, f"ORDER_{ord_st}")
                                position_status = pos.status

                        if pos.exit_order_id:
                            matched_exit = next((o for o in broker_orders if str(o.get("order_id") or o.get("order_reference") or "") == pos.exit_order_id), None)
                            if matched_exit:
                                exit_st = str(matched_exit.get("status") or matched_exit.get("order_status") or "").upper()
                                trade_status += f" | EXIT_{exit_st} ({pos.exit_order_id})"

                    # Check broker open derivative positions
                    if is_live and broker_positions and contract:
                        b_match = None
                        for bp in broker_positions:
                            sec_id = str(bp.get("security_id") or bp.get("securityId") or "")
                            b_sym = str(bp.get("symbol") or bp.get("trading_symbol") or bp.get("tradingSymbol") or bp.get("stock_code") or "")
                            if sym in b_sym or (contract.security_id in sec_id or sec_id in contract.security_id):
                                b_match = bp
                                break

                        if b_match:
                            net_qty = float(b_match.get("net_qty") or b_match.get("netQty") or b_match.get("net_quantity") or b_match.get("quantity") or b_match.get("qty") or 0.0)
                            if net_qty > 0:
                                position_status = f"OPEN (Broker Qty: {int(net_qty)})"
                            elif net_qty == 0 and pos.status == "OPEN":
                                pos.close(pos.last_price, now_ts, "EXIT_EXTERNAL")
                                position_status = "CLOSED_ON_BROKER"

                    if pos.status == "OPEN":
                        diff = pos.last_price - pos.entry
                        diff_pct = (diff / pos.entry * 100) if pos.entry else 0.0
                        sign = "+" if diff > 0 else ""
                        tsl_st = "TSL Active" if pos.trailing else "Fixed"
                        print(f"  [10s STATUS] {sym:<10} | Trade: {trade_status:<22} | Position: {position_status:<16} | "
                              f"Entry: Rs {pos.entry:>7.2f} | LTP: Rs {pos.last_price:>7.2f} ({sign}{diff:.2f}, {sign}{diff_pct:.1f}%) | "
                              f"Trail SL: Rs {pos.stop:>7.2f} ({tsl_st}) | P&L: Rs {pos.pnl:>+8.2f}")
                    else:
                        diff = (pos.exit_price or pos.last_price) - pos.entry
                        diff_pct = (diff / pos.entry * 100) if pos.entry else 0.0
                        sign = "+" if diff > 0 else ""
                        tsl_st = "Trailed" if pos.trailing else "Fixed"
                        print(f"  [10s STATUS] {sym:<10} | Trade: {trade_status:<22} | Position: {pos.status:<16} | "
                              f"Exit: Rs {pos.exit_price or pos.last_price:>7.2f} ({sign}{diff:.2f}, {sign}{diff_pct:.1f}%) | "
                              f"Trail SL: Rs {pos.stop:>7.2f} ({tsl_st}) | P&L: Rs {pos.pnl:>+8.2f}")
                else:
                    # Check if broker shows an open position for it even without local pos
                    broker_has_pos = False
                    if is_live and broker_positions and contract:
                        for bp in broker_positions:
                            sec_id = str(bp.get("security_id") or bp.get("securityId") or "")
                            b_sym = str(bp.get("symbol") or bp.get("trading_symbol") or bp.get("tradingSymbol") or bp.get("stock_code") or "")
                            net_qty = float(bp.get("net_qty") or bp.get("netQty") or bp.get("net_quantity") or bp.get("quantity") or bp.get("qty") or 0.0)
                            if net_qty > 0 and (sym in b_sym or (contract.security_id in sec_id or sec_id in contract.security_id)):
                                broker_has_pos = True
                                break
                    if broker_has_pos:
                        trade_status = "EXTERNAL"
                        position_status = "OPEN_ON_BROKER"
                        print(f"  [10s STATUS] {sym:<10} | Trade: {trade_status:<22} | Position: {position_status:<16}")
                    else:
                        trade_status = "WAITING_BREAKOUT"
                        position_status = "NONE"
                        print(f"  [10s STATUS] {sym:<10} | Trade: {trade_status:<22} | Position: {position_status:<16}")

                status_report[sym] = {
                    "trade_status": trade_status,
                    "position_status": position_status,
                    "position": pos,
                }

            self._persist()
        return status_report

    def refresh_30s(self) -> list[Position]:
        """30-second refresh to check candidate scripts.
        If the script is in open position, it will NOT take that script again.
        If not in open position, checks for confirmation breakout candle and enters if qualified.
        """
        time_str = datetime.now(IST).strftime("%H:%M:%S")
        print(f"\n[30s REFRESH] Checking selected scripts (30s refresh cycle at {time_str})...")
        newly_opened = []
        is_live = (getattr(self.cfg, "trading_mode", "paper") == "live")

        for m in self.movers:
            sym = m["symbol"]
            contract = self.picks.get(sym)
            if not contract:
                continue

            pos = self._get_position(sym)
            # CRITICAL: 1 trade per stock per day rule.
            # If the script was already traded today (in self.positions, trades.csv, or on broker), do NOT trade again.
            if pos is not None or is_script_traded_today(sym, contract, self.positions, self.broker if is_live else None):
                status_str = pos.status if pos else "TRADED_OR_OPEN"
                print(f"  [30s REFRESH] Script {sym} was already traded today (Status: {status_str}) -> will not take that script again.")
                continue

            # Script is not in open position and has not entered yet: check if breakout confirmation occurred
            try:
                stock_sid = m.get("security_id", sym)
                spot_bars = None
                if hasattr(self.broker, "stock_candles"):
                    try:
                        res = self.broker.stock_candles(stock_sid, today())
                        if isinstance(res, list) and res and all(isinstance(b, Bar) for b in res):
                            spot_bars = res
                    except Exception as e:
                        print(f"  [SPOT CANDLES ERROR] Could not fetch spot candles for {sym}: {e}")

                bars = self.broker.option_candles(contract.security_id, today())
                new_pos, rest, why = open_position(sym, m["direction"], contract, bars,
                                                   self.cfg.max_risk_per_trade, self.cfg.enforce_risk_limit,
                                                   spot_bars=spot_bars)
                if new_pos is None:
                    print(f"  [30s REFRESH] Script {sym}: {why}")
                    continue

                if is_live:
                    new_pos.mode = "live"
                    has_margin, req_margin, avail_margin = self.broker.check_margin_available(
                        contract, new_pos.entry, new_pos.lots
                    )
                    if not has_margin:
                        msg = f"insufficient F&O margin (required: Rs {req_margin:,.2f}, available: Rs {avail_margin:,.2f})"
                        print(f"  [MARGIN NOT AVAILABLE] {sym} {contract.option_type} {contract.strike}: {msg}. Trade not taken.")
                        self.skipped.append((sym, msg))
                        continue

                    order_res = self.broker.place_order(sym, contract, action="BUY", qty=new_pos.qty,
                                                        price=new_pos.entry, order_type="MARKET")
                    order_id = str(order_res.get("order_id") or "").strip()
                    if not order_id or order_id == "None":
                        err = order_res.get("error") or order_res.get("raw")
                        print(f"  [LIVE ORDER FAILED] {sym} BUY {new_pos.qty} failed: {err}")
                        self.skipped.append((sym, f"order rejected: {err}"))
                        continue

                    new_pos.order_id = order_id
                    print(f"  [LIVE ORDER PLACED] {sym} BUY {new_pos.qty} {contract.option_type} {contract.strike} (Order ID: {order_id})")

                    time.sleep(1)
                    try:
                        trades = self.broker.get_trade_list(today())
                        matched = next((t for t in trades if str(t.get("order_id") or "") == order_id or
                                        (t.get("action", "").upper() == "BUY" and sym in str(t.get("symbol", "")))), None)
                        if matched and float(matched.get("price") or 0) > 0:
                            exec_px = float(matched["price"])
                            new_pos.entry = exec_px
                            new_pos.executed_entry = exec_px
                            new_pos.risk = abs(new_pos.entry - new_pos.initial_stop)
                            print(f"  [LIVE TRADE CONFIRMED] {sym} filled at Rs {exec_px:.2f} (Trade ID: {matched.get('trade_id', order_id)})")
                    except Exception as e:
                        print(f"  [LIVE TRADE LIST] Could not verify trade list for {sym}: {e}")
                else:
                    new_pos.mode = "paper"

                with self.lock:
                    if any(p.symbol == sym for p in self.positions):
                        print(f"  [30s REFRESH] Race condition detected: {sym} already in positions. Skipping duplicate.")
                        continue
                    self.positions.append(new_pos)
                newly_opened.append(new_pos)
                self._persist()
                print(f"  [30s REFRESH] New trade taken for {sym}: Entry Rs {new_pos.entry:.2f}, Stop Rs {new_pos.stop:.2f}")
            except Exception as e:
                print(f"  [30s REFRESH] Error evaluating {sym}: {e}")

        return newly_opened

    def _poll_quotes(self) -> None:
        """Poll latest quotes from broker to advance ticks / stops."""
        try:
            sids = [c.security_id for c in self.picks.values()]
            quotes = self.broker.quotes(sids)
            now_ts = int(time.time())
            for sid, q in quotes.items():
                lp = float(q.get("last_price") or 0.0)
                if lp > 0:
                    self.on_tick(sid, lp, now_ts)
        except Exception as e:
            log.debug("Quote poll error: %s", e)

    def _force_close_at_exit(self) -> list[Position]:
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

        self.last_10s_check = time.time()
        self.last_30s_refresh = time.time()

        try:
            while datetime.now(IST).time() < self.cfg.force_exit:
                time.sleep(1)
                now = time.time()

                # Every 10 seconds: check trade status or open position status
                if now - self.last_10s_check >= 10.0:
                    self.last_10s_check = now
                    self.check_status_10s()

                # Every 30 seconds: refresh candidate scripts (will not take open scripts again)
                if now - self.last_30s_refresh >= 30.0:
                    self.last_30s_refresh = now
                    self.refresh_30s()

                # If no ticks received for 90s, poll quotes via REST
                if time.time() - self.last_tick_time > 90:
                    self.last_tick_time = time.time()
                    self._poll_quotes()
        finally:
            if self.stream:
                self.stream.stop()

        return self._force_close_at_exit()

    def _fallback_polling(self) -> list[Position]:
        self.last_10s_check = time.time()
        self.last_30s_refresh = time.time()

        while datetime.now(IST).time() < self.cfg.force_exit:
            time.sleep(1)
            now = time.time()

            # Every 10 seconds: check status and update quotes
            if now - self.last_10s_check >= 10.0:
                self.last_10s_check = now
                self.check_status_10s()
                self._poll_quotes()

            # Every 30 seconds: refresh candidate scripts (will not take open scripts again)
            if now - self.last_30s_refresh >= 30.0:
                self.last_30s_refresh = now
                self.refresh_30s()

        return self._force_close_at_exit()


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
    trade.add_argument("--watch", action="store_true", help="watch live positions with 10s status check and 30s signal refresh")
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
