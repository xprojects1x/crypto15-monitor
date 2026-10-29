# Crypto 15-Minute Monitor for Kalshi (BTC, ETH, SOL)

A live dashboard and learning agent team for Kalshi's **15-minute Up or Down** markets on
**Bitcoin** (`KXBTC15M`), **Ether** (`KXETH15M`) and **Solana** (`KXSOL15M`). All three run side by side; switch
coins with the tabs at the top, and the strip under the header shows every coin at a glance.
It runs on your own computer (or a server) and opens in your browser at **http://localhost:8765**.

Monitoring and paper trading only. It never places real orders and never asks for your Kalshi keys.

---

## Quick start

**Windows:** double-click `start_windows.bat`.
**Mac / Linux:** run `./start_mac_linux.sh` in a terminal.

Either launcher installs the one dependency (`aiohttp`) and opens the dashboard.
You need **Python 3.9 or newer** (from python.org; on Windows tick "Add Python to PATH" during install).

Manual start:

```
pip install -r requirements.txt
python app.py                  # live data, BTC + ETH + SOL
python app.py --assets BTC     # just one coin (or BTC,ETH)
python app.py --demo           # simulated data, works offline
python app.py --demo --demo-window 180   # 3-minute demo windows, to watch settlements quickly
```

Stop it with **Ctrl+C** in the terminal window.

---

## Run it 24/7 on a server

See **[DEPLOY_DIGITALOCEAN.md](DEPLOY_DIGITALOCEAN.md)**: one command on a $6/month DigitalOcean Droplet sets up the
always-on monitor with HTTPS, a password, phone alerts (Telegram, Discord or email), hourly history reports,
nightly backups and automatic restarts.

**Alerts on your own PC:** put the same settings in a file called `.env` next to `app.py` (see the top of
`alerts.py` for the list). Optional built-in login for other hosts: `APP_PASSWORD=...` (user `admin`).

---

## Run it online with GitHub

The repo includes everything GitHub needs. It gives you two things:

| | What it does | Cost |
|---|---|---|
| **History reports on GitHub Pages** | A GitHub Action runs every hour: downloads new windows for all three coins, validates them, and publishes the reports to `https://<your-username>.github.io/<repo-name>/` (BTC), `/eth/` and `/sol/`. Works on your phone; nothing to keep running. | Free for public repos |
| **Live dashboard in a Codespace** | One click starts `app.py` in a cloud machine and opens the dashboard in your browser. Runs while you're using it. | Free up to ~60 hours/month (GitHub Free) |

### 1. Put the code on GitHub
Easiest: install **GitHub Desktop** → *File → Add local repository* → pick this folder → *create a repository* → **Publish repository**.
Untick *Keep this code private* if you want the free Pages site (Pages on private repos needs GitHub Pro).
GitHub Desktop uploads the hidden `.github` and `.devcontainer` folders, which the website's drag-and-drop can skip.

Your local `data/` folder (paper trades, logs) is never uploaded; `.gitignore` excludes it.

### 2. Turn on the hourly report
1. Repo → **Settings → Pages** → *Build and deployment* → Source: **GitHub Actions**.
2. Repo → **Actions** tab → enable workflows if asked → **Update history report** → **Run workflow**.
   For the first run, set *prices days* to `30` (or `all`, about an hour) to fill the "Was Kalshi's price right?" section.
3. When it finishes (a few minutes), the run page shows your report link. After that it updates every hour by itself.

Data is kept between runs on a branch named `history-data` (replaced each run, so the repo stays small).
If GitHub ever pauses the schedule (it does for public repos with no activity for 60 days), click **Enable workflow** on the Actions tab.
On a private repo, change the cron line in `.github/workflows/update-history.yml` to `"7 */2 * * *"` to stay inside the 2,000 free minutes a month.

### 3. Open the live dashboard in the cloud
Repo → green **Code** button → **Codespaces** → **Create codespace on main**.
It installs itself, loads the saved history, starts the monitor and opens the dashboard in a new tab
(if not, open the **PORTS** tab and click the globe next to port 8765). The link is private to your GitHub login.
Codespaces stop after a period of inactivity (default 30 minutes; raise it to 4 hours in GitHub settings → Codespaces),
and paper trades made there live in that codespace. Stop or delete the codespace when you're done to save your free hours.

---

## What you're looking at

| Panel | What it shows |
|---|---|
| **Index proxy** | 1-second price for the coin built from Coinbase, Kraken, Bitstamp, Gemini and Crypto.com (median of exchange mid prices; any feed >0.5% off or >10s stale is dropped). It stands in for CF Benchmarks' BRTI, ETHUSD RTI or SOLUSD RTI. |
| **Exchange feeds** | Each exchange's mid, plus the **Kraken perpetual future**, taker **buy/sell flow** (Coinbase + Kraken trades), Kraken **order-book imbalance**, futures **premium** and **funding**. |
| **Price to beat** | Kalshi's `floor_strike` for the live window: the 60-second BRTI average before the window opened. |
| **Settlement average (est.)** | The model's expected value of the 60 prints Kalshi will average, with ±1σ. In the final minute it shows how many prints are already locked in and the move needed in the remaining prints to flip the result. |
| **Fair P(Up)** | Model probability that the settlement average finishes at or above the price to beat, next to Kalshi's mid. |
| **Signal** | Edge per contract after Kalshi's taker fee for each side, a resting-limit idea, and the entry rules that are blocking a trade. |
| **Charts** | This window's price vs. strike and settlement estimate; model probability vs. Kalshi bid/ask; the last 60 minutes. Hover for values. |
| **Exchange feeds** | Each exchange's mid, distance from the proxy, and data age. Hover a row to see a connection error. |
| **Proxy calibration** | How far our 60-second averages land from Kalshi's own strike and settlement values. The median gap is applied as an offset. |
| **Paper trading** | Buy Up/Down at the live ask (taker fee included), sell at the bid, or hold to settlement. Tracks P&L, win rate, fees, and what the model expected you to make. |

### Why a proxy and not the real BRTI?
CF Benchmarks licenses the real-time BRTI; it isn't free, and scraping their site is prohibited.
The proxy uses the same constituent exchanges (LMAX Digital and Bullish are left out; they need accounts).
Watch the **typical tracking error** in the calibration panel after an hour or so of running. Treat any window
where the estimated settlement is within about 2× that error of the strike as a coin flip.

---

## The model

Kalshi settles on the average of the 60 one-second BRTI prints before the close.
The app treats each coin as a random walk over the next few minutes with volatility measured from the last
10 minutes of 1-second changes (times a safety multiplier, default 1.15).

* **Before the final minute:** settlement average ≈ current price, with spread ≈ σ·√(seconds until the final minute + 20.5).
* **Inside the final minute:** the prints already taken are known exactly; only the remaining ones are uncertain,
  which is why a late spike moves the result far less than it seems to.

Fair P(Up) = probability that average ≥ price to beat.
**Edge** = fair value − ask − taker fee, where the fee is `round_up(0.07 × contracts × P × (1−P))`.

The model ignores jumps and news, so it will be overconfident in fast markets. Raise the volatility multiplier if
paper results show it losing more often than it expects.

---

## Default strategy rules (editable in the dashboard)

| Setting | Default | Why |
|---|---|---|
| Min edge to enter | 3¢ per contract after fees | Filters out noise and model error |
| No new entries in last | 120 s | Spreads widen and index-reading bots dominate the final minutes |
| Max Yes spread | 4¢ | Wide spreads eat the edge |
| Min live exchanges | 3 | Proxy is unreliable with fewer feeds |
| Exit when bid beats fair by | 2¢ | Take the market's overpayment instead of holding to settlement |
| Contracts per trade | 10 | Used for fee rounding and paper trades |

Near 50¢ the taker fee peaks (≈1.75¢ per contract). The dashboard suggests a resting **limit** price instead,
since maker orders on most Kalshi markets pay no fee.

---

## The agent team (learning, paper trading)

Each coin has its own team of ten specialist agents. Each one forecasts P(Up) for every window from one kind of
information and keeps learning from every settled window. Memory is saved per coin (`data/agents_state.json` for BTC,
`data/eth/…`, `data/sol/…`), so learning carries across restarts.

| Agent | Category | Looks at | Starts as |
|---|---|---|---|
| Price & volatility | Fundamentals | the settlement-average model | Active |
| Momentum | Trend | moves over the last 5 s, 15 s, 1 min, 5 min | Active |
| Exchange lead/lag | Microstructure | each exchange's gap to the composite | Active |
| Kalshi crowd | Sentiment | Kalshi's price, its 30-second drift, the spread | Active |
| Time & streaks | Patterns | hour, quarter-hour slot, current streak | Probation |
| Order flow | Trades & order book | taker buy vs sell pressure (15 s – 5 min), Kraken top-10 book imbalance | Probation |
| Derivatives | Futures | Kraken perpetual premium, its 1-min change, funding, futures moving before spot | Probation |
| Cross-asset | Other coins | moves in the other two coins this one hasn't matched yet | Probation |
| Events & volatility | Regime | data-release times, Fed decisions, US open, weekends, volatility spikes (adjusts confidence) | Probation |
| Chart reading | Technical | RSI(14), Bollinger %B(20), MACD(12,26,9) on 1-minute bars (needs ~36 min of data) | Probation |

* **Probation:** agents on probation forecast, learn and are scored, but have **no say** in the team forecast or trades.
  A shadow coordinator that includes everyone measures what each would add. After ~200 windows (setting: *Probation
  length*), an agent is **promoted** if it clearly improves the team, or if its own forecasts clearly beat the team's.
  An active agent is **demoted** if it clearly makes the team worse. Promotions and demotions appear in
  *What the team learned recently*.
* **Coordinator:** combines the active agents' forecasts and learns how much to trust each one, separately for the early, middle
  and late part of the window.
* **Scorekeeper:** for each agent, shows its **value added** (how much worse the team's forecasts would be without it),
  its accuracy against **Kalshi's own price**, its hit rate, and its **solo paper P&L** (what it would have made trading
  alone, after fees).
* **Risk manager:** places team paper trades only when all of these hold:
  * it has learned from at least 50 windows;
  * the team's recent forecasts beat Kalshi's prices by a **statistically clear** margin;
  * the edge after fees clears your minimum;
  * spread, feed and time rules pass;
  * no high-impact event (like a Fed decision) is within 10 minutes of the window;
  * the daily loss limit hasn't been hit.

  Size is **quarter-Kelly**, capped at 2% of the paper bankroll per window. It also sells early when the bid pays more
  than the team's fair value.

All of these settings are on the dashboard under *Agent team (paper)*, separately for each coin.

**Event calendar:** `events.json` lists Fed decision dates (marked high impact) and the recurring weekday times when
US data comes out or stocks open. Add CPI and jobs-report dates as `"impact": "high"` and the team will sit out around
them. The app re-reads the file every minute.

**Head start from history:** `python history.py --asset BTC --candles-days 30`, then `python train_agents.py --asset BTC`,
and the same with `ETH` and `SOL` (Windows: `train_agents.bat` does all three).
Close the dashboard first. This trains the crowd and pattern agents on past windows and prints an honest out-of-sample comparison with
Kalshi's prices. The price, momentum and exchange agents need live second-by-second data, so they learn from the first live window.

**What was tested:**
* On a synthetic market where the truth is known, the agents that could see real signals earned trust, the
  noise-only pattern agent was given about zero weight, and the team traded profitably.
* With five agents on probation, where only the derivatives agent had real information, the derivatives agent was promoted
  and the four noise agents stayed on probation.
* On an efficient synthetic market, the team never proved an edge and placed **zero** trades.

The team can only find edge that really exists. If Kalshi's prices are already efficient, the correct outcome
is that it watches and doesn't trade. Learning is continuous only while the app is running, so a 24/7 host lets it learn from all 96 windows a day.

## Historical data (every settled window since launch)

**Windows:** double-click `get_history_windows.bat` (all three coins). **Any OS:** `python history.py --asset BTC` (or `ETH`, `SOL`).

This pulls every settled window for the coin from Kalshi's free public API: recent windows from `/markets` and older ones
from the `/historical/markets` archive. That's a few dozen requests, usually under a minute. It then builds
a report (`data/history/report.html` for BTC, `data/eth/history/…`, `data/sol/history/…`), which also opens from
the **History ↗** button on the dashboard and links to the other coins' reports. Distances like "within $25" scale with
the coin: $25 for BTC, $1 for ETH, 5¢ for SOL. The report shows:

* Up rate by day, plus a full daily table (windows, Up/Down, median and largest move, open and close price, volume)
* Up rate and typical move size by hour of day, weekday and quarter-hour slot, with 95% ranges so you can tell real patterns from luck
* How far windows finish from the line, streak lengths, and whether the last result predicts the next one
* **Was Kalshi's price right?** Kalshi's Yes price 10/5/2/1 minutes before close against how often windows actually settled Up,
  with the after-fee edge of buying each side at that price. Needs `--candles`.

```
python history.py                    # all windows (fast)
python history.py --candles-days 14  # + Kalshi's prices before close, last 14 days (~15 min)
python history.py --candles          # + prices for every window (~1 hr first time; Ctrl+C safe, resumes)
python history.py --tz PT            # group days and hours in Pacific time (default ET, Kalshi's clock)
python history.py --report-only      # rebuild the report without downloading
```

Rerun any time to add new windows; prices already fetched are kept.

The report also has:

* **Up vs Down, every day:** green/red stacked bars with the count of Up and Down windows per day.
* **Every window, day by day:** a grid with one row per day and one cell per 15-minute window, colored by result.
* **Colorblind colors** toggle in the header, which switches green/red to blue/orange.
* **Sequence & progression lab:** replays betting rules on the real sequence of results with Kalshi fees, a bankroll
  and a max-bet limit. Side rules: always Up/Down, same as last, opposite of last, switch after a loss, fade or follow N in a row.
  Staking: flat, d'Alembert, capped martingale, full martingale. It shows profit or loss, fees, largest bet, drawdown,
  the equity curve, a table of every rule × staking combination, and a **luck test** that reruns the rule on 200 shuffled
  orderings of the same results. Entry price is an assumed ask (default 51¢), or Kalshi's real ask one minute after the open when
  `--candles` data is downloaded.

## Validating the data and the patterns

**Windows:** double-click `validate_history.bat` (all three coins). **Any OS:** `python validate.py --asset BTC` (`--coinbase 0` to skip the online cross-check).

Data checks:
* No duplicate windows; any gaps in the 15-minute schedule are listed.
* Every result agrees with settlement ≥ price to beat.
* **Chain check:** each window's price to beat must equal the previous window's settlement exactly, since both are the
  index average over the same 60 seconds. A break points to a bad record.
* Prices are sane.
* Direction agrees with Coinbase 1-minute candles, an independent source. Windows that finished well clear of the line should agree ~100%.
* 20 random kalshi.com links to check by eye.

Pattern checks:
* Runs test and lag-1 correlation: does the order of Ups and Downs look random?
* Out-of-sample: hour-of-day leans, streak behaviour and Kalshi price edges found in the first half of history are re-tested on the second half.
* How many "significant" findings to expect from luck alone.

Results print in the terminal, save to `data/history/validation.json`, and show at the top of the report under **Data checks**.

Each window's price to beat and settlement value are official 60-second averages of CF Benchmarks' index, so
`windows.csv` doubles as a free official index series at 15-minute resolution.

---

## Files it writes (in `data/` for BTC, `data/eth/` and `data/sol/` for the others)

* `ticks_YYYYMMDD.csv`: one row per second (proxy, each exchange, strike, model, Kalshi bid/ask, edge, signal, team forecast,
  order flow, futures). Finished days are compressed to `.csv.gz` (about 85% smaller) and days older than 90 are deleted
  (setting: *Keep tick logs*), so storage stays around 1 GB per coin per year.
* `agents_state.json`: everything the agent team has learned.
* `windows.json`: every settled window with Kalshi's strike and settlement value next to ours.
* `paper_trades.json`: open and closed paper trades.
* `config.json`: your saved settings.
* `history/`: `windows.csv` (one row per settled window), `daily.csv`, `hourly.csv`, `weekday.csv`, `summary.json`, `report.html`, and `candles.jsonl` (cached Kalshi prices).

---

## Data sources

* Kalshi public market data: `GET https://api.elections.kalshi.com/trade-api/v2/markets?series_ticker=KXBTC15M&status=open`
  (and `KXETH15M`, `KXSOL15M`; falls back to `external-api.kalshi.com`). Each coin is polled once per second, staggered.
* Exchange websockets: Coinbase `ws-feed.exchange.coinbase.com` (ticker + matches), Kraken `ws.kraken.com/v2` (ticker, book,
  trade), Bitstamp `ws.bitstamp.net`, Gemini `api.gemini.com/v1/marketdata`, Crypto.com `stream.crypto.com/exchange/v1/market`.
* Kraken Futures `futures.kraken.com/ws/v1` ticker for `PF_XBTUSD`, `PF_ETHUSD`, `PF_SOLUSD`: public market data. If it
  isn't reachable from your location, the derivatives agent sits out and everything else runs normally.

If Kalshi or an exchange changes its API, the affected panel shows the error and everything else keeps running.

---

## Next steps (when you're ready)

1. Run it for a few days and compare **Realized P&L** with **Model-expected P&L** in paper trading.
2. Backtest rule changes on the tick CSVs.
3. Only then consider live execution through Kalshi's authenticated trading API, with hard position and loss limits.

Event contracts can lose your whole stake. Nothing here is financial advice.
