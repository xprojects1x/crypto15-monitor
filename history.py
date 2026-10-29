#!/usr/bin/env python3
"""
history.py - download every settled Kalshi 15-minute crypto window (BTC, ETH or SOL) and build a report.

  python history.py                 every settled BTC window since launch (fast: a few dozen API calls)
  python history.py --asset ETH     the same for ETH (or SOL)
  python history.py --candles       also pull 1-minute Kalshi prices per window (slow; resumable)
  python history.py --candles-days 14   candles only for the last 14 days
  python history.py --tz PT         group days/hours in Pacific time (default ET, Kalshi's clock)
  python history.py --report-only   rebuild the report from what's already downloaded

Writes to data/history/ for BTC, data/eth/history/ and data/sol/history/ for the others:
  windows.csv   one row per window: open/close, price to beat, settlement value, move, result, volume
                (+ Kalshi Yes bid/ask 14, 10, 5, 2 and 1 minute before close when --candles is used)
  daily.csv, hourly.csv, weekday.csv   summary tables
  summary.json  everything the report shows
  report.html   open in any browser (also served by app.py at http://localhost:8765/history)

Uses only Kalshi's free public market data. No account or keys needed.
"""

import argparse
import csv
import datetime as dt
import json
import math
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
# step = a "close call" distance for each coin, about 3-4 basis points (BTC $25, ETH $1, SOL 5 cents)
COINS = {
    "BTC": {"series": "KXBTC15M", "step": 25.0, "decimals": 2, "product": "BTC-USD", "slug": "kxbtc15m/bitcoin-price-up-down"},
    "ETH": {"series": "KXETH15M", "step": 1.0, "decimals": 2, "product": "ETH-USD", "slug": "kxeth15m/eth-15m-price-up-down"},
    "SOL": {"series": "KXSOL15M", "step": 0.05, "decimals": 4, "product": "SOL-USD", "slug": "kxsol15m/solana-15-minutes"},
}
ASSET = "BTC"
OUT = os.path.join(HERE, "data", "history")
SERIES = COINS["BTC"]["series"]
STEP = COINS["BTC"]["step"]


def set_asset(asset):
    """Point the downloader, report, validator and trainer at one coin."""
    global ASSET, OUT, SERIES, STEP
    asset = asset.upper()
    if asset not in COINS:
        raise SystemExit(f"Unknown coin {asset}; choose from {', '.join(COINS)}")
    ASSET, SERIES, STEP = asset, COINS[asset]["series"], COINS[asset]["step"]
    OUT = os.path.join(HERE, "data", "history") if asset == "BTC" else os.path.join(HERE, "data", asset.lower(), "history")
    return OUT


def money(v, signed=False):
    """$ amount with enough decimals for the coin's price scale."""
    if v is None:
        return "?"
    d = 0 if STEP >= 10 else 2 if STEP >= 0.5 else 3
    return f"{'-' if v < 0 else '+' if signed else ''}${abs(v):,.{d}f}"
BASES = ["https://api.elections.kalshi.com/trade-api/v2", "https://external-api.kalshi.com/trade-api/v2"]
WINDOW = 900
CANDLE_MARKS = (14, 10, 5, 2, 1)      # minutes before close to sample Kalshi's price (14 = 1 min after open)


# --------------------------------------------------------------------------- helpers

def fnum(x):
    if x is None or x == "":
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def parse_ts(s):
    if not s:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def utc(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc)


def _nth_sunday(year, month, n):
    d = dt.date(year, month, 1)
    d += dt.timedelta(days=(6 - d.weekday()) % 7)
    return d + dt.timedelta(weeks=n - 1)


def to_local(ts, tz):
    """US Eastern or Pacific wall-clock time without needing a tz database (works on Windows)."""
    std = {"ET": -5, "PT": -8}[tz]
    u = utc(ts)
    y = u.year
    start = dt.datetime.combine(_nth_sunday(y, 3, 2), dt.time(2), dt.timezone.utc) - dt.timedelta(hours=std)
    end = dt.datetime.combine(_nth_sunday(y, 11, 1), dt.time(2), dt.timezone.utc) - dt.timedelta(hours=std + 1)
    off = std + 1 if start <= u < end else std
    return (u + dt.timedelta(hours=off)).replace(tzinfo=None)


def ci95(p, n):
    return 1.96 * math.sqrt(max(p * (1 - p), 1e-9) / n) if n else None


def taker_fee(p):
    """Kalshi taker fee per contract at size 100 (rounding becomes negligible)."""
    return 0.07 * p * (1 - p)


# --------------------------------------------------------------------------- API client

class Api:
    def __init__(self, base=None, rate=8.0):
        self.bases = [base] if base else list(BASES)
        self.base = self.bases[0]
        self.min_gap = 1.0 / rate
        self.last = 0.0
        self.calls = 0

    def get(self, path, params=None, allow_404=False):
        q = ("?" + urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})) if params else ""
        delay, last_err = 1.0, None
        for attempt in range(8):
            base = self.base
            wait = self.min_gap - (time.time() - self.last)
            if wait > 0:
                time.sleep(wait)
            self.last = time.time()
            self.calls += 1
            req = urllib.request.Request(base + path + q, headers={"User-Agent": "btc15-history/1.0",
                                                                   "Accept": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    return json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                if e.code == 404 and allow_404:
                    return None
                last_err = f"HTTP {e.code} on {path}"
                if e.code in (429, 500, 502, 503, 504):
                    time.sleep(delay)
                    delay = min(delay * 2, 30)
                    continue
                raise RuntimeError(last_err) from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last_err = f"{e} on {path}"
                self.base = self.bases[(self.bases.index(base) + 1) % len(self.bases)]   # try the other host
                time.sleep(delay)
                delay = min(delay * 2, 30)
        raise RuntimeError(f"gave up after retries: {last_err}")

    def pages(self, path, params, label):
        cursor, n, page = None, 0, 0
        while True:
            data = self.get(path, dict(params, cursor=cursor))
            items = data.get("markets", []) or []
            n += len(items)
            page += 1
            print(f"\r  {label}: {n:,} markets ({page} pages)", end="", flush=True)
            for it in items:
                yield it
            cursor = data.get("cursor")
            if not cursor or not items:
                break
        print()


# --------------------------------------------------------------------------- download

def row_from_market(m):
    close_ts = parse_ts(m.get("close_time"))
    if close_ts is None:
        return None
    strike, settle = fnum(m.get("floor_strike")), fnum(m.get("expiration_value"))
    result = (m.get("result") or "").lower()
    if result not in ("yes", "no"):
        return None
    move = (settle - strike) if (settle is not None and strike is not None) else None
    return {
        "ticker": m.get("ticker"),
        "open_utc": utc(close_ts - WINDOW).isoformat().replace("+00:00", "Z"),
        "close_utc": utc(close_ts).isoformat().replace("+00:00", "Z"),
        "close_ts": int(close_ts),
        "strike": strike, "settle_value": settle,
        "move": round(move, 4) if move is not None else None,
        "move_bps": round(move / strike * 1e4, 2) if (move is not None and strike) else None,
        "result": result,
        "volume": fnum(m.get("volume_fp") if m.get("volume_fp") is not None else m.get("volume")),
        "open_interest": fnum(m.get("open_interest_fp") if m.get("open_interest_fp") is not None else m.get("open_interest")),
        "settled_utc": m.get("settlement_ts") or "",
    }


def download_windows(api):
    cutoff = None
    try:
        c = api.get("/historical/cutoff")
        cutoff = parse_ts((c or {}).get("market_settled_ts"))
        if cutoff:
            print(f"  Kalshi archive cutoff: markets settled before {utc(cutoff):%Y-%m-%d %H:%M} UTC are in /historical")
    except RuntimeError as e:
        print(f"  (could not read historical cutoff: {e}; continuing)")

    merged = {}
    for m in api.pages("/markets", {"series_ticker": SERIES, "status": "settled", "limit": 1000}, "recent windows"):
        merged[m.get("ticker")] = m
    try:
        for m in api.pages("/historical/markets", {"series_ticker": SERIES, "limit": 1000}, "archived windows"):
            merged.setdefault(m.get("ticker"), m)
    except RuntimeError as e:
        print(f"\n  (archive request failed: {e}; using recent windows only)")
    rows = [r for r in (row_from_market(m) for m in merged.values()) if r]
    rows.sort(key=lambda r: r["close_ts"])
    return rows, cutoff


def download_candles(api, rows, cutoff, days=None, fetch=True):
    """1-minute Yes bid/ask at 10/5/2/1 min before close. Cached + resumable."""
    path = os.path.join(OUT, "candles.jsonl")
    done = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                    done[d["ticker"]] = d
                except (ValueError, KeyError):
                    pass
    since = time.time() - days * 86400 if days else 0
    todo = [r for r in rows if (r["ticker"] not in done or "m14_bid" not in done[r["ticker"]])
            and r["close_ts"] >= since] if fetch else []
    if todo:
        est = len(todo) / (1 / api.min_gap) / 60
        print(f"  Kalshi price history: {len(todo):,} windows to fetch (~{est:.0f} min). "
              f"Safe to stop with Ctrl+C and rerun later; it resumes.")
    with open(path, "a", encoding="utf-8") as f:
        try:
            for i, r in enumerate(todo, 1):
                tk, close = r["ticker"], r["close_ts"]
                params = {"start_ts": close - WINDOW, "end_ts": close, "period_interval": 1}
                hist_first = cutoff is not None and close < cutoff
                paths = [f"/historical/markets/{tk}/candlesticks", f"/series/{SERIES}/markets/{tk}/candlesticks"]
                if not hist_first:
                    paths.reverse()
                data = None
                for p in paths:
                    try:
                        data = api.get(p, params, allow_404=True)
                    except RuntimeError:
                        data = None
                    if data and data.get("candlesticks"):
                        break
                rec = {"ticker": tk}
                candles = sorted((data or {}).get("candlesticks", []) or [], key=lambda c: c.get("end_period_ts", 0))
                for mk in CANDLE_MARKS:
                    target = close - mk * 60
                    c = None
                    for cand in candles:          # latest candle ending at or before the mark
                        if cand.get("end_period_ts", 0) <= target:
                            c = cand
                    yb = fnum(((c or {}).get("yes_bid") or {}).get("close_dollars"))
                    ya = fnum(((c or {}).get("yes_ask") or {}).get("close_dollars"))
                    rec[f"m{mk}_bid"] = yb if yb and 0 < yb < 1 else None
                    rec[f"m{mk}_ask"] = ya if ya and 0 < ya < 1 else None
                f.write(json.dumps(rec) + "\n")
                done[tk] = rec
                if i % 25 == 0 or i == len(todo):
                    f.flush()
                    print(f"\r  Kalshi price history: {i:,}/{len(todo):,}", end="", flush=True)
        except KeyboardInterrupt:
            print("\n  Stopped. Progress saved; rerun with --candles to continue.")
    if todo:
        print()
    for r in rows:
        rec = done.get(r["ticker"])
        for mk in CANDLE_MARKS:
            for side in ("bid", "ask"):
                r[f"m{mk}_{side}"] = rec.get(f"m{mk}_{side}") if rec else None
    return sum(1 for r in rows if r["ticker"] in done)


# --------------------------------------------------------------------------- analysis

def build_summary(rows, tz):
    if not rows:
        return {"empty": True}
    for r in rows:
        lt = to_local(r["close_ts"] - WINDOW, tz)       # label a window by its local start time
        r["_date"], r["_hour"], r["_wd"], r["_slot"] = lt.date().isoformat(), lt.hour, lt.weekday(), lt.minute
        r["_up"] = 1 if r["result"] == "yes" else 0

    n = len(rows)
    ups = sum(r["_up"] for r in rows)
    moves = [r["move"] for r in rows if r["move"] is not None]
    absm = [abs(m) for m in moves]
    span = rows[-1]["close_ts"] - rows[0]["close_ts"]
    expected = int(span // WINDOW) + 1
    mismatch = sum(1 for r in rows if r["move"] is not None and (r["move"] >= 0) != (r["result"] == "yes"))

    def agg(group):
        k = len(group)
        u = sum(r["_up"] for r in group)
        am = [abs(r["move"]) for r in group if r["move"] is not None]
        vol = [r["volume"] for r in group if r["volume"] is not None]
        p = u / k if k else None
        return {"n": k, "up": u, "down": k - u, "up_rate": p, "ci": ci95(p, k) if k else None,
                "avg_abs_move": statistics.mean(am) if am else None,
                "med_abs_move": statistics.median(am) if am else None,
                "within_25": (sum(1 for a in am if a < STEP) / len(am)) if am else None,
                "avg_volume": statistics.mean(vol) if vol else None}

    by_day, by_hour, by_wd, by_slot = defaultdict(list), defaultdict(list), defaultdict(list), defaultdict(list)
    for r in rows:
        by_day[r["_date"]].append(r)
        by_hour[r["_hour"]].append(r)
        by_wd[r["_wd"]].append(r)
        by_slot[r["_slot"]].append(r)

    daily = []
    for d in sorted(by_day):
        g = by_day[d]
        a = agg(g)
        a.update({"date": d, "btc_open": g[0]["strike"], "btc_close": g[-1]["settle_value"],
                  "max_abs_move": max((abs(r["move"]) for r in g if r["move"] is not None), default=None),
                  "volume": sum(r["volume"] or 0 for r in g)})
        daily.append(a)
    hourly = [dict(agg(by_hour[h]), hour=h) for h in range(24) if by_hour[h]]
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    weekday = [dict(agg(by_wd[w]), weekday=names[w]) for w in range(7) if by_wd[w]]
    slots = [dict(agg(by_slot[s]), slot=f":{s:02d}") for s in sorted(by_slot)]

    # streaks and "does the last result predict the next?"
    runs, cur, best = Counter(), 1, {"yes": (0, None), "no": (0, None)}
    for i in range(1, n + 1):
        if i < n and rows[i]["result"] == rows[i - 1]["result"] and rows[i]["close_ts"] - rows[i - 1]["close_ts"] == WINDOW:
            cur += 1
            continue
        res = rows[i - 1]["result"]
        runs[min(cur, 10)] += 1
        if cur > best[res][0]:
            best[res] = (cur, rows[i - 1]["close_utc"])
        cur = 1
    cond = {}
    for k in (1, 2, 3):
        for side in ("yes", "no"):
            nxt = [rows[i]["_up"] for i in range(k, n)
                   if all(rows[i - j]["result"] == side and rows[i - j + 1]["close_ts"] - rows[i - j]["close_ts"] == WINDOW
                          for j in range(1, k + 1))]
            if nxt:
                p = sum(nxt) / len(nxt)
                cond[f"after_{k}_{'up' if side == 'yes' else 'down'}"] = {"n": len(nxt), "up_rate": p, "ci": ci95(p, len(nxt))}

    # how big are the moves?
    edges = [k * STEP for k in range(-12, 13)]
    hist = [0] * (len(edges) + 1)
    for m in moves:
        idx = 0 if m < edges[0] else len(edges) if m >= edges[-1] else 1 + min(len(edges) - 2, int((m - edges[0]) // STEP))
        hist[idx] += 1
    labels = [f"< {money(edges[0])}"] + [f"{money(a, True)}…{money(a + STEP, True)}" for a in edges[:-1]] + [f"≥ {money(edges[-1], True)}"]
    move_hist = [{"bucket": labels[i], "count": hist[i], "lo": ([-1e9] + edges)[i], "hi": (edges + [1e9])[i]}
                 for i in range(len(hist))]

    # was Kalshi's price right? (only with --candles)
    calib = {}
    for mk in (m for m in CANDLE_MARKS if m != 14):
        buckets = defaultdict(list)
        for r in rows:
            b, a = r.get(f"m{mk}_bid"), r.get(f"m{mk}_ask")
            if b is None or a is None:
                continue
            buckets[min(int((b + a) / 2 * 10), 9)].append(r)
        out = []
        for b in range(10):
            g = buckets.get(b, [])
            if not g:
                continue
            mids = [(r[f"m{mk}_bid"] + r[f"m{mk}_ask"]) / 2 for r in g]
            p = sum(r["_up"] for r in g) / len(g)
            ev_yes = statistics.mean(r["_up"] - r[f"m{mk}_ask"] - taker_fee(r[f"m{mk}_ask"]) for r in g)
            ev_no = statistics.mean((1 - r["_up"]) - (1 - r[f"m{mk}_bid"]) - taker_fee(1 - r[f"m{mk}_bid"]) for r in g)
            out.append({"bucket": f"{b * 10}–{b * 10 + 10}¢", "n": len(g), "avg_mid": statistics.mean(mids),
                        "up_rate": p, "ci": ci95(p, len(g)), "ev_buy_yes": ev_yes, "ev_buy_no": ev_no})
        if out:
            calib[f"{mk}m"] = out

    # compact sequence for the in-report strategy lab: [close_ts, up, up_ask_at_open, down_ask_at_open]
    seq = []
    for r in rows:
        b, a = r.get("m14_bid"), r.get("m14_ask")
        seq.append([r["close_ts"], r["_up"], round(a, 4) if a else None, round(1 - b, 4) if b else None])
    # one string per day: a character per 15-minute slot from local midnight (U, D, or . for no window)
    grid = []
    for d in sorted(by_day):
        cells = ["."] * 100
        for r in by_day[d]:
            lt = to_local(r["close_ts"] - WINDOW, tz)
            cells[min(99, (lt.hour * 60 + lt.minute) // 15)] = "U" if r["_up"] else "D"
        grid.append({"date": d, "cells": "".join(cells).rstrip(".")})

    return {
        "asset": ASSET, "series": SERIES, "step": STEP, "decimals": COINS[ASSET]["decimals"],
        "seq": seq, "grid": grid,
        "tz": tz, "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "overall": {
            "windows": n, "up": ups, "down": n - ups, "up_rate": ups / n, "ci": ci95(ups / n, n),
            "first_close_utc": rows[0]["close_utc"], "last_close_utc": rows[-1]["close_utc"],
            "days": len(by_day), "expected_windows": expected, "missing_windows": max(0, expected - n),
            "avg_abs_move": statistics.mean(absm) if absm else None,
            "med_abs_move": statistics.median(absm) if absm else None,
            "within_25": sum(1 for a in absm if a < STEP) / len(absm) if absm else None,
            "within_50": sum(1 for a in absm if a < 2 * STEP) / len(absm) if absm else None,
            "over_100": sum(1 for a in absm if a >= 4 * STEP) / len(absm) if absm else None,
            "result_mismatches": mismatch,
            "btc_first": rows[0]["strike"], "btc_last": rows[-1]["settle_value"],
            "with_prices": sum(1 for r in rows if r.get("m2_bid") is not None),
        },
        "daily": daily, "hourly": hourly, "weekday": weekday, "slots": slots,
        "streaks": {"run_lengths": {str(k): v for k, v in sorted(runs.items())},
                    "longest_up": {"len": best["yes"][0], "ended_utc": best["yes"][1]},
                    "longest_down": {"len": best["no"][0], "ended_utc": best["no"][1]},
                    "conditional": cond},
        "move_hist": move_hist, "calibration": calib,
    }


# --------------------------------------------------------------------------- output

WINDOW_COLS = ["ticker", "open_utc", "close_utc", "strike", "settle_value", "move", "move_bps", "result",
               "volume", "open_interest", "settled_utc"] + [f"m{m}_{s}" for m in CANDLE_MARKS for s in ("bid", "ask")]


def write_csv(path, rows, cols):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({c: ("" if r.get(c) is None else round(r[c], 4) if isinstance(r.get(c), float) else r.get(c))
                        for c in cols})


def read_windows_csv(path):
    rows = []
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            row = {k: (fnum(v) if k not in ("ticker", "open_utc", "close_utc", "result", "settled_utc") else v)
                   for k, v in r.items()}
            row["close_ts"] = int(parse_ts(row["close_utc"]))
            rows.append(row)
    return rows


def write_outputs(rows, summary):
    write_csv(os.path.join(OUT, "windows.csv"), rows, WINDOW_COLS)
    if summary.get("empty"):
        return
    write_csv(os.path.join(OUT, "daily.csv"), summary["daily"],
              ["date", "n", "up", "down", "up_rate", "avg_abs_move", "med_abs_move", "max_abs_move",
               "within_25", "btc_open", "btc_close", "volume"])
    write_csv(os.path.join(OUT, "hourly.csv"), summary["hourly"],
              ["hour", "n", "up", "down", "up_rate", "ci", "avg_abs_move", "within_25", "avg_volume"])
    write_csv(os.path.join(OUT, "weekday.csv"), summary["weekday"],
              ["weekday", "n", "up", "down", "up_rate", "ci", "avg_abs_move", "within_25", "avg_volume"])
    vpath = os.path.join(OUT, "validation.json")
    if "validation" not in summary and os.path.exists(vpath):
        try:
            with open(vpath, encoding="utf-8") as f:
                summary["validation"] = json.load(f)
        except ValueError:
            pass
    with open(os.path.join(OUT, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=1)
    tpl_path = os.path.join(HERE, "static", "history_template.html")
    with open(tpl_path, encoding="utf-8") as f:
        tpl = f.read()
    html = tpl.replace("/*__SUMMARY__*/null", json.dumps(summary).replace("</", "<\\/"))
    with open(os.path.join(OUT, "report.html"), "w", encoding="utf-8") as f:
        f.write(html)


def main():
    ap = argparse.ArgumentParser(description="Download Kalshi 15-minute crypto history and build a report")
    ap.add_argument("--asset", default="BTC", help="BTC, ETH or SOL (default BTC)")
    ap.add_argument("--candles", action="store_true", help="also fetch Kalshi's 1-minute prices per window (slow)")
    ap.add_argument("--candles-days", type=int, default=None, help="only fetch prices for the last N days")
    ap.add_argument("--tz", choices=["ET", "PT"], default="ET", help="time zone for days and hours (default ET)")
    ap.add_argument("--report-only", action="store_true", help="rebuild the report from downloaded data")
    ap.add_argument("--rate", type=float, default=8.0, help="max API requests per second (default 8)")
    ap.add_argument("--base-url", default=None, help=argparse.SUPPRESS)   # for testing against a mock server
    args = ap.parse_args()
    set_asset(args.asset)
    os.makedirs(OUT, exist_ok=True)
    csv_path = os.path.join(OUT, "windows.csv")

    if args.report_only:
        if not os.path.exists(csv_path):
            sys.exit(f"No downloaded data yet. Run: python history.py --asset {ASSET}")
        rows = read_windows_csv(csv_path)
    else:
        print(f"\nDownloading settled {SERIES} windows from Kalshi...")
        api = Api(args.base_url, args.rate)
        try:
            rows, cutoff = download_windows(api)
        except RuntimeError as e:
            sys.exit(f"\nCould not reach Kalshi: {e}\nCheck your internet connection and try again.")
        if not rows:
            sys.exit("Kalshi returned no settled windows.")
        print(f"  {len(rows):,} settled windows, {rows[0]['close_utc'][:10]} to {rows[-1]['close_utc'][:10]}")
        if args.candles or args.candles_days:
            n = download_candles(api, rows, cutoff, args.candles_days)
            print(f"  Kalshi prices available for {n:,} windows")
        else:
            old = os.path.join(OUT, "candles.jsonl")     # keep previously fetched prices
            if os.path.exists(old):
                download_candles(api, rows, cutoff, fetch=False)
        print(f"  {api.calls} API calls")

    summary = build_summary(rows, args.tz)
    write_outputs(rows, summary)
    o = summary["overall"]
    print(f"\n  Up {o['up']:,} / Down {o['down']:,}  ({o['up_rate'] * 100:.1f}% up, ±{o['ci'] * 100:.1f}%)")
    print(f"  Median move {money(o['med_abs_move'])}; {o['within_25'] * 100:.0f}% of windows finished within {money(STEP)} of the line")
    print(f"\n  Report: {os.path.join(OUT, 'report.html')}")
    print(f"  (also at http://localhost:8765/history?asset={ASSET} while app.py is running)\n")


if __name__ == "__main__":
    main()
