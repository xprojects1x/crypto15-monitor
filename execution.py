"""
execution.py - simulated limit ("maker") orders for paper trading.

Taking the ask pays Kalshi's taker fee (up to ~1.75¢ per contract near 50¢). A resting limit order usually pays
nothing (series not listed in Kalshi's fee schedule have a maker multiplier of 0), but it only fills when someone
else sells to you, and that tends to happen when the price is moving against you. This module measures which
effect wins.

Execution test (always on, from day one, no proof needed)
  For every window it trades the team's forecast two ways, with the same flat size and the same entry rules:
    taker  buy at the ask, the first second the edge after taker fees reaches min_edge
    limit  post a bid 1¢ above the best bid (or join it when the spread is 1¢), the first second the edge at that
           price reaches min_edge; cancel if the team's fair value falls below the limit or at the no-entry cutoff
  and keeps score: trades, wins, P&L, fees, fill rate, and how the unfilled orders would have done.

Team paper orders (setting order_type = "maker")
  The risk manager's own paper trades rest as limit orders in the same way, instead of buying at the ask.

Late entries (the final 2 minutes)
  These markets settle on the average of the index over the last 60 seconds, so in the final minutes part of the
  answer is already locked in. The normal rules never enter then; the "late" lane is a separate taker test from
  120 s to 3 s before the close (same edge rule, fees, size and reality check), kept apart from everything else.

Entry log (for the edge finder)
  Every settled taker entry (normal and late) is appended to <coin data>/exec_entries.jsonl with its context:
  seconds left, claimed edge, price, distance from the price to beat, volatility, hour, how many active agents
  agreed, the reality-check result, the outcome and P&L. edgefinder.py looks for groups that held up in both halves.

Reality check (are the prices we see real?)
  Our Kalshi prices are a copy fetched once a second, which can trail Kalshi's real order book. So:
    * every taker entry is checked against Kalshi's actual trade prints in the next few seconds: did anyone really
      buy at the ask we assumed ("confirmed"), did trades only print at worse prices ("price was gone"), or was there
      no trading to tell ("unknown")? P&L is reported for each group.
    * every printed trade is compared with the quote we had at that second: a taker buy prints at the real ask and a
      taker sell at the real bid, so the number of seconds until our copy shows that price measures how stale it is.

Fill model (deliberately conservative)
  * A buy-Yes limit at L fills when Kalshi prints a trade where the taker sold Yes (taker_side "no") at a Yes price
    at or below L. At exactly L we first wait behind the size that was already resting there when we posted.
  * A buy-No limit at L fills when the taker bought Yes (taker_side "yes") at a No price at or below L.
  * If the other side of the book crosses our limit (e.g. Yes ask <= L), the remainder fills at once.
  * No price improvement, no queue jumping; partial fills are kept.
"""

import json
import math
import os
import time
import uuid
from collections import deque

UNKNOWN_QUEUE = 1000         # resting size assumed ahead of us when Kalshi doesn't report the level's size
CHECK_AFTER = 6              # seconds of trade prints after a taker entry that decide whether its price was real
TOL = 0.0051                 # half a cent: prices are in whole cents
MAX_LAG = 15                 # quote staleness is measured up to this many seconds


def taker_fee_total(price, contracts):
    if price is None or not 0 < price < 1 or contracts <= 0:
        return 0.0
    return math.ceil(0.07 * contracts * price * (1 - price) * 100 - 1e-9) / 100


def maker_fee_total(price, contracts, mult):
    """Kalshi maker fee: round_up(M x 0.0175 x C x P x (1-P)); M is 0 for most series."""
    if not mult or price is None or not 0 < price < 1 or contracts <= 0:
        return 0.0
    return math.ceil(mult * 0.0175 * contracts * price * (1 - price) * 100 - 1e-9) / 100


def maker_limit(bid, ask):
    """Where a limit buy would rest: 1¢ above the best bid when there's room, else join the best bid."""
    if bid is None or ask is None or ask <= bid:
        return None
    lim = round(bid + 0.01, 2) if ask - bid >= 0.02 - 1e-9 else round(bid, 2)
    return lim if 0.01 <= lim <= 0.99 and lim < ask else None


def side_quotes(market, side):
    """(best bid, best ask, size resting at the bid) for buying Yes ('up') or No ('down')."""
    if side == "up":
        return market.get("yes_bid"), market.get("yes_ask"), market.get("yes_bid_size")
    # the best No bid is the other side of the best Yes ask
    return market.get("no_bid"), market.get("no_ask"), market.get("yes_ask_size")


def parse_trade(t, parse_ts):
    def num(v):
        try:
            return float(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None
    yp = num(t.get("yes_price_dollars"))
    if yp is None and t.get("yes_price") is not None:
        yp = num(t.get("yes_price"))
        yp = yp / 100 if yp is not None and yp > 1 else yp
    cnt = num(t.get("count_fp"))
    if cnt is None:
        cnt = num(t.get("count"))
    side = (t.get("taker_side") or "").lower()
    if yp is None or not cnt or side not in ("yes", "no"):
        return None
    return {"id": t.get("trade_id") or f"{t.get('created_time')}-{yp}-{cnt}-{side}", "yes_price": yp,
            "count": cnt, "taker_side": side, "ts": parse_ts(t.get("created_time")) or 0.0}


def blank_checks():
    return {k: {"trades": 0, "wins": 0, "pnl": 0.0} for k in ("ok", "gone", "unknown")}


def taker_check(pos, trades):
    """Was the ask a taker entry assumed really there? 'ok', 'gone', or None (undecided yet)."""
    t0, px = pos["ts"], pos["price"]
    near = [t for t in trades if t0 - 1 <= t["ts"] <= t0 + CHECK_AFTER]
    for t in near:                         # someone bought our side at our price or better
        side_px = t["yes_price"] if pos["side"] == "up" else 1 - t["yes_price"]
        bought_our_side = t["taker_side"] == ("yes" if pos["side"] == "up" else "no")
        if bought_our_side and side_px <= px + TOL:
            return "ok"
    for t in near:                         # trades only printed at prices worse than ours
        side_px = t["yes_price"] if pos["side"] == "up" else 1 - t["yes_price"]
        if side_px > px + TOL:
            return "gone"
    return None


def blank_stats():
    return {"taker": {"trades": 0, "wins": 0, "pnl": 0.0, "fees": 0.0, "contracts": 0},
            "late": {"trades": 0, "wins": 0, "pnl": 0.0, "fees": 0.0, "contracts": 0},
            "maker": {"orders": 0, "windows": 0, "trades": 0, "wins": 0, "pnl": 0.0, "fees": 0.0,
                      "contracts_posted": 0, "contracts_filled": 0, "cancelled": 0,
                      "missed_win": 0, "missed_loss": 0}}


class ExecSim:
    def __init__(self, path, window_sec=900):
        self.path = path
        self.window_sec = window_sec
        self.orders = []                       # open orders, both lanes
        self.test = {}                         # ticker -> {"taker": pos|None, "maker": pos|None, "posted": bool, ...}
        self.stats = blank_stats()
        self.recent = deque(maxlen=1500)       # one record per settled window (about 2 weeks)
        self.since = time.time()
        self.seen = {}                         # ticker -> set of trade ids already applied
        self.trades_ok = None                  # did the last trades request work?
        self.trades_err = ""
        self.trades_ts = 0.0
        self.checks = blank_checks()           # taker P&L split by reality-check result
        self.checks_late = blank_checks()      # the same for the late lane
        self.log_path = os.path.join(os.path.dirname(path) or ".", "exec_entries.jsonl")
        self.lags = deque(maxlen=3000)         # per printed trade: seconds until our quote showed its price (None = never)
        self.trade_buf = {}                    # ticker -> recent parsed trades (for the reality check)
        self.dirty = False
        self.load()

    # ----- persistence ---------------------------------------------------------------

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            return
        self.orders = d.get("orders", [])
        self.test = d.get("test", {})
        st = blank_stats()
        for lane in st:
            st[lane].update((d.get("stats") or {}).get(lane, {}))
        self.stats = st
        self.recent = deque(d.get("recent", []), maxlen=1500)
        self.since = d.get("since", self.since)
        ck = blank_checks()
        for k in ck:
            ck[k].update((d.get("checks") or {}).get(k, {}))
        self.checks = ck
        ckl = blank_checks()
        for k in ckl:
            ckl[k].update((d.get("checks_late") or {}).get(k, {}))
        self.checks_late = ckl
        self.lags = deque(d.get("lags", []), maxlen=3000)

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"orders": self.orders, "test": self.test, "stats": self.stats, "checks": self.checks,
                       "checks_late": self.checks_late, "lags": list(self.lags), "recent": list(self.recent), "since": self.since}, f)
        os.replace(tmp, self.path)
        self.dirty = False

    def reset_test(self):
        self.test, self.stats, self.recent, self.since = {}, blank_stats(), deque(maxlen=1500), time.time()
        self.checks, self.checks_late, self.lags = blank_checks(), blank_checks(), deque(maxlen=3000)
        self.orders = [o for o in self.orders if o["lane"] != "test"]
        self.save()

    # ----- orders --------------------------------------------------------------------

    def open_orders(self, ticker=None, lane=None):
        return [o for o in self.orders if (ticker is None or o["ticker"] == ticker) and (lane is None or o["lane"] == lane)]

    def post(self, lane, market, side, contracts, limit=None, fair=None, now_ts=None):
        bid, ask, bid_size = side_quotes(market, side)
        lim = limit if limit is not None else maker_limit(bid, ask)
        if lim is None or contracts < 1:
            return None
        joining = bid is not None and abs(lim - bid) < 1e-9
        o = {"id": uuid.uuid4().hex[:8], "lane": lane, "ticker": market["ticker"], "side": side, "limit": lim,
             "contracts": int(contracts), "filled": 0, "fee": 0.0,
             "queue_ahead": (bid_size if bid_size is not None else UNKNOWN_QUEUE) if joining else 0.0,
             "posted_ts": now_ts or time.time(), "close_ts": market.get("close_ts"), "fair": fair,
             "best_bid_at_post": bid, "ask_at_post": ask}
        self.orders.append(o)
        if lane == "test":
            self.stats["maker"]["orders"] += 1
            self.stats["maker"]["contracts_posted"] += o["contracts"]
            t = self.test.setdefault(o["ticker"], {"taker": None, "maker": None})
            t["posted"] = True
        self.dirty = True
        return o

    def cancel(self, o, why):
        self.orders = [x for x in self.orders if x["id"] != o["id"]]
        o["cancelled"] = why
        if o["lane"] == "test":
            if o["filled"] < o["contracts"]:
                self.stats["maker"]["cancelled"] += 1
            t = self.test.setdefault(o["ticker"], {"taker": None, "maker": None})
            if o["filled"] == 0:
                t.setdefault("unfilled", []).append({"side": o["side"], "limit": o["limit"], "contracts": o["contracts"]})
        self.dirty = True

    def _fill(self, o, qty, mult, how):
        qty = int(min(qty, o["contracts"] - o["filled"]))
        if qty < 1:
            return None
        prev_fee = o["fee"]
        o["filled"] += qty
        o["fee"] = maker_fee_total(o["limit"], o["filled"], mult)
        o.setdefault("fills", []).append({"ts": time.time(), "qty": qty, "how": how})
        ev = {"order": o, "qty": qty, "fee": o["fee"] - prev_fee}
        if o["lane"] == "test":
            t = self.test.setdefault(o["ticker"], {"taker": None, "maker": None})
            t["maker"] = {"side": o["side"], "price": o["limit"], "contracts": o["filled"], "fee": o["fee"]}
        if o["filled"] >= o["contracts"]:
            self.orders = [x for x in self.orders if x["id"] != o["id"]]
        self.dirty = True
        return ev

    def on_book(self, market, mult):
        """The other side crossed our limit: whatever is left fills now."""
        events = []
        if not market:
            return events
        for o in self.open_orders(market["ticker"]):
            _, ask, _ = side_quotes(market, o["side"])
            if ask is not None and ask <= o["limit"] + 1e-9:
                ev = self._fill(o, o["contracts"] - o["filled"], mult, "crossed")
                if ev:
                    events.append(ev)
        return events

    def on_trades(self, ticker, trades, mult):
        """Kalshi's printed trades: would any of them have reached our resting orders?"""
        events = []
        seen = self.seen.setdefault(ticker, set())
        for tr in sorted(trades, key=lambda t: t["ts"]):
            if tr["id"] in seen:
                continue
            seen.add(tr["id"])
            left = tr["count"]
            # best-priced orders first; ours at the same price share in posting order
            for o in sorted(self.open_orders(ticker), key=lambda o: (-o["limit"], o["posted_ts"])):
                if left <= 0 or tr["ts"] < o["posted_ts"]:
                    continue
                if o["side"] == "up":
                    hits, px = tr["taker_side"] == "no", tr["yes_price"]
                else:
                    hits, px = tr["taker_side"] == "yes", round(1 - tr["yes_price"], 4)
                if not hits or px > o["limit"] + 1e-9:
                    continue
                if abs(px - o["limit"]) < 1e-9 and o["queue_ahead"] > 0:      # wait our turn at our own price
                    used = min(left, o["queue_ahead"])
                    o["queue_ahead"] -= used
                    left -= used
                    self.dirty = True
                if left <= 0:
                    continue
                ev = self._fill(o, left, mult, "trade")
                if ev:
                    left -= ev["qty"]
                    events.append(ev)
        if len(seen) > 5000:
            self.seen[ticker] = set(list(seen)[-2000:])
        for tk in [k for k in self.seen if k != ticker][:-2]:
            del self.seen[tk]
        return events

    def manage(self, ticker, p_team, secs_left, cfg, no_entry):
        """Pull orders the team no longer wants: fair value below the limit, the no-entry cutoff, or a finished window."""
        mult = float(cfg.get("maker_fee_mult", 0) or 0)
        for o in list(self.orders):
            if o["ticker"] != ticker:
                self.cancel(o, "window ended")
                continue
            if secs_left is not None and secs_left <= no_entry:
                self.cancel(o, "no-entry cutoff")
                continue
            if p_team is not None:
                prob = p_team if o["side"] == "up" else 1 - p_team
                if prob - o["limit"] - maker_fee_total(o["limit"], 1, mult) < 0:
                    self.cancel(o, "fair value fell below the limit")

    # ----- the always-on execution test ------------------------------------------------

    def _taker_entry(self, ctx, p_team, cfg, C, info):
        """The best side to buy at the ask if its edge after fees reaches min_edge, else None."""
        best = None
        mk = {"yes_ask": ctx.get("yes_ask"), "no_ask": ctx.get("no_ask"), "yes_bid": ctx.get("yes_bid"), "no_bid": ctx.get("no_bid")}
        for side, prob in (("up", p_team), ("down", 1 - p_team)):
            _, ask, _ = side_quotes(mk, side)
            if ask is None:
                continue
            fee = taker_fee_total(ask, C)
            ev = prob - ask - fee / C
            if ev >= cfg["min_edge"] and (best is None or ev > best[3]):
                best = (side, ask, fee, ev)
        if not best:
            return None
        cx = dict(info or {}, secs_left=round(ctx["secs_left"], 1), ev=round(best[3], 4), spread=ctx.get("spread"))
        ag = (info or {}).get("agent_probs") or {}
        if ag:                                  # how many active agents agreed this side beats its price
            yes = [p for p in ag.values() if (p > best[1] if best[0] == "up" else 1 - p > best[1])]
            cx["agree"] = round(len(yes) / len(ag), 3)
        cx.pop("agent_probs", None)
        return {"side": best[0], "price": best[1], "contracts": C, "fee": best[2], "ev": best[3],
                "ts": time.time(), "check": None, "ctx": cx}

    def evaluate(self, ctx, p_team, cfg, scale=1.0, info=None):
        """Called every second: enter the taker variant and post the limit variant when their edge is there;
        in the final 2 minutes, the separate late lane instead."""
        if not ctx or p_team is None:
            return
        tk, secs = ctx["ticker"], ctx["secs_left"]
        if (ctx.get("n_feeds") or 0) < cfg["min_feeds"]:
            return
        if ctx.get("spread") is not None and ctx["spread"] > cfg["max_spread"] + 1e-9:
            return
        if cfg.get("avoid_events", True) and ctx.get("high_event"):
            return
        C = int(cfg["contracts"])
        mult = float(cfg.get("maker_fee_mult", 0) or 0)
        for old in [k for k, v in self.test.items() if k != tk and time.time() - v.get("t0", 0) > 4 * self.window_sec]:
            del self.test[old]                 # windows that never settled here (e.g. the app was restarted)
            self.dirty = True
        t = self.test.setdefault(tk, {"taker": None, "maker": None})
        t.setdefault("t0", time.time())
        if secs <= cfg["no_entry_last_sec"] * scale:          # the final minutes: only the late lane
            late_from = cfg.get("late_test_from_sec", 120) * scale
            late_until = cfg.get("late_test_until_sec", 3) * scale
            if cfg.get("late_test", True) and late_until < secs <= late_from and t.get("late") is None:
                pos = self._taker_entry(ctx, p_team, cfg, C, info)
                if pos:
                    t["late"] = pos
                    self.dirty = True
            return
        mk = {"ticker": tk, "close_ts": None, "yes_bid": ctx.get("yes_bid"), "yes_ask": ctx.get("yes_ask"),
              "no_bid": ctx.get("no_bid"), "no_ask": ctx.get("no_ask"),
              "yes_bid_size": ctx.get("yes_bid_size"), "yes_ask_size": ctx.get("yes_ask_size")}
        if t.get("taker") is None:
            pos = self._taker_entry(ctx, p_team, cfg, C, info)
            if pos:
                t["taker"] = pos
                self.dirty = True
        if t.get("maker") is None and not self.open_orders(tk, "test"):
            best = None
            for side, prob in (("up", p_team), ("down", 1 - p_team)):
                bid, ask, _ = side_quotes(mk, side)
                lim = maker_limit(bid, ask)
                if lim is None:
                    continue
                ev = prob - lim - maker_fee_total(lim, C, mult) / C
                if ev >= cfg["min_edge"] and (best is None or ev > best[2]):
                    best = (side, lim, ev)
            if best:
                mk["close_ts"] = ctx.get("close_ts")
                self.post("test", mk, best[0], C, limit=best[1], fair=p_team if best[0] == "up" else 1 - p_team)

    def needs_trades(self):
        """Tickers whose trade prints are still needed: resting orders, or taker entries not yet checked."""
        tks = {o["ticker"] for o in self.orders}
        tks |= {tk for tk, t in self.test.items()
                if any(t.get(l) and t[l].get("check") is None for l in ("taker", "late"))}
        return tks

    def check_takers(self, ticker, trades, now_ts=None):
        """Add new prints for a ticker and settle any taker reality checks they decide."""
        now_ts = now_ts or time.time()
        buf = self.trade_buf.setdefault(ticker, deque(maxlen=3000))
        known = {t["id"] for t in buf}
        buf.extend(t for t in trades if t["id"] not in known)
        for tk in [k for k in self.trade_buf if k != ticker and k not in self.test][:-1]:
            del self.trade_buf[tk]
        t = self.test.get(ticker)
        for lane in ("taker", "late"):
            pos = t.get(lane) if t else None
            if not pos or pos.get("check") is not None or "ts" not in pos:
                continue
            res = taker_check(pos, buf)
            if res == "ok" or (res and now_ts > pos["ts"] + CHECK_AFTER):
                pos["check"] = res
            elif now_ts > pos["ts"] + CHECK_AFTER + 4:      # prints for the whole check period are in: decide
                pos["check"] = res or "unknown"
            self.dirty = True

    def record_lag(self, lag):
        self.lags.append(lag)

    def freshness(self):
        """How stale our copy of Kalshi's prices is, from recent trade prints."""
        n = len(self.lags)
        if n < 20:
            return {"n": n}
        seen = sorted(x for x in self.lags if x is not None)
        # we fetch Kalshi once a second, so a print matched within 1 s is as fresh as this setup can be
        return {"n": n, "within_1s": sum(1 for x in self.lags if x is not None and x <= 1) / n,
                "within_3s": sum(1 for x in self.lags if x is not None and x <= 3) / n,
                "never": sum(1 for x in self.lags if x is None) / n,
                "median_lag": seen[len(seen) // 2] if seen else None}

    def settle(self, ticker, result, close_ts=None):
        """A window settled: score both variants and remember how the unfilled limit orders would have done."""
        for o in self.open_orders(ticker):
            self.cancel(o, "window ended")
        t = self.test.pop(ticker, None)
        if result not in ("yes", "no") or not t:
            if t is not None:
                self.dirty = True
            return
        y = result == "yes"
        rec = {"ticker": ticker, "close_ts": close_ts or time.time(), "result": result}
        for lane in ("taker", "late", "maker"):
            pos = t.get(lane)
            s = self.stats[lane]
            if pos:
                won = (pos["side"] == "up") == y
                pnl = (pos["contracts"] * (1 - pos["price"]) if won else -pos["contracts"] * pos["price"]) - pos["fee"]
                s["trades"] += 1
                s["wins"] += 1 if won else 0
                s["pnl"] += pnl
                s["fees"] += pos["fee"]
                if lane in ("taker", "late"):
                    s["contracts"] += pos["contracts"]
                else:
                    s["contracts_filled"] += pos["contracts"]
                rec[lane] = {"side": pos["side"], "price": pos["price"], "n": pos["contracts"], "pnl": round(pnl, 4),
                             "fee": pos["fee"]}
                if lane in ("taker", "late") and "ts" in pos:
                    ck = pos.get("check") or "unknown"
                    rec[lane]["check"] = ck
                    book = self.checks if lane == "taker" else self.checks_late
                    book[ck]["trades"] += 1
                    book[ck]["wins"] += 1 if won else 0
                    book[ck]["pnl"] += pnl
                    self._log_entry(lane, ticker, pos, result, won, pnl, ck)
        if t.get("posted"):
            self.stats["maker"]["windows"] += 1
            if not t.get("maker"):
                missed = (t.get("unfilled") or [{}])[-1]
                if missed.get("side"):
                    would_win = (missed["side"] == "up") == y
                    self.stats["maker"]["missed_win" if would_win else "missed_loss"] += 1
                    rec["maker_missed"] = "win" if would_win else "loss"
        if len(rec) > 3:
            self.recent.append(rec)
        self.dirty = True

    def _log_entry(self, lane, ticker, pos, result, won, pnl, check):
        row = {"ts": round(pos["ts"], 1), "lane": lane, "ticker": ticker, "side": pos["side"], "price": pos["price"],
               "n": pos["contracts"], "fee": pos["fee"], "result": result, "won": won, "pnl": round(pnl, 4),
               "check": check, **(pos.get("ctx") or {})}
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
        except OSError:
            pass

    # ----- reporting ---------------------------------------------------------------------

    def summary(self, since_ts=None):
        """Totals (all-time, or for windows closing after since_ts)."""
        if since_ts is None:
            s = json.loads(json.dumps(self.stats))
        else:
            s = blank_stats()
            for r in self.recent:
                if r["close_ts"] < since_ts:
                    continue
                for lane in ("taker", "late", "maker"):
                    p = r.get(lane)
                    if p:
                        s[lane]["trades"] += 1
                        s[lane]["wins"] += 1 if p["pnl"] > 0 else 0
                        s[lane]["pnl"] += p["pnl"]
                        s[lane]["fees"] += p.get("fee", 0.0)
                if r.get("maker_missed"):
                    s["maker"]["missed_" + r["maker_missed"]] += 1
        for lane, key, source in (("taker", "checks", self.checks), ("late", "checks_late", self.checks_late)):
            if since_ts is None:
                ck = json.loads(json.dumps(source))
            else:
                ck = blank_checks()
                for r in self.recent:
                    p = r.get(lane)
                    if r["close_ts"] >= since_ts and p and p.get("check"):
                        c = ck[p["check"]]
                        c["trades"] += 1
                        c["wins"] += 1 if p["pnl"] > 0 else 0
                        c["pnl"] += p["pnl"]
            for c in ck.values():
                c["per_trade"] = c["pnl"] / c["trades"] if c["trades"] else None
            s[key] = ck
        s["freshness"] = self.freshness()
        for lane in ("taker", "late", "maker"):
            x = s[lane]
            x["per_trade"] = x["pnl"] / x["trades"] if x["trades"] else None
            x["win_rate"] = x["wins"] / x["trades"] if x["trades"] else None
        m = s["maker"]
        m["fill_rate"] = (m["trades"] / m["windows"]) if m.get("windows") else None
        m["contract_fill_rate"] = (m["contracts_filled"] / m["contracts_posted"]) if m.get("contracts_posted") else None
        s["open_orders"] = [{k: o.get(k) for k in ("lane", "side", "limit", "contracts", "filled", "queue_ahead")}
                            for o in self.orders]
        s["since"] = self.since
        s["trades_feed"] = {"ok": self.trades_ok, "error": self.trades_err,
                            "age": round(time.time() - self.trades_ts, 1) if self.trades_ts else None}
        return s
