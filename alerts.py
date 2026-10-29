"""
alerts.py - phone/email alerts and a heartbeat for the monitor.

Configure with environment variables (the VPS setup script writes them to /etc/crypto15.env; on your own PC you
can put the same lines in a file called .env next to app.py):

  TELEGRAM_BOT_TOKEN=123456:ABC...      Telegram bot from @BotFather
  TELEGRAM_CHAT_ID=987654321            your chat with that bot
  DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
  SMTP_HOST=smtp.gmail.com  SMTP_PORT=587  SMTP_USER=you@gmail.com  SMTP_PASSWORD=app-password
  ALERT_EMAIL_TO=you@gmail.com
  HEALTHCHECK_URL=https://hc-ping.com/...   pinged every minute while data is flowing (dead-man's switch)
  DAILY_SUMMARY_ET=20:00                    time for the daily summary, US Eastern (blank to turn off)
  ALERT_TRADES=0                            1 = also message every team paper trade

Any combination of channels works; with none configured, alerts just show on the dashboard.
"""

import asyncio
import os
import smtplib
import ssl
import time
from collections import deque
from email.message import EmailMessage

import aiohttp


def load_env_file(path):
    """Read KEY=VALUE lines from a .env file into the environment (existing variables win)."""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except OSError:
        pass


class Alerter:
    def __init__(self):
        e = os.environ.get
        self.tg_token, self.tg_chat = e("TELEGRAM_BOT_TOKEN", ""), e("TELEGRAM_CHAT_ID", "")
        self.discord = e("DISCORD_WEBHOOK_URL", "")
        self.smtp = {"host": e("SMTP_HOST", ""), "port": int(e("SMTP_PORT", "587") or 587), "user": e("SMTP_USER", ""),
                     "password": e("SMTP_PASSWORD", ""), "to": e("ALERT_EMAIL_TO", ""), "from": e("ALERT_EMAIL_FROM", "")}
        self.healthcheck = e("HEALTHCHECK_URL", "")
        self.summary_time = e("DAILY_SUMMARY_ET", "20:00").strip()
        self.alert_trades = e("ALERT_TRADES", "0").strip() in ("1", "true", "yes")
        self.tg_api = e("TELEGRAM_API_BASE", "https://api.telegram.org")       # overridable for testing
        self.session = None
        self.last_sent = {}                    # key -> time, for cooldowns
        self.recent = deque(maxlen=30)         # what was sent, for the dashboard
        self.errors = deque(maxlen=10)
        self.last_ping = None

    def channels(self):
        out = []
        if self.tg_token and self.tg_chat:
            out.append("Telegram")
        if self.discord:
            out.append("Discord")
        if self.smtp["host"] and self.smtp["to"]:
            out.append("Email")
        return out

    async def send(self, text, key=None, cooldown=3600, force=False):
        """Send to every configured channel. Messages with the same key are held back during the cooldown."""
        t = time.time()
        if key and not force and t - self.last_sent.get(key, 0) < cooldown:
            return False
        if key:
            self.last_sent[key] = t
        self.recent.appendleft({"t": t, "text": text})
        results = await asyncio.gather(*[self._one(ch, text) for ch in self.channels()], return_exceptions=True)
        for ch, r in zip(self.channels(), results):
            if isinstance(r, Exception) or r is False:
                self.errors.appendleft({"t": t, "channel": ch, "error": str(r)[:200]})
        return True

    async def _one(self, channel, text):
        if channel == "Telegram":
            url = f"{self.tg_api}/bot{self.tg_token}/sendMessage"
            async with self.session.post(url, json={"chat_id": self.tg_chat, "text": text[:4000],
                                                    "disable_web_page_preview": True},
                                         timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status != 200:
                    raise RuntimeError(f"Telegram HTTP {r.status}: {(await r.text())[:120]}")
            return True
        if channel == "Discord":
            async with self.session.post(self.discord, json={"content": text[:1900]},
                                         timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status >= 300:
                    raise RuntimeError(f"Discord HTTP {r.status}")
            return True
        if channel == "Email":
            await asyncio.get_running_loop().run_in_executor(None, self._email, text)
            return True
        return False

    def _email(self, text):
        s = self.smtp
        msg = EmailMessage()
        msg["Subject"] = "Crypto 15m monitor: " + text.split("\n", 1)[0][:80]
        msg["From"] = s["from"] or s["user"]
        msg["To"] = s["to"]
        msg.set_content(text)
        if s["port"] == 465:
            with smtplib.SMTP_SSL(s["host"], s["port"], context=ssl.create_default_context(), timeout=20) as m:
                if s["user"]:
                    m.login(s["user"], s["password"])
                m.send_message(msg)
        else:
            with smtplib.SMTP(s["host"], s["port"], timeout=20) as m:
                m.starttls(context=ssl.create_default_context())
                if s["user"]:
                    m.login(s["user"], s["password"])
                m.send_message(msg)

    async def ping(self, ok=True):
        """Heartbeat for an outside watchdog: silence (or a /fail ping) makes it alert you."""
        if not self.healthcheck:
            return
        url = self.healthcheck if ok else self.healthcheck.rstrip("/") + "/fail"
        try:
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                self.last_ping = {"t": time.time(), "status": r.status, "ok": ok}
        except Exception as e:
            self.last_ping = {"t": time.time(), "status": None, "ok": ok, "error": str(e)[:120]}

    def status(self):
        return {"channels": self.channels(), "healthcheck": bool(self.healthcheck), "last_ping": self.last_ping,
                "summary_time": self.summary_time, "recent": list(self.recent)[:8], "errors": list(self.errors)[:5]}
