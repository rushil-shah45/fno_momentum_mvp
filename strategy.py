"""Pure strategy logic. No network, no files, so it is easy to test.

Pipeline:  classify()  ->  rank_movers()  ->  pick_atm()  ->  open_position()
           -> Position.on_bar() driven by run_session()
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
MARKET_OPEN = time(9, 15)
ENTRY_TIME = time(9, 16)   # v1.1: only the 09:15 1-min candle is the "opening" candle


# --------------------------------------------------------------------------- data
@dataclass(frozen=True)
class Bar:
    ts: int            # epoch seconds
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass(frozen=True)
class OpeningRange:
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass(frozen=True)
class Contract:
    security_id: str
    option_type: str   # "CE" or "PE"
    expiry: date
    strike: float
    lot_size: int


def clock(ts: int) -> time:
    return datetime.fromtimestamp(ts, IST).time()


def fmt_time(ts: int | None) -> str:
    return "" if ts is None else datetime.fromtimestamp(ts, IST).strftime("%H:%M:%S")


# ------------------------------------------------------------ 1. the setup signal
def classify(r: OpeningRange, tolerance: float, close_zone: float,
             max_range_pct: float) -> tuple[str, str, float]:
    """Return (direction, reason, range_pct) for the 09:15 1-min opening candle.

    CALL: Open = Low  and the 1-min close in the top 20% of the range (bullish candle).
    PUT : Open = High and the 1-min close in the bottom 20% of the range (bearish candle).
    direction is "" when the stock does not qualify.
    """
    rng = r.high - r.low
    if rng <= 0 or r.low <= 0:
        return "", "flat opening range", 0.0
    range_pct = rng / r.low * 100
    if range_pct > max_range_pct:
        return "", "opening range too large", range_pct

    close_near_high = (r.high - r.close) <= rng * close_zone
    close_near_low = (r.close - r.low) <= rng * close_zone
    if abs(r.open - r.low) <= tolerance and close_near_high:
        return "CALL", "Open = Low, 1-min close near High (bullish)", range_pct
    if abs(r.high - r.open) <= tolerance and close_near_low:
        return "PUT", "Open = High, 1-min close near Low (bearish)", range_pct
    return "", "pattern not qualified", range_pct


# ------------------------------------------------------- 2. keep only top movers
def rank_movers(setups: list[dict], top_n: int) -> list[dict]:
    """Calls must be gainers, Puts must be losers. Keep the strongest top_n of each.

    Each setup dict needs: direction ("CALL"/"PUT") and change_pct (vs prev close).
    """
    gainers = sorted((s for s in setups if s["direction"] == "CALL" and s["change_pct"] > 0),
                     key=lambda s: s["change_pct"], reverse=True)[:top_n]
    losers = sorted((s for s in setups if s["direction"] == "PUT" and s["change_pct"] < 0),
                    key=lambda s: s["change_pct"])[:top_n]
    return gainers + losers


# --------------------------------------------------------- 3. choose the ATM option
def pick_atm(contracts: list[Contract], spot: float, today: date,
             min_days: int) -> Contract:
    """Nearest expiry that is >= min_days away, then the strike closest to spot."""
    eligible = [c for c in contracts if c.expiry >= today + timedelta(days=min_days)]
    if not eligible:
        raise ValueError(f"no expiry at least {min_days} days away")
    expiry = min(c.expiry for c in eligible)
    return min((c for c in eligible if c.expiry == expiry),
               key=lambda c: abs(c.strike - spot))


# ------------------------------------------------------------ 4. paper position
@dataclass
class Position:
    symbol: str
    direction: str
    contract: Contract
    lots: int
    entry_ts: int
    entry: float
    initial_stop: float
    stop: float
    risk: float                  # rupees of premium per unit (entry - initial_stop)
    trailing: bool = False
    status: str = "OPEN"
    last_price: float = 0.0
    exit_price: float | None = None
    exit_ts: int | None = None

    @property
    def qty(self) -> int:
        return self.lots * self.contract.lot_size

    @property
    def price_now(self) -> float:
        return self.exit_price if self.exit_price is not None else self.last_price

    @property
    def pnl(self) -> float:
        return (self.price_now - self.entry) * self.qty

    @property
    def r_multiple(self) -> float:
        return (self.price_now - self.entry) / self.risk

    def close(self, price: float, ts: int, status: str) -> None:
        self.exit_price, self.exit_ts, self.status = price, ts, status

    def on_bar(self, bar: Bar) -> None:
        """Advance one 1-minute option candle. We always BUY options, so a stop
        sits below price and triggers when the bar's low touches it.

        v1.1 trailing rule: once price closes 1 point above entry, trailing
        becomes active and the stop is ratcheted up by 1 point whenever the
        candle low exceeds the previous stop + 1 point.
        """
        if self.status != "OPEN":
            return
        self.last_price = bar.close
        if bar.low <= self.stop:
            self.close(self.stop, bar.ts, "EXIT_STOP")
        elif not self.trailing and bar.close >= self.entry + 1.0:
            # Price moved 1 point above entry on close: activate 1-point TSL.
            self.trailing = True
            self.stop = max(self.stop, self.entry + 1.0)
        elif self.trailing:
            # Ratchet stop up in 1-point increments as price advances.
            new_stop = bar.low - 1.0
            if new_stop > self.stop:
                self.stop = new_stop


def open_position(symbol: str, direction: str, contract: Contract, bars: list[Bar],
                  max_risk: float, enforce_risk: bool
                  ) -> tuple[Position | None, list[Bar], str]:
    """Build the paper position from the option's candles.

    v1.1 logic (1-min timeframe):
      - Opening bar  = the single 09:15 1-min candle.
      - Confirmation = the first subsequent 1-min candle whose CLOSE breaks above
                       the 09:15 high (CALL) or below the 09:15 low (PUT).
      - Entry        = the close price of that confirmation candle.
      - Stop         = the low of the 09:15 candle (CALL) or its high (PUT).
      - Lots         = always 1 (fixed).

    Returns (position or None, remaining bars after entry, skip_reason).
    """
    opening = [b for b in bars if clock(b.ts) == MARKET_OPEN]
    later = [b for b in bars if clock(b.ts) >= ENTRY_TIME]
    if not opening:
        return None, [], "no 09:15 candle available"
    if not later:
        return None, [], "no candles after 09:15 to confirm breakout"

    setup = opening[0]                   # the single 09:15 1-min candle
    setup_high = setup.high
    setup_low = setup.low

    # Scan for the confirmation candle (close beyond the 09:15 range).
    confirm_idx: int | None = None
    for i, b in enumerate(later):
        if direction == "CALL" and b.close > setup_high:
            confirm_idx = i
            break
        if direction == "PUT" and b.close < setup_low:
            confirm_idx = i
            break

    if confirm_idx is None:
        return None, [], "no breakout/breakdown confirmation candle found"

    entry_bar = later[confirm_idx]
    rest = later[confirm_idx + 1:]
    entry = entry_bar.close              # enter on the confirmation candle close

    # Stop: below the 09:15 low for CALL; above the 09:15 high for PUT.
    stop = setup_low if direction == "CALL" else setup_high
    risk = abs(entry - stop)
    if entry <= 0 or risk <= 0:
        return None, [], "invalid entry or zero risk"

    lots = 1                             # v1.1: always 1 lot

    pos = Position(symbol, direction, contract, lots, entry_bar.ts, entry, stop, stop,
                   risk, last_price=entry_bar.close)
    return pos, rest, ""


def run_session(positions: list[Position], later: dict[str, list[Bar]],
                max_daily_loss: float, force_exit: time) -> str:
    """Replay every position minute by minute, sharing one daily-loss limit.

    Returns a note: "" normally, or why the session was cut short.
    """
    by_symbol = {s: {b.ts: b for b in bars} for s, bars in later.items()}
    for ts in sorted({b.ts for bars in later.values() for b in bars}):
        if clock(ts) >= force_exit:
            for p in positions:
                if p.status == "OPEN":
                    p.close(p.last_price, ts, "EXIT_FORCED")
            return "force-exit time reached"

        for p in positions:
            bar = by_symbol[p.symbol].get(ts)
            if bar:
                p.on_bar(bar)

        if sum(p.pnl for p in positions) <= -max_daily_loss:   # realised + open
            for p in positions:
                if p.status == "OPEN":
                    p.close(p.last_price, ts, "EXIT_DAILY_LOSS")
            return "daily loss limit hit"
    return ""
