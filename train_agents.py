#!/usr/bin/env python3
"""
train_agents.py - give the agent team a head start from the downloaded history.

  python history.py --candles-days 30   (first: windows + Kalshi's prices before each close)
  python train_agents.py                (stop app.py first; add --asset ETH or --asset SOL for the other coins)

What it can learn from history:
  * Kalshi crowd agent: Kalshi's price 14, 10, 5 and 2 minutes before close vs what actually happened
    (needs the --candles prices)
  * Time & streaks agent: Up/Down base rates by hour, quarter-hour slot and streak, for every window

The price, momentum and exchange agents need second-by-second BTC data that history doesn't have,
so they learn live from their first window onward.

Every forecast is scored before the agent learns from that window ("predict, then learn"), so the
numbers printed are honest out-of-sample results, not a fit to data the agent has already seen.
"""

import argparse
import os
import sys

import history as H
from agents import Council, logloss

MARKS = ((14, 840), (10, 600), (5, 300), (2, 120))


def main():
    ap = argparse.ArgumentParser(description="Warm-start the agent team from downloaded history")
    ap.add_argument("--reset", action="store_true", help="forget everything the team has learned and start over")
    ap.add_argument("--asset", default="BTC", help="BTC, ETH or SOL (default BTC)")
    args = ap.parse_args()
    H.set_asset(args.asset)
    path = os.path.join(H.OUT, "windows.csv")
    if not os.path.exists(path):
        sys.exit(f"No history yet. Run: python history.py --asset {H.ASSET} --candles-days 30")
    rows = sorted(H.read_windows_csv(path), key=lambda r: r["close_ts"])
    state = os.path.join(os.path.dirname(H.OUT), "agents_state.json")
    if args.reset and os.path.exists(state):
        os.remove(state)
    council = Council(state, 900, peers=[a for a in H.COINS if a != H.ASSET])
    crowd, pattern = council.by_key["crowd"], council.by_key["pattern"]
    seen = set(council.counted)
    start_after = council.history_until

    n_new = 0
    tally = {"crowd": [], "pattern": [], "market": []}          # (log-loss, is in last 30%)
    cut = int(len(rows) * 0.7)
    for i, r in enumerate(rows):
        if r["ticker"] in seen or r["close_ts"] <= start_after:
            continue
        n_new += 1
        y = 1 if r["result"] == "yes" else 0
        lt = H.to_local(r["close_ts"] - H.WINDOW, "ET")
        late = i >= cut
        made = []
        for mk, secs in MARKS:
            b, a = r.get(f"m{mk}_bid"), r.get(f"m{mk}_ask")
            ctx = {"hour": lt.hour, "slot": lt.minute, "secs_left": secs, "window_sec": 900}
            if b is not None and a is not None and 0 < b < a < 1:
                ctx.update(mid=(a + b) / 2, spread=a - b, dmid30=0.0)
            for agent in (crowd, pattern):
                p, x = agent.predict(ctx, council)
                if p is not None:
                    made.append((agent, p, x, ctx.get("mid")))
            if ctx.get("mid") is not None:
                tally["market"].append((logloss(ctx["mid"], y), late))
        k = max(1, sum(1 for m in made if m[0] is crowd))
        for agent, p, x, mid in made:
            agent.score.add(p, y, mid)
            tally[agent.key].append((logloss(p, y), late))
            agent.learn(x, y, 1.0 / k if agent is crowd else 0.25)
        council.observe_result(r["ticker"], r["close_ts"], y, lt.hour, lt.minute)
        if i % 2000 == 0:
            print(f"\r  learning... {i:,}/{len(rows):,} windows", end="", flush=True)
    print(f"\r  learned from {n_new:,} new windows ({len(rows) - n_new:,} already known)        ")
    council.history_until = max(council.history_until, rows[-1]["close_ts"])
    council.save()

    if not n_new:
        print("  Nothing new to learn. Download more with python history.py, then rerun.\n")
        return

    def avg(key, late_only=False):
        v = [ll for ll, late in tally[key] if late or not late_only]
        return (sum(v) / len(v), len(v)) if v else (None, 0)

    print("\n  Forecast error (log-loss, lower is better; 0.693 = coin flip). Every forecast was made before learning from it.")
    for label, late in (("all windows", False), ("most recent 30%", True)):
        c, nc = avg("crowd", late)
        m, nm = avg("market", late)
        p, npat = avg("pattern", late)
        line = f"    {label:16}"
        if c is not None and m is not None:
            line += f"  Kalshi price {m:.4f} vs crowd agent {c:.4f} ({'better' if c < m else 'worse'} by {abs(m - c):.4f}, n={nc:,})"
        else:
            line += "  (no Kalshi prices downloaded: run python history.py --candles-days 30)"
        if p is not None:
            line += f" | patterns {p:.4f} vs coin flip 0.6931"
        print(line)
    w = crowd.model.w
    print(f"\n  Crowd agent learned: slope on Kalshi's log-odds {w[0]:.3f} (1.0 = Kalshi is well calibrated; "
          f">1 = Kalshi underconfident, <1 = overconfident), bias {w[3]:+.3f}")
    print(f"  Saved to {state}. Start app.py and the {H.ASSET} team continues learning live.\n")


if __name__ == "__main__":
    main()
