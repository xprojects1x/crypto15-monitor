# Run it 24/7 on DigitalOcean

About 30 minutes, start to finish. You end up with the monitor running around the clock for all three coins,
learning from every window, behind HTTPS with a password, sending alerts to your phone, backing itself up, and
restarting itself if anything goes wrong.

**Cost:** about **$7.20/month**: a $6 Basic Droplet (1 GB RAM) plus $1.20 for DigitalOcean's weekly backups.
The $4 (512 MB) Droplet is too small for three coins.

---

## Before you start (10 minutes)

1. **Put the code on GitHub** (see README → *Run it online with GitHub*, step 1). Make the repository **public**:
   the server downloads the code from there. Nothing private is in it. Your password, alert keys, paper trades and
   agent memory live only on the server.
2. **Create a Telegram bot** (recommended for alerts): in Telegram, open **@BotFather**, send `/newbot`, pick a name,
   and keep the token it gives you. Then open your new bot and send it any message, like *hi*.
   *(Prefer Discord or email? The setup offers those too.)*
3. **Optional dead-man's switch:** make a free account at **healthchecks.io**, add a check with
   *Period 5 minutes* and *Grace 5 minutes*, and copy its **ping URL**. If the server ever goes completely silent,
   healthchecks.io notifies you, even though the server itself can't.

## Create the Droplet (5 minutes)

In DigitalOcean: **Create → Droplets**, then choose:

| Setting | Choose |
|---|---|
| Region | **New York** or **San Francisco** (US, close to Kalshi and the exchanges) |
| Image | **Ubuntu 24.04 (LTS) x64** (26.04 also works) |
| Size | **Basic → Regular → $6/mo** (1 GB RAM, 1 CPU) |
| Authentication | **Password** is simplest (or an SSH key if you have one) |
| Backups | **Enable weekly backups** (+$1.20/mo) |
| Hostname | `crypto15` |

Click **Create Droplet** and wait about a minute.

## Run the setup (15–35 minutes, mostly waiting)

1. Open the Droplet's page, then **Access → Launch Droplet Console**. A terminal opens in your browser, logged in as root.
2. Paste this one line, replacing `YOU/REPO` with your GitHub username and repository name (twice), and press Enter:

   ```
   bash <(curl -fsSL https://raw.githubusercontent.com/YOU/REPO/main/deploy/setup_vps.sh) https://github.com/YOU/REPO
   ```

3. Answer the questions as they come:
   * **Username and password** for the dashboard (8+ characters).
   * **Your own domain:** just press Enter to use the free address it shows (like `https://203-0-113-7.sslip.io`).
   * **Alerts:** choose 1 for Telegram, paste the bot token, then press Enter. It finds your chat by itself and sends a test message.
   * **healthchecks.io ping URL** (or Enter to skip), **daily summary time** (Enter = 8 pm Eastern), and whether to be messaged on every paper trade.
   * **30 days of Kalshi prices** for a head start: say yes. It takes about 20 minutes and makes the Kalshi crowd agent smarter from day one.
4. At the end it prints your **dashboard address**. Open it on your computer or phone and log in.
5. In the dashboard, open **Strategy settings → Alerts → Send test alert + summary** to confirm alerts reach your phone.

## What happens from now on, without you

| What | When |
|---|---|
| Monitor and agent teams for BTC, ETH, SOL | Always running; restarts itself within 5 seconds if it ever crashes, and after reboots |
| History reports (the **History ↗** button) | Refreshed every hour at :20 |
| Backup of agent memory, paper trades and settings | Nightly at 03:40 UTC, last 14 kept in `/opt/crypto15/backups` |
| Old tick logs | Compressed daily, deleted after 90 days |
| Ubuntu security updates | Installed automatically |

**Alerts you'll get:**
* The monitor started (so you'll know if it ever restarted)
* A coin's exchange feeds or Kalshi data have been down for 5+ minutes, and again when they're back
* A team clearly beats Kalshi and starts paper trading, or loses its edge and goes back to watching
* An agent promoted from probation or demoted
* A daily loss limit hit
* The disk almost full
* The daily summary
* Optionally, every paper trade

## Everyday commands (in the Droplet Console)

```
/opt/crypto15/deploy/status.sh        # is everything healthy? feeds, logs, disk, backups
/opt/crypto15/deploy/update.sh        # install the newest version from GitHub (backs up first)
nano /etc/crypto15.env                # change alert settings, then: systemctl restart crypto15
bash /opt/crypto15/deploy/setup_vps.sh   # change password or address; keeps all your data
journalctl -u crypto15 -f             # watch the live log (Ctrl+C to stop watching)
```

**Restore a backup:**
`systemctl stop crypto15 && tar -xzf /opt/crypto15/backups/<file>.tar.gz -C /opt/crypto15 && systemctl start crypto15`

## Your monthly 5 minutes

* Add upcoming **CPI and jobs-report** dates to `events.json` (in your GitHub copy, then run `update.sh`, or edit
  `/opt/crypto15/events.json` directly) with `"impact": "high"`, so the teams sit out around them.
* Glance at the daily summaries and the dashboard's **vs Kalshi** and paper P&L numbers.

## If something looks wrong

* **The address doesn't open right after setup:** the HTTPS certificate can take a minute or two. If it still fails,
  check that you haven't added a DigitalOcean *Cloud Firewall* that blocks ports 80/443.
* **An alert says feeds or Kalshi are down for a long time:** run `status.sh`. If one exchange or Kalshi blocks
  this server's region, recreate the Droplet in the other US region; everything else keeps running in the meantime.
* **Derivatives agent shows no data:** Kraken Futures may not serve your region. That agent just sits out.
* **Out of memory or very slow:** resize the Droplet to 2 GB in DigitalOcean (*Resize*, about a minute of downtime).

Paper trading only. Nothing on the server can place real orders, and it never asks for your Kalshi login.
