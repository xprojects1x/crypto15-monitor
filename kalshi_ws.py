"""
kalshi_ws.py - Kalshi's live data stream (WebSocket), market data only.

With a Kalshi API key, this replaces the once-a-second copy of Kalshi's prices with the live feed:
  * orderbook_delta        the full order book for the current window's market, updated the moment it changes
  * trade                  every trade as it prints
  * cfbenchmarks_value_5hz the official CF Benchmarks index these markets settle on (BRTI, ETHUSD_RTI, SOLUSD_RTI),
                           up to 5 updates a second
  * cfbenchmarks_value     the same index once a second, with Kalshi's own trailing 60-second average

Nothing here places, changes or cancels orders. The API key is only used to sign the connection request, which
Kalshi requires even for public market data.

Connection: wss://external-api-ws.kalshi.com/trade-api/ws/v2 (falls back to wss://api.elections.kalshi.com/...).
Login headers: KALSHI-ACCESS-KEY, KALSHI-ACCESS-TIMESTAMP (ms) and KALSHI-ACCESS-SIGNATURE = base64 of
RSA-PSS(SHA-256, MGF1-SHA-256, salt = digest length) over timestamp + "GET" + "/trade-api/ws/v2" (Ed25519 keys are
signed directly).

Test a key from the command line:
    python kalshi_ws.py --test --key-id YOUR_KEY_ID --key /path/to/private_key.pem
"""

import argparse
import asyncio
import base64
import json
import time
from collections import deque

import aiohttp

WS_URLS = ("wss://external-api-ws.kalshi.com/trade-api/ws/v2", "wss://api.elections.kalshi.com/trade-api/ws/v2")
WS_PATH = "/trade-api/ws/v2"
BOOK_FRESH_SEC = 10          # a book with no updates is still valid (nothing changed); this is just the stream health limit


def load_private_key(path):
    from cryptography.hazmat.primitives import serialization
    with open(path, "rb") as f:
        data = f.read()
    return serialization.load_pem_private_key(data, password=None)


def sign(private_key, text):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    msg = text.encode("utf-8")
    if isinstance(private_key, Ed25519PrivateKey):
        sig = private_key.sign(msg)
    else:
        sig = private_key.sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                                salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
    return base64.b64encode(sig).decode("utf-8")


def auth_headers(key_id, private_key, path=WS_PATH):
    ts = str(int(time.time() * 1000))
    return {"KALSHI-ACCESS-KEY": key_id, "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": sign(private_key, ts + "GET" + path)}


def _f(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


class KalshiStream:
    def __init__(self, key_id, key_path, index_ids=(), urls=WS_URLS):
        self.key_id = key_id
        self.key = load_private_key(key_path)
        self.urls = list(urls)
        self.index_ids = list(index_ids)
        self.books = {}             # ticker -> {"yes": {price: qty}, "no": {price: qty}, "ts": last change}
        self.index = {}             # index id -> (value, source ts, received ts)
        self.index_avg = {}         # index id -> (Kalshi's trailing 60 s average, ts)
        self.trades = {}            # ticker -> deque of parsed trades not yet collected
        self.want = set()           # tickers we should be subscribed to
        self.subs = {}              # ticker -> {channel: sid}
        self.pending = {}           # command id -> (channel, ticker)
        self.seq = {}               # sid -> last seq
        self.sid_of = {}            # sid -> (channel, ticker)
        self.ws = None
        self.connected = False
        self.error = ""
        self.since = None
        self.last_msg = 0.0
        self.msgs = 0
        self.reconnects = 0
        self.resyncs = 0
        self.resync = set()         # tickers whose book looked corrupt and should be fetched again
        self._id = 0

    # ----- what the monitor reads ----------------------------------------------------------

    def healthy(self):
        return self.connected and time.time() - self.last_msg < BOOK_FRESH_SEC

    def quote(self, ticker):
        """Best Yes bid/ask (and No bid/ask) with sizes, from the live book. None if we don't have the book."""
        b = self.books.get(ticker)
        if not b or not self.healthy():
            return None
        yes = [(p, q) for p, q in b["yes"].items() if q > 0]
        no = [(p, q) for p, q in b["no"].items() if q > 0]
        yb = max(yes) if yes else None           # best bid to buy Yes
        nb = max(no) if no else None             # best bid to buy No = the other side of the best Yes ask
        if yb and nb and yb[0] + nb[0] > 1.0001:  # a crossed book can't be real: our copy is out of sync
            self.resync.add(ticker)
            return None
        return {"yes_bid": yb[0] if yb else None, "yes_bid_size": yb[1] if yb else None,
                "no_bid": nb[0] if nb else None,
                "yes_ask": round(1 - nb[0], 4) if nb else None, "yes_ask_size": nb[1] if nb else None,
                "no_ask": round(1 - yb[0], 4) if yb else None, "book_ts": b["ts"]}

    def index_value(self, index_id, max_age=3.0):
        v = self.index.get(index_id)
        return v[0] if v and time.time() - v[2] <= max_age else None

    def pop_trades(self, ticker):
        q = self.trades.get(ticker)
        if not q:
            return []
        out = list(q)
        q.clear()
        return out

    def status(self):
        return {"connected": self.connected, "healthy": self.healthy(), "error": self.error,
                "age": round(time.time() - self.last_msg, 1) if self.last_msg else None,
                "msgs": self.msgs, "reconnects": self.reconnects, "resyncs": self.resyncs, "markets": sorted(self.subs),
                "index": {k: v[0] for k, v in self.index.items()}}

    # ----- subscriptions ----------------------------------------------------------------------

    async def _send(self, cmd, params, tag=None):
        self._id += 1
        if tag:
            self.pending[self._id] = tag
        await self.ws.send_str(json.dumps({"id": self._id, "cmd": cmd, "params": params}))

    async def watch(self, tickers):
        """Follow exactly these markets (current windows, plus any with resting orders)."""
        self.want = {t for t in tickers if t}
        if not self.connected or not self.ws:
            return
        for tk in sorted(self.want - set(self.subs)):
            await self._subscribe_market(tk)
        for tk in sorted(self.resync & set(self.subs)):
            await self._resubscribe_book(tk)
        self.resync.clear()
        for tk in sorted(set(self.subs) - self.want):
            sids = list(self.subs.pop(tk, {}).values())
            self.books.pop(tk, None)
            if sids:
                await self._send("unsubscribe", {"sids": sids})

    async def _subscribe_market(self, tk):
        self.subs.setdefault(tk, {})
        for ch in ("orderbook_delta", "trade"):
            await self._send("subscribe", {"channels": [ch], "market_ticker": tk}, tag=(ch, tk))

    async def _resubscribe_book(self, tk):
        """A gap in the order book's sequence (or a book that can't be right): drop it and ask for a fresh snapshot."""
        self.resyncs += 1
        sid = self.subs.get(tk, {}).pop("orderbook_delta", None)
        self.books.pop(tk, None)
        if sid is not None:
            await self._send("unsubscribe", {"sids": [sid]})
        await self._send("subscribe", {"channels": ["orderbook_delta"], "market_ticker": tk}, tag=("orderbook_delta", tk))

    # ----- messages ---------------------------------------------------------------------------

    async def handle(self, d):
        typ, msg = d.get("type"), d.get("msg") or {}
        sid = d.get("sid")
        if typ == "subscribed":
            tag = self.pending.pop(d.get("id"), None)
            s = msg.get("sid", sid)
            if tag and s is not None:
                ch, tk = tag
                self.sid_of[s] = tag
                if tk:
                    self.subs.setdefault(tk, {})[ch] = s
            return
        if typ == "error":
            self.pending.pop(d.get("id"), None)
            self.error = f"Kalshi stream: {msg.get('msg') or msg}"[:200]
            return
        if sid is not None and d.get("seq") is not None and typ in ("orderbook_snapshot", "orderbook_delta"):
            last = self.seq.get(sid)
            self.seq[sid] = d["seq"]
            if typ == "orderbook_delta" and last is not None and d["seq"] != last + 1:
                tk = msg.get("market_ticker")
                if tk:
                    await self._resubscribe_book(tk)
                return
        if typ == "orderbook_snapshot":
            tk = msg.get("market_ticker")
            book = {"yes": {}, "no": {}, "ts": time.time()}
            for side in ("yes", "no"):
                for lvl in msg.get(f"{side}_dollars_fp") or []:
                    p, q = _f(lvl[0]), _f(lvl[1])
                    if p is not None and q:
                        book[side][round(p, 4)] = q
            self.books[tk] = book
        elif typ == "orderbook_delta":
            tk = msg.get("market_ticker")
            b = self.books.get(tk)
            p, dq, side = _f(msg.get("price_dollars")), _f(msg.get("delta_fp")), msg.get("side")
            if b is not None and p is not None and dq is not None and side in ("yes", "no"):
                p = round(p, 4)
                q = b[side].get(p, 0.0) + dq
                if q > 1e-9:
                    b[side][p] = q
                else:
                    b[side].pop(p, None)
                b["ts"] = time.time()
        elif typ == "trade":
            tk = msg.get("market_ticker")
            yp, cnt, side = _f(msg.get("yes_price_dollars")), _f(msg.get("count_fp")), (msg.get("taker_side") or "").lower()
            ts = msg.get("ts_ms")
            ts = ts / 1000 if ts else _f(msg.get("ts"))
            if tk and yp is not None and cnt and side in ("yes", "no"):
                b = self.books.get(tk)                  # what our live book showed as the trade arrived
                yes = [p for p, q in b["yes"].items() if q > 0] if b else []
                no = [p for p, q in b["no"].items() if q > 0] if b else []
                self.trades.setdefault(tk, deque(maxlen=5000)).append(
                    {"id": msg.get("trade_id") or f"{tk}-{ts}-{yp}-{cnt}-{side}", "yes_price": yp, "count": cnt,
                     "taker_side": side, "ts": ts or time.time(),
                     "book_bid": max(yes) if yes else None, "book_ask": round(1 - max(no), 4) if no else None})
        elif typ == "cfbenchmarks_value_5hz":
            v = _f(msg.get("value_usd"))
            if v is not None and msg.get("index_id"):
                src = msg.get("source_ts_ms")
                self.index[msg["index_id"]] = (v, src / 1000 if src else time.time(), time.time())
        elif typ == "cfbenchmarks_value":
            a = (msg.get("avg_60s_data") or {}).get("value")
            if a is not None and msg.get("index_id"):
                self.index_avg[msg["index_id"]] = (_f(a), time.time())
            if msg.get("index_id") and msg["index_id"] not in self.index:     # 5 Hz not flowing: use the 1/s value
                try:
                    raw = json.loads(msg.get("data") or "{}")
                    v = _f(raw.get("value"))
                    if v is not None:
                        self.index[msg["index_id"]] = (v, (raw.get("time") or 0) / 1000 or time.time(), time.time())
                except (ValueError, TypeError):
                    pass

    # ----- connection loop ------------------------------------------------------------------

    async def run(self, session):
        backoff = 2
        while True:
            for url in self.urls:
                try:
                    async with session.ws_connect(url, headers=auth_headers(self.key_id, self.key),
                                                  heartbeat=20, timeout=aiohttp.ClientWSTimeout(ws_close=10)) as ws:
                        self.ws, self.connected, self.error = ws, True, ""
                        self.since = time.time()
                        self.subs, self.pending, self.seq, self.sid_of, self.books = {}, {}, {}, {}, {}
                        backoff = 2
                        if self.index_ids:
                            await self._send("subscribe", {"channels": ["cfbenchmarks_value_5hz"], "index_ids": self.index_ids},
                                             tag=("cfbenchmarks_value_5hz", None))
                            await self._send("subscribe", {"channels": ["cfbenchmarks_value"], "index_ids": self.index_ids},
                                             tag=("cfbenchmarks_value", None))
                        for tk in sorted(self.want):
                            await self._subscribe_market(tk)
                        async for m in ws:
                            if m.type == aiohttp.WSMsgType.TEXT:
                                self.last_msg = time.time()
                                self.msgs += 1
                                try:
                                    await self.handle(json.loads(m.data))
                                except (ValueError, KeyError, TypeError):
                                    continue
                            elif m.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                break
                        self.error = self.error or "Kalshi stream closed; reconnecting"
                except asyncio.CancelledError:
                    raise
                except aiohttp.WSServerHandshakeError as e:
                    self.error = (f"Kalshi stream login refused (HTTP {e.status}): check the API key ID and private key"
                                  if e.status in (401, 403) else f"Kalshi stream: HTTP {e.status}")
                except Exception as e:
                    self.error = f"Kalshi stream: {type(e).__name__}: {e}"[:200]
                finally:
                    self.connected, self.ws = False, None
                self.reconnects += 1
            await asyncio.sleep(backoff)
            backoff = min(60, backoff * 2)


async def _test(key_id, key_path, index_id, seconds, url=None):
    s = KalshiStream(key_id, key_path, index_ids=[index_id], urls=(url,) if url else WS_URLS)
    async with aiohttp.ClientSession() as session:
        task = asyncio.create_task(s.run(session))
        t0 = time.time()
        while time.time() - t0 < seconds and s.index_value(index_id) is None:
            await asyncio.sleep(0.5)
        task.cancel()
    v = s.index_value(index_id, max_age=60)
    if v is not None:
        print(f"OK: connected to Kalshi's live stream. {index_id} = {v:,.2f}")
        return 0
    print("FAILED: " + (s.error or "connected, but no index values arrived"))
    return 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Test a Kalshi API key against the live data stream")
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--key-id", required=True)
    ap.add_argument("--key", required=True, help="path to the private key file (PEM)")
    ap.add_argument("--index", default="BRTI")
    ap.add_argument("--seconds", type=float, default=20)
    ap.add_argument("--url", help=argparse.SUPPRESS)          # for testing against a stand-in server
    a = ap.parse_args()
    raise SystemExit(asyncio.run(_test(a.key_id, a.key, a.index, a.seconds, a.url)))
