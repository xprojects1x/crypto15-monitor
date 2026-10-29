#!/usr/bin/env python3
"""
Crypto 15-Minute Monitor for Kalshi (BTC, ETH and SOL Up/Down markets)
=====================================================================

Runs on your own computer (or a server) and serves a live dashboard at http://localhost:8765

What it does, for each coin
  * Streams prices from five constituent exchanges of CF Benchmarks' real-time index (Coinbase, Kraken,
    Bitstamp, Gemini, Crypto.com) over free public websockets, plus trades and order-book depth
    (Coinbase, Kraken) and Kraken perpetual-futures data.
  * Builds a 1-second index proxy (median of exchange mids) and self-calibrates it against Kalshi's
    published price to beat and settlement values.
  * Polls Kalshi's public market data for the live window (KXBTC15M, KXETH15M, KXSOL15M).
  * Models the 60-second settlement average and prices a fair P(Up).
  * Runs a team of learning agents (agents.py) that paper-trade only once they clearly beat Kalshi.
  * Logs every second to CSV (compressed daily, old days pruned) for backtesting.

It never places real orders and never needs your Kalshi keys.

Usage
  python app.py                      all three coins, live data
  python app.py --assets BTC         just BTC
  python app.py --demo               simulated markets (no internet needed)
  python app.py --port 9000          different port
"""

import argparse
import asyncio
import base64
import hmac
import csv
import datetime as dt
import gzip
import glob
import shutil
import threading
import json
import math
import os
import random
import statistics
import time
import uuid
from collections import deque

import aiohttp
from aiohttp import web

from agents import Council, Events, build_ctx
from alerts import Alerter, load_env_file
from history import to_local

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
STATIC_DIR = os.path.join(HERE, "static")

ASSETS = {
    "BTC": {"series": "KXBTC15M", "index": "BRTI", "coinbase": "BTC-USD", "kraken": "BTC/USD", "bitstamp": "btcusd",
            "gemini": "BTCUSD", "cryptocom": "BTC_USD", "perp": "PF_XBTUSD", "decimals": 2,
            "demo_price": 80000.0, "demo_lag": 0},
    "ETH": {"series": "KXETH15M", "index": "ETHUSD RTI", "coinbase": "ETH-USD", "kraken": "ETH/USD", "bitstamp": "ethusd",
            "gemini": "ETHUSD", "cryptocom": "ETH_USD", "perp": "PF_ETHUSD", "decimals": 2,
            "demo_price": 2600.0, "demo_lag": 3},
    "SOL": {"series": "KXSOL15M", "index": "SOLUSD RTI", "coinbase": "SOL-USD", "kraken": "SOL/USD", "bitstamp": "solusd",
            "gemini": "SOLUSD", "cryptocom": "SOL_USD", "perp": "PF_SOLUSD", "decimals": 4,
            "demo_price": 110.0, "demo_lag": 5},
}
SERIES = ASSETS["BTC"]["series"]
KALSHI_BASES = [
    "https://api.elections.kalshi.com/trade-api/v2",
    "https://external-api.kalshi.com/trade-api/v2",
]
FEED_STALE_SEC = 10          # a feed older than this is ignored
OUTLIER_PCT = 0.005          # a feed >0.5% away from the others is ignored
SETTLE_SAMPLES = 60          # Kalshi averages 60 one-second index prints
WINDOW_SEC = 900             # 15 minutes (shortened in demo mode if asked)

DEFAULT_CONFIG = {
    "composite": "median",       # median | mean of exchange mids
    "use_calibration": True,     # add learned offset vs Kalshi's own values
    "vol_lookback_sec": 600,     # realized-vol lookback for the model
    "vol_mult": 1.15,            # >1 = more conservative (fatter tails)
    "min_edge": 0.03,            # $ per contract after fees to flag an entry
    "no_entry_last_sec": 120,    # research: don't chase the final 2 minutes
    "max_spread": 0.04,          # skip if Yes bid/ask spread is wider
    "min_feeds": 3,              # need this many live exchanges
    "contracts": 10,             # size used for fee math and paper trades
    "exit_margin": 0.02,         # sell when bid beats fair value by this
    "poll_ms": 1000,             # Kalshi polling interval
    # --- agent team (paper trading only) ---
    "auto_trade": True,          # let the team's risk manager place paper trades by itself
    "agent_bankroll": 1000.0,    # paper bankroll the team trades with
    "kelly_fraction": 0.25,      # fraction of the Kelly stake to use (0.25 = quarter Kelly)
    "max_bet_pct": 0.02,         # never risk more than this share of the bankroll on one window
    "daily_loss_limit": 100.0,   # pause team trading for the rest of the UTC day after this loss
    "require_proof": True,       # only trade once the team clearly beats Kalshi's own prices
    "min_learned_windows": 50,   # settled windows of learning before any trade
    "max_entries_per_window": 1,
    "avoid_events": True,        # sit out around high-impact events listed in events.json
    "event_buffer_min": 10,      # ...this many minutes either side
    "probation_windows": 200,    # windows a new agent is scored before it can be promoted
    "keep_days": 90,             # days of per-second tick logs to keep (older ones are deleted)
}


def now() -> float:
    return time.time()


def iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat().replace("+00:00", "Z")


def parse_ts(s):
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def fnum(x):
    """Parse Kalshi's string fixed-point numbers (or plain numbers)."""
    if x is None or x == "":
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def norm_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def taker_fee_total(price: float, contracts: int) -> float:
    """Kalshi standard taker fee: round_up(0.07 * C * P * (1-P)) to the cent."""
    if price is None or price <= 0 or price >= 1 or contracts <= 0:
        return 0.0
    raw = 0.07 * contracts * price * (1 - price)
    return math.ceil(raw * 100 - 1e-9) / 100


def fee_per_contract(price, contracts):
    if price is None:
        return None
    return taker_fee_total(price, contracts) / max(contracts, 1)


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Exchange feeds
# ---------------------------------------------------------------------------

class Feed:
    def __init__(self, name):
        self.name = name
        self.bid = None
        self.ask = None
        self.last = None
        self.ts = 0.0
        self.connected = False
        self.msgs = 0
        self.error = ""

    def update(self, bid=None, ask=None, last=None):
        if bid is not None:
            self.bid = bid
        if ask is not None:
            self.ask = ask
        if last is not None:
            self.last = last
        if self.bid and self.ask and self.bid > self.ask:   # crossed book: trust last
            self.bid = self.ask = None
        self.ts = now()
        self.msgs += 1

    @property
    def mid(self):
        if self.bid and self.ask:
            return (self.bid + self.ask) / 2
        return self.last

    def fresh(self):
        return self.mid is not None and (now() - self.ts) < FEED_STALE_SEC

    def to_dict(self, composite=None, valid=False):
        m = self.mid
        return {
            "name": self.name,
            "connected": self.connected,
            "mid": m,
            "bid": self.bid,
            "ask": self.ask,
            "age": round(now() - self.ts, 1) if self.ts else None,
            "dev": (m - composite) if (m is not None and composite) else None,
            "used": valid,
            "error": self.error,
        }


async def run_ws(session, feed, url, on_open, on_msg, heartbeat=20):
    backoff = 1
    while True:
        try:
            async with session.ws_connect(url, heartbeat=heartbeat, timeout=15) as ws:
                feed.connected = True
                feed.error = ""
                backoff = 1
                await on_open(ws)
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        try:
                            await on_msg(ws, json.loads(msg.data))
                        except Exception as e:  # malformed message: keep going
                            feed.error = f"parse: {e}"[:120]
                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break
        except asyncio.CancelledError:
            raise
        except Exception as e:
            feed.error = str(e)[:160] or e.__class__.__name__
        feed.connected = False
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 30)


class Flow:
    """Taker trades (Coinbase + Kraken) and Kraken's top-10 order book, for the order-flow agent."""

    def __init__(self):
        self.trades = deque(maxlen=60000)      # (ts, +notional for taker buys / -notional for taker sells)
        self.bids, self.asks = {}, {}
        self.book_ts = 0.0

    def trade(self, taker_buy, price, qty, ts=None):
        if price and qty:
            self.trades.append((ts or now(), price * qty if taker_buy else -price * qty))

    def book(self, bids, asks, snapshot=False, depth=10):
        if snapshot:
            self.bids, self.asks = {}, {}
        for side, levels in ((self.bids, bids), (self.asks, asks)):
            for lv in levels or []:
                p, q = fnum(lv.get("price")), fnum(lv.get("qty"))
                if p is None or q is None:
                    continue
                if q == 0:
                    side.pop(p, None)
                else:
                    side[p] = q
        for side, rev in ((self.bids, True), (self.asks, False)):     # keep only the subscribed depth
            for p in sorted(side, reverse=rev)[depth:]:
                del side[p]
        self.book_ts = now()

    def tfi(self, h):
        cut, buy, sell = now() - h, 0.0, 0.0
        for ts, v in reversed(self.trades):
            if ts < cut:
                break
            if v > 0:
                buy += v
            else:
                sell -= v
        tot = buy + sell
        return (buy - sell) / tot if tot > 0 else None

    def book_imb(self):
        if now() - self.book_ts > 15 or not self.bids or not self.asks:
            return None
        bq, aq = sum(self.bids.values()), sum(self.asks.values())
        return (bq - aq) / (bq + aq) if bq + aq > 0 else None

    def features(self):
        return {"tfi15": self.tfi(15), "tfi60": self.tfi(60), "tfi300": self.tfi(300), "book": self.book_imb()}

    def stats(self):
        n60 = sum(1 for ts, _ in self.trades if ts >= now() - 60)
        return dict(self.features(), trades_per_min=n60)


class Perp:
    """Kraken perpetual future for the coin: mid, premium over the index (%), funding."""

    def __init__(self, product):
        self.product = product
        self.bid = self.ask = self.last = self.mid = self.premium = self.funding = None
        self.ts = 0.0
        self.connected = False
        self.error = ""
        self.hist = deque(maxlen=900)          # (sec, mid, premium)

    def update(self, d):
        self.bid = fnum(d.get("bid")) or self.bid
        self.ask = fnum(d.get("ask")) or self.ask
        self.last = fnum(d.get("last")) or self.last
        self.mid = (self.bid + self.ask) / 2 if self.bid and self.ask else self.last
        if d.get("premium") is not None:
            self.premium = fnum(d.get("premium"))
        if d.get("relative_funding_rate") is not None:
            self.funding = fnum(d.get("relative_funding_rate"))
        self.ts = now()

    def fresh(self):
        return self.mid is not None and now() - self.ts < 15

    def sample(self, sec):
        if self.fresh():
            self.hist.append((sec, self.mid, self.premium))

    def at(self, sec):
        for s, m, pr in reversed(self.hist):
            if s <= sec:
                return (s, m, pr) if sec - s <= 3 else None
        return None

    def to_dict(self, spot=None):
        return {"product": self.product, "connected": self.connected, "mid": self.mid, "premium": self.premium,
                "funding": self.funding, "age": round(now() - self.ts, 1) if self.ts else None,
                "dev": (self.mid - spot) if (self.mid and spot) else None, "error": self.error}


def coinbase_task(session, feed, product, flow):
    async def on_open(ws):
        await ws.send_json({"type": "subscribe", "product_ids": [product], "channels": ["ticker", "matches"]})

    async def on_msg(ws, d):
        t = d.get("type")
        if t == "ticker":
            feed.update(fnum(d.get("best_bid")), fnum(d.get("best_ask")), fnum(d.get("price")))
        elif t == "match":
            # Coinbase reports the MAKER side: a "sell" maker means the taker bought
            flow.trade(d.get("side") == "sell", fnum(d.get("price")), fnum(d.get("size")))
        elif t == "error":
            feed.error = str(d.get("message"))[:120]

    return run_ws(session, feed, "wss://ws-feed.exchange.coinbase.com", on_open, on_msg)


def kraken_task(session, feed, symbol, flow):
    async def on_open(ws):
        await ws.send_json({"method": "subscribe",
                            "params": {"channel": "ticker", "symbol": [symbol], "event_trigger": "bbo"}})
        await ws.send_json({"method": "subscribe", "params": {"channel": "book", "symbol": [symbol], "depth": 10}})
        await ws.send_json({"method": "subscribe", "params": {"channel": "trade", "symbol": [symbol], "snapshot": False}})

    async def on_msg(ws, d):
        ch, data = d.get("channel"), d.get("data")
        if not isinstance(data, list):
            return
        if ch == "ticker":
            for t in data:
                feed.update(fnum(t.get("bid")), fnum(t.get("ask")), fnum(t.get("last")))
        elif ch == "book":
            for b in data:
                flow.book(b.get("bids"), b.get("asks"), snapshot=d.get("type") == "snapshot")
        elif ch == "trade":
            for t in data:                      # Kraken reports the TAKER side
                flow.trade(t.get("side") == "buy", fnum(t.get("price")), fnum(t.get("qty")))

    return run_ws(session, feed, "wss://ws.kraken.com/v2", on_open, on_msg)


def bitstamp_task(session, feed, pair):
    async def on_open(ws):
        await ws.send_json({"event": "bts:subscribe", "data": {"channel": f"order_book_{pair}"}})

    async def on_msg(ws, d):
        ev = d.get("event")
        if ev == "data":
            data = d.get("data") or {}
            bids, asks = data.get("bids") or [], data.get("asks") or []
            if bids and asks:
                feed.update(fnum(bids[0][0]), fnum(asks[0][0]))
        elif ev == "bts:request_reconnect":
            await ws.close()

    return run_ws(session, feed, "wss://ws.bitstamp.net", on_open, on_msg)


def gemini_task(session, feed, symbol):
    url = f"wss://api.gemini.com/v1/marketdata/{symbol}?top_of_book=true&bids=true&offers=true&trades=true"

    async def on_open(ws):
        return None

    async def on_msg(ws, d):
        for e in d.get("events", []) or []:
            t = e.get("type")
            if t == "change":
                p = fnum(e.get("price"))
                if fnum(e.get("remaining")) == 0:
                    continue
                if e.get("side") == "bid":
                    feed.update(bid=p)
                elif e.get("side") == "ask":
                    feed.update(ask=p)
            elif t == "trade":
                feed.update(last=fnum(e.get("price")))

    return run_ws(session, feed, url, on_open, on_msg)


def cryptocom_task(session, feed, instrument):
    async def on_open(ws):
        await asyncio.sleep(1)   # Crypto.com asks clients to wait 1s before sending
        await ws.send_json({"id": 1, "method": "subscribe",
                            "params": {"channels": [f"ticker.{instrument}"]}, "nonce": int(now() * 1000)})

    async def on_msg(ws, d):
        if d.get("method") == "public/heartbeat":
            await ws.send_json({"id": d.get("id"), "method": "public/respond-heartbeat"})
            return
        res = d.get("result") or {}
        if res.get("channel") == "ticker":
            for t in res.get("data", []) or []:
                feed.update(fnum(t.get("b")), fnum(t.get("k")), fnum(t.get("a")))

    return run_ws(session, feed, "wss://stream.crypto.com/exchange/v1/market", on_open, on_msg, heartbeat=None)


def kraken_futures_task(session, perp):
    async def on_open(ws):
        await ws.send_json({"event": "subscribe", "feed": "ticker", "product_ids": [perp.product]})

    async def on_msg(ws, d):
        if d.get("feed") == "ticker" and d.get("product_id") == perp.product:
            perp.update(d)
        elif d.get("event") in ("error", "alert"):
            perp.error = str(d.get("message"))[:120]

    return run_ws(session, perp, "wss://futures.kraken.com/ws/v1", on_open, on_msg)


# ---------------------------------------------------------------------------
# Demo simulator (no internet): fake exchanges + fake Kalshi market
# ---------------------------------------------------------------------------

class DemoWorld:
    """Simulated market for one coin: futures move first and spot follows 2 s later, alts follow BTC's moves a
    few seconds later, trade flow and the order book hint at short-term pressure, and a Kalshi market maker
    quotes around the plain settlement model - so the specialist agents have something real to discover."""

    def __init__(self, window_sec, asset, spec, hub=None):
        self.window = window_sec
        self.asset, self.spec, self.hub = asset, spec, hub
        self.sc = spec["demo_price"] / 80000.0
        self.perp_px = spec["demo_price"]
        self.true = self.perp_px
        self.perp_hist = deque(maxlen=3)
        self.hist = deque(maxlen=7200)      # (sec, true index)
        self.bias = 4.0 * self.sc           # true index sits a little above the exchange median
        self.offsets = {k: v * self.sc for k, v in
                        {"Coinbase": 1.5, "Kraken": -3.0, "Bitstamp": 0.5, "Gemini": -1.0, "Crypto.com": 2.0}.items()}
        self.vol = 3.0 * self.sc
        self.pressure = 0.0

    def step(self, feeds, flow=None, perp=None):
        sc = self.sc
        if random.random() < 0.01:
            self.vol = random.choice([1.5, 3.0, 5.0, 8.0]) * sc
        self.pressure = 0.97 * self.pressure + random.gauss(0, 0.05)
        sec = int(now())
        common = self.hub.common(sec - self.spec.get("demo_lag", 0)) if self.hub else random.gauss(0, 1)
        shock = self.vol * (0.7 * common + 0.71 * random.gauss(0, 1) + 0.3 * self.pressure)
        if random.random() < 0.003:
            shock += random.gauss(0, 40 * sc)
        self.perp_px += shock                       # futures move first...
        self.perp_hist.append(self.perp_px)
        self.true = self.perp_hist[0]               # ...spot follows 2 seconds later
        self.hist.append((sec, self.true))
        base = self.true - self.bias
        for f in feeds.values():
            if random.random() < 0.002:
                continue    # occasional gap
            m = base + self.offsets[f.name] + random.gauss(0, 1.5 * sc)
            spread = random.uniform(0.5, 4) * sc
            f.connected = True
            f.update(m - spread / 2, m + spread / 2, m)
        if flow is not None:
            lean = 0.25 * math.tanh(3 * self.pressure)
            for _ in range(random.randint(1, 6)):
                flow.trade(random.random() < 0.5 + lean, self.true, random.expovariate(1.0) * 0.1)
            b = min(0.95, max(0.05, 0.5 + 0.8 * lean + random.gauss(0, 0.1)))
            flow.book([{"price": self.true - i * sc, "qty": b} for i in range(1, 11)],
                      [{"price": self.true + i * sc, "qty": 1 - b} for i in range(1, 11)], snapshot=True)
        if perp is not None:
            prem = 0.02 + random.gauss(0, 0.005)
            perp.connected = True
            perp.update({"bid": self.perp_px - sc, "ask": self.perp_px + sc, "last": self.perp_px,
                         "premium": prem, "relative_funding_rate": 0.00001})

    def avg_before(self, ts):
        vals = [v for s, v in self.hist if ts - SETTLE_SAMPLES <= s < ts]
        return round(sum(vals) / len(vals), self.spec["decimals"]) if vals else None

    def market(self, model_p):
        t = now()
        start = t - (t % self.window)
        close = start + self.window
        strike = self.avg_before(start) or self.true
        ticker = f"{self.spec['series']}-DEMO-{dt.datetime.fromtimestamp(close).strftime('%y%b%d%H%M%S').upper()}"
        # crude market maker: plain model price + noise, 2-4c spread
        fair = model_p if model_p is not None else 0.5
        mid = min(0.99, max(0.01, fair + random.gauss(0, 0.04)))
        half = random.choice([0.01, 0.01, 0.02])
        yb, ya = max(0.01, round(mid - half, 2)), min(0.99, round(mid + half, 2))
        return {
            "ticker": ticker, "status": "active", "open_time": iso(start), "close_time": iso(close),
            "floor_strike": strike, "yes_bid_dollars": f"{yb:.4f}", "yes_ask_dollars": f"{ya:.4f}",
            "no_bid_dollars": f"{1 - ya:.4f}", "no_ask_dollars": f"{1 - yb:.4f}",
            "last_price_dollars": f"{mid:.4f}", "volume_fp": str(random.randint(100000, 3000000)),
            "yes_bid_size_fp": str(random.randint(50, 5000)), "yes_ask_size_fp": str(random.randint(50, 5000)),
        }

    def settle(self, ticker, close_ts, strike):
        v = self.avg_before(close_ts)
        if v is None:
            return None
        return {"ticker": ticker, "status": "finalized", "result": "yes" if v >= strike else "no",
                "expiration_value": f"{v:.{self.spec['decimals']}f}", "floor_strike": strike}


# ---------------------------------------------------------------------------
# Core engine
# ---------------------------------------------------------------------------

def asset_data_dir(asset, demo=False):
    """BTC keeps the original layout (data/), other coins get data/eth, data/sol; demo runs live under data/demo."""
    base = os.path.join(DATA_DIR, "demo") if demo else DATA_DIR
    return base if asset == "BTC" else os.path.join(base, asset.lower())


class Engine:
    def __init__(self, asset="BTC", hub=None, demo=False, demo_window=WINDOW_SEC):
        self.asset, self.hub = asset, hub
        self.spec = ASSETS[asset]
        self.series = self.spec["series"]
        # demo data lives in its own folder so it never pollutes live calibration or paper results
        self.data_dir = asset_data_dir(asset, demo)
        os.makedirs(self.data_dir, exist_ok=True)
        self.config_path = os.path.join(self.data_dir, "config.json")
        self.paper_path = os.path.join(self.data_dir, "paper_trades.json")
        self.windows_path = os.path.join(self.data_dir, "windows.json")
        self.demo = demo
        self.window_sec = demo_window if demo else WINDOW_SEC
        self.world = DemoWorld(self.window_sec, asset, self.spec, hub) if demo else None
        cfg = dict(DEFAULT_CONFIG)
        cfg.update({k: v for k, v in load_json(self.config_path, {}).items() if k in DEFAULT_CONFIG})
        self.cfg = cfg
        self.feeds = {n: Feed(n) for n in ["Coinbase", "Kraken", "Bitstamp", "Gemini", "Crypto.com"]}
        self.flow = Flow()
        self.perp = Perp(self.spec["perp"])
        self._sigma_cache = (None, (None, 0))
        self.samples = deque(maxlen=4 * 3600)      # (sec, raw composite, n_feeds, {name: mid})
        self.sample_idx = {}                       # sec -> raw composite
        self.market = None                         # current Kalshi market dict (parsed)
        self.market_err = ""
        self.market_ts = 0.0
        self.kalshi_base = KALSHI_BASES[0]
        self.window_series = {}                    # ticker -> dict of lists
        self.windows = load_json(self.windows_path, [])  # settled-window history
        self.pending = {}                          # ticker -> {close_ts, strike, ...}
        self.paper = load_json(self.paper_path, {"positions": [], "closed": []})
        self.model = {}
        self.signal = {}
        peers = [a for a in (hub.assets if hub else []) if a != asset] or [a for a in ASSETS if a != asset]
        self.council = Council(os.path.join(self.data_dir, "agents_state.json"), self.window_sec, peers=peers)
        self.clients = set()
        self.session = None
        self._csv_day = None
        self._csv_file = None
        self._csv_writer = None

    # ----- composite / calibration -------------------------------------------------

    def composite(self):
        mids = {n: f.mid for n, f in self.feeds.items() if f.fresh()}
        if not mids:
            return None, {}, 0
        med = statistics.median(mids.values())
        used = {n: m for n, m in mids.items() if abs(m - med) / med <= OUTLIER_PCT}
        if not used:
            return None, {}, 0
        vals = list(used.values())
        val = statistics.median(vals) if self.cfg["composite"] == "median" else sum(vals) / len(vals)
        return val, used, len(used)

    def calibration(self):
        """Median gap between Kalshi's own 60s averages and ours (last 24 points)."""
        diffs = []
        for w in self.windows[-24:]:
            for k_kal, k_our in (("strike", "our_open_avg"), ("settle_value", "our_close_avg")):
                a, b = w.get(k_kal), w.get(k_our)
                if a is not None and b is not None:
                    diffs.append(a - b)
        if not diffs:
            return {"offset": 0.0, "n": 0, "mae": None}
        off = statistics.median(diffs)
        mae = sum(abs(d - off) for d in diffs) / len(diffs)
        return {"offset": off, "n": len(diffs), "mae": mae}

    def offset(self):
        return self.calibration()["offset"] if self.cfg["use_calibration"] else 0.0

    def avg_raw(self, end_ts):
        vals = [self.sample_idx[s] for s in range(int(end_ts) - SETTLE_SAMPLES, int(end_ts)) if s in self.sample_idx]
        if len(vals) < SETTLE_SAMPLES * 0.8:
            return None
        return sum(vals) / len(vals)

    def sigma(self, look=None):
        """Realized $/second volatility from 1-second composite changes (cached per second)."""
        if look is None:
            key = int(now())
            if self._sigma_cache[0] == key:
                return self._sigma_cache[1]
            res = self._sigma(int(self.cfg["vol_lookback_sec"]))
            self._sigma_cache = (key, res)
            return res
        return self._sigma(look)

    def _sigma(self, look):
        look = int(look)
        cutoff = int(now()) - look
        diffs, nxt = [], None
        for s, v, _, _ in reversed(self.samples):      # newest first; stop once past the lookback
            if nxt and nxt[0] - s == 1:
                diffs.append(nxt[1] - v)
            if s < cutoff:
                break
            nxt = (s, v)
        if len(diffs) < min(30, look // 2):
            return None, len(diffs)
        rms = math.sqrt(sum(d * d for d in diffs) / len(diffs))
        floor = 0.25 * self.spec["demo_price"] / 80000.0 if self.demo else 0.25 * (self.samples[-1][1] / 80000.0)
        return max(rms, floor), len(diffs)

    def norm_returns(self):
        """Recent moves in units of their own typical size (for the cross-asset agent)."""
        sig, _ = self.sigma()
        if not sig or not self.samples:
            return None
        last_sec, last = self.samples[-1][0], self.samples[-1][1]
        out = {}
        for h in (15, 60):
            past = self.sample_idx.get(last_sec - h)
            out[h] = (last - past) / (sig * math.sqrt(h)) if past is not None else None
        return out

    def tech(self):
        """RSI(14), Bollinger %B(20) and MACD(12,26,9) histogram on 1-minute closes of the proxy."""
        if not self.samples:
            return None
        sig, _ = self.sigma()
        last_sec = self.samples[-1][0]
        closes = []
        for k in range(40, -1, -1):
            v = None
            for back in range(0, 5):
                v = self.sample_idx.get(last_sec - 60 * k - back)
                if v is not None:
                    break
            if v is not None:
                closes.append(v)
        if len(closes) < 36 or not sig:
            return None
        d = [b - a for a, b in zip(closes[-15:-1], closes[-14:])]
        gain, loss = sum(x for x in d if x > 0), -sum(x for x in d if x < 0)
        rsi = 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)
        w = closes[-20:]
        mu = sum(w) / len(w)
        sd = math.sqrt(sum((x - mu) ** 2 for x in w) / len(w))
        pb = (closes[-1] - (mu - 2 * sd)) / (4 * sd) if sd > 0 else 0.5

        def ema(vals, n):
            k, e, out = 2 / (n + 1), vals[0], []
            for v in vals:
                e = v * k + e * (1 - k)
                out.append(e)
            return out
        macd = [a - b for a, b in zip(ema(closes, 12), ema(closes, 26))]
        hist = macd[-1] - ema(macd[-12:], 9)[-1]
        return {"rsi": rsi, "pb": pb, "macd": hist / (sig * math.sqrt(60))}

    def extra_ctx(self, ctx):
        """Extra information for the newer agents: order flow, futures, other coins, events, chart indicators."""
        out = {"flow": None, "perp": None, "cross": {}, "regime": None, "tech": None, "high_event": None}
        sd = ctx.get("sd") or 0.0
        if any(v is not None for v in self.flow.features().values()):
            out["flow"] = self.flow.features()
        if self.perp.fresh() and sd > 0 and self.samples:
            last_sec, spot = self.samples[-1][0], self.samples[-1][1]

            def lead(h):
                p0, s0 = self.perp.at(last_sec - h), self.sample_idx.get(last_sec - h)
                if p0 is None or s0 is None:
                    return None
                return ((self.perp.mid - p0[1]) - (spot - s0)) / sd
            old = self.perp.at(last_sec - 60)
            out["perp"] = {"premium": self.perp.premium, "lead15": lead(15), "lead60": lead(60), "funding": self.perp.funding,
                           "dprem60": (self.perp.premium - old[2]) if old and old[2] is not None and self.perp.premium is not None else None}
        if self.hub:
            out["cross"] = self.hub.cross(self.asset, ctx["secs_left"])
        sig, _ = self.sigma()
        s60, _ = self.sigma(60)
        t = now()
        lt = to_local(t, "ET")
        mins = lt.hour * 60 + lt.minute
        close = self.market["close_ts"] if self.market else t
        near = self.hub.events.between(t - 900, close + 900, 1) if self.hub else []
        out["regime"] = {"near_event": bool(near), "vol_ratio": (s60 / sig - 1) if (s60 and sig) else 0.0,
                         "us_open": lt.weekday() < 5 and 570 <= mins < 630, "weekend": lt.weekday() >= 5}
        if self.hub:
            buf = float(self.cfg["event_buffer_min"]) * 60
            high = self.hub.events.between(t - buf, close + buf, 2)
            if high:
                out["high_event"] = f"{high[0][1]} at {to_local(high[0][0], 'ET'):%H:%M} ET"
        out["tech"] = self.tech()
        return out

    # ----- fair value model ------------------------------------------------------------

    def compute_model(self):
        m = self.market
        if not m or not self.samples:
            self.model = {}
            return
        off = self.offset()
        t = now()
        close = m["close_ts"]
        settle_start = close - SETTLE_SAMPLES
        last_sec, last_raw = self.samples[-1][0], self.samples[-1][1]
        S = last_raw + off
        K = m["strike"]
        sig, nsig = self.sigma()
        sig_eff = sig * self.cfg["vol_mult"] if sig else None

        # settlement samples are seconds close-60 ... close-1
        known, future = [], []
        for s in range(int(settle_start), int(close)):
            if s <= last_sec:
                v = self.sample_idx.get(s)
                if v is None:  # gap: carry nearest earlier sample
                    for back in range(1, 6):
                        v = self.sample_idx.get(s - back)
                        if v is not None:
                            break
                known.append((v if v is not None else last_raw) + off)
            else:
                future.append(s - last_sec)       # seconds ahead of the latest print
        k, mfut = len(known), len(future)
        mean = (sum(known) + mfut * S) / SETTLE_SAMPLES
        # Var(sum of Brownian prints at times t_i) = sigma^2 * sum_i t_i * (2(m-i)+1)
        var_sum = sum(ti * (2 * (mfut - i) + 1) for i, ti in enumerate(sorted(future), start=1)) if mfut else 0
        sd = (sig_eff * math.sqrt(var_sum) / SETTLE_SAMPLES) if (sig_eff and var_sum > 0) else 0.0

        if K is None or sig_eff is None:
            p = None            # no strike yet, or not enough history to estimate volatility
        elif sd > 1e-9:
            p = norm_cdf((mean - K) / sd)
        else:
            p = 1.0 if mean >= K else 0.0
        run_avg = (sum(known) / k) if k else None
        # "what's locked in": the move needed in the remaining prints to flip the result
        need = None
        if K is not None and mfut:
            need = (K * SETTLE_SAMPLES - sum(known)) / mfut - S
        self.model = {
            "spot": S, "spot_raw": last_raw, "offset": off, "strike": K,
            "dist": (S - K) if K is not None else None,
            "settle_mean": mean, "settle_sd": sd, "sigma": sig, "sigma_eff": sig_eff, "sigma_n": nsig,
            "p_up": p, "samples_in": k, "samples_left": mfut, "run_avg": run_avg,
            "move_needed": need, "secs_left": close - t, "in_settle": t >= settle_start,
        }

    # ----- signals ---------------------------------------------------------------------

    def compute_signal(self):
        m, md, cfg = self.market, self.model, self.cfg
        C = int(cfg["contracts"])
        sig = {"action": "WAIT", "side": None, "reasons": [], "up": {}, "down": {}, "exits": []}
        if not m or md.get("p_up") is None:
            sig["reasons"].append("Waiting for market and price data")
            self.signal = sig
            return
        p = md["p_up"]
        ya, yb, na, nb = m["yes_ask"], m["yes_bid"], m["no_ask"], m["no_bid"]

        def side(prob, ask, bid):
            if ask is None:
                return {"prob": prob, "ask": None}
            fee = fee_per_contract(ask, C)
            ev = prob - ask - fee
            limit = math.floor((prob - cfg["min_edge"]) * 100) / 100   # maker fee usually 0
            return {"prob": prob, "ask": ask, "bid": bid, "fee": fee, "ev": ev,
                    "limit": limit if 0.01 <= limit < ask else None,
                    "fee_pct": fee / ask if ask else None}

        up, down = side(p, ya, yb), side(1 - p, na, nb)
        sig["up"], sig["down"] = up, down

        reasons = []
        secs = md["secs_left"]
        nfeeds = self.samples[-1][2] if self.samples else 0
        spread = (ya - yb) if (ya is not None and yb is not None) else None
        no_entry = cfg["no_entry_last_sec"] * self.window_sec / WINDOW_SEC
        if secs <= no_entry:
            reasons.append(f"Inside final {int(no_entry)}s: bots are fastest here")
        if nfeeds < cfg["min_feeds"]:
            reasons.append(f"Only {nfeeds} exchange feeds live (need {cfg['min_feeds']})")
        if spread is not None and spread > cfg["max_spread"] + 1e-9:
            reasons.append(f"Spread {spread * 100:.0f}¢ wider than {cfg['max_spread'] * 100:.0f}¢")
        if md.get("sigma") is None:
            reasons.append("Collecting volatility history (needs ~1 min)")
        best = max((("UP", up), ("DOWN", down)), key=lambda kv: kv[1].get("ev", -9))
        bev = best[1].get("ev")
        if bev is None:
            reasons.append("No ask on the book")
        elif bev < cfg["min_edge"]:
            reasons.append(f"Best edge {bev * 100:+.1f}¢ below {cfg['min_edge'] * 100:.0f}¢ threshold")
        if not reasons:
            sig["action"] = "BUY " + best[0]
            sig["side"] = best[0].lower()
            px = best[1]["ask"]
            if 0.4 <= px <= 0.6:
                sig["note"] = "Near 50¢: fee peak. A resting limit saves the taker fee."
        sig["reasons"] = reasons

        # exit advice for open paper positions in this market
        for pos in self.paper["positions"]:
            if pos["ticker"] != m["ticker"]:
                continue
            prob = p if pos["side"] == "up" else 1 - p
            bid = yb if pos["side"] == "up" else nb
            if bid is None:
                adv = "HOLD (no bid)"
                val = None
            else:
                val = bid - fee_per_contract(bid, pos["contracts"])
                if val - prob >= cfg["exit_margin"]:
                    adv = "SELL: bid pays more than fair value"
                elif prob < pos["entry_price"] - 0.25 and secs > no_entry:
                    adv = "REVIEW: fair value well below entry"
                else:
                    adv = "HOLD"
            tp = math.ceil((prob + cfg["exit_margin"]) * 100) / 100
            sig["exits"].append({"id": pos["id"], "advice": adv, "fair": prob, "sell_value": val,
                                 "take_profit_limit": tp if tp < 1 else None})
        self.signal = sig

    # ----- Kalshi ----------------------------------------------------------------------

    def parse_market(self, raw):
        close_ts = parse_ts(raw.get("close_time"))
        if close_ts is None:
            return None

        def px(key_dollars, key_cents):
            v = fnum(raw.get(key_dollars))
            if v is None and raw.get(key_cents) is not None:
                v = fnum(raw.get(key_cents))
                v = v / 100 if v is not None else None
            if v is not None and (v <= 0 or v >= 1):
                return None        # 0 or 1 means "no quote"
            return v

        yb, ya = px("yes_bid_dollars", "yes_bid"), px("yes_ask_dollars", "yes_ask")
        nb, na = px("no_bid_dollars", "no_bid"), px("no_ask_dollars", "no_ask")
        if na is None and yb is not None:
            na = round(1 - yb, 4)
        if nb is None and ya is not None:
            nb = round(1 - ya, 4)
        return {
            "ticker": raw.get("ticker"), "status": raw.get("status"),
            "open_ts": close_ts - self.window_sec, "close_ts": close_ts,
            "strike": fnum(raw.get("floor_strike")),
            "yes_bid": yb, "yes_ask": ya, "no_bid": nb, "no_ask": na,
            "last": px("last_price_dollars", "last_price"),
            "volume": fnum(raw.get("volume_fp") or raw.get("volume")),
            "yes_bid_size": fnum(raw.get("yes_bid_size_fp")), "yes_ask_size": fnum(raw.get("yes_ask_size_fp")),
        }

    async def kalshi_get(self, path, params=None):
        last_err = None
        bases = [self.kalshi_base] + [b for b in KALSHI_BASES if b != self.kalshi_base]
        for base in bases:
            try:
                async with self.session.get(base + path, params=params,
                                            timeout=aiohttp.ClientTimeout(total=6)) as r:
                    if r.status == 429:
                        raise RuntimeError("rate limited (429)")
                    if r.status != 200:
                        raise RuntimeError(f"HTTP {r.status}")
                    self.kalshi_base = base
                    return await r.json()
            except Exception as e:
                last_err = e
        raise RuntimeError(str(last_err))

    async def kalshi_loop(self, delay=0.0):
        await asyncio.sleep(delay)             # stagger the coins so requests don't bunch up
        while True:
            try:
                if self.demo:
                    raw_list = [self.world.market(self.model.get("p_up"))]
                else:
                    data = await self.kalshi_get("/markets", {"series_ticker": self.series, "status": "open", "limit": 20})
                    raw_list = data.get("markets", [])
                t = now()
                parsed = [p for p in (self.parse_market(r) for r in raw_list) if p and p["close_ts"] > t]
                parsed.sort(key=lambda p: p["close_ts"])
                new = parsed[0] if parsed else None
                old = self.market
                if old and (not new or new["ticker"] != old["ticker"]):
                    self.queue_settlement(old)
                if new and new["strike"] is None:
                    # strike not published yet: fall back to our own 60s open average
                    a = self.avg_raw(new["open_ts"])
                    new["strike"] = (a + self.offset()) if a is not None else None
                    new["strike_is_proxy"] = True
                self.market = new
                self.market_err = "" if new else f"No open {self.series} market right now"
                self.market_ts = t
            except Exception as e:
                self.market_err = f"Kalshi: {e}"[:200]
            await asyncio.sleep(max(0.25, self.cfg["poll_ms"] / 1000))

    def queue_settlement(self, mk):
        if mk["ticker"] in self.pending:
            return
        self.pending[mk["ticker"]] = {
            "ticker": mk["ticker"], "open_ts": mk["open_ts"], "close_ts": mk["close_ts"],
            "strike": None if mk.get("strike_is_proxy") else mk["strike"],
            "model_p_final": self.model.get("p_up"), "tries": 0,
        }

    async def settle_loop(self):
        while True:
            await asyncio.sleep(5)
            for tk, w in list(self.pending.items()):
                if now() < w["close_ts"] + 2:
                    continue
                w["tries"] += 1
                try:
                    if self.demo:
                        m = self.world.settle(tk, w["close_ts"], w["strike"])
                    else:
                        m = (await self.kalshi_get(f"/markets/{tk}")).get("market", {})
                except Exception:
                    m = None
                result = (m or {}).get("result") or ""
                try:
                    if result in ("yes", "no"):
                        self.record_window(w, m)
                        del self.pending[tk]
                    elif w["tries"] > 120:    # ~10 minutes: give up waiting, record what we have
                        self.record_window(w, m or {})
                        del self.pending[tk]
                except Exception as e:        # e.g. disk full: report it and keep the loop alive
                    self.market_err = f"Could not record settled window {tk}: {e}"[:200]
                    self.pending.pop(tk, None)

    def record_window(self, w, m):
        strike = w["strike"] if w["strike"] is not None else fnum(m.get("floor_strike"))
        sv = fnum(m.get("expiration_value"))
        oo, oc = self.avg_raw(w["open_ts"]), self.avg_raw(w["close_ts"])
        result = m.get("result") or None
        rec = {
            "ticker": w["ticker"], "open": iso(w["open_ts"]), "close": iso(w["close_ts"]),
            "strike": strike, "settle_value": sv, "result": result,
            "our_open_avg": round(oo, self.spec["decimals"]) if oo is not None else None,
            "our_close_avg": round(oc, self.spec["decimals"]) if oc is not None else None,
            "model_p_final": w.get("model_p_final"),
        }
        if oc is not None and strike is not None:
            rec["our_result"] = "yes" if (oc + self.offset()) >= strike else "no"
        self.windows.append(rec)
        self.windows = self.windows[-500:]
        save_json(self.windows_path, self.windows)
        self.settle_paper(w["ticker"], result)
        lt = to_local(w["open_ts"], "ET")
        try:
            self.council.settle(w["ticker"], result, w["close_ts"], lt.hour, lt.minute, self.cfg)
        except Exception as e:          # learning must never take the monitor down
            print(f"agent learning error: {e}")

    # ----- paper trading ---------------------------------------------------------------

    def paper_buy(self, side, contracts, source="you", fair=None):
        m, md = self.market, self.model
        if not m:
            raise ValueError("No live market")
        price = m["yes_ask"] if side == "up" else m["no_ask"]
        if price is None:
            raise ValueError("No ask on that side")
        fee = taker_fee_total(price, contracts)
        if fair is None:
            prob = md.get("p_up")
            fair = prob if side == "up" else (1 - prob if prob is not None else None)
        pos = {
            "id": uuid.uuid4().hex[:8], "ticker": m["ticker"], "side": side, "contracts": contracts, "source": source,
            "entry_price": price, "entry_fee": fee, "entry_ts": iso(now()),
            "entry_fair": fair, "entry_ev": (fair - price - fee / contracts) if fair is not None else None,
            "secs_left_at_entry": round(md.get("secs_left", 0)), "strike": m["strike"],
        }
        self.paper["positions"].append(pos)
        save_json(self.paper_path, self.paper)
        return pos

    def paper_sell(self, pid):
        m = self.market
        pos = next((p for p in self.paper["positions"] if p["id"] == pid), None)
        if not pos:
            raise ValueError("Position not found")
        if not m or m["ticker"] != pos["ticker"]:
            raise ValueError("That window has closed; it will settle automatically")
        price = m["yes_bid"] if pos["side"] == "up" else m["no_bid"]
        if price is None:
            raise ValueError("No bid to sell into")
        fee = taker_fee_total(price, pos["contracts"])
        self._close(pos, "sold", price, fee)
        return pos

    def settle_paper(self, ticker, result):
        for pos in [p for p in self.paper["positions"] if p["ticker"] == ticker]:
            if result in ("yes", "no"):
                won = (result == "yes") == (pos["side"] == "up")
                self._close(pos, "settled", 1.0 if won else 0.0, 0.0, result)
            else:
                self._close(pos, "void", pos["entry_price"], 0.0, None)

    def _close(self, pos, how, price, fee, result=None):
        self.paper["positions"] = [p for p in self.paper["positions"] if p["id"] != pos["id"]]
        cost = pos["entry_price"] * pos["contracts"] + pos["entry_fee"]
        proceeds = price * pos["contracts"] - fee
        pos.update({"exit": how, "exit_price": price, "exit_fee": fee, "exit_ts": iso(now()),
                    "result": result, "pnl": round(proceeds - cost, 4)})
        self.paper["closed"].append(pos)
        self.paper["closed"] = self.paper["closed"][-1000:]
        save_json(self.paper_path, self.paper)

    def paper_summary(self):
        closed = self.paper["closed"]
        pnl = sum(p["pnl"] for p in closed)
        wins = sum(1 for p in closed if p["pnl"] > 0)
        fees = sum(p["entry_fee"] + p.get("exit_fee", 0) for p in closed)
        evs = [p["entry_ev"] * p["contracts"] for p in closed if p.get("entry_ev") is not None]
        open_mtm = 0.0
        m = self.market
        for p in self.paper["positions"]:
            if m and m["ticker"] == p["ticker"]:
                bid = m["yes_bid"] if p["side"] == "up" else m["no_bid"]
                if bid is not None:
                    open_mtm += bid * p["contracts"] - p["entry_price"] * p["contracts"] - p["entry_fee"]
        return {"trades": len(closed), "pnl": pnl, "wins": wins,
                "win_rate": wins / len(closed) if closed else None, "fees": fees,
                "expected": sum(evs) if evs else None, "open_mtm": open_mtm,
                "open": self.paper["positions"], "recent": closed[-25:][::-1]}

    # ----- agent team ----------------------------------------------------------------------

    def team_book(self):
        m = self.market
        tk = m["ticker"] if m else None
        team_open = [p for p in self.paper["positions"] if p.get("source") == "team"]
        team_closed = [p for p in self.paper["closed"] if p.get("source") == "team"]
        today = dt.datetime.now(dt.timezone.utc).date().isoformat()
        realized = sum(p["pnl"] for p in team_closed)
        return {
            "open": [p for p in team_open if p["ticker"] == tk],
            "entries_this_window": sum(1 for p in team_open + team_closed if p["ticker"] == tk),
            "today_pnl": sum(p["pnl"] for p in team_closed if p.get("exit_ts", "")[:10] == today),
            "equity": self.cfg["agent_bankroll"] + realized,
            "realized": realized, "trades": len(team_closed),
            "wins": sum(1 for p in team_closed if p["pnl"] > 0),
            "fees": sum(p["entry_fee"] + p.get("exit_fee", 0) for p in team_closed),
            "open_all": team_open, "recent": team_closed[-15:][::-1],
        }

    def run_team(self):
        try:
            ctx = build_ctx(self, to_local)
            f = self.council.step(ctx, self.cfg)
            act = self.council.decide(ctx, f, self.cfg, self.team_book())
            if act["action"] == "BUY":
                pos = self.paper_buy(act["side"], act["contracts"], source="team", fair=act["prob"])
                self.notify_trade(f"{self.asset} team paper BUY {act['side'].upper()} ×{act['contracts']} at "
                                  f"{pos['entry_price'] * 100:.0f}¢ (fair {act['prob'] * 100:.0f}¢, edge {act['ev'] * 100:+.1f}¢) · {pos['ticker']}")
            elif act["action"] == "SELL":
                pos = self.paper_sell(act["id"])
                self.notify_trade(f"{self.asset} team paper SELL {pos['side'].upper()} ×{pos['contracts']} at "
                                  f"{pos['exit_price'] * 100:.0f}¢ · P&L ${pos['pnl']:+.2f}")
        except Exception as e:          # the team must never take the monitor down
            self.council.decision = {"action": "WAIT", "reasons": [f"Agent error: {e}"]}

    def notify_trade(self, text):
        if self.hub and self.hub.alerter.alert_trades:
            try:
                asyncio.get_running_loop().create_task(self.hub.alerter.send(text))
            except RuntimeError:
                pass

    def health(self):
        """What the watchdog checks: live exchange feeds, fresh Kalshi data."""
        fresh = sum(1 for f in self.feeds.values() if f.fresh())
        age = now() - self.market_ts if self.market_ts else None
        return {"feeds": fresh, "kalshi_ok": bool(self.market) and not self.market_err and age is not None and age < 30,
                "kalshi_err": self.market_err, "data_ok": bool(self.samples) and now() - self.samples[-1][0] < 30}

    # ----- sampling, logging, broadcast -----------------------------------------------

    async def sample_loop(self):
        while True:
            await asyncio.sleep(1 - (now() % 1) + 0.002)   # align to whole seconds
            sec = int(now())
            if self.demo:
                self.world.step(self.feeds, self.flow, self.perp)
            self.perp.sample(sec)
            val, used, n = self.composite()
            if val is not None:
                self.samples.append((sec, val, n, used))
                self.sample_idx[sec] = val
                if len(self.sample_idx) > self.samples.maxlen + 100:
                    cutoff = self.samples[0][0]
                    self.sample_idx = {s: v for s, v in self.sample_idx.items() if s >= cutoff}
            self.compute_model()
            self.compute_signal()
            self.track_series(sec)
            self.run_team()
            self.log_csv(sec)
            await self.broadcast()

    def track_series(self, sec):
        m, md = self.market, self.model
        if not m or not md:
            return
        s = self.window_series.setdefault(m["ticker"], {k: [] for k in
                                          ("t", "spot", "mean", "run", "p", "yb", "ya", "lo", "hi")})
        if s["t"] and s["t"][-1] == sec:
            return
        sd = md.get("settle_sd") or 0
        s["t"].append(sec)
        dec = self.spec["decimals"] + 1
        s["spot"].append(round(md["spot"], dec))
        s["mean"].append(round(md["settle_mean"], dec))
        s["run"].append(round(md["run_avg"], dec) if md.get("run_avg") is not None else None)
        s["lo"].append(round(md["settle_mean"] - sd, dec))
        s["hi"].append(round(md["settle_mean"] + sd, dec))
        s["p"].append(round(md["p_up"] * 100, 2) if md.get("p_up") is not None else None)
        s["yb"].append(round(m["yes_bid"] * 100, 1) if m["yes_bid"] is not None else None)
        s["ya"].append(round(m["yes_ask"] * 100, 1) if m["yes_ask"] is not None else None)
        for tk in [k for k in self.window_series if k != m["ticker"]][:-1]:
            del self.window_series[tk]       # keep current + previous window only

    def log_csv(self, sec):
        m, md, sg = self.market, self.model, self.signal
        if not md or not self.samples or self.samples[-1][0] != sec:
            return
        day = dt.datetime.fromtimestamp(sec, dt.timezone.utc).strftime("%Y%m%d")
        if day != self._csv_day:
            if self._csv_file:
                self._csv_file.close()
            self._rotate_logs(day)
            path = os.path.join(self.data_dir, f"ticks_{day}.csv")
            new = not os.path.exists(path)
            self._csv_file = open(path, "a", newline="", encoding="utf-8")
            self._csv_writer = csv.writer(self._csv_file)
            if new:
                self._csv_writer.writerow(
                    ["ts_utc", "ticker", "secs_left", "proxy_raw", "proxy_cal", "n_feeds"]
                    + [f"mid_{n}" for n in self.feeds]
                    + ["strike", "settle_mean", "settle_sd", "sigma", "p_up",
                       "yes_bid", "yes_ask", "no_bid", "no_ask", "ev_up", "ev_down", "signal",
                       "team_p", "tfi60", "book_imb", "perp_mid", "perp_premium"])
            self._csv_day = day
        used = self.samples[-1][3]
        r = lambda x, n=4: round(x, n) if isinstance(x, (int, float)) else ""
        self._csv_writer.writerow(
            [iso(sec), m["ticker"] if m else "", r(md.get("secs_left"), 1), r(md.get("spot_raw"), 2),
             r(md.get("spot"), 2), self.samples[-1][2]]
            + [r(used.get(n), 2) for n in self.feeds]
            + [r(md.get("strike"), 2), r(md.get("settle_mean"), 2), r(md.get("settle_sd"), 2),
               r(md.get("sigma"), 3), r(md.get("p_up")),
               r(m.get("yes_bid")) if m else "", r(m.get("yes_ask")) if m else "",
               r(m.get("no_bid")) if m else "", r(m.get("no_ask")) if m else "",
               r(sg.get("up", {}).get("ev")), r(sg.get("down", {}).get("ev")), sg.get("action", ""),
               r((self.council.live or {}).get("p_team")), r(self.flow.tfi(60)), r(self.flow.book_imb()),
               r(self.perp.mid if self.perp.fresh() else None, 4), r(self.perp.premium if self.perp.fresh() else None)])
        if sec % 10 == 0:
            self._csv_file.flush()

    def _rotate_logs(self, today):
        """Compress finished days (about 85% smaller) and delete days older than keep_days, in the background."""
        def work():
            for f in glob.glob(os.path.join(self.data_dir, "ticks_*.csv")):
                if os.path.basename(f) != f"ticks_{today}.csv":
                    try:
                        with open(f, "rb") as src, gzip.open(f + ".gz", "wb") as dst:
                            shutil.copyfileobj(src, dst)
                        os.remove(f)
                    except OSError:
                        pass
            cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=int(self.cfg["keep_days"]))).strftime("%Y%m%d")
            for f in glob.glob(os.path.join(self.data_dir, "ticks_*.csv.gz")):
                d = os.path.basename(f)[6:14]
                if d.isdigit() and d < cutoff:
                    try:
                        os.remove(f)
                    except OSError:
                        pass
        threading.Thread(target=work, daemon=True).start()

    def snapshot(self):
        comp = self.samples[-1][1] if self.samples else None
        used = self.samples[-1][3] if self.samples else {}
        m = self.market
        series = self.window_series.get(m["ticker"]) if m else None
        dec = self.spec["decimals"]
        hour = [(s, round(v + self.offset(), dec)) for s, v, _, _ in list(self.samples)[-3600:][::5]]
        return {
            "type": "tick", "server_ts": now(), "demo": self.demo, "window_sec": self.window_sec,
            "asset": self.asset, "series": self.series, "index_name": self.spec["index"], "decimals": dec,
            "assets": list(self.hub.engines) if self.hub else [self.asset],
            "overview": self.hub.overview() if self.hub else [],
            "alerts": self.hub.alerter.status() if self.hub else None,
            "flow": self.flow.stats(), "perp": self.perp.to_dict(comp),
            "config": self.cfg, "feeds": [f.to_dict(comp, n in used) for n, f in self.feeds.items()],
            "market": m, "market_err": self.market_err, "market_age": now() - self.market_ts if self.market_ts else None,
            "model": self.model, "signal": self.signal, "calibration": self.calibration(),
            "series": series, "hour": hour, "windows": self.windows[-30:][::-1],
            "paper": self.paper_summary(), "pending": len(self.pending),
            "team": dict(self.council.snapshot(self.cfg), book={k: v for k, v in self.team_book().items() if k != "open"}),
        }

    async def broadcast(self):
        if not self.clients:
            return
        msg = json.dumps(self.snapshot(), default=lambda o: None)
        for ws in list(self.clients):
            try:
                await ws.send_str(msg)
            except Exception:
                self.clients.discard(ws)

    # ----- lifecycle -------------------------------------------------------------------

    def tasks(self, session, delay=0.0):
        self.session = session
        tasks = [self.sample_loop(), self.kalshi_loop(delay), self.settle_loop()]
        if not self.demo:
            f, sp = self.feeds, self.spec
            tasks += [coinbase_task(session, f["Coinbase"], sp["coinbase"], self.flow),
                      kraken_task(session, f["Kraken"], sp["kraken"], self.flow),
                      bitstamp_task(session, f["Bitstamp"], sp["bitstamp"]),
                      gemini_task(session, f["Gemini"], sp["gemini"]),
                      cryptocom_task(session, f["Crypto.com"], sp["cryptocom"]),
                      kraken_futures_task(session, self.perp)]
        return tasks

    def close(self):
        if self._csv_file:
            self._csv_file.close()


class Hub:
    """Runs one engine per coin and shares what they need from each other (cross-asset moves, events)."""

    def __init__(self, assets, demo=False, demo_window=WINDOW_SEC):
        self.assets = [a for a in assets if a in ASSETS]
        self.demo = demo
        self.events = Events(os.path.join(HERE, "events.json"), to_local)
        self._common = {}
        self.engines = {a: Engine(a, self, demo, demo_window) for a in self.assets}
        self.session = None
        self.alerter = Alerter()
        self.watch = {"down_since": {}, "alerted": set(), "proven": {}, "log_seen": {}, "loss_day": {},
                      "summary_day": None, "started": now()}

    def common(self, sec):
        """Demo only: a market-wide shock per second, so coins move together (BTC first)."""
        if sec not in self._common:
            self._common[sec] = random.gauss(0, 1)
            for old in [k for k in self._common if k < sec - 120]:
                del self._common[old]
        return self._common[sec]

    def cross(self, asset, secs_left):
        me = self.engines[asset].norm_returns()
        out = {}
        if not me:
            return out
        for a, e in self.engines.items():
            if a == asset:
                continue
            r = e.norm_returns()
            if not r:
                continue
            out[a] = {h: (r[h] - me[h]) * math.sqrt(h / (max(secs_left, 1) + 20))
                      for h in (15, 60) if r.get(h) is not None and me.get(h) is not None}
        return out

    def overview(self):
        rows = []
        for a, e in self.engines.items():
            live = e.council.live or {}
            ok, why = e.council.proven(e.cfg)
            closed = [p for p in e.paper["closed"] if p.get("source") == "team"]
            rows.append({"asset": a, "ticker": (e.market or {}).get("ticker"), "secs_left": e.model.get("secs_left"),
                         "p_team": live.get("p_team"), "mid": live.get("mid"), "proven": ok,
                         "learned": e.council.windows_learned, "need": e.cfg["min_learned_windows"],
                         "active": sum(1 for v in e.council.status.values() if v == "active"),
                         "pnl": sum(p["pnl"] for p in closed), "trades": len(closed),
                         "action": (e.council.decision or {}).get("action", "WAIT")})
        return rows

    async def start(self, app):
        self.events.reload()
        self.session = aiohttp.ClientSession(headers={"User-Agent": "crypto15-monitor/2.0"})
        coros = []
        for i, e in enumerate(self.engines.values()):
            coros += e.tasks(self.session, delay=i * 0.33)
        coros.append(self.events_loop())
        self.alerter.session = self.session
        coros.append(self.watch_loop())
        app["tasks"] = [asyncio.create_task(c) for c in coros]

    async def events_loop(self):
        while True:
            await asyncio.sleep(60)
            self.events.reload()               # pick up edits to events.json without a restart

    async def watch_loop(self):
        """Watchdog: tells you when something needs attention, sends the daily summary, pings the heartbeat."""
        await asyncio.sleep(5)
        await self.alerter.send(f"Monitor started{' (DEMO)' if self.demo else ''}: {', '.join(self.assets)}. "
                                f"Alerts via {', '.join(self.alerter.channels()) or 'dashboard only'}.", key="start", cooldown=0)
        for a, e in self.engines.items():          # don't announce states that were already true at start
            self.watch["proven"][a] = e.council.proven(e.cfg)[0]
            self.watch["log_seen"][a] = max([l.get("t", 0) for l in e.council.log] + [0])
        tick = 0
        while True:
            await asyncio.sleep(30)
            tick += 1
            try:
                await self.check_once()
                if tick % 2 == 0:                 # heartbeat about once a minute, only while data is flowing
                    await self.alerter.ping(ok=any(e.health()["data_ok"] for e in self.engines.values()))
            except Exception as ex:               # the watchdog must never stop
                self.alerter.errors.appendleft({"t": now(), "channel": "watchdog", "error": str(ex)[:200]})

    async def check_once(self):
        w, t = self.watch, now()
        send = self.alerter.send

        async def condition(key, bad, minutes, msg_bad, msg_ok):
            if bad:
                w["down_since"].setdefault(key, t)
                if t - w["down_since"][key] >= minutes * 60 and key not in w["alerted"]:
                    w["alerted"].add(key)
                    await send(msg_bad, key=key, cooldown=0)
            else:
                if key in w["alerted"]:
                    mins = (t - w["down_since"].get(key, t)) / 60
                    await send(f"{msg_ok} (after {mins:.0f} min)", key=key + ":ok", cooldown=0)
                    w["alerted"].discard(key)
                w["down_since"].pop(key, None)

        for a, e in self.engines.items():
            h = e.health()
            await condition(f"{a}:feeds", h["feeds"] < e.cfg["min_feeds"], 5,
                            f"⚠ {a}: only {h['feeds']} exchange feeds live for 5+ minutes. The team is holding off.",
                            f"✓ {a}: exchange feeds back ({h['feeds']} live)")
            await condition(f"{a}:kalshi", not h["kalshi_ok"], 5,
                            f"⚠ {a}: no fresh Kalshi data for 5+ minutes. {h['kalshi_err'] or ''}".strip(),
                            f"✓ {a}: Kalshi data flowing again")
            ok, why = e.council.proven(e.cfg)
            if ok != w["proven"].get(a):
                if ok:
                    await send(f"✅ {a}: the team now clearly beats Kalshi's prices. Paper trading is switched on.", cooldown=0)
                elif w["proven"].get(a) is not None:
                    await send(f"⏸ {a}: the team no longer clearly beats Kalshi ({why}). Back to watching only.", cooldown=0)
                w["proven"][a] = ok
            seen = w["log_seen"].get(a, 0)
            for l in sorted([l for l in e.council.log if l.get("event") and l.get("t", 0) > seen], key=lambda l: l["t"]):
                await send(f"{'⬆' if l['event'] == 'promoted' else '⬇'} {a}: {l['agent']} {l['event']}: {l['detail']}", cooldown=0)
            w["log_seen"][a] = max([l.get("t", 0) for l in e.council.log] + [seen])
            book = e.team_book()
            day = dt.datetime.now(dt.timezone.utc).date().isoformat()
            if book["today_pnl"] <= -abs(e.cfg["daily_loss_limit"]) and w["loss_day"].get(a) != day:
                w["loss_day"][a] = day
                await send(f"🛑 {a}: daily paper loss limit hit (${book['today_pnl']:.2f}). Team trading paused until 00:00 UTC.",
                           cooldown=0)
        try:
            du = shutil.disk_usage(DATA_DIR)
            if du.free / du.total < 0.10:
                await send(f"⚠ Disk almost full: {du.free / 1e9:.1f} GB free. Lower 'Keep tick logs' or resize the server.",
                           key="disk", cooldown=12 * 3600)
        except OSError:
            pass
        await self.maybe_summary()

    async def maybe_summary(self, force=False):
        st = self.alerter.summary_time
        if not st and not force:
            return
        lt = to_local(now(), "ET")
        try:
            hh, mm = (int(v) for v in (st or "20:00").split(":"))
        except ValueError:
            return
        if not force and (lt.hour * 60 + lt.minute < hh * 60 + mm or self.watch["summary_day"] == lt.date()):
            return
        self.watch["summary_day"] = lt.date()
        await self.alerter.send(self.summary_text(), cooldown=0)

    def summary_text(self):
        lines = [f"📊 Daily summary {to_local(now(), 'ET'):%a %b %d} (paper only)"]
        day = dt.datetime.now(dt.timezone.utc).date().isoformat()
        for a, e in self.engines.items():
            c, book = e.council, e.team_book()
            edge, n = c.team.recent_edge()
            today = [p for p in e.paper["closed"] if p.get("source") == "team" and p.get("exit_ts", "")[:10] == day]
            ok, why = c.proven(e.cfg)
            active = [ag.name for ag in c.agents if c.status[ag.key] == "active"]
            lines.append(
                f"\n{a}: {'TRADING' if ok else 'watching'} · learned {c.windows_learned} windows · "
                f"team vs Kalshi {'%+.4f' % edge if edge is not None else 'n/a'}\n"
                f"  today {len(today)} trades ${sum(p['pnl'] for p in today):+.2f} · all-time {book['trades']} trades "
                f"${book['realized']:+.2f} · bankroll ${book['equity']:.2f}\n"
                f"  active agents: {', '.join(active)}")
        return "\n".join(lines)

    async def stop(self, app):
        for t in app.get("tasks", []):
            t.cancel()
        await asyncio.gather(*app.get("tasks", []), return_exceptions=True)
        if self.session:
            await self.session.close()
        for e in self.engines.values():
            e.close()


# ---------------------------------------------------------------------------
# Web server
# ---------------------------------------------------------------------------

def password_middleware(user, password):
    """Optional built-in login (APP_PASSWORD). On the VPS, Caddy already asks for a password in front of the app."""
    expected = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()

    @web.middleware
    async def mw(request, handler):
        if hmac.compare_digest(request.headers.get("Authorization", ""), expected):
            return await handler(request)
        return web.Response(status=401, text="Login required", headers={"WWW-Authenticate": 'Basic realm="crypto15"'})
    return mw


def build_app(hub: Hub) -> web.Application:
    pw = os.environ.get("APP_PASSWORD", "")
    app = web.Application(middlewares=[password_middleware(os.environ.get("APP_USER", "admin"), pw)] if pw else [])
    first = hub.assets[0]

    def eng(request):
        return hub.engines.get(request.query.get("asset", first).upper(), hub.engines[first])

    async def index(request):
        return web.FileResponse(os.path.join(STATIC_DIR, "index.html"))

    async def history(request):
        e = eng(request)
        path = os.path.join(asset_data_dir(e.asset), "history", "report.html")
        if not os.path.exists(path):
            flag = "" if e.asset == "BTC" else f" --asset {e.asset}"
            return web.Response(content_type="text/html", text=(
                "<body style='background:#0d0d0d;color:#f0efec;font:15px system-ui;padding:40px'>"
                f"<h2>No {e.asset} history downloaded yet</h2><p>In a terminal in this folder run "
                f"<code>python history.py{flag}</code> (or double-click <b>get_history_windows.bat</b>), then reload this page.</p>"
                "<p><a style='color:#3987e5' href='/'>Back</a></p></body>"))
        return web.FileResponse(path, headers={"Cache-Control": "no-store"})

    async def ws_handler(request):
        engine = eng(request)
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        engine.clients.add(ws)
        await ws.send_str(json.dumps(engine.snapshot(), default=lambda o: None))
        try:
            async for _ in ws:
                pass
        finally:
            engine.clients.discard(ws)
        return ws

    async def api_state(request):
        return web.json_response(eng(request).snapshot(), dumps=lambda o: json.dumps(o, default=lambda x: None))

    async def api_config(request):
        engine = eng(request)
        body = await request.json()
        for k, v in body.items():
            if k not in DEFAULT_CONFIG:
                continue
            typ = type(DEFAULT_CONFIG[k])
            try:
                engine.cfg[k] = (v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes")) \
                    if typ is bool else typ(v)
            except (TypeError, ValueError):
                return web.json_response({"error": f"bad value for {k}"}, status=400)
        engine.cfg["contracts"] = max(1, int(engine.cfg["contracts"]))
        save_json(engine.config_path, engine.cfg)
        return web.json_response(engine.cfg)

    async def api_paper(request):
        engine = eng(request)
        body = await request.json()
        try:
            if body.get("action") == "buy":
                side = body.get("side")
                if side not in ("up", "down"):
                    raise ValueError("side must be up or down")
                res = engine.paper_buy(side, int(body.get("contracts") or engine.cfg["contracts"]))
            elif body.get("action") == "sell":
                res = engine.paper_sell(body.get("id"))
            elif body.get("action") == "reset":
                engine.paper = {"positions": [], "closed": []}
                save_json(engine.paper_path, engine.paper)
                res = {}
            else:
                raise ValueError("unknown action")
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response({"ok": True, "result": res})

    async def api_test_alert(request):
        if not hub.alerter.channels():
            return web.json_response({"error": "No alert channel is set up (see alerts.py or the setup script)."}, status=400)
        await hub.alerter.send("🔔 Test alert from your crypto 15-minute monitor. Alerts are working.", cooldown=0)
        await hub.maybe_summary(force=True)
        errs = [e for e in hub.alerter.errors if now() - e["t"] < 30]
        return web.json_response({"ok": not errs, "errors": errs, "channels": hub.alerter.channels()})

    async def api_health(request):
        return web.json_response({a: e.health() for a, e in hub.engines.items()})

    app.router.add_get("/", index)
    app.router.add_post("/api/test-alert", api_test_alert)
    app.router.add_get("/api/health", api_health)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/history", history)
    app.router.add_get("/api/state", api_state)
    app.router.add_post("/api/config", api_config)
    app.router.add_post("/api/paper", api_paper)
    app.router.add_static("/static", STATIC_DIR)
    app.on_startup.append(hub.start)
    app.on_cleanup.append(hub.stop)
    return app


def main():
    ap = argparse.ArgumentParser(description="Kalshi 15-minute crypto monitor (BTC, ETH, SOL)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--assets", default="BTC,ETH,SOL", help="comma-separated coins to run (default BTC,ETH,SOL)")
    ap.add_argument("--demo", action="store_true", help="simulated data, no internet needed")
    ap.add_argument("--demo-window", type=int, default=WINDOW_SEC,
                    help="demo window length in seconds (e.g. 180 to see settlements quickly)")
    args = ap.parse_args()
    load_env_file(os.path.join(HERE, ".env"))       # alert settings etc. when running on your own PC
    assets = [a.strip().upper() for a in args.assets.split(",") if a.strip().upper() in ASSETS] or ["BTC"]
    hub = Hub(assets, demo=args.demo, demo_window=max(120, args.demo_window))
    print(f"\n  Crypto 15-min monitor {'(DEMO) ' if args.demo else ''}running at http://{args.host}:{args.port}\n"
          f"  Coins: {', '.join(assets)}\n  Data and logs: {DATA_DIR}\n  Press Ctrl+C to stop.\n")
    web.run_app(build_app(hub), host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
