# F&O Opening-Range Momentum: Paper Trading MVP

A small, Python-only (no JavaScript) paper-trading tool for NSE stock options.
It reads Dhan market data and **cannot place orders**.

## The idea

1. **Setup (09:15 to 09:20 IST).** Look at each F&O stock's first 5-minute range.
   - `Open = Low` and the close in the top 20% of the range -> **CALL** candidate
   - `Open = High` and the close in the bottom 20% of the range -> **PUT** candidate
   - Skip stocks whose range is wider than 3%.
2. **Select.** Calls must be among today's top gainers, Puts among top losers (vs previous close). Keep the top 5 of each.
3. **Enter (09:20).** Paper-buy the ATM option: nearest expiry at least 3 days away, strike closest to the stock's 5-minute close.
4. **Manage.**
   - Stop = lowest low of the option's 09:15 to 09:20 candles.
   - At +1R the stop moves to entry, then trails each candle's low.
   - Exit on stop, on the daily-loss limit, or at the force-exit time (default 15:00).

## Layout

```
settings.py   config from .env
strategy.py   pure logic: classify, rank, pick ATM, Position, run_session (no network)
broker.py     read-only Dhan client + instrument master
run.py        CLI: universe | scan | trade | daemon | dashboard
scheduler.py  run-once daily loop
auth.py       automatic Dhan tokens (TOTP)
dashboard.py  web dashboard (no JavaScript)
tests/        offline unit tests
```

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env        # then add your Dhan Client ID and login details (see below)
python -m unittest discover -s tests    # optional: verify the logic offline
```

## No more manual access tokens (automatic login)

Dhan access tokens expire after 24 hours, and the "Renew Token" API only works on a token that is still
active, so it cannot revive an expired one. The reliable fix is Dhan's TOTP login endpoint, which this
project uses (`auth.py`):

1. Log in to Dhan Web, open **DhanHQ Trading APIs**, choose **Setup TOTP**, and confirm with the OTP.
2. When the QR code appears, also copy the **text secret key** shown with it, and finish by entering the first code in an authenticator app.
3. Put these in `.env`:
   ```
   DHAN_CLIENT_ID=...
   DHAN_PIN=your 6-digit Dhan PIN
   DHAN_TOTP_SECRET=the secret key from step 2
   ```
   Leave `DHAN_ACCESS_TOKEN` empty.

From then on the program creates a token itself, caches it in `data/token.json`, replaces it when
less than 8 hours remain, and renews it automatically if Dhan answers 401 mid-session.

Notes:
- Your **PIN + TOTP secret are as sensitive as your password**. Keep `.env` private (it is gitignored) and never share it.
- Your PC clock must be accurate (within about 30 seconds) or the TOTP codes will be rejected.
- Generating a new token may stop an older token from working (Dhan's docs describe one active token for the API-key flow), so don't run other tools that use a separate manual token at the same time.
- If you leave the PIN/secret empty, it falls back to `DHAN_ACCESS_TOKEN` and you are back to manual renewal.

## Run once, automatic every day (recommended)

```bash
python run.py daemon
```

Leave it running. Every trading day it does the following by itself (IST):

| Time | Action |
|---|---|
| 09:00 | refresh the F&O universe |
| 09:20:05 | scan and select the top movers |
| 09:20 to 15:00 | paper-trade the ATM options, refreshing every minute |
| 15:00 | force-exit, append the day to `data/daily_summary.csv`, sleep until tomorrow |

- **Weekends** are skipped automatically. **NSE holidays**: list them in `holidays.txt`. There is also a best-effort check that skips a day when the quotes are clearly from a previous day.
- **Errors** (network, rate limits, bad token) are logged to `data/scanner.log` and retried every 60 seconds. One bad day never stops the scheduler.
- **Token expiry:** with the automatic login above, tokens renew themselves. With a manual token, `.env` is re-read on every attempt, so paste a new `DHAN_ACCESS_TOKEN` while the daemon runs. No restart needed.
- **Restart-safe:** if you stop it and start it again mid-day, it reuses today's movers and carries on.
- The computer must stay **on and awake** (disable sleep) and online. If you want it to start automatically after a reboot, add `python run.py daemon` to Windows Task Scheduler ("At log on").

## Dashboard

```bash
python run.py dashboard        # then open http://127.0.0.1:8501
```

`python run.py daemon` starts it automatically too (turn off with `DASHBOARD=false`, change the port with `DASHBOARD_PORT`).

It shows, for today or any past day: every trade's **entry time/price, stop, exit time/price, P&L, R-multiple and status**, plus **net P&L, win rate, best/worst trade, avg win/loss**, the selected movers and the trades that were skipped (with the reason). A second section shows **all-days totals**: total P&L, win rate, winning days, profit factor, max drawdown, a cumulative P&L curve and daily P&L bars.

- Pure Python (standard library): the page is plain HTML + inline SVG, with **no JavaScript**. Today's view refreshes itself every 30 seconds.
- Read-only: it only reads `data/<date>/*.csv`, so it needs no broker login and works after market close.
- Local only: it listens on `127.0.0.1`, so only this PC can open it.
- Win rate counts **closed** trades only; open trades appear in net P&L as unrealised.

## Manual commands (IST)

```bash
python run.py universe          # first run, then occasionally
python run.py scan              # run at 09:20-09:21 for the fast live snapshot
python run.py trade             # one-off paper replay
python run.py trade --watch     # refresh every minute until force exit
```

Outputs go to `data/<date>/`: `setups.csv`, `movers.csv`, `trades.csv`, `skipped.csv`.
If you run `scan` outside 09:20-09:21 it falls back to historical candles
(about one request per stock, roughly 4 minutes for 210 stocks).

## Differences from the original project

| Original | This MVP |
|---|---|
| Risk limits in `.env` were never used | `MAX_RISK_PER_TRADE` sizes lots (or skips the trade); `MAX_DAILY_LOSS` closes everything when breached |
| Entry candle chosen by list position | Chosen by clock time, which is correct when an illiquid option has no 09:15 candle |
| Epoch timestamps in CSVs | Readable `HH:MM:SS` IST |
| 11 step scripts, PowerShell launchers, JS dashboard and Excel builders | 4 modules, 3 commands, CSV and terminal output |

## Known limits

- Same-day only: it needs live quotes for the previous close and last price.
- Fills are assumed at the candle open and at the exact stop price. Real fills have slippage and wide spreads, especially on cheap options.
- Daily loss is checked on realised plus open mark-to-market P&L at each candle close.
- `ENFORCE_RISK_LIMIT=true` may skip most trades because one lot of many stocks risks more than Rs 250. Set it to `false` to always trade 1 lot.
- The Dhan field names are taken from the original project's working code. I could not test against the live API here, only the logic offline.
