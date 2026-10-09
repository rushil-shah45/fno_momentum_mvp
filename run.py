"""Command-line entry point.

    python run.py universe          # once a day (or week): list F&O stocks
    python run.py scan              # at ~09:20 IST: find setups + top movers
    python run.py trade             # after 09:20: paper-trade the movers' ATM options
    python run.py trade --watch     # same, refreshing every minute until force exit
    python run.py daemon            # run ONCE; does all of the above every trading day
    python run.py dashboard         # web dashboard at http://127.0.0.1:8501
"""

from __future__ import annotations

import argparse
import csv
import os
import time
from datetime import datetime
from pathlib import Path

from broker import Dhan, build_universe, load_master, option_contracts
from settings import ROOT, Settings, load_settings
from strategy import (IST, OpeningRange, Position, classify, fmt_time, open_position,
                      pick_atm, rank_movers, run_session)

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
    rows = build_universe(load_master())
    write_csv(DATA / "universe.csv", rows)
    print(f"Universe: {len(rows)} F&O stocks -> data/universe.csv")


# ------------------------------------------------------------------------- scan
def cmd_scan(cfg: Settings, use_history: bool) -> None:
    uni_file = DATA / "universe.csv"
    if not uni_file.exists():
        raise SystemExit("Run `python run.py universe` first.")
    with uni_file.open(encoding="utf-8") as fh:
        universe = list(csv.DictReader(fh))

    dhan, day = Dhan(cfg), today()
    now = datetime.now(IST).time()
    # At 09:20-09:21 the 5-min OHLC is fully formed in one quote request.
    # Outside that window we fall back to ~1 request per stock (historical candles).
    live = not use_history and (now.hour, now.minute) in {(9, 20), (9, 21)}
    quotes = dhan.quotes([u["security_id"] for u in universe])   # also gives prev close
    if quotes_are_stale(quotes, datetime.now(IST)):
        raise NoMarketData("quotes are not from today (market holiday?)")
    print(f"Scanning {len(universe)} stocks ({'live snapshot' if live else 'historical candles'})...")

    setups = []
    for u in universe:
        sid, q = u["security_id"], quotes.get(u["security_id"], {})
        try:
            if live:
                o = q["ohlc"]
                rng = OpeningRange(float(o["open"]), float(o["high"]), float(o["low"]),
                                   float(q.get("last_price") or o["close"]), float(q.get("volume", 0)))
            else:
                rng = dhan.stock_opening_range(sid, day)
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
def simulate_once(cfg: Settings, dhan: Dhan, movers: list[dict], picks: dict) -> list[Position]:
    positions, later, skipped = [], {}, []
    for m in movers:
        contract = picks[m["symbol"]]
        bars = dhan.option_candles(contract.security_id, today())
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


def cmd_trade(cfg: Settings, watch: bool) -> list[Position]:
    movers = load_movers()
    if not movers:
        raise SystemExit("No movers for today - run `python run.py scan` first.")

    master, dhan = load_master(), Dhan(cfg)
    picks = {}
    for m in movers:     # choose each ATM contract once, using the 5-min close as spot
        kind = "CE" if m["direction"] == "CALL" else "PE"
        picks[m["symbol"]] = pick_atm(option_contracts(master, m["security_id"], kind),
                                      m["close"], datetime.now(IST).date(), cfg.min_days_to_expiry)

    while True:
        positions = simulate_once(cfg, dhan, movers, picks)
        if not watch or datetime.now(IST).time() >= cfg.force_exit:
            return positions
        time.sleep(60)


def main() -> None:
    parser = argparse.ArgumentParser(description="F&O opening-range momentum - paper trading MVP")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("universe")
    scan = sub.add_parser("scan")
    scan.add_argument("--history", action="store_true", help="force historical candles")
    trade = sub.add_parser("trade")
    trade.add_argument("--watch", action="store_true", help="refresh every minute")
    sub.add_parser("daemon", help="run once; handles every trading day automatically")
    dash = sub.add_parser("dashboard", help="open the trade dashboard in your browser")
    dash.add_argument("--port", type=int, default=None)
    args = parser.parse_args()

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
