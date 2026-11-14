"""
edgefinder.py - looks for groups of taker entries that made money for real, without fooling itself.

The execution test's taker entries lose about the fee on average. An average can hide a profitable pocket (for
example only big claimed edges, only long shots, or only the final minute), so this splits the settled entries
(exec_entries.jsonl, written by execution.py) by their context and scores each group.

Only entries the reality check *confirmed* (a real trade happened at our price) count. A group "holds up" only if:
  * it has at least MIN_N confirmed entries,
  * its profit per contract after fees is more than twice its error band, and
  * it made money in both the first and the second half of its entries (in time order).
Many groups are tested, so a few can pass by luck alone; the report says how many were tested and how many luck
alone would be expected to pass.

    python edgefinder.py data/exec_entries.jsonl          (a coin's log; ETH and SOL are in data/eth, data/sol)
"""

import json
import math
import sys

MIN_N = 40

DIMENSIONS = [
    ("claimed edge", "ev", [(0.0, 0.05, "3–5¢"), (0.05, 0.08, "5–8¢"), (0.08, 0.12, "8–12¢"), (0.12, 9, "12¢+")], 1),
    ("price paid", "price", [(0, 0.15, "under 15¢"), (0.15, 0.35, "15–35¢"), (0.35, 0.65, "35–65¢"),
                             (0.65, 0.85, "65–85¢"), (0.85, 1.01, "over 85¢")], 1),
    ("time left", "secs_left", [(0, 30, "last 30 s"), (30, 60, "30–60 s"), (60, 120.01, "1–2 min"),
                                (120.01, 300, "2–5 min"), (300, 600, "5–10 min"), (600, 99999, "10+ min")], 1),
    ("distance from price to beat", "z", [(0, 0.5, "under 0.5 sd"), (0.5, 1, "0.5–1 sd"), (1, 2, "1–2 sd"),
                                          (2, 99, "2+ sd")], 1),
    ("hour (ET)", "hour_et", [(0, 6, "midnight–6am"), (6, 9.5, "6–9:30am"), (9.5, 16, "9:30am–4pm"),
                              (16, 24.01, "4pm–midnight")], 1),
    ("agents agreeing", "agree", [(0, 0.5, "under half"), (0.5, 0.75, "half to 3/4"), (0.75, 1.01, "3/4 or more")], 1),
    ("side", "side", [("up", None, "Up"), ("down", None, "Down")], 0),
]


def load(path, limit=50000):
    rows = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return rows[-limit:]


def per_contract(r):
    return r["pnl"] / r["n"] if r.get("n") else 0.0


def stats(vals):
    n = len(vals)
    if n == 0:
        return None
    m = sum(vals) / n
    sd = math.sqrt(sum((v - m) ** 2 for v in vals) / (n - 1)) if n > 1 else 0.0
    return {"n": n, "mean": m, "band": 2 * sd / math.sqrt(n) if n > 1 else float("inf")}


def in_bucket(r, key, lo, hi, numeric):
    v = r.get(key)
    if v is None:
        return False
    if not numeric:
        return v == lo
    v = abs(v) if key == "z" else v
    return lo <= v < hi


def score(rows):
    vals = [per_contract(r) for r in sorted(rows, key=lambda r: r["ts"])]
    s = stats(vals)
    if not s:
        return None
    h = len(vals) // 2
    a, b = stats(vals[:h]), stats(vals[h:])
    s["first_half"] = a["mean"] if a else None
    s["second_half"] = b["mean"] if b else None
    s["holds"] = (s["n"] >= MIN_N and s["mean"] - s["band"] > 0 and a and b and a["mean"] > 0 and b["mean"] > 0)
    s["win_rate"] = sum(1 for r in rows if r.get("won")) / len(rows)
    return s


def analyze(rows):
    """Score every lane x dimension x bucket on confirmed entries. Returns a report dict."""
    groups = []
    for lane in ("taker", "late"):
        conf = [r for r in rows if r.get("lane") == lane and r.get("check") == "ok"]
        allr = [r for r in rows if r.get("lane") == lane]
        overall = score(conf)
        if overall:
            groups.append(dict(overall, lane=lane, dimension="all entries", bucket="all", all_n=len(allr)))
        for dim, key, buckets, numeric in DIMENSIONS:
            for lo, hi, label in buckets:
                sub = [r for r in conf if in_bucket(r, key, lo, hi, numeric)]
                sc = score(sub)
                if sc:
                    groups.append(dict(sc, lane=lane, dimension=dim, bucket=label))
    testable = [g for g in groups if g["n"] >= MIN_N]
    holds = [g for g in testable if g["holds"]]
    return {"groups": sorted(groups, key=lambda g: -g["mean"]), "tested": len(testable), "holds": holds,
            # with no real edge, about 1 group in 50 clears the 2-band bar (and both halves) by luck alone
            "expected_by_luck": round(len(testable) * 0.02, 1),
            "entries": len(rows), "confirmed": sum(1 for r in rows if r.get("check") == "ok"), "min_n": MIN_N}


def describe(rep):
    lines = [f"{rep['entries']} settled entries, {rep['confirmed']} confirmed by real trades; "
             f"{rep['tested']} groups with at least {rep['min_n']} confirmed entries tested."]
    if rep["holds"]:
        for g in rep["holds"]:
            lines.append(f"HOLDS: {g['lane']} · {g['dimension']} = {g['bucket']}: {g['mean'] * 100:+.1f}¢ per contract "
                         f"±{g['band'] * 100:.1f} (n={g['n']}; halves {g['first_half'] * 100:+.1f}¢ / {g['second_half'] * 100:+.1f}¢)")
        lines.append(f"Luck alone would be expected to pass about {rep['expected_by_luck']} of these. Treat a single pass "
                     "as a lead to watch, not proof.")
    else:
        lines.append("No group holds up yet.")
    return "\n".join(lines)


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "data/exec_entries.jsonl"
    rep = analyze(load(path))
    print(describe(rep))
    print()
    print(f"{'lane':6} {'group':44} {'n':>5} {'¢/contract':>11} {'±':>6} {'1st half':>9} {'2nd half':>9}")
    for g in rep["groups"]:
        if g["n"] < 10:
            continue
        print(f"{g['lane']:6} {(g['dimension'] + ' = ' + g['bucket'])[:44]:44} {g['n']:5d} {g['mean'] * 100:+10.1f}¢ "
              f"{g['band'] * 100:5.1f}¢ {(g['first_half'] or 0) * 100:+8.1f}¢ {(g['second_half'] or 0) * 100:+8.1f}¢"
              + ("  HOLDS" if g["holds"] else ""))
