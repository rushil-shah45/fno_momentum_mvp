# F&O Opening-Range Momentum: Comprehensive Architecture & Strategy Summary

This document explains the end-to-end architecture, trading strategy logic, mathematical rules, and step-by-step operational lifecycle of the **F&O Opening-Range Momentum Paper Trading MVP**.

---

## 1. System Overview

The system is an automated, pure-Python paper-trading bot designed for NSE (National Stock Exchange of India) stock options. 

### Key Characteristics
* **Safety First (Read-Only):** The bot consumes read-only market data (quotes, historical candles, security master files). It **never connects to order execution APIs**; all positions and P&L are tracked virtually.
* **Dual Broker Support:** Native market data integration with both **DhanHQ API** and **ICICI Direct (Breeze API)**, toggled effortlessly via an environment variable (`ACTIVE_BROKER=dhan` or `ACTIVE_BROKER=icici`).
* **Zero-Touch Automated Authentication:**
  * **Dhan:** Automatic 24-hour token minting using Client ID, 6-digit PIN, and TOTP secret.
  * **ICICI Direct:** Automated morning login via headless Chromium (Playwright) using User ID, Password, and TOTP secret, capturing the redirect session and generating Breeze API sessions.
* **Zero JavaScript Web Dashboard:** A built-in web server at `http://127.0.0.1:8501` serving server-rendered HTML and inline SVG charts, refreshing automatically via `<meta http-equiv="refresh">`.

---

## 2. Strategy Logic & Formulas (Opening Range Breakout v1.1)

The trading strategy is rooted in **Opening Range Momentum**. It identifies institutional accumulation or distribution in the first 5 minutes of market open (09:15 to 09:20 IST) and executes At-The-Money (ATM) options upon breakout confirmation before 11:30 IST.

```
+-----------------------------------------------------------------------------------+
|                                STRATEGY PIPELINE                                  |
|                                                                                   |
|  1. Universe (~213 F&O Stocks)                                                    |
|         │                                                                         |
|         ▼                                                                         |
|  2. 09:15 - 09:20 5-min Candle (OpeningRange: Open, High, Low, Close, Volume)     |
|         │                                                                         |
|         ▼                                                                         |
|  3. Setup Classification: classify()                                              |
|     ├─ CALL: Open ≈ Low  AND Close in Top 20% of 5-min range                     |
|     └─ PUT : Open ≈ High AND Close in Bottom 20% of 5-min range                   |
|         │                                                                         |
|         ▼                                                                         |
|  4. Ranking: rank_movers()                                                        |
|     └─ Select Top N gainers for CALLs and Top N losers for PUTs                   |
|         │                                                                         |
|         ▼                                                                         |
|  5. Contract Selection: pick_atm()                                                |
|     └─ Expiry ≥ 3 days away, strike closest to 09:20 spot price                   |
|         │                                                                         |
|         ▼                                                                         |
|  6. Confirmation & Paper Entry: open_position()                                   |
|     └─ 5-min candle closes above 09:15 High (CALL) or below 09:15 Low (PUT)       |
|         │                                                                         |
|         ▼                                                                         |
|  7. Trailing & Exit Management: Position.on_bar()                                 |
|     ├─ Hard Stop Loss: 09:15 candle Low (CALL) / High (PUT)                       |
|     ├─ 1-Point Ratchet Trailing Stop once in profit                               |
|     ├─ Portfolio Circuit Breaker (MAX_DAILY_LOSS)                                 |
|     └─ Intraday Square-off (15:00 IST)                                            |
+-----------------------------------------------------------------------------------+
```

---

### Step 1: The Universe Definition
* **Source:** All NSE equity derivatives actively traded in the F&O segment (~213 stocks).
* **Metadata:** Downloaded daily directly from the exchange/broker:
  * For Dhan: Daily `api-scrip-master.csv` filtered for `SEM_INSTRUMENT_NAME == "FUTSTK"`.
  * For ICICI: Daily `SecurityMaster.zip` (`FONSEScripMaster.txt`) filtered for `OPTSTK` contracts.
* **Attributes extracted:** NSE Symbol, Security ID, Lot Size, and Strike steps.

---

### Step 2: The 5-Minute Opening Range (`OpeningRange`)
At market open (09:15 IST), price discovery begins. The bot inspects the first 5-minute candle (09:15 to 09:20 IST):
$$\text{Range} = High_{09:15} - Low_{09:15}$$
$$\text{Range \%} = \frac{\text{Range}}{Low_{09:15}} \times 100$$

---

### Step 3: Setup Classification Rules (`classify()`)

A stock qualifies for a directional breakout setup only if it meets all three conditions:

#### 1. Bullish (CALL) Setup:
1. **Open Equals Low (Buying from open):**
   $$|Open_{09:15} - Low_{09:15}| \le \text{tolerance} \quad (\text{default } 0.05 \text{, i.e. 1 tick})$$
2. **Strong Candle Close (Close near High):**
   The close must be in the top 20% of the candle range:
   $$(High_{09:15} - Close_{09:15}) \le \text{Range} \times \text{close\_zone} \quad (\text{default } 0.20)$$
3. **Range Size Filter:**
   Avoid runaway gap-ups or exhausted moves:
   $$\text{Range \%} \le \text{max\_range\_pct} \quad (\text{default } 3.0\%)$$

#### 2. Bearish (PUT) Setup:
1. **Open Equals High (Selling from open):**
   $$|High_{09:15} - Open_{09:15}| \le \text{tolerance} \quad (\text{default } 0.05 \text{, i.e. 1 tick})$$
2. **Strong Candle Close (Close near Low):**
   The close must be in the bottom 20% of the candle range:
   $$(Close_{09:15} - Low_{09:15}) \le \text{Range} \times \text{close\_zone} \quad (\text{default } 0.20)$$
3. **Range Size Filter:**
   $$\text{Range \%} \le \text{max\_range\_pct} \quad (\text{default } 3.0\%)$$

Stocks not satisfying these criteria are disqualified.

---

### Step 4: Ranking & Selecting Top Movers (`rank_movers()`)

From all qualified setups:
1. **Directional alignment with day's change:**
   * CALL candidates must have positive percentage change vs previous close:
     $$\% \Delta = \frac{Last - Close_{prev}}{Close_{prev}} \times 100 > 0$$
   * PUT candidates must have negative percentage change vs previous close:
     $$\% \Delta < 0$$
2. **Selection:**
   * Sorted by magnitude of $\% \Delta$.
   * Selects up to `TOP_N` (default 5) strongest gainers for CALLs and up to `TOP_N` (default 5) deepest losers for PUTs.

---

### Step 5: Option Contract Selection (`pick_atm()`)

For each selected stock, the bot picks the optimal option contract:
1. **Expiry Date:** Nearest monthly expiry that is at least `MIN_DAYS_TO_EXPIRY` (default 3 days) away. This protects against extreme gamma risk and rapid theta decay on expiry day.
2. **Strike Price:** At-The-Money (ATM) strike, defined as the available strike closest to the underlying spot price at 09:20 IST:
   $$\text{Strike} = \arg\min_c |Strike_c - Spot_{09:20}|$$
3. **Type:** `CE` (Call Option) for CALL setups, `PE` (Put Option) for PUT setups.

---

### Step 6: Confirmation & Entry Trigger (`open_position()`)

Instead of blindly entering at 09:20, the strategy waits for price confirmation:
1. **Setup Reference:** The high and low of the 09:15 candle ($Setup_{high}, Setup_{low}$).
2. **Confirmation Window:** From 09:20 up to 11:25 IST (the candle closing at 11:30 IST).
3. **Trigger Condition:**
   * **CALL:** The first 5-minute candle whose **Close** breaks above $Setup_{high}$:
     $$Close_{bar} > Setup_{high}$$
   * **PUT:** The first 5-minute candle whose **Close** breaks below $Setup_{low}$:
     $$Close_{bar} < Setup_{low}$$
4. **Entry Execution:**
   * **Entry Price ($Entry$):** The closing price of that confirmation candle.
   * **Initial Stop-Loss ($Stop$):**
     * For CALL: $Setup_{low}$
     * For PUT: $Setup_{high}$
   * **Per-Unit Risk ($R$):**
     $$\text{Risk} = |Entry - Stop|$$
   * **Sizing:** Fixed at 1 lot per trade.
   * If no breakout occurs by 11:30 IST, the setup is abandoned.

---

### Step 7: Position Management & Trailing Stop (`Position.on_bar()`)

All options are **bought** (long premium). Each subsequent 5-minute bar updates the position state:
1. **Hard Stop Check:** If $Low_{bar} \le Stop$, position is closed immediately at $Stop$ with status `EXIT_STOP`.
2. **Trailing Stop Activation (1-Point TSL):**
   * If price closes $\ge Entry + 1.0$, trailing mode becomes **Active**:
     $$Stop = \max(Stop, Entry + 1.0)$$
3. **Ratchet Trailing:**
   * Once trailing is active, on every new candle where:
     $$Low_{bar} - 1.0 > Stop$$
     The stop is ratcheted up:
     $$Stop = Low_{bar} - 1.0$$
4. **Metrics Tracked:**
   * $\text{P\&L} = (\text{Current Price} - Entry) \times \text{Lots} \times \text{Lot Size}$
   * $\text{R-Multiple} = \frac{\text{Current Price} - Entry}{\text{Risk}}$
   * $\text{Capital Used} = Entry \times \text{Lots} \times \text{Lot Size}$

---

### Step 8: Risk Limits & Portfolio Circuit Breakers

* **Per-Trade Cap (`MAX_RISK_PER_TRADE` = ₹250):** If risk exceeds allowable limit, trade is skipped.
* **Daily Circuit Breaker (`MAX_DAILY_LOSS` = ₹500):** If the aggregate portfolio P&L (realized + unrealized) touches or falls below $-\text{MAX\_DAILY\_LOSS}$, all open positions are immediately terminated with status `EXIT_DAILY_LOSS`.
* **Time Cutoff (`FORCE_EXIT` = 15:00 IST):** Any positions still open at 15:00 IST are closed at the last traded price with status `EXIT_FORCED`.

---

## 3. Step-by-Step Daily Execution Lifecycle

The scheduler daemon (`python run.py daemon`) coordinates the entire trading day automatically:

| Time (IST) | Scheduled Event | Logic Performed | Output Artifacts |
| :--- | :--- | :--- | :--- |
| **Pre-Market** | Daemon Initialization | Validates calendar (`holidays.txt`), skips weekends/holidays, initializes logging to `data/scanner.log`. | `data/scanner.log` |
| **09:00:00** | Universe Refresh | Downloads scrip master from active broker, filters for ~213 liquid F&O stocks, saves symbol mapping and lot sizes. | `data/universe.csv` |
| **09:14:00** | Auth Pre-flight | Checks broker token validity; auto-renews via Dhan API or ICICI Playwright if expiring. | `data/token.json` or `data/icici_token.json` |
| **09:20:05** | Opening Range Scan | Fetches 09:15-09:20 5-min OHLC for all universe stocks. Runs `classify()` and `rank_movers()`. | `data/<date>/setups.csv`<br>`data/<date>/movers.csv` |
| **09:20 - 15:00** | Paper Trading Engine | Monitors option candles. Evaluates confirmation breakout, triggers entries, ratchets trailing stops, checks portfolio loss limits. | `data/<date>/trades.csv`<br>`data/<date>/skipped.csv` |
| **15:00:00** | Intraday Force Exit | Force-squares off any remaining open paper trades. Logs final statistics. | `data/daily_summary.csv` |
| **Continuous** | Live Web Dashboard | Serves real-time P&L, trade logs, and charts on `http://127.0.0.1:8501`. | Web UI |

---

## 4. Codebase Architecture & File Roles

```
fno_momentum_mvp/
├── settings.py         # Configuration loader (.env parser with quote-stripping)
├── auth.py             # Dual-broker auth engine (Dhan TOTP + ICICI Playwright/Breeze)
├── broker.py           # Unified broker abstraction (Dhan & ICICI Direct implementations)
├── strategy.py         # Pure mathematical strategy & position management (no I/O)
├── scheduler.py        # Automated daily daemon scheduler with retries
├── dashboard.py        # Zero-JS HTTP dashboard with SVG charts
├── run.py              # CLI entry point (universe, scan, trade, login, daemon, dashboard)
├── tests/              # Complete test suite (64 unit tests covering auth, broker, strategy)
├── holidays.txt        # NSE market holidays list
└── data/               # Daily CSV logs, token caches, and summary records
```

### Module Responsibilities:

1. **`settings.py`**:
   * Reads `.env` and environment variables into an immutable `Settings` dataclass.
   * Strips surrounding quotes and provides defaults for risk parameters (`MAX_RISK_PER_TRADE`, `MAX_DAILY_LOSS`, `FORCE_EXIT`).

2. **`auth.py`**:
   * **Dhan Flow:** Post request with Client ID, 6-digit PIN, and dynamic TOTP generated via Python's standard `hmac`/`hashlib`.
   * **ICICI Flow:** Headless Playwright script that logs into ICICI Direct, fills User ID and Password, enters the 6-digit TOTP, captures the callback URL containing `apisession`, and initializes BreezeConnect session keys.
   * Multi-broker dispatchers: `get_access_token()`, `ensure_login()`, `check_access()`.

3. **`broker.py`**:
   * Abstract interface defining:
     * `quotes(security_ids)`: Snapshot quotes with OHLC and previous close.
     * `stock_opening_range(security_id, day)`: 5-minute opening candle.
     * `option_candles(security_id, day)`: 5-minute historical candles for options.
   * Concrete implementations: `Dhan` (REST API) and `ICICIDirect` (`BreezeConnect` SDK).
   * Factory method `get_broker(cfg)` resolves the active broker.

4. **`strategy.py`**:
   * Completely decoupled from network and disk for fast testing.
   * Contains dataclasses `Bar`, `OpeningRange`, `Contract`, `Position`.
   * Implements `classify()`, `rank_movers()`, `pick_atm()`, `open_position()`, and `run_session()`.

5. **`scheduler.py`**:
   * Implements the cron-like execution engine with retry loops, holiday detection, and daily CSV aggregation in `data/daily_summary.csv`.

6. **`dashboard.py`**:
   * Self-contained `ThreadingHTTPServer` on port 8501.
   * Translates CSV data into styled HTML tables and inline SVG performance charts.

7. **`run.py`**:
   * The command-line interface:
     * `python run.py universe`: Build/refresh the ~213 stock universe.
     * `python run.py scan`: Run the 09:20 momentum scan and print qualified setups.
     * `python run.py trade`: Simulate option trading for the day.
     * `python run.py --login`: On-demand broker authentication.
     * `python run.py daemon`: Start the complete automated daily daemon.
     * `python run.py dashboard`: Start only the web dashboard.

---

## 5. Summary of Generated Data Artifacts

Every trading day creates a dedicated directory under `data/<YYYY-MM-DD>/`:
* **`setups.csv`**: Every stock evaluated, its 5-minute OHLC, calculated range %, and qualification status.
* **`movers.csv`**: The selected top gainers and losers.
* **`trades.csv`**: Complete trade log including entry time, strike, premium, stop loss, exit time, exit reason, and P&L.
* **`skipped.csv`**: Stocks that qualified but had no breakout confirmation or breached risk caps.
* **`daily_summary.csv`** (in `data/`): Lifetime ledger with columns: `date, trades, wins, losses, net_pnl`.

---

## 6. Verification & Testing

The system includes a dedicated unit test suite in `tests/`:
* **`test_strategy.py`**: Validates classification rules, ranking, ATM selection, trailing stop logic, and loss cutoff.
* **`test_broker.py` & `test_icici.py`**: Validates API response parsing, option security master decompression, error handling, and session verification.
* **`test_scheduler.py`**: Validates trading day checks, retry mechanics, and daily summary generation.

To run all unit tests:
```bash
python -m unittest discover tests
```
*Current test suite status: **64 passed, 0 failed**.*
