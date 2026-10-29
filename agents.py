"""
agents.py - a team of learning agents for Kalshi's 15-minute crypto Up/Down markets (BTC, ETH, SOL).

Specialist agents each forecast P(Up) from one kind of information and keep learning from every settled
window. A coordinator learns how much to trust each of them, a risk manager turns the combined forecast
into paper trades (or no trade), and a scorekeeper measures what each agent really contributes.

  Specialists                       starts as
    price     Price & volatility    active      settlement-average model; learns over/under-confidence
    momentum  Momentum              active      do the last 5s-5min of movement keep going or snap back?
    lead      Exchange lead/lag     active      does one exchange move first?
    crowd     Kalshi crowd          active      Kalshi's own price; learns where traders mis-price
    pattern   Time & streaks        probation   hour of day, quarter-hour slot, streak of results
    flow      Order flow            probation   taker buy vs sell pressure, order-book depth imbalance
    derivs    Derivatives           probation   perpetual-futures premium, funding, futures-leads-spot
    cross     Cross-asset           probation   do the other coins move first?
    regime    Events & volatility   probation   confidence near data releases, Fed days, US open, vol spikes
    tech      Chart reading         probation   RSI, Bollinger %B, MACD on 1-minute bars

  Probation   New agents forecast, learn and are scored, but have no say in the team forecast. A "shadow"
              coordinator that includes every agent measures what each would add. An agent is promoted when
              its added value is clearly positive over ~200 windows, and an active agent is demoted when its
              value is clearly negative.
  Coordinator online logistic "stacking" per phase of the window (early / middle / late)
  Risk manager trades only after the team clearly beats Kalshi's prices, with fractional Kelly sizing,
              hard caps, a daily loss limit, and stand-asides around high-impact events
  Scorekeeper log-loss vs Kalshi's price, value added (leave-one-out), solo paper P&L

Nothing here places real orders.
"""

import datetime as dt
import json
import math
import os
import time
from collections import deque

EPS = 1e-6
CHECKPOINTS = (840, 720, 600, 480, 360, 240, 150)     # seconds-left marks where forecasts are recorded
PHASES = (("early", 480), ("middle", 240), ("late", 0))
STATE_VERSION = 2
IMPACT = {"low": 0, "medium": 1, "high": 2}


def sigmoid(z):
    z = max(-30.0, min(30.0, z))
    return 1.0 / (1.0 + math.exp(-z))


def logit(p):
    p = min(1 - EPS, max(EPS, p))
    return math.log(p / (1 - p))


def clip(x, lim=6.0):
    return max(-lim, min(lim, x))


def logloss(p, y):
    p = min(1 - 1e-4, max(1e-4, p))
    return -math.log(p if y else 1 - p)


def fee_pc(price):
    """Kalshi taker fee per contract (before cent rounding, which is negligible at size)."""
    return 0.07 * price * (1 - price) if price and 0 < price < 1 else 0.0


def phase_of(secs_left, scale):
    for name, lo in PHASES:
        if secs_left > lo * scale:
            return name
    return "late"


def mean_se(values, cluster=len(CHECKPOINTS)):
    """Mean and standard error; forecasts from the same window are correlated, so count windows, not forecasts."""
    n = len(values)
    if n < 2:
        return None, None
    m = sum(values) / n
    sd = math.sqrt(sum((v - m) ** 2 for v in values) / (n - 1))
    return m, sd / math.sqrt(max(1.0, n / cluster))


# --------------------------------------------------------------------------- learner

class OnlineLogit:
    """Logistic regression trained one example at a time, gently pulled back toward its starting beliefs."""

    def __init__(self, init, lr=0.05, l2=5e-4):
        self.w = list(init)
        self.prior = list(init)
        self.lr, self.l2 = lr, l2
        self.n = 0

    def score(self, x):
        return sum(wi * xi for wi, xi in zip(self.w, x))

    def predict(self, x):
        return sigmoid(self.score(x))

    def update(self, x, y, weight=1.0):
        g = self.predict(x) - y
        lr = self.lr * weight / math.sqrt(1 + self.n / 500)
        for i, xi in enumerate(x):
            self.w[i] -= lr * (g * xi + self.l2 * (self.w[i] - self.prior[i]))
        self.n += 1

    def dump(self):
        return {"w": self.w, "prior": self.prior, "n": self.n}

    def load(self, d):
        if d and len(d.get("w", [])) == len(self.w):
            self.w, self.n = list(d["w"]), int(d.get("n", 0))
            if len(d.get("prior", [])) == len(self.w):
                self.prior = list(d["prior"])


class Score:
    """Running forecast quality: log-loss, Brier, hit rate, edge over Kalshi's price, value added."""

    def __init__(self):
        self.n = 0
        self.ll = self.brier = self.hits = 0.0
        self.recent = deque(maxlen=1400)         # (own log-loss, market log-loss or None): ~200 windows
        self.value_added = 0.0                   # all-time sum of team log-loss improvement (agents only)
        self.va_n = 0
        self.va_recent = deque(maxlen=1400)      # recent per-forecast value added, for promotion/demotion
        self.vs_team = deque(maxlen=1400)        # recent (team log-loss - own log-loss): does it beat the team alone?
        self.solo = {"trades": 0, "wins": 0, "pnl": 0.0, "fees": 0.0}

    def add(self, p, y, p_mkt=None):
        self.n += 1
        self.ll += logloss(p, y)
        self.brier += (p - y) ** 2
        self.hits += 1 if (p >= 0.5) == bool(y) else 0
        self.recent.append((logloss(p, y), logloss(p_mkt, y) if p_mkt is not None else None))

    def add_value(self, v):
        self.value_added += v
        self.va_n += 1
        self.va_recent.append(v)

    def recent_edge(self, with_se=False):
        """Average log-loss advantage over Kalshi's price (positive = better), over recent forecasts."""
        d = [b - a for a, b in self.recent if b is not None]
        if len(d) < 20:
            return (None, len(d), None) if with_se else (None, len(d))
        m, se = mean_se(d)
        return (m, len(d), se) if with_se else (m, len(d))

    def summary(self):
        edge, n_edge = self.recent_edge()
        va_m, va_se = mean_se(list(self.va_recent))
        vt_m, vt_se = mean_se(list(self.vs_team))
        return {"n": self.n, "logloss": self.ll / self.n if self.n else None,
                "brier": self.brier / self.n if self.n else None,
                "hit_rate": self.hits / self.n if self.n else None,
                "edge_vs_market": edge, "edge_n": n_edge,
                "value_added": self.value_added / self.va_n if self.va_n else None, "va_n": self.va_n,
                "va_recent": va_m, "va_recent_se": va_se, "va_recent_n": len(self.va_recent),
                "vs_team": vt_m, "vs_team_se": vt_se,
                "solo": dict(self.solo)}

    def dump(self):
        return {"n": self.n, "ll": self.ll, "brier": self.brier, "hits": self.hits, "recent": list(self.recent),
                "va": self.value_added, "va_n": self.va_n, "va_recent": list(self.va_recent),
                "vs_team": list(self.vs_team), "solo": self.solo}

    def load(self, d):
        if not d:
            return
        self.n, self.ll, self.brier, self.hits = d.get("n", 0), d.get("ll", 0.0), d.get("brier", 0.0), d.get("hits", 0.0)
        self.recent = deque([tuple(x) for x in d.get("recent", [])], maxlen=1400)
        self.value_added, self.va_n = d.get("va", 0.0), d.get("va_n", 0)
        self.va_recent = deque(d.get("va_recent", []), maxlen=1400)
        self.vs_team = deque(d.get("vs_team", []), maxlen=1400)
        self.solo = d.get("solo", self.solo)


# --------------------------------------------------------------------------- specialists

class Agent:
    key = name = category = blurb = ""
    init = (0.0,)
    lr = 0.05
    start_status = "active"

    def __init__(self):
        self.model = OnlineLogit(self.init, lr=self.lr)
        self.score = Score()

    def features(self, ctx, council):
        raise NotImplementedError

    def predict(self, ctx, council):
        try:
            x = self.features(ctx, council)
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            x = None                       # missing or odd data: this agent sits this one out
        if x is None:
            return None, None
        return self.model.predict(x), x

    def learn(self, x, y, weight):
        self.model.update(x, y, weight)

    def dump(self):
        return {"model": self.model.dump(), "score": self.score.dump()}

    def load(self, d):
        self.model.load((d or {}).get("model"))
        self.score.load((d or {}).get("score"))


class PriceAgent(Agent):
    key, name, category = "price", "Price & volatility", "Fundamentals"
    blurb = "Settlement-average model from the live index proxy; learns when it is over- or under-confident."
    init = (1.0, 0.0, 0.0)          # [model log-odds, log-odds x share of window left, bias]

    def features(self, ctx, council):
        if ctx.get("p_model") is None:
            return None
        L = clip(logit(ctx["p_model"]), 8)
        return [L, L * ctx["secs_left"] / ctx["window_sec"], 1.0]


class MomentumAgent(Agent):
    key, name, category = "momentum", "Momentum", "Trend"
    blurb = "Recent moves over 5 s to 5 min: does price keep going (trend) or snap back (mean reversion)?"
    init = (1.6, 0.0, 0.0, 0.0, 0.0, 0.0)
    lr = 0.08

    def features(self, ctx, council):
        if ctx.get("z") is None or not ctx.get("ret"):
            return None
        r = ctx["ret"]
        return [clip(ctx["z"])] + [clip(r.get(h) or 0.0, 4) for h in (5, 15, 60, 300)] + [1.0]


class LeadAgent(Agent):
    key, name, category = "lead", "Exchange lead/lag", "Microstructure"
    blurb = "Gaps between each exchange and the composite: does one venue move first and drag the index?"
    venues = ("Coinbase", "Kraken", "Bitstamp", "Gemini", "Crypto.com")
    init = (1.6, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    lr = 0.08

    def features(self, ctx, council):
        if ctx.get("z") is None or ctx.get("dev") is None:
            return None
        d = ctx["dev"]
        return [clip(ctx["z"])] + [clip(d.get(v) or 0.0, 4) for v in self.venues] + [clip(ctx.get("disp") or 0.0, 4), 1.0]


class CrowdAgent(Agent):
    key, name, category = "crowd", "Kalshi crowd", "Sentiment"
    blurb = "Kalshi's own price, its last-30s drift and spread; learns where traders systematically mis-price."
    init = (1.0, 0.0, 0.0, 0.0)

    def features(self, ctx, council):
        if ctx.get("mid") is None:
            return None
        return [clip(logit(ctx["mid"]), 8), clip((ctx.get("dmid30") or 0.0) * 10, 4),
                clip((ctx.get("spread") or 0.0) * 10, 4), 1.0]


class PatternAgent(Agent):
    key, name, category = "pattern", "Time & streaks", "Patterns"
    blurb = "Base rates by hour of day, quarter-hour slot and the current streak; ignores price entirely."
    init = (0.5, 0.5, 0.5, 0.0)
    start_status = "probation"
    PRIOR = 50                       # strong prior toward 50/50: a pattern must earn its way in

    def __init__(self):
        super().__init__()
        self.counts = {"hour": {}, "slot": {}, "streak": {}}

    def rate(self, table, key):
        up, n = self.counts[table].get(str(key), (0, 0))
        return (up + self.PRIOR) / (n + 2 * self.PRIOR)

    @staticmethod
    def streak_key(results):
        if not results:
            return "none"
        last, k = results[-1], 0
        for r in reversed(results):
            if r != last:
                break
            k += 1
        return f"{'U' if last else 'D'}{min(k, 5)}"

    def features(self, ctx, council):
        if ctx.get("hour") is None:
            return None
        return [logit(self.rate("hour", ctx["hour"])), logit(self.rate("slot", ctx["slot"])),
                logit(self.rate("streak", self.streak_key(council.results))), 1.0]

    def count(self, hour, slot, streak, up):
        for table, key in (("hour", hour), ("slot", slot), ("streak", streak)):
            u, n = self.counts[table].get(str(key), (0, 0))
            self.counts[table][str(key)] = (u + up, n + 1)

    def dump(self):
        d = super().dump()
        d["counts"] = self.counts
        return d

    def load(self, d):
        super().load(d)
        c = (d or {}).get("counts")
        if c:
            self.counts = {k: {kk: tuple(vv) for kk, vv in c.get(k, {}).items()} for k in ("hour", "slot", "streak")}


class FlowAgent(Agent):
    key, name, category = "flow", "Order flow", "Trades & order book"
    blurb = "Taker buy vs sell pressure (Coinbase + Kraken trades) over 15 s to 5 min, and Kraken order-book depth imbalance."
    init = (1.6, 0.0, 0.0, 0.0, 0.0, 0.0)
    lr = 0.08
    start_status = "probation"

    def features(self, ctx, council):
        f = ctx.get("flow")
        if ctx.get("z") is None or not f or all(f.get(k) is None for k in ("tfi15", "tfi60", "tfi300", "book")):
            return None
        return [clip(ctx["z"])] + [clip(f.get(k) or 0.0, 1) for k in ("tfi15", "tfi60", "tfi300", "book")] + [1.0]


class DerivsAgent(Agent):
    key, name, category = "derivs", "Derivatives", "Futures"
    blurb = "Kraken perpetual futures: premium over the index, its 1-min change, funding, and whether futures move before spot."
    init = (1.6, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    lr = 0.08
    start_status = "probation"

    def features(self, ctx, council):
        p = ctx.get("perp")
        if ctx.get("z") is None or not p or p.get("premium") is None:
            return None
        return [clip(ctx["z"]), clip(p["premium"] * 10, 4), clip((p.get("dprem60") or 0.0) * 50, 4),
                clip(p.get("lead15") or 0.0, 4), clip(p.get("lead60") or 0.0, 4),
                clip((p.get("funding") or 0.0) * 1e4, 4), 1.0]


class CrossAgent(Agent):
    key, name, category = "cross", "Cross-asset", "Other coins"
    blurb = "Moves in the other coins over the last 15 s and 1 min that this coin hasn't matched yet."
    init = (1.6, 0.0, 0.0, 0.0, 0.0, 0.0)
    lr = 0.08
    start_status = "probation"

    def features(self, ctx, council):
        c = ctx.get("cross") or {}
        peers = (list(council.peers) + [None, None])[:2]
        if ctx.get("z") is None or not any(c.get(p) for p in peers if p):
            return None
        x = [clip(ctx["z"])]
        for p in peers:
            d = c.get(p) or {}
            x += [clip(d.get(15) or 0.0, 4), clip(d.get(60) or 0.0, 4)]
        return x + [1.0]


class RegimeAgent(Agent):
    key, name, category = "regime", "Events & volatility", "Regime"
    blurb = "Adjusts confidence near scheduled data releases and Fed decisions, the US stock open, weekends and volatility spikes."
    init = (1.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    start_status = "probation"

    def features(self, ctx, council):
        r = ctx.get("regime")
        if ctx.get("p_model") is None or not r:
            return None
        L = clip(logit(ctx["p_model"]), 8)
        return [L, L * (1.0 if r.get("near_event") else 0.0), L * clip(r.get("vol_ratio") or 0.0, 2),
                L * (1.0 if r.get("us_open") else 0.0), L * (1.0 if r.get("weekend") else 0.0), 1.0]


class TechAgent(Agent):
    key, name, category = "tech", "Chart reading", "Technical"
    blurb = "Classic chart indicators on 1-minute bars: RSI(14), Bollinger %B(20), MACD(12,26,9) histogram."
    init = (1.6, 0.0, 0.0, 0.0, 0.0)
    lr = 0.08
    start_status = "probation"

    def features(self, ctx, council):
        t = ctx.get("tech")
        if ctx.get("z") is None or not t or t.get("rsi") is None:
            return None
        return [clip(ctx["z"]), clip((t["rsi"] - 50) / 25, 2), clip((t.get("pb", 0.5) - 0.5) * 2, 3),
                clip(t.get("macd") or 0.0, 4), 1.0]


AGENT_CLASSES = (PriceAgent, MomentumAgent, LeadAgent, CrowdAgent, PatternAgent,
                 FlowAgent, DerivsAgent, CrossAgent, RegimeAgent, TechAgent)
TEAM_INIT = {"price": 0.5, "momentum": 0.2, "lead": 0.1, "crowd": 0.3}


# --------------------------------------------------------------------------- the council

class Council:
    def __init__(self, state_path, window_sec=900, peers=()):
        self.state_path = state_path
        self.window_sec = window_sec
        self.scale = window_sec / 900
        self.peers = tuple(peers)
        self.agents = [cls() for cls in AGENT_CLASSES]
        self.by_key = {a.key: a for a in self.agents}
        self.status = {a.key: a.start_status for a in self.agents}
        init = [TEAM_INIT.get(a.key, 0.0) for a in self.agents] + [0.0]
        self.meta = {ph: OnlineLogit(init, lr=0.03) for ph, _ in PHASES}       # live team (active agents)
        self.shadow = {ph: OnlineLogit(init, lr=0.1) for ph, _ in PHASES}      # everyone, for probation scoring
        self.team = Score()                    # the combined forecast
        self.market = Score()                  # Kalshi's own mid, as the benchmark
        self.pending = {}                      # ticker -> list of checkpoint records
        self.marks_done = {}                   # ticker -> set of checkpoint marks recorded
        self.solo_open = {}                    # ticker -> {agent key: hypothetical trade}
        self.results = deque(maxlen=50)        # previous window results (1 = Up), oldest first
        self.counted = deque(maxlen=5000)      # tickers already counted into pattern base rates
        self.windows_learned = 0
        self.history_until = 0                 # close time of the last window learned by train_agents.py
        self.curve = []                        # per settled window: [close_ts, team ll, market ll, n]
        self.live = {}                         # latest per-second forecasts for the dashboard
        self.decision = {"action": "WAIT", "reasons": ["Starting up"]}
        self.log = deque(maxlen=60)            # recent learning events
        self.load()
        self._sync_status_weights()

    # ----- persistence -----------------------------------------------------------------

    def dump(self):
        return {"version": STATE_VERSION, "window_sec": self.window_sec, "saved": time.time(),
                "agents": {a.key: a.dump() for a in self.agents}, "status": self.status,
                "meta": {k: m.dump() for k, m in self.meta.items()},
                "shadow": {k: m.dump() for k, m in self.shadow.items()},
                "team": self.team.dump(), "market": self.market.dump(),
                "results": list(self.results), "counted": list(self.counted),
                "windows_learned": self.windows_learned, "history_until": self.history_until,
                "curve": self.curve[-3000:], "log": list(self.log)}

    def save(self):
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.dump(), f)
        os.replace(tmp, self.state_path)

    def load(self):
        try:
            with open(self.state_path, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            return
        if d.get("version") != STATE_VERSION:
            return
        for a in self.agents:
            a.load(d.get("agents", {}).get(a.key))
        for k, s in (d.get("status") or {}).items():
            if k in self.status and s in ("active", "probation"):
                self.status[k] = s
        for k, m in self.meta.items():
            m.load(d.get("meta", {}).get(k))
        for k, m in self.shadow.items():
            m.load(d.get("shadow", {}).get(k))
        self.team.load(d.get("team"))
        self.market.load(d.get("market"))
        self.results = deque(d.get("results", []), maxlen=50)
        self.counted = deque(d.get("counted", []), maxlen=5000)
        self.windows_learned = d.get("windows_learned", 0)
        self.history_until = d.get("history_until", 0)
        self.curve = d.get("curve", [])
        self.log = deque(d.get("log", []), maxlen=60)

    def _sync_status_weights(self):
        """Agents on probation have zero weight in the live team."""
        for i, a in enumerate(self.agents):
            if self.status[a.key] == "probation":
                for m in self.meta.values():
                    m.w[i] = m.prior[i] = 0.0

    # ----- forecasting -----------------------------------------------------------------

    def forecast(self, ctx):
        """Every agent's P(Up), the team's P(Up), and what each agent's removal would do (shadow team)."""
        preds = {a.key: a.predict(ctx, self) for a in self.agents}
        if all(p is None for p, _ in preds.values()):
            return None
        phase = phase_of(ctx["secs_left"], self.scale)
        meta, shadow = self.meta[phase], self.shadow[phase]
        mx = [clip(logit(preds[a.key][0]), 8) if preds[a.key][0] is not None else 0.0 for a in self.agents] + [1.0]
        live_mx = [0.0 if (i < len(self.agents) and self.status[self.agents[i].key] != "active") else v
                   for i, v in enumerate(mx)]
        p_team = meta.predict(live_mx)
        p_shadow = shadow.predict(mx)
        without, influence = {}, {}
        total = sum(abs(meta.w[i] * live_mx[i]) for i in range(len(self.agents))) or 1.0
        for i, a in enumerate(self.agents):
            x2 = list(mx)
            x2[i] = 0.0
            without[a.key] = shadow.predict(x2)
            influence[a.key] = abs(meta.w[i] * live_mx[i]) / total if self.status[a.key] == "active" else None
        return {"phase": phase, "preds": preds, "mx": mx, "live_mx": live_mx, "p_team": p_team,
                "p_shadow": p_shadow, "without": without, "influence": influence}

    def step(self, ctx, cfg):
        """Called every second with the current context. Records forecasts at checkpoints."""
        if not ctx or ctx.get("secs_left") is None or ctx["secs_left"] <= 0:
            self.live = {}
            return None
        f = self.forecast(ctx)
        if not f:
            self.live = {}
            return None
        tk, secs = ctx["ticker"], ctx["secs_left"]
        self.live = {"ticker": tk, "phase": f["phase"], "p_team": f["p_team"], "mid": ctx.get("mid"),
                     "agents": {k: v[0] for k, v in f["preds"].items()}, "influence": f["influence"]}
        done = self.marks_done.setdefault(tk, set())
        for mark in CHECKPOINTS:
            m = mark * self.scale
            if mark not in done and secs <= m and secs > m - 15 * self.scale:
                done.add(mark)
                self.record(ctx, f, mark, cfg)
        for old in [k for k in self.marks_done if k != tk][:-2]:
            del self.marks_done[old]
        return f

    def record(self, ctx, f, mark, cfg):
        rec = {"mark": mark, "phase": f["phase"], "mx": f["mx"], "live_mx": f["live_mx"], "p_team": f["p_team"],
               "p_shadow": f["p_shadow"], "mid": ctx.get("mid"), "without": f["without"],
               "agents": {k: (p, x) for k, (p, x) in f["preds"].items() if p is not None}}
        self.pending.setdefault(ctx["ticker"], []).append(rec)
        # solo paper trades: each agent (and the team) trades alone, flat size, first qualifying checkpoint
        if ctx["secs_left"] <= cfg["no_entry_last_sec"] * self.scale:
            return
        open_ = self.solo_open.setdefault(ctx["ticker"], {})
        for key, p in [(k, v[0]) for k, v in f["preds"].items()] + [("team", f["p_team"])]:
            if p is None or key in open_:
                continue
            best = None
            for side, prob, ask in (("up", p, ctx.get("yes_ask")), ("down", 1 - p, ctx.get("no_ask"))):
                if ask is None:
                    continue
                ev = prob - ask - fee_pc(ask)
                if ev >= cfg["min_edge"] and (best is None or ev > best[2]):
                    best = (side, ask, ev)
            if best:
                open_[key] = {"side": best[0], "price": best[1], "contracts": int(cfg["contracts"])}

    # ----- learning --------------------------------------------------------------------

    def observe_result(self, ticker, close_ts, up, hour=None, slot=None):
        """Count a settled window into the pattern base rates (once) and the streak history."""
        if ticker in self.counted:
            return
        pat = self.by_key["pattern"]
        if hour is not None:
            pat.count(hour, slot, PatternAgent.streak_key(self.results), up)
        self.results.append(up)
        self.counted.append(ticker)

    def settle(self, ticker, result, close_ts=None, hour=None, slot=None, cfg=None):
        """A window settled: every forecast made during it becomes a lesson."""
        recs = self.pending.pop(ticker, [])
        solo = self.solo_open.pop(ticker, {})
        if result not in ("yes", "no"):
            return
        y = 1 if result == "yes" else 0
        if recs:
            w = 1.0 / len(recs)
            team_ll = mkt_ll = 0.0
            n_mkt = 0
            for r in recs:
                team_loss = logloss(r["p_team"], y)
                for key, (p, x) in r["agents"].items():
                    a = self.by_key[key]
                    a.score.add(p, y, r["mid"])
                    a.score.vs_team.append(team_loss - logloss(p, y))
                    a.learn(x, y, w)
                for key in r["agents"]:          # only agents that actually forecast this time
                    self.by_key[key].score.add_value(logloss(r["without"][key], y) - logloss(r["p_shadow"], y))
                self.team.add(r["p_team"], y, r["mid"])
                self.meta[r["phase"]].update(r["live_mx"], y, w * 2)
                self.shadow[r["phase"]].update(r["mx"], y, w * 2)
                team_ll += logloss(r["p_team"], y)
                if r["mid"] is not None:
                    self.market.add(r["mid"], y)
                    mkt_ll += logloss(r["mid"], y)
                    n_mkt += 1
            self._sync_status_weights()
            self.curve.append([close_ts or time.time(), team_ll / len(recs), mkt_ll / n_mkt if n_mkt else None, len(recs)])
            self.windows_learned += 1
        for key, t in solo.items():
            won = (t["side"] == "up") == bool(y)
            fees = fee_pc(t["price"]) * t["contracts"]
            pnl = (t["contracts"] * (1 - t["price"]) if won else -t["contracts"] * t["price"]) - fees
            sc = self.team if key == "team" else self.by_key[key].score
            sc.solo["trades"] += 1
            sc.solo["wins"] += 1 if won else 0
            sc.solo["pnl"] += pnl
            sc.solo["fees"] += fees
        self.observe_result(ticker, close_ts, y, hour, slot)
        if recs:
            best = max(self.agents, key=lambda a: a.score.value_added / a.score.va_n if a.score.va_n else -9)
            self.log.appendleft({"t": time.time(), "ticker": ticker, "result": "Up" if y else "Down",
                                 "team_p": recs[-1]["p_team"], "lessons": len(recs) * len(self.agents),
                                 "top": best.name})
            self.review(cfg or {})
        self.save()

    def probation_need(self, cfg):
        return min(1400, int(cfg.get("probation_windows", 200)) * len(CHECKPOINTS))

    def review(self, cfg):
        """Promote agents whose added value is clearly positive; demote active ones that clearly hurt."""
        need = self.probation_need(cfg)
        active = [a for a in self.agents if self.status[a.key] == "active"]
        for i, a in enumerate(self.agents):
            vals = list(a.score.va_recent)
            if len(vals) < need:
                continue
            m, se = mean_se(vals)
            vt, vt_se = mean_se(list(a.score.vs_team))
            if m is None:
                continue
            adds = m > 0.0002 and m > 2 * se                                  # improves the team as a member
            beats = vt is not None and vt > 0.002 and vt > 2 * vt_se          # beats the whole team on its own
            if self.status[a.key] == "probation" and (adds or beats):
                self.status[a.key] = "active"
                for ph in self.meta:
                    wi = max(self.shadow[ph].w[i], 0.25 if beats else 0.0)
                    self.meta[ph].w[i] = self.meta[ph].prior[i] = wi
                detail = (f"its forecasts beat the team's by {vt:+.4f} log-loss (±{2 * vt_se:.4f})" if beats else
                          f"adds {m:+.4f} log-loss per forecast to the team (±{2 * se:.4f})")
                self.log.appendleft({"t": time.time(), "event": "promoted", "agent": a.name, "detail": detail})
            elif self.status[a.key] == "active" and m < 0 and -m > 2 * se and len(active) > 2:
                self.status[a.key] = "probation"
                active.remove(a)
                for ph in self.meta:
                    self.meta[ph].w[i] = self.meta[ph].prior[i] = 0.0
                self.log.appendleft({"t": time.time(), "event": "demoted", "agent": a.name,
                                     "detail": f"costs {m:+.4f} log-loss per forecast (±{2 * se:.4f})"})

    # ----- risk manager ----------------------------------------------------------------

    def proven(self, cfg):
        """Trade only once the team has beaten Kalshi's own prices by a statistically clear margin."""
        edge, n, se = self.team.recent_edge(with_se=True)
        need = int(cfg["min_learned_windows"])
        if self.windows_learned < need:
            return False, f"Still learning: {self.windows_learned}/{need} windows settled"
        if not cfg["require_proof"]:
            return True, ""
        if edge is None or n < 350:
            return False, f"Collecting comparisons with Kalshi's prices ({n}/350 forecasts)"
        if edge <= 0.002 or edge < 2 * se:
            state = "behind" if edge < 0 else "ahead, but not clearly enough" if edge > 0 else "level"
            return False, (f"Team hasn't clearly beaten Kalshi's prices: {state} "
                           f"(log-loss edge {edge:+.4f} ± {2 * se:.4f})")
        return True, ""

    def decide(self, ctx, f, cfg, book):
        """Turn the team forecast into an action. book: open team positions, bankroll, today's P&L."""
        reasons = []
        act = {"action": "WAIT", "reasons": reasons}
        if not ctx or not f:
            reasons.append("Waiting for market and price data")
            self.decision = act
            return act
        p = f["p_team"]
        sides = []
        for side, prob, ask, bid in (("up", p, ctx.get("yes_ask"), ctx.get("yes_bid")),
                                     ("down", 1 - p, ctx.get("no_ask"), ctx.get("no_bid"))):
            if ask is None:
                continue
            ev = prob - ask - fee_pc(ask)
            sides.append({"side": side, "prob": prob, "ask": ask, "bid": bid, "ev": ev})
        act["sides"] = sides

        # exits first: take the market's overpayment
        for pos in book["open"]:
            prob = p if pos["side"] == "up" else 1 - p
            bid = ctx.get("yes_bid") if pos["side"] == "up" else ctx.get("no_bid")
            if bid is not None and bid - fee_pc(bid) >= prob + cfg["exit_margin"]:
                act.update({"action": "SELL", "id": pos["id"], "side": pos["side"],
                            "why": f"bid {bid * 100:.0f}¢ beats team fair value {prob * 100:.0f}¢"})
                self.decision = act
                return act

        if not cfg["auto_trade"]:
            reasons.append("Auto-trading is off (team still learns and keeps score)")
        ok, why = self.proven(cfg)
        if not ok:
            reasons.append(why)
        if ctx["secs_left"] <= cfg["no_entry_last_sec"] * self.scale:
            reasons.append(f"Inside the final {int(cfg['no_entry_last_sec'] * self.scale)}s")
        if (ctx.get("n_feeds") or 0) < cfg["min_feeds"]:
            reasons.append("Too few exchange feeds")
        if ctx.get("spread") is not None and ctx["spread"] > cfg["max_spread"] + 1e-9:
            reasons.append(f"Spread {ctx['spread'] * 100:.0f}¢ too wide")
        if cfg.get("avoid_events", True) and ctx.get("high_event"):
            reasons.append(f"Sitting out: {ctx['high_event']}")
        if book["entries_this_window"] >= cfg["max_entries_per_window"]:
            reasons.append("Already traded this window")
        if book["today_pnl"] <= -abs(cfg["daily_loss_limit"]):
            reasons.append(f"Daily loss limit hit (${book['today_pnl']:.2f}); paused until tomorrow (UTC)")
        best = max(sides, key=lambda s: s["ev"]) if sides else None
        if best is None:
            reasons.append("No prices on the book")
        elif best["ev"] < cfg["min_edge"]:
            reasons.append(f"Best edge {best['ev'] * 100:+.1f}¢ below {cfg['min_edge'] * 100:.0f}¢")
        if reasons:
            self.decision = act
            return act

        price = best["ask"] + fee_pc(best["ask"])
        kelly = max(0.0, (best["prob"] - price) / (1 - price))
        bank = max(0.0, book["equity"])
        stake = min(cfg["kelly_fraction"] * kelly * bank, cfg["max_bet_pct"] * bank)
        n = int(stake // price)
        cap = ctx.get("yes_ask_size") if best["side"] == "up" else None
        if cap:
            n = min(n, int(cap))
        if n < 1:
            reasons.append("Edge too small for the paper bankroll at this Kelly fraction")
            self.decision = act
            return act
        act.update({"action": "BUY", "side": best["side"], "contracts": n, "price": best["ask"],
                    "prob": best["prob"], "ev": best["ev"], "kelly": kelly, "stake": n * price})
        self.decision = act
        return act

    # ----- dashboard -------------------------------------------------------------------

    def snapshot(self, cfg):
        live = self.live or {}
        phase = live.get("phase") or "early"
        need = self.probation_need(cfg)
        rows = []
        for i, a in enumerate(self.agents):
            s = a.score.summary()
            rows.append({"key": a.key, "name": a.name, "category": a.category, "blurb": a.blurb,
                         "status": self.status[a.key], "probation_need": need,
                         "p_now": (live.get("agents") or {}).get(a.key),
                         "influence": (live.get("influence") or {}).get(a.key),
                         "trust": {ph: self.meta[ph].w[i] for ph, _ in PHASES},
                         "shadow_trust": self.shadow[phase].w[i],
                         "lessons": a.model.n, **s})
        ok, why = self.proven(cfg)
        return {"agents": rows, "team": self.team.summary(), "market": self.market.summary(),
                "live": {k: v for k, v in live.items() if k in ("ticker", "phase", "p_team", "mid")},
                "decision": self.decision, "windows_learned": self.windows_learned,
                "proven": ok, "proven_why": why, "curve": self.curve[-400:], "log": list(self.log)[:15],
                "active": sum(1 for v in self.status.values() if v == "active"),
                "phases": {ph: {"bias": self.meta[ph].w[-1], "n": self.meta[ph].n} for ph, _ in PHASES}}


# --------------------------------------------------------------------------- event calendar

class Events:
    """Scheduled market-moving events from events.json (dated) plus recurring weekday times, all in US Eastern."""

    def __init__(self, path, to_local):
        self.to_local = to_local
        self.path = path
        self.dated, self.recurring, self.mtime = [], [], None
        self.reload()

    def reload(self):
        try:
            mtime = os.path.getmtime(self.path)
            if mtime == self.mtime:
                return
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            return
        self.mtime = mtime
        self.dated = []
        for e in d.get("events", []):
            ts = self.et_ts(e.get("date", ""), e.get("time_et", "00:00"))
            if ts:
                self.dated.append((ts, e.get("name", "event"), IMPACT.get(e.get("impact", "medium"), 1)))
        self.recurring = [(set(r.get("weekdays", [0, 1, 2, 3, 4])), r.get("time_et", "08:30"), r.get("name", "event"),
                           IMPACT.get(r.get("impact", "medium"), 1)) for r in d.get("recurring", [])]

    def et_ts(self, date_s, hhmm):
        try:
            day = dt.date.fromisoformat(date_s)
            h, m = (int(v) for v in hhmm.split(":"))
        except (ValueError, AttributeError):
            return None
        naive = dt.datetime(day.year, day.month, day.day, h, m)
        for off in (4, 5):          # EDT or EST
            ts = (naive + dt.timedelta(hours=off)).replace(tzinfo=dt.timezone.utc).timestamp()
            if self.to_local(ts, "ET").replace(second=0, microsecond=0) == naive:
                return ts
        return None

    def between(self, t0, t1, min_impact=0):
        """Events with start time in [t0, t1]."""
        out = [(ts, n, imp) for ts, n, imp in self.dated if t0 <= ts <= t1 and imp >= min_impact]
        d0 = self.to_local(t0, "ET").date()
        d1 = self.to_local(t1, "ET").date()
        day = d0
        while day <= d1:
            for days, hhmm, name, imp in self.recurring:
                if day.weekday() in days and imp >= min_impact:
                    ts = self.et_ts(day.isoformat(), hhmm)
                    if ts and t0 <= ts <= t1:
                        out.append((ts, name, imp))
            day += dt.timedelta(days=1)
        return sorted(out)


# --------------------------------------------------------------------------- context

def build_ctx(engine, to_local):
    """Everything the agents may look at right now - nothing from the future."""
    m, md = engine.market, engine.model
    if not m or not md or md.get("strike") is None or not engine.samples:
        return None
    secs = md.get("secs_left")
    sd = md.get("settle_sd") or 0.0
    sig = md.get("sigma")
    if secs is None or secs <= 0:
        return None
    ctx = {"ticker": m["ticker"], "secs_left": secs, "window_sec": engine.window_sec,
           "p_model": md.get("p_up"), "z": None, "ret": None, "dev": None, "disp": None, "sd": sd,
           "yes_bid": m.get("yes_bid"), "yes_ask": m.get("yes_ask"), "no_bid": m.get("no_bid"), "no_ask": m.get("no_ask"),
           "yes_ask_size": m.get("yes_ask_size"), "n_feeds": engine.samples[-1][2]}
    if sd > 0 and sig:
        ctx["z"] = (md["settle_mean"] - md["strike"]) / sd
        last_sec, last = engine.samples[-1][0], engine.samples[-1][1]
        ret = {}
        for h in (5, 15, 60, 300):
            past = engine.sample_idx.get(last_sec - h)
            ret[h] = (last - past) / sd if past is not None else None
        ctx["ret"] = ret
        used = engine.samples[-1][3]
        ctx["dev"] = {k: (v - last) / sd for k, v in used.items()}
        if len(used) > 1:
            vals = list(used.values())
            mu = sum(vals) / len(vals)
            ctx["disp"] = math.sqrt(sum((v - mu) ** 2 for v in vals) / len(vals)) / sd
    yb, ya = m.get("yes_bid"), m.get("yes_ask")
    if yb is not None and ya is not None:
        ctx["mid"] = (yb + ya) / 2
        ctx["spread"] = ya - yb
        s = engine.window_series.get(m["ticker"])
        if s and len(s["t"]) > 30 and s["yb"][-31] is not None and s["ya"][-31] is not None:
            ctx["dmid30"] = ctx["mid"] - (s["yb"][-31] + s["ya"][-31]) / 200
    lt = to_local(m["open_ts"], "ET")
    ctx["hour"], ctx["slot"] = lt.hour, lt.minute
    if hasattr(engine, "extra_ctx"):
        ctx.update(engine.extra_ctx(ctx))
    return ctx
