#!/usr/bin/env python3
"""
validate.py - check the downloaded Kalshi 15-minute crypto history (BTC, ETH or SOL), and the patterns found in it.

  python validate.py                  all checks (samples 300 windows against Coinbase)
  python validate.py --coinbase 1000  bigger independent price sample
  python validate.py --coinbase 0     skip the Coinbase cross-check (offline)
  python validate.py --asset ETH      check ETH (or SOL) instead of BTC

Run history.py first. Results print here, are saved to data/history/validation.json,
and appear in the "Data checks" section of report.html.

Part 1 - is the data right?
  * no duplicate windows; gaps in the 15-minute schedule listed
  * every result agrees with settlement >= price to beat
  * chain check: each window's price to beat must equal the previous window's settlement
    (both are the index average over the same 60 seconds), so any break flags a bad record
  * prices are sane (no zeros, no impossible jumps)
  * direction agrees with an independent source (Coinbase 1-minute candles)
  * a random sample of kalshi.com links to eyeball

Part 2 - are the patterns real or luck?
  * runs test and lag-1 correlation: does the order of Ups and Downs look random?
  * out-of-sample: patterns found in the first half of history re-tested on the second half
  * a multiple-comparisons count: how many "significant" cells you'd expect by chance alone
"""

import argparse
import datetime as dt
import json
import math
import os
import random
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict

import history as H

COINBASE = "https://api.exchange.coinbase.com"


def z_to_p(z):
    return math.erfc(abs(z) / math.sqrt(2))      # two-sided p-value


def pearson(xs, ys):
    if len(xs) < 3:
        return 0.0
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    sxy = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
    sxx = sum((a - mx) ** 2 for a in xs)
    syy = sum((b - my) ** 2 for b in ys)
    return sxy / math.sqrt(sxx * syy) if sxx and syy else 0.0


def check(name, status, detail):
    return {"name": name, "status": status, "detail": detail}


# --------------------------------------------------------------------------- part 1: data integrity

def integrity(rows):
    out = []
    tickers = [r["ticker"] for r in rows]
    closes = [r["close_ts"] for r in rows]
    dup_t = len(tickers) - len(set(tickers))
    dup_c = len(closes) - len(set(closes))
    out.append(check("No duplicate windows", "PASS" if not (dup_t or dup_c) else "FAIL",
                     f"{len(rows):,} windows, {dup_t} duplicate tickers, {dup_c} duplicate close times"))

    gaps = []
    for a, b in zip(rows, rows[1:]):
        if b["close_ts"] - a["close_ts"] > H.WINDOW:
            gaps.append((a["close_ts"], b["close_ts"], (b["close_ts"] - a["close_ts"]) // H.WINDOW - 1))
    missing = sum(g[2] for g in gaps)
    if gaps:
        big = sorted(gaps, key=lambda g: -g[2])[:3]
        desc = "; ".join(f"{g[2]} after {H.utc(g[0]):%Y-%m-%d %H:%M} UTC" for g in big)
        out.append(check("Complete 15-minute schedule", "WARN",
                         f"{missing:,} windows missing in {len(gaps)} gaps (largest: {desc}). "
                         "Usually exchange downtime or markets Kalshi voided; rerun history.py to fill recent ones."))
    else:
        out.append(check("Complete 15-minute schedule", "PASS", "one window every 15 minutes, no gaps"))

    bad = [r for r in rows if r["move"] is not None and (r["move"] >= 0) != (r["result"] == "yes")]
    nomove = sum(1 for r in rows if r["move"] is None)
    out.append(check("Result matches settlement ≥ price to beat", "PASS" if not bad else "FAIL",
                     (f"all {len(rows) - nomove:,} checkable windows agree" if not bad else
                      f"{len(bad)} disagree, e.g. {', '.join(r['ticker'] for r in bad[:4])}")
                     + (f"; {nomove} windows lack a price to beat or settlement" if nomove else "")))

    pairs = breaks = 0
    worst = []
    for a, b in zip(rows, rows[1:]):
        if b["close_ts"] - a["close_ts"] != H.WINDOW or a["settle_value"] is None or b["strike"] is None:
            continue
        pairs += 1
        diff = b["strike"] - a["settle_value"]
        if abs(diff) > 1.1 * 10 ** -H.COINS[H.ASSET]["decimals"]:      # one unit of the last published digit
            breaks += 1
            worst.append((abs(diff), b["ticker"], diff))
    if pairs:
        share = 1 - breaks / pairs
        status = "PASS" if not breaks else "WARN" if share >= 0.995 else "FAIL"
        ex = ", ".join(f"{t} ({H.money(d, True)})" for _, t, d in sorted(worst, reverse=True)[:3])
        out.append(check("Chain: price to beat = previous settlement", status,
                         f"{pairs - breaks:,} of {pairs:,} back-to-back pairs match exactly ({share * 100:.2f}%)"
                         + (f"; largest breaks: {ex}" if breaks else "")))

    strikes = [r["strike"] for r in rows if r["strike"]]
    jumps = [(r["ticker"], abs(r["move"]) / r["strike"]) for r in rows if r["move"] is not None and r["strike"]]
    wild = [j for j in jumps if j[1] > 0.03]
    zero = sum(1 for r in rows if not r["strike"] or not r["settle_value"])
    out.append(check("Prices are sane", "PASS" if not wild and not zero else "WARN",
                     f"price to beat ranges {H.money(min(strikes))}–{H.money(max(strikes))}; "
                     f"{len(wild)} windows moved more than 3% in 15 minutes; {zero} missing or zero values"))
    return out


def coinbase_check(rows, n, base, rate=5.0):
    """Direction from Coinbase 1-minute candles vs Kalshi's result, bucketed by how close the window was."""
    if n <= 0:
        return check("Independent price source (Coinbase)", "INFO", "skipped (--coinbase 0)"), None
    rng = random.Random(42)
    sample = rng.sample(rows, min(n, len(rows)))
    m1, m2, m4 = H.money(H.STEP), H.money(2 * H.STEP), H.money(4 * H.STEP)
    names = (f"under {m1}", f"{m1}–{m2[1:]}", f"{m2}–{m4[1:]}", f"{m4}+")
    buckets = {k: [0, 0] for k in names}
    errors = 0
    last = 0.0
    print(f"  Coinbase cross-check on {len(sample)} random windows...")
    for i, r in enumerate(sample, 1):
        close = r["close_ts"]
        open_ = close - H.WINDOW
        params = {"granularity": 60, "start": H.utc(open_ - 120).isoformat().replace("+00:00", "Z"),
                  "end": H.utc(close + 60).isoformat().replace("+00:00", "Z")}
        wait = 1 / rate - (time.time() - last)
        if wait > 0:
            time.sleep(wait)
        last = time.time()
        try:
            req = urllib.request.Request(f"{base}/products/{H.COINS[H.ASSET]['product']}/candles?{urllib.parse.urlencode(params)}",
                                         headers={"User-Agent": "btc15-validate/1.0", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                candles = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, ValueError, ConnectionError):
            errors += 1
            continue
        by_t = {int(c[0]): c for c in candles if isinstance(c, list) and len(c) >= 5}

        def minute_avg(end_ts):          # the 60 seconds before end_ts
            c = by_t.get(int(end_ts) - 60)
            return None if not c else (c[1] + c[2] + c[3] + c[4]) / 4

        a, b = minute_avg(open_), minute_avg(close)
        if a is None or b is None or r["move"] is None:
            errors += 1
            continue
        cb_up = b >= a
        m = abs(r["move"])
        key = names[0] if m < H.STEP else names[1] if m < 2 * H.STEP else names[2] if m < 4 * H.STEP else names[3]
        buckets[key][0] += 1
        buckets[key][1] += int(cb_up == (r["result"] == "yes"))
        if i % 25 == 0:
            print(f"\r    {i}/{len(sample)}", end="", flush=True)
    print()
    total = sum(v[0] for v in buckets.values())
    if not total:
        return check("Independent price source (Coinbase)", "WARN",
                     f"could not get Coinbase candles ({errors} failed requests); check your connection"), None
    wide_n = buckets[names[2]][0] + buckets[names[3]][0]
    wide_ok = buckets[names[2]][1] + buckets[names[3]][1]
    agree = sum(v[1] for v in buckets.values()) / total
    wide = wide_ok / wide_n if wide_n else None
    parts = [f"{k}: {v[1]}/{v[0]}" for k, v in buckets.items() if v[0]]
    status = "PASS" if (wide is None or wide >= 0.99) and agree >= 0.9 else "WARN" if agree >= 0.8 else "FAIL"
    detail = (f"Coinbase agreed on direction in {agree * 100:.1f}% of {total} sampled windows"
              + (f" and {wide * 100:.1f}% of those that finished {m2}+ from the line" if wide is not None else "")
              + f" ({'; '.join(parts)}). Disagreement near the line is expected: Coinbase is one exchange, the index averages several."
              + (f" {errors} windows had no candles." if errors else ""))
    return check("Independent price source (Coinbase)", status, detail), buckets


def spot_links(rows, k=20):
    rng = random.Random(7)
    out = []
    for r in sorted(rng.sample(rows, min(k, len(rows))), key=lambda r: r["close_ts"]):
        tk = r["ticker"]
        event = tk.rsplit("-", 1)[0] if tk.count("-") >= 2 else tk
        out.append({"ticker": tk, "url": f"https://kalshi.com/markets/{H.COINS[H.ASSET]['slug']}/{event.lower()}",
                    "strike": r["strike"], "settle_value": r["settle_value"], "result": r["result"]})
    return out


# --------------------------------------------------------------------------- part 2: are patterns real?

def randomness(rows):
    out = []
    x = [1 if r["result"] == "yes" else 0 for r in rows]
    n = len(x)
    n1, n0 = sum(x), n - sum(x)
    runs = 1 + sum(1 for a, b in zip(x, x[1:]) if a != b)
    mu = 2 * n1 * n0 / n + 1
    var = (mu - 1) * (mu - 2) / (n - 1)
    z = (runs - mu) / math.sqrt(var) if var > 0 else 0
    p = z_to_p(z)
    if p >= 0.05:
        verdict = "the order of Ups and Downs is consistent with coin flips; streaks are about as long and as common as chance predicts"
        status = "PASS"
    else:
        verdict = ("fewer switches than chance: results cluster in streaks" if z < 0 else
                   "more switches than chance: results tend to alternate") + ". Check that it also holds out of sample"
        status = "INFO"
    out.append(check("Runs test (is the sequence random?)", status,
                     f"{runs:,} runs vs {mu:,.0f} expected (z = {z:+.2f}, p = {p:.3f}): {verdict}."))

    mean = n1 / n
    num = sum((a - mean) * (b - mean) for a, b in zip(x, x[1:]))
    den = sum((a - mean) ** 2 for a in x)
    r1 = num / den if den else 0
    zr = r1 * math.sqrt(n)
    out.append(check("Does one result predict the next?", "PASS" if abs(zr) < 1.96 else "INFO",
                     f"lag-1 correlation {r1:+.4f} (z = {zr:+.2f}). "
                     + ("Indistinguishable from zero: knowing the last window tells you nothing about the next."
                        if abs(zr) < 1.96 else "Statistically non-zero, but check its size: a correlation this small "
                                                "moves the next window's odds by only about "
                                                f"{abs(r1) * 50:.1f} percentage points, less than fees cost.")))
    return out


def halves(rows):
    mid = len(rows) // 2
    return rows[:mid], rows[mid:]


def out_of_sample(rows, tz):
    out = []
    a, b = halves(rows)
    if len(a) < 500:
        return [check("Out-of-sample tests", "INFO", "need at least ~1,000 windows to split the history in two")]
    split_day = H.utc(b[0]["close_ts"]).date().isoformat()

    def by(rs, key):
        g = defaultdict(list)
        for r in rs:
            g[key(r)].append(1 if r["result"] == "yes" else 0)
        return g

    hour = lambda r: H.to_local(r["close_ts"] - H.WINDOW, tz).hour
    ga, gb = by(a, hour), by(b, hour)
    flagged, held = [], []
    xs, ys = [], []
    for h in range(24):
        if not ga.get(h) or not gb.get(h):
            continue
        pa, pb = statistics.mean(ga[h]), statistics.mean(gb[h])
        xs.append(pa - 0.5)
        ys.append(pb - 0.5)
        if abs(pa - 0.5) > H.ci95(pa, len(ga[h])):
            flagged.append(h)
            if (pb - 0.5) * (pa - 0.5) > 0 and abs(pb - 0.5) > H.ci95(pb, len(gb[h])):
                held.append(h)
    corr = pearson(xs, ys)
    fmt = lambda hs: ", ".join(f"{h:02d}:00" for h in hs) or "none"
    out.append(check("Hour-of-day lean holds out of sample?", "PASS" if held else "INFO",
                     f"Split at {split_day}. Hours that looked significant in the first half: {fmt(flagged)}; "
                     f"still significant, same direction, in the second half: {fmt(held)}. "
                     f"Correlation of hourly leans between halves: {corr:+.2f} "
                     + ("(near zero: the hourly pattern is noise)." if abs(corr) < 0.3 else
                        "(some persistence; worth watching)." if corr > 0 else
                        "(negative: the leans flipped between halves, so not a stable pattern).")))

    # streak rule: after 3 in a row
    def after3(rs):
        res = {"up": [], "down": []}
        for i in range(3, len(rs)):
            last = rs[i - 1]["result"]
            if all(rs[i - j]["result"] == last and rs[i - j + 1]["close_ts"] - rs[i - j]["close_ts"] == H.WINDOW
                   for j in range(1, 4)):
                res["up" if last == "yes" else "down"].append(1 if rs[i]["result"] == "yes" else 0)
        return res
    sa, sb = after3(a), after3(b)
    parts = []
    for side in ("up", "down"):
        if sa[side] and sb[side]:
            parts.append(f"after 3 {side.capitalize()}s the next was Up {statistics.mean(sa[side]) * 100:.1f}% "
                         f"(first half, n={len(sa[side]):,}) vs {statistics.mean(sb[side]) * 100:.1f}% (second half, n={len(sb[side]):,})")
    out.append(check("Streak behaviour holds out of sample?", "INFO",
                     "; ".join(parts) + ". For a fade-or-follow rule to pay, the same lean must show in both halves "
                                        "and be larger than the ~2–3¢ per contract that spread and fees cost."))

    # Kalshi pricing edge by bucket, 2 minutes before close
    def buckets(rs):
        g = defaultdict(list)
        for r in rs:
            bid, ask = r.get("m2_bid"), r.get("m2_ask")
            if bid is None or ask is None:
                continue
            up = 1 if r["result"] == "yes" else 0
            k = min(int((bid + ask) / 2 * 10), 9)
            g[k].append((up - ask - H.taker_fee(ask), (1 - up) - (1 - bid) - H.taker_fee(1 - bid)))
        return g
    priced = [r for r in rows if r.get("m2_bid") is not None and r.get("m2_ask") is not None]
    pa_, pb_ = halves(priced)               # split the windows that have Kalshi prices, not the whole history
    ba, bb = buckets(pa_), buckets(pb_)
    if sum(len(v) for v in ba.values()) < 200:
        out.append(check("Kalshi price edge holds out of sample?", "INFO",
                         "needs Kalshi's price history: run python history.py --candles (or --candles-days 30) first"))
    else:
        # A level only counts if its profit is clear of its own error band (2 standard errors), first in the
        # first half and then again in the second. Small paper profits inside the band are reported as noise.
        def stat(vals):
            m = statistics.mean(vals)
            return m, (2 * statistics.stdev(vals) / math.sqrt(len(vals)) if len(vals) > 1 else float("inf")), len(vals)

        def fmt(s):
            return f"{s[0] * 100:+.1f}¢ ±{s[1] * 100:.1f} (n={s[2]})"

        clear, held, noisy, busy = [], [], [], []
        for k in range(10):
            if len(ba.get(k, [])) < 30 or len(bb.get(k, [])) < 30:
                continue
            for side, idx in (("Yes", 0), ("No", 1)):
                sa_, sb_ = stat([v[idx] for v in ba[k]]), stat([v[idx] for v in bb[k]])
                label = f"buy {side} at {k * 10}–{k * 10 + 10}¢"
                if sa_[0] - sa_[1] > 0:
                    clear.append(label)
                    if sb_[0] - sb_[1] > 0:
                        held.append(f"{label}: {fmt(sa_)} → {fmt(sb_)}")
                elif sa_[0] > 0:
                    noisy.append((sa_[0], f"{label}: {fmt(sa_)}"))
                if k in (0, 9):
                    busy.append(sb_[0])
        if held:
            detail = (f"{len(held)} price level{'s' if len(held) != 1 else ''} beat Kalshi clearly in both halves, "
                      f"beyond the error band ({'; '.join(held[:4])}). Worth paper trading before believing it.")
        elif clear:
            detail = (f"{len(clear)} price level{'s' if len(clear) != 1 else ''} looked clearly profitable in the first half "
                      f"({', '.join(clear[:4])}) but did not hold up in the second.")
        else:
            detail = "No price level was clearly profitable after fees."
        if noisy:
            noisy.sort(reverse=True)
            detail += (f" {len(noisy)} more showed a paper profit that sits inside its error band, so it could just be "
                       f"luck: {'; '.join(t for _, t in noisy[:3])}.")
        if busy:
            detail += (f" Most windows trade under 10¢ or over 90¢; there, recent results ran "
                       f"{min(busy) * 100:+.1f}¢ to {max(busy) * 100:+.1f}¢ per contract.")
        out.append(check("Kalshi price edge holds out of sample?", "PASS" if held else "INFO", detail))

    # multiple comparisons
    tests = sig = 0
    for keyf in (hour, lambda r: H.to_local(r["close_ts"] - H.WINDOW, tz).weekday(),
                 lambda r: H.to_local(r["close_ts"] - H.WINDOW, tz).minute):
        for k, v in by(rows, keyf).items():
            tests += 1
            p = statistics.mean(v)
            sig += abs(p - 0.5) > H.ci95(p, len(v))
    out.append(check("Chance findings to expect", "INFO",
                     f"The report tests {tests} hour, weekday and slot groups; at 95% confidence about "
                     f"{tests * 0.05:.1f} would look significant by luck alone. Found: {sig}. "
                     + ("That's no more than luck would produce." if sig <= max(2, round(tests * 0.05 * 2)) else
                        "More than luck would usually produce; the out-of-sample check above is the real test.")))
    return out


# --------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="Validate downloaded Kalshi 15-minute crypto history")
    ap.add_argument("--asset", default="BTC", help="BTC, ETH or SOL (default BTC)")
    ap.add_argument("--coinbase", type=int, default=300, help="windows to cross-check against Coinbase (0 = skip)")
    ap.add_argument("--coinbase-url", default=COINBASE, help=argparse.SUPPRESS)
    args = ap.parse_args()
    H.set_asset(args.asset)
    path = os.path.join(H.OUT, "windows.csv")
    if not os.path.exists(path):
        sys.exit(f"No history yet. Run: python history.py --asset {H.ASSET}")
    rows = H.read_windows_csv(path)
    rows.sort(key=lambda r: r["close_ts"])
    tz = "ET"
    try:
        with open(os.path.join(H.OUT, "summary.json"), encoding="utf-8") as f:
            tz = json.load(f).get("tz", "ET")
    except (OSError, ValueError):
        pass

    print(f"\nValidating {len(rows):,} {H.ASSET} windows...\n")
    checks = integrity(rows)
    cb, buckets = coinbase_check(rows, args.coinbase, args.coinbase_url)
    checks.append(cb)
    checks += randomness(rows)
    checks += out_of_sample(rows, tz)

    counts = defaultdict(int)
    for c in checks:
        counts[c["status"]] += 1
    result = {"generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
              "windows": len(rows), "counts": dict(counts), "checks": checks,
              "spot_check": spot_links(rows), "coinbase_buckets": buckets}
    with open(os.path.join(H.OUT, "validation.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, indent=1)

    marks = {"PASS": "✓ PASS", "WARN": "! WARN", "FAIL": "✕ FAIL", "INFO": "i INFO"}
    for c in checks:
        print(f"  {marks[c['status']]:8}  {c['name']}\n            {c['detail']}\n")
    print("  Spot-check these on kalshi.com (price to beat and outcome should match):")
    for s in result["spot_check"][:5]:
        print(f"    {s['url']}  →  {s['strike']} → {s['settle_value']}, {'Up' if s['result'] == 'yes' else 'Down'}")
    print(f"    ...{len(result['spot_check']) - 5} more in the report\n")

    summary = H.build_summary(rows, tz)
    summary["validation"] = result
    H.write_outputs(rows, summary)
    print(f"  {counts['PASS']} passed, {counts['WARN']} warnings, {counts['FAIL']} failed.")
    print(f"  Report updated: {os.path.join(H.OUT, 'report.html')}\n")


if __name__ == "__main__":
    main()
