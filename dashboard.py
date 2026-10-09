"""Read-only web dashboard. Pure Python: the server builds plain HTML + inline SVG,
so there is no JavaScript anywhere. The page refreshes itself with an HTML meta tag.

    python run.py dashboard            -> http://127.0.0.1:8501
(`python run.py daemon` also starts it in the background.)

It only reads the CSV files in data/<date>/ and never talks to the broker, so it
works without credentials, and also after the market has closed.
"""

from __future__ import annotations

import csv
import re
import threading
from datetime import date, datetime
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from settings import ROOT
from strategy import IST

DATA = ROOT / "data"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
REFRESH_SECONDS = 30


# ------------------------------------------------------------------ formatting
def inr(x: float, decimals: int = 0) -> str:
    """Rupees with Indian digit grouping, e.g. 1234567 -> -₹12,34,567."""
    sign = "-" if x < 0 else ""
    whole, _, frac = f"{abs(x):.{decimals}f}".partition(".")
    if len(whole) > 3:
        head, tail, parts = whole[:-3], whole[-3:], []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts + [tail])
    return f"{sign}₹{whole}" + (f".{frac}" if frac else "")


def pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.0f}%"


def tone(x: float) -> str:
    return "pos" if x > 0 else "neg" if x < 0 else "flat"


# ------------------------------------------------------------------- data loading
def _read_rows(path) -> list[dict]:
    try:
        with path.open(encoding="utf-8") as fh:
            return list(csv.DictReader(fh))
    except OSError:
        return []


def load_trades(day: str) -> list[dict]:
    trades = []
    for row in _read_rows(DATA / day / "trades.csv"):
        try:
            row["pnl"] = float(row["pnl"])
            row["r_multiple"] = float(row["r_multiple"])
            row["entry"] = float(row["entry"])
            row["exit_or_last"] = float(row["exit_or_last"])
            row["capital_used"] = float(row.get("capital_used") or 0)
            trades.append(row)
        except (KeyError, ValueError, TypeError):
            continue                                     # skip a malformed row, keep the rest
    return trades


def list_days() -> list[str]:
    if not DATA.exists():
        return []
    return sorted(p.name for p in DATA.iterdir()
                  if p.is_dir() and DATE_RE.match(p.name) and (p / "trades.csv").exists())


def summarize(trades: list[dict]) -> dict:
    closed = [t for t in trades if t["status"] != "OPEN"]
    wins = [t for t in closed if t["pnl"] > 0]
    losses = [t for t in closed if t["pnl"] < 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = -sum(t["pnl"] for t in losses)
    return {
        "trades": len(trades), "open": len(trades) - len(closed), "closed": len(closed),
        "wins": len(wins), "losses": len(losses),
        "win_rate": len(wins) / len(closed) * 100 if closed else None,
        "net": sum(t["pnl"] for t in trades),
        "realised": sum(t["pnl"] for t in closed),
        "unrealised": sum(t["pnl"] for t in trades if t["status"] == "OPEN"),
        "best": max((t["pnl"] for t in trades), default=0.0),
        "worst": min((t["pnl"] for t in trades), default=0.0),
        "avg_win": gross_win / len(wins) if wins else 0.0,
        "avg_loss": -gross_loss / len(losses) if losses else 0.0,
        "profit_factor": gross_win / gross_loss if gross_loss else None,
    }


def history() -> dict:
    """All recorded days -> daily P&L, cumulative curve, drawdown, overall stats."""
    daily, all_trades = [], []
    for day in list_days():
        trades = load_trades(day)
        if trades:
            daily.append((day, sum(t["pnl"] for t in trades)))
            all_trades += trades
    cumulative, running, peak, max_dd = [], 0.0, 0.0, 0.0
    for day, pnl in daily:
        running += pnl
        peak = max(peak, running)
        max_dd = max(max_dd, peak - running)
        cumulative.append((day, running))
    stats = summarize(all_trades)
    stats.update(days=len(daily), winning_days=sum(p > 0 for _, p in daily), max_drawdown=max_dd,
                 avg_day=(running / len(daily)) if daily else 0.0)
    return {"daily": daily, "cumulative": cumulative, "stats": stats}


# ------------------------------------------------------------------------ charts
def _scale(values: list[float], height: int, pad: int) -> tuple:
    lo, hi = min(0.0, *values), max(0.0, *values)
    if hi == lo:
        hi = lo + 1
    return (lambda v: pad + (hi - v) / (hi - lo) * (height - 2 * pad)), lo, hi


def line_chart(points: list[tuple[str, float]], width: int = 720, height: int = 220) -> str:
    if not points:
        return '<p class="muted">No closed days yet.</p>'
    pad = 28
    y, _, _ = _scale([v for _, v in points], height, pad)
    n = len(points)
    xs = [pad + (i * (width - 2 * pad) / (n - 1) if n > 1 else (width - 2 * pad) / 2)
          for i in range(n)]
    coords = " ".join(f"{x:.1f},{y(v):.1f}" for x, (_, v) in zip(xs, points))
    dots = "".join(f'<circle cx="{x:.1f}" cy="{y(v):.1f}" r="3" class="dot"><title>'
                   f'{escape(d)}: {escape(inr(v))}</title></circle>' for x, (d, v) in zip(xs, points))
    last = points[-1][1]
    return (f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" '
            f'aria-label="Cumulative profit and loss">'
            f'<line x1="{pad}" x2="{width - pad}" y1="{y(0):.1f}" y2="{y(0):.1f}" class="axis"/>'
            f'<polyline points="{coords}" class="line {tone(last)}" fill="none"/>{dots}'
            f'<text x="{pad}" y="{height - 6}" class="lbl">{escape(points[0][0])}</text>'
            f'<text x="{width - pad}" y="{height - 6}" class="lbl" text-anchor="end">'
            f'{escape(points[-1][0])}</text></svg>')


def bar_chart(items: list[tuple[str, float]], width: int = 720, height: int = 220) -> str:
    if not items:
        return '<p class="muted">Nothing to chart yet.</p>'
    pad = 28
    y, _, _ = _scale([v for _, v in items], height, pad)
    slot = (width - 2 * pad) / len(items)
    bw = min(48.0, slot * 0.6)
    out = [f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" aria-label="Profit and loss bars">',
           f'<line x1="{pad}" x2="{width - pad}" y1="{y(0):.1f}" y2="{y(0):.1f}" class="axis"/>']
    for i, (label, v) in enumerate(items):
        cx = pad + slot * (i + 0.5)
        top, bottom = sorted((y(v), y(0)))
        out.append(f'<rect x="{cx - bw / 2:.1f}" y="{top:.1f}" width="{bw:.1f}" '
                   f'height="{max(bottom - top, 1):.1f}" class="bar {tone(v)}">'
                   f'<title>{escape(label)}: {escape(inr(v))}</title></rect>')
        out.append(f'<text x="{cx:.1f}" y="{height - 6}" class="lbl" text-anchor="middle">'
                   f'{escape(label[:9])}</text>')
    out.append("</svg>")
    return "".join(out)


# -------------------------------------------------------------------------- HTML
CSS = """
:root{--bg:#f6f7f9;--card:#fff;--text:#16181d;--muted:#6b7280;--line:#e5e7eb;--pos:#15803d;
--neg:#b91c1c;--flat:#6b7280;--accent:#2563eb;--posbg:#dcfce7;--negbg:#fee2e2;--openbg:#dbeafe}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#171a21;--text:#e8eaed;--muted:#9aa3af;
--line:#2a2f3a;--pos:#4ade80;--neg:#f87171;--flat:#9aa3af;--accent:#60a5fa;--posbg:#14301f;
--negbg:#3b1717;--openbg:#13294b}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:1100px;margin:0 auto;padding:20px 16px 48px}
header{display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center;justify-content:space-between}
h1{font-size:20px;margin:0}h2{font-size:15px;margin:28px 0 10px}
.badge{font-size:11px;font-weight:600;padding:2px 8px;border-radius:99px;background:var(--openbg);
color:var(--accent)}.muted{color:var(--muted)}
nav{display:flex;flex-wrap:wrap;gap:6px;margin:14px 0}
nav a{padding:4px 10px;border:1px solid var(--line);border-radius:6px;color:var(--text);
text-decoration:none;background:var(--card)}nav a.on{background:var(--accent);color:#fff;border-color:var(--accent)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.card .k{font-size:12px;color:var(--muted)}.card .v{font-size:22px;font-weight:650;margin-top:2px}
.card .s{font-size:12px;color:var(--muted)}
.pos{color:var(--pos)}.neg{color:var(--neg)}.flat{color:var(--flat)}
.wrap{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:10px}
table{border-collapse:collapse;width:100%;min-width:780px;font-size:13px}
th,td{padding:7px 8px;text-align:right;border-bottom:1px solid var(--line);white-space:nowrap}
th{font-size:12px;color:var(--muted);font-weight:600}tr:last-child td{border-bottom:0}
th:first-child,td:first-child,th.l,td.l{text-align:left}
.tag{display:inline-block;padding:1px 8px;border-radius:99px;font-size:12px;font-weight:600}
.tag.pos{background:var(--posbg)}.tag.neg{background:var(--negbg)}.tag.flat{background:var(--line)}
.tag.open{background:var(--openbg);color:var(--accent)}
.chart{width:100%;height:auto;background:var(--card);border:1px solid var(--line);border-radius:10px}
.axis{stroke:var(--line);stroke-width:1}.lbl{fill:var(--muted);font-size:10px}
.line{stroke-width:2.2}.line.pos{stroke:var(--pos)}.line.neg{stroke:var(--neg)}.line.flat{stroke:var(--flat)}
.dot{fill:var(--accent)}.bar.pos{fill:var(--pos)}.bar.neg{fill:var(--neg)}.bar.flat{fill:var(--flat)}
.two{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:14px}
footer{margin-top:32px;font-size:12px;color:var(--muted)}
"""

STATUS = {"OPEN": "Open", "EXIT_STOP": "Stop hit", "EXIT_FORCED": "Time exit",
          "EXIT_DAILY_LOSS": "Daily-loss exit"}


def card(key: str, value: str, sub: str = "", cls: str = "") -> str:
    return (f'<div class="card"><div class="k">{escape(key)}</div>'
            f'<div class="v {cls}">{escape(value)}</div><div class="s">{escape(sub)}</div></div>')


def status_tag(t: dict) -> str:
    if t["status"] == "OPEN":
        return '<span class="tag open">Open</span>'
    label = STATUS.get(t["status"], t["status"])
    if t["status"] == "EXIT_STOP" and t["pnl"] > 0:
        label = "Trailed out"
    return f'<span class="tag {tone(t["pnl"])}">{escape(label)}</span>'


def contract_label(t: dict) -> str:
    try:
        strike = f"{float(t['strike']):g}"
        expiry = date.fromisoformat(t["expiry"]).strftime("%d %b")
    except (KeyError, ValueError):
        strike, expiry = t.get("strike", ""), t.get("expiry", "")
    return f"{t.get('symbol', '')} {strike} {t.get('option', '')} · {expiry}"


def trailing_tag(t: dict) -> str:
    """Show TSL badge when trailing is active; plain stop value otherwise."""
    is_trailing = str(t.get("trailing", "")).lower() in ("true", "1")
    if is_trailing and t["status"] == "OPEN":
        return '<span class="tag open">TSL active</span>'
    if is_trailing:
        return '<span class="tag flat">Trailed</span>'
    return '<span class="tag flat">Fixed</span>'


def trade_table(trades: list[dict]) -> str:
    if not trades:
        return '<p class="muted">No trades recorded for this day.</p>'
    rows = []
    for t in sorted(trades, key=lambda x: (x.get("entry_time", ""), x.get("symbol", ""))):
        capital = t.get("capital_used", 0)
        capital_str = inr(float(capital)) if capital else "-"
        rows.append(
            f'<tr><td>{escape(t["symbol"])}</td>'
            f'<td class="l">{escape(t["direction"])}</td>'
            f'<td class="l">{escape(contract_label(t))}</td>'
            f'<td>{escape(str(t["lots"]))} / {escape(str(t["qty"]))}</td>'
            f'<td class="pos">{capital_str}</td>'
            f'<td>{escape(t["entry_time"])}</td><td>{t["entry"]:.2f}</td>'
            f'<td>{escape(str(t["initial_stop"]))} → {escape(str(t["stop_now"]))}</td>'
            f'<td>{escape(t["exit_time"] or "-")}</td>'
            f'<td>{t["exit_or_last"]:.2f}</td>'
            f'<td class="{tone(t["pnl"])}"><b>{escape(inr(t["pnl"]))}</b></td>'
            f'<td class="{tone(t["r_multiple"])}">{t["r_multiple"]:+.2f}R</td>'
            f'<td>{trailing_tag(t)}</td>'
            f'<td>{status_tag(t)}</td></tr>')
    head = ("<tr><th>Stock</th><th class='l'>Side</th><th class='l'>Contract</th><th>Lots / Qty</th>"
            "<th>Capital Used</th>"
            "<th>Entry time</th><th>Entry ₹</th><th>Stop (start → now)</th><th>Exit time</th>"
            "<th>Exit / Last ₹</th><th>P&amp;L</th><th>R</th><th>Trailing</th><th>Status</th></tr>")
    return f'<div class="wrap"><table>{head}{"".join(rows)}</table></div>'


def small_table(rows: list[dict], cols: list[tuple[str, str]]) -> str:
    if not rows:
        return '<p class="muted">None.</p>'
    head = "".join(f"<th>{escape(label)}</th>" for _, label in cols)
    body = "".join("<tr>" + "".join(f"<td>{escape(str(r.get(k, '')))}</td>" for k, _ in cols) + "</tr>"
                   for r in rows)
    return f'<div class="wrap"><table style="min-width:0"><tr>{head}</tr>{body}</table></div>'


def render(requested_day: str | None = None) -> str:
    today = datetime.now(IST).strftime("%Y-%m-%d")
    days = list_days()
    day = requested_day if requested_day and DATE_RE.match(requested_day) else today
    live = day == today

    trades = load_trades(day)
    s = summarize(trades)
    hist = history()
    h = hist["stats"]

    day_cards = "".join([
        card("Net P&L" + (" (live)" if live else ""), inr(s["net"]),
             f'realised {inr(s["realised"])} · open {inr(s["unrealised"])}', tone(s["net"])),
        card("Win rate", pct(s["win_rate"]), f'{s["wins"]} won · {s["losses"]} lost · {s["open"]} open'),
        card("Trades", str(s["trades"]), f'{s["closed"]} closed'),
        card("Best / worst", f'{inr(s["best"])}', f'worst {inr(s["worst"])}', tone(s["best"])),
        card("Avg win / loss", inr(s["avg_win"]), f'avg loss {inr(s["avg_loss"])}', "pos"),
    ])
    pf = "n/a" if h["profit_factor"] is None else f'{h["profit_factor"]:.2f}'
    all_cards = "".join([
        card("Total P&L", inr(h["net"]), f'over {h["days"]} day(s)', tone(h["net"])),
        card("Win rate (all trades)", pct(h["win_rate"]), f'{h["wins"]} won · {h["losses"]} lost'),
        card("Winning days", f'{h["winning_days"]} / {h["days"]}', f'avg day {inr(h["avg_day"])}',
             tone(h["avg_day"])),
        card("Profit factor", pf, "gross profit ÷ gross loss"),
        card("Max drawdown", inr(h["max_drawdown"]), "peak to trough, daily", "neg" if h["max_drawdown"] else ""),
    ])

    recent = sorted(set(days[-14:] + [today]), reverse=True)
    nav = "".join(f'<a href="?date={d}" class="{"on" if d == day else ""}">'
                  f'{"Today" if d == today else d}</a>' for d in recent)

    movers = small_table(_read_rows(DATA / day / "movers.csv"),
                         [("symbol", "Stock"), ("direction", "Side"), ("change_pct", "Change %"),
                          ("range_pct", "5-min range %")])
    skipped = small_table(_read_rows(DATA / day / "skipped.csv"),
                          [("symbol", "Stock"), ("reason", "Why it was skipped")])

    refresh = f'<meta http-equiv="refresh" content="{REFRESH_SECONDS}">' if live else ""
    now = datetime.now(IST).strftime("%d %b %Y, %H:%M:%S IST")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">{refresh}
<title>F&amp;O Momentum - Paper Dashboard</title><style>{CSS}</style></head><body><main>
<header><h1>F&amp;O Momentum Dashboard <span class="badge">PAPER ONLY</span></h1>
<span class="muted">Updated {escape(now)}{" · auto-refresh every %ds" % REFRESH_SECONDS if live else ""}</span></header>
<nav>{nav}</nav>

<h2>{escape("Today" if live else day)} ({escape(day)})</h2>
<div class="grid">{day_cards}</div>

<h2>Trades: entry and exit</h2>
{trade_table(trades)}

<div class="two"><div><h2>P&amp;L per trade</h2>{bar_chart([(t["symbol"], t["pnl"]) for t in trades])}</div>
<div><h2>Selected movers</h2>{movers}<h2>Skipped trades</h2>{skipped}</div></div>

<h2>All days</h2>
<div class="grid">{all_cards}</div>
<div class="two"><div><h2>Cumulative P&amp;L</h2>{line_chart(hist["cumulative"])}</div>
<div><h2>Daily P&amp;L (last 30 days)</h2>{bar_chart([(day[5:], v) for day, v in hist["daily"][-30:]])}</div></div>

<footer>Simulated results only; no order is ever sent. Fills are assumed at the candle open and exits at the
exact stop price, so real trading would differ (slippage, spreads). Win rate counts closed trades only.</footer>
</main></body></html>"""


# ------------------------------------------------------------------------ server
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):                                       # noqa: N802 (stdlib name)
        url = urlparse(self.path)
        if url.path != "/":
            self.send_error(404)
            return
        day = parse_qs(url.query).get("date", [None])[0]
        body = render(day).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):                           # keep the console quiet
        pass


def make_server(host: str = "127.0.0.1", port: int = 8501) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), Handler)


def serve(host: str = "127.0.0.1", port: int = 8501) -> None:
    server = make_server(host, port)
    print(f"Dashboard running at http://{host}:{port}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Dashboard stopped.")


def start_in_background(host: str = "127.0.0.1", port: int = 8501) -> ThreadingHTTPServer:
    server = make_server(host, port)
    threading.Thread(target=server.serve_forever, daemon=True, name="dashboard").start()
    return server
