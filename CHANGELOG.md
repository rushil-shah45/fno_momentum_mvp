# Changelog

All notable changes to the F&O Momentum MVP are recorded here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [1.1.1] – 2026-10-09

### Strategy Changes

#### Timeframe: 1-minute → 5-minute (reverted)
- Switched back from 1m to **5-minute candles** (`interval: "5"` in the Dhan API call).
- The 09:15 opening candle is now a proper **5-min candle** (09:15 – 09:20).
- `ENTRY_TIME` restored to `09:20` (the first confirmation candle starts at 09:20).
- Affected files: `strategy.py`, `broker.py`.

#### Confirmation Window Capped at 11:30 IST
- Added `TRADE_CUTOFF = time(11, 25)` in `strategy.py`.
- After the 09:15 5-min candle, the system scans 5-min candles for a
  breakout/breakdown close, but **only up to the 11:25 candle** (which closes at 11:30).
- If no confirmation is found by 11:30, the trade is skipped:
  `"no breakout/breakdown confirmation by 11:30"`.
- Affected file: `strategy.py` (`open_position`).

#### Broker: 5-min Candle Window for Opening Range
- `stock_opening_range()` now fetches `09:15:00`–`09:20:59` (was `09:16:59`).
- Filters for the `09:15` 5-min candle by timestamp.
- Affected file: `broker.py`.

#### Scan: Live Window Restored to 09:20
- Live-snapshot scan window restored to `{(9, 20), (9, 21)}` to match when
  the 5-min candle is fully formed.
- Affected file: `run.py`.

#### Dashboard: "5-min range %" Label Restored
- Movers table column restored to "5-min range %".
- Affected file: `dashboard.py`.

---

## [1.1.0] – 2026-10-07

### Strategy Changes

#### Timeframe: 5-minute → 1-minute
- **Before:** The opening range was built from five 1-minute candles (09:15–09:19) aggregated into a single 5-minute bar.
- **After:** The opening range is now the single **09:15 1-minute candle** only (`ENTRY_TIME` moved from `09:20` → `09:16`).
- Affected files: `strategy.py`, `broker.py` (`stock_opening_range`), `run.py` (live-scan window).

#### Entry: Breakout / Breakdown Confirmation on Candle Close
- **Before:** Entry was taken at the **open** of the first candle at or after 09:20, regardless of direction.
- **After:**
  - After the 09:15 candle closes **bullish** (`CALL` setup), the system waits for a subsequent 1-minute candle whose **close exceeds the 09:15 high** — enters at that candle's close price.
  - After the 09:15 candle closes **bearish** (`PUT` setup), the system waits for a 1-minute candle whose **close drops below the 09:15 low** — enters at that candle's close price.
  - If no confirmation candle occurs before force-exit, the trade is skipped with reason `"no breakout/breakdown confirmation candle found"`.
- Stop is set to:
  - `CALL`: low of the 09:15 candle.
  - `PUT`: high of the 09:15 candle.
- Affected file: `strategy.py` (`open_position`).

#### Lot Sizing: Fixed at 1 Lot
- **Before:** Lot count was computed dynamically via `math.floor(max_risk / risk_per_lot)`, respecting the `MAX_RISK_PER_TRADE` limit.
- **After:** Always **1 lot**, regardless of risk or settings. The `enforce_risk_limit` and `max_risk_per_trade` parameters are preserved in `Settings` but are no longer used for sizing.
- Affected file: `strategy.py` (`open_position`).

#### Trailing Stop Loss: 1-Point Activation, 1-Point Ratchet
- **Before:** Trailing activated when price reached **+1R** above entry; stop was ratcheted to break-even, then tracked candle lows directly.
- **After:**
  - Trailing activates when the candle **closes >= 1 point above entry**.
  - On activation, the stop is moved to `entry + 1.0` (locking in 1 point of profit).
  - While trailing is active, the stop ratchets up to `bar.low - 1.0` whenever that exceeds the current stop (stop stays 1 point below the candle low).
- Affected file: `strategy.py` (`Position.on_bar`).

---

### Dashboard Changes

#### Capital Used Column (Trade Table)
- A new **Capital Used** column is shown in the trade list.
- Formula: `entry_price x qty` (total premium paid, in Rs).
- Displayed in INR format with Indian digit grouping (e.g., Rs12,500).
- Affected files: `run.py` (`trade_row`), `dashboard.py` (`load_trades`, `trade_table`).

#### Trailing TSL Column (Trade Table)
- A new **Trailing** column is shown next to Status in the trade list.
- Shows:
  - **TSL active** (blue badge) — open trade where trailing has been triggered.
  - **Trailed** (grey badge) — closed trade that had trailing active at exit.
  - **Fixed** (grey badge) — stop was never moved to trailing mode.
- Affected files: `dashboard.py` (`trailing_tag`, `trade_table`).

#### Label: "5-min range %" -> "1-min range %"
- The Selected Movers table column label updated to reflect the new 1-minute timeframe.
- Affected file: `dashboard.py` (`render`).

---

## [1.0.0] – 2026-10-05 (initial)

- First working MVP: 5-minute opening range strategy on NSE F&O stocks.
- Dhan broker integration (market data only, paper trading).
- Dashboard: read-only HTML dashboard, auto-refreshing every 30 s.
- Scheduler / daemon mode for automatic daily scan + trade cycle.
