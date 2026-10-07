"""Run-once scheduler: `python run.py daemon`.

Every trading day, in IST:
    09:00  refresh the F&O universe
    09:20  scan + select movers (live snapshot at 09:20:05)
    09:20  paper-trade the movers' ATM options, refreshing every minute
    15:00  force-exit, write the day's summary, sleep until tomorrow

Tokens renew themselves when DHAN_PIN + DHAN_TOTP_SECRET are set (see auth.py). .env is
re-read on every attempt, so any change you make while it runs is picked up.
"""

from __future__ import annotations

import csv
import logging
import time as _time
from datetime import date, datetime, time, timedelta
from logging.handlers import RotatingFileHandler

import run
from settings import ROOT, load_settings
from strategy import IST

log = logging.getLogger("scanner")

UNIVERSE_AT = time(9, 0)
SCAN_AT = time(9, 20, 5)           # inside the 09:20-09:21 live-snapshot window
SCAN_RETRY_UNTIL = time(11, 0)     # late scans fall back to historical candles
RETRY_PAUSE = 60                   # seconds between retries


def now_ist() -> datetime:
    return datetime.now(IST)


def setup_logging() -> None:
    (ROOT / "data").mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    for handler in (logging.StreamHandler(),
                    RotatingFileHandler(ROOT / "data" / "scanner.log",
                                        maxBytes=1_000_000, backupCount=3, encoding="utf-8")):
        handler.setFormatter(fmt)
        log.addHandler(handler)
    log.setLevel(logging.INFO)


# ----------------------------------------------------------------------- calendar
def load_holidays() -> set[date]:
    path = ROOT / "holidays.txt"
    days: set[date] = set()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.split("#")[0].strip()
            if line:
                try:
                    days.add(date.fromisoformat(line))
                except ValueError:
                    log.warning("holidays.txt: ignoring bad line %r", line)
    return days


def is_trading_day(day: date, holidays: set[date]) -> bool:
    return day.weekday() < 5 and day not in holidays


# ------------------------------------------------------------------------ helpers
def sleep_until(target: datetime) -> None:
    """Sleep in short slices so Ctrl+C works and PC sleep/wake doesn't confuse us."""
    while True:
        remaining = (target - now_ist()).total_seconds()
        if remaining <= 0:
            return
        _time.sleep(min(remaining, 30))


def at(day: date, clock: time) -> datetime:
    return datetime.combine(day, clock, tzinfo=IST)


def attempt(label: str, fn, until: time):
    """Run fn(cfg) until it succeeds or `until` passes. Settings are re-read each
    try (so a fixed .env/token is picked up). Returns (ok, result)."""
    while now_ist().time() < until:
        try:
            return True, fn(load_settings())
        except run.NoMarketData:
            raise
        except (Exception, SystemExit) as err:           # SystemExit: run.py's friendly stops
            log.error("%s failed: %s - retrying in %ds", label, err, RETRY_PAUSE)
            if "401" in str(err) or "403" in str(err):
                log.error("Authentication problem. With DHAN_PIN/DHAN_TOTP_SECRET set, token renewal is "
                          "automatic - check those values and your PC clock. Otherwise update "
                          "DHAN_ACCESS_TOKEN in .env.")
            _time.sleep(RETRY_PAUSE)
    log.error("%s: gave up (retry window closed)", label)
    return False, None


def write_summary(day: date, positions: list) -> None:
    path = ROOT / "data" / "daily_summary.csv"
    path.parent.mkdir(exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        if new:
            writer.writerow(["date", "trades", "wins", "losses", "net_pnl"])
        writer.writerow([day.isoformat(), len(positions),
                         sum(p.pnl > 0 for p in positions),
                         sum(p.pnl < 0 for p in positions),
                         round(sum(p.pnl for p in positions), 2)])


# ---------------------------------------------------------------------- one day
def process_day(day: date) -> None:
    cfg = load_settings()
    sleep_until(at(day, UNIVERSE_AT))

    log.info("%s: refreshing universe", day)
    # An older universe file is fine if the refresh fails, but with no file at all
    # (first ever run) we must keep trying, or the scan can never start.
    have_universe = (run.DATA / "universe.csv").exists()
    attempt("universe", run.cmd_universe, until=time(9, 15) if have_universe else SCAN_RETRY_UNTIL)

    sleep_until(at(day, SCAN_AT))
    if not run.load_movers():
        log.info("%s: scanning", day)
        try:
            ok, _ = attempt("scan", lambda c: run.cmd_scan(c, use_history=False), SCAN_RETRY_UNTIL)
        except run.NoMarketData as err:
            log.warning("%s: skipping day - %s", day, err)
            return
        if not ok:
            return

    if not run.load_movers():
        log.info("%s: no qualifying movers today - nothing to trade", day)
        return

    log.info("%s: paper trading (refreshes every minute until %s)", day, cfg.force_exit)
    ok, positions = attempt("trade", lambda c: run.cmd_trade(c, watch=True), cfg.force_exit)
    if ok:
        write_summary(day, positions)
        log.info("%s: done. Net P&L Rs %s", day, f"{sum(p.pnl for p in positions):,.0f}")


# ------------------------------------------------------------------------ forever
def run_forever() -> None:
    setup_logging()
    log.info("Scheduler started (paper only). Press Ctrl+C to stop.")
    cfg = load_settings()
    if cfg.dashboard_enabled:
        try:
            import dashboard
            dashboard.start_in_background(port=cfg.dashboard_port)
            log.info("Dashboard: http://127.0.0.1:%d", cfg.dashboard_port)
        except OSError as err:                   # e.g. port already in use
            log.warning("Dashboard not started (%s). Change DASHBOARD_PORT in .env.", err)
    finished: date | None = None
    try:
        while True:
            cfg, now = load_settings(), now_ist()
            today = now.date()
            day_over = now >= at(today, cfg.force_exit) + timedelta(minutes=2)

            if finished == today or day_over or not is_trading_day(today, load_holidays()):
                nxt = at(today + timedelta(days=1), time(8, 55))
                why = "already done" if finished == today else \
                    "market closed for today" if day_over else "weekend/holiday"
                log.info("%s - sleeping until %s", why, nxt.strftime("%a %d %b %H:%M"))
                sleep_until(nxt)
                continue

            try:
                process_day(today)
            except Exception:                    # never let one bad day kill the scheduler
                log.exception("unexpected error on %s", today)
            finished = today
    except KeyboardInterrupt:
        log.info("Stopped by user.")
