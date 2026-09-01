# BSL/SSL Liquidity Scanner & Webhook — NSE:NIFTY

A real-time liquidity tracking and alert system that replicates the TradingView Pine indicator **"BSL / SSL Liquidity Start Signals — v5 LITE"** (`abcd.txt` in this repo) **1:1**, eliminates timing conflicts and latency, and broadcasts verified **BUY / SELL / SWEEP signals** to **two Telegram bots** simultaneously.

---

## 🔍 Deep Analysis: Why Chart Signals & Scanner Alerts Conflict or Differ

When analyzing the TradingView chart indicator alongside Telegram scanner alerts, four core factors explain why signals may appear to conflict or arrive with delays:

### 1. The 8-Bar Pivot Confirmation (Chart Visual Anchor vs Real-Time Trigger)
* **On TradingView Chart (`abcd.txt`)**: 
  When a swing pivot low forms at `09:45 IST`, it cannot be confirmed until `pivLen = 8` bars close without breaking that low. Therefore, the signal mathematically confirms at `10:25 IST` (`8 bars × 5m = 40 min`).
  Once confirmed, TradingView Pine Script executes `label.new(bar_index - 8, ...)` which retroactively draws the green `BUY` badge **8 bars back** sitting directly under the `09:45 IST` candle.
* **In Telegram Alerts**:
  The alert fires in real-time when the `10:25 IST` bar closes.
* **The Confusion / Conflict**:
  A trader checking the chart at `10:25 IST` does not see a badge at `10:25 IST`; they see it sitting back at `09:45 IST`. Conversely, looking at the `09:45 IST` candle, they wonder why the alert says `Bar: 10:25 IST` with `Entry: 24,061.05` (the 10:25 bar close).
* **The Solution**:
  Every alert now provides **Dual Timestamps**:
  - `📍 Chart Anchor (Swing Low/High)`: e.g. `2026-09-01 09:45 IST` (matches the exact candle with the visual badge on TradingView).
  - `⚡ Signal Confirmed & Fired`: e.g. `2026-09-01 10:25 IST` (the closed bar that triggered the trade in real time).

---

### 2. Sweep Event vs New Pool Start Signal
* **Sweeps (`🧹 SSL/BSL SWEPT`)**: 
  A sweep happens instantly when price trades through an existing liquidity pool level and closes back inside (`low < lvl and close > lvl`). It signals a liquidity grab / immediate reversal opportunity.
* **New Pool Signals (`🟢 BUY / 🔴 SELL`)**:
  Occur when a brand-new swing pivot is confirmed (8 bars after the high/low).
* **Why they appeared adjacent in the timeline**:
  - `09:45 IST`: Price swept old SSL pool `@ 24,020.50` (Bullish Reclaim Sweep marker plotted on bar `09:45 IST`).
  - The wick low of that 09:45 bar reached `24,010.55`.
  - At `10:25 IST` (8 bars later), that `24,010.55` low was confirmed as the start of a new `SSL-169` pool, triggering the `🟢 BUY SIGNAL`.
  - Both events are part of the same liquidity cycle: the sweep took old liquidity, and the swing low established the new baseline.

---

### 3. Data Provider Latency vs Zero-Delay Webhooks
* **Why third-party polling has delay**:
  Free Yahoo Finance (`^NSEI`) endpoints cache and throttle intraday data for Indian indices, causing 15-minute to multi-hour delays.
* **How Zero-Delay Webhook solves this**:
  TradingView executes Pine Script on real-time live market ticks. When the 5m bar closes, TradingView's Webhook sends the signal payload in **0 milliseconds** directly to the built-in Webhook receiver (`python run_scanner.py --webhook`), forwarding to both Telegram bots in `< 50ms`.

---

### 4. Signal Direction Mapping: Fade vs Magnet Mode
* **Default Mode (`BSL→SELL · SSL→BUY`)**:
  Fades the fresh swing: a new SSL below price indicates buyers defended the low → **BUY**. A new BSL above price indicates sellers capped the high → **SELL**.
* **Magnet Mode (`BSL→BUY · SSL→SELL`)**:
  Trades toward the newly formed liquidity pool as an upside/downside price target.
* Ensure `SIG_DIR` in `.env` matches the dropdown option in your TradingView indicator settings.

---

## 📡 Signal & Alert Parity Table

| Feature | TradingView Indicator (`abcd.txt`) | Scanner & Webhook Receiver |
|---|---|---|
| **ATR Period** | `ta.atr(14)` (Wilder's RMA of TR) | Wilder's RMA ATR(14) |
| **Pivot Detection** | `ta.pivothigh/low(high/low, 8, 8)` | Strict pivot confirmed after 8 bars (non-repainting) |
| **Equal Merge** | Swing within `eqTol` (`ATR × 0.15`) merges (touch +1, no alert) | Exact same tolerance merge logic |
| **SL / TP Math** | `Entry ∓ ATR × 1.2` / `Entry ± ATR × 1.2 × 2.0` (1:2 R:R) | Identical formula |
| **Nearest Target** | Nearest opposing pool (`nextBSL` / `nextSSL`) | Matches active pool memory |
| **Chart Anchor** | Plotted at `bar_index - pivLen` | Displayed explicitly in alert (`📍 Chart Anchor`) |
| **Alert Delivery** | Instant via Webhook (`alert()`) | Dual Telegram bots + persistent deduplication |

### 5. Cloud scanning (GitHub Actions) — optional, no server needed

`.github/workflows/scanner.yml` runs the scanner on GitHub's cloud, so the
**5m signals reach you even when your machine is off**. It runs **every
5 minutes, Mon–Fri, during NSE hours (09:15–15:30 IST)** and can also be
triggered manually (Actions tab → *NIFTY BSL/SSL Scanner* → **Run workflow**).

Setup:
1. Push this repo to GitHub and merge to `main`
   (**scheduled workflows only run from the default branch**).
2. Add **repository secrets** — *Settings → Secrets and variables → Actions*:

   | Secret | Meaning |
   |---|---|
   | `BOT1_TOKEN` | Telegram bot 1 token (from @BotFather) |
   | `BOT2_TOKEN` | Telegram bot 2 token |
   | `CHAT_ID` | chat/group/channel ID for bot 1 (and bot 2) |
   | `CHAT_ID_2` | optional — separate chat for bot 2 (defaults to `CHAT_ID`) |

   The names must match exactly — the workflow reads them by these names.
3. Done. Watch runs in the **Actions** tab; the first one appears within
   5 minutes during market hours.

How it works — **lookback mode** (`LOOKBACK_MINUTES=20`):
- Every Actions run is a **fresh machine**, so it cannot wait for new bars
  like the local scanner. Instead each run:
  1. restores `data/sent_alerts.json` from the **Actions cache**;
  2. downloads the full warm-up window (60 days of 5m bars) so the engine's
     pool state converges exactly like a live scanner;
  3. evaluates the BSL/SSL engine over all history, but **alerts only on
     closed bars inside the last 20 minutes**;
  4. saves the updated dedupe state back to the cache.
- Because every alert key is persisted in the cached state file, **no alert
  can ever be sent twice**, no matter how many fresh runs replay the same
  bars. Old keys are trimmed weekly-style by `scanner/prune_state.py` to keep
  the cache small.

> 📌 The cloud workflow is **5m-only** (GitHub's minimum schedule is 5
> minutes). Your **1m signals** still come from the local run
> (`python run_scanner.py` with `.env`) — both can run side by side with the
> same bots, and the shared no-duplicate guarantee still holds per run type.
>
> ⚠️ **yfinance** is free and unofficial; Yahoo can throttle — if you see
> repeated fetch errors, raise `SCAN_INTERVAL_SEC` (local) or accept that a
> throttled cloud run simply skips until the next 5-minute slot.

---

## 📱 Alert Formats

### 🟢 BUY SIGNAL (SSL Pool Start)
```
🟢 BUY SIGNAL — NSE:NIFTY (5m)
📌 Fresh SSL-169 pool start @ 24,010.55
💵 Entry: 24,061.05 (Closed Confirmation Bar)
🛑 SL: 24,038.43  ·  🎯 TP: 24,106.30 (1:2 R:R)
🎯 Nearest Target Pool: 24,114.00
📍 Chart Anchor (Swing LOW): 2026-09-01 09:45 IST
⚡ Swing confirmed 8 bars after actual LOW (non-repainting)
Bar: 2026-09-01 10:25 IST
```

### 🔴 SELL SIGNAL (BSL Pool Start)
```
🔴 SELL SIGNAL — NSE:NIFTY (5m)
📌 Fresh BSL-169 pool start @ 24,142.85
💵 Entry: 24,117.75 (Closed Confirmation Bar)
🛑 SL: 24,136.48  ·  🎯 TP: 24,080.29 (1:2 R:R)
🎯 Nearest Target Pool: 24,010.55
📍 Chart Anchor (Swing HIGH): 2026-09-01 11:30 IST
⚡ Swing confirmed 8 bars after actual HIGH (non-repainting)
Bar: 2026-09-01 12:10 IST
```

### 🧹 SSL SWEEP (Bullish Reclaim)
```
🧹 SSL SWEPT (Bullish Reclaim) — NSE:NIFTY (5m)
📈 Sell-side liquidity pool @ 24,020.50 was swept — bullish (price reclaimed the level)
Close 24,025.10 > level 24,020.50
📍 Chart Marker: ▲ Green Triangle below bar
Bar: 2026-09-01 09:45 IST
```

---

## 🚀 Quick Start

| Variable | Default | Meaning |
|---|---|---|
| `SYMBOL` / `DISPLAY_SYMBOL` | `^NSEI` / `NSE:NIFTY` | only the NIFTY index |
| `TIMEFRAMES` | `1m,5m` | comma-separated intervals |
| `SCAN_INTERVAL_SEC` | `20` | scan cadence |
| `MARKET_HOURS_ONLY` | `true` | scan only 09:15–15:30 IST Mon–Fri |
| `SESSION_START/END` | `09:15` / `15:30` | NSE session |
| `LOOKBACK_MINUTES` | `0` | cloud lookback: scan the last N minutes of closed bars and exit (0 = live mode; the Actions workflow sets `20`) |
| `SWEEP_ALERTS` | `true` | send separate 🧹 sweep alerts |
| `PIV_LEN` | `8` | swing pivot strength (bars each side) |
| `ATR_LEN` | `14` | ATR period |
| `ZONE_ATR_MULT` | `0.25` | zone thickness × ATR |
| `EQ_TOL_ATR` | `0.15` | equal H/L merge tolerance × ATR |
| `MAX_POOLS` | `12` | max live pools per side |
| `POOL_EXPIRY` | `300` | pool expiry in bars |
| `SIG_DIR` | `BSL→SELL · SSL→BUY` | or `BSL→BUY · SSL→SELL` (magnet) |
| `ATR_SL` / `RR_TARGET` | `1.2` / `2.0` | SL and TP multiples |

> ⚠️ **Settings must match your TradingView chart.** If you changed any input
> in the indicator's settings panel, set the same value in `.env` — otherwise
> signals will differ.

---

## ⚡ Deployment Modes

### Mode A: Zero-Delay TradingView Webhook (Recommended)
This mode connects directly to TradingView for **0ms latency**:
```bash
python run_scanner.py --webhook
```
abcd.txt                        # the original Pine Script (reference)
run_scanner.py                  # entry point
config.py                       # env-based configuration
selftest.py                     # offline engine parity tests
.github/workflows/scanner.yml   # cloud scanner (5m, Mon-Fri, NSE hours)
scanner/
  indicators/bsl_ssl.py         # 1:1 Python port of the indicator
  data/yfinance_feed.py         # yfinance feed (warm-up + incremental)
  data/mock.py                  # synthetic data for offline preview
  alerts/telegram.py            # dual-bot notifier
  live.py                       # scan loop, lookback, dedupe, message building
  state.py                      # persistent no-repeat dedupe
  prune_state.py                # trims old dedupe keys (cache hygiene)
```

### Mode C: Offline Validation & Preview
```bash
python selftest.py                  # Verify math, pivots, sweeps, dedupe & webhook
python run_scanner.py --dump-sample # Preview sample Telegram alerts
python run_scanner.py --mock        # Stream mock data
```

## 🔧 Troubleshooting

- **Actions run is green but no Telegram alert arrives**: check the run log
  for `timeframe 5m failed` / `yfinance fetch failed`. yfinance is unofficial
  and can be throttled — the run skips to the next 5-minute slot by design.
  Also confirm the four secrets (`BOT1_TOKEN`, `BOT2_TOKEN`, `CHAT_ID`,
  `CHAT_ID_2`) are set; if they are missing, alerts are only logged and are
  **not** marked as sent, so they will be delivered once the secrets are added.
- **Local scanner downloads bars once and then never sends again**: make sure
  the yfinance feed can refresh (check `logs/scanner.log` for fetch errors).
  The feed normalizes any yfinance column layout (TitleCase / MultiIndex
  `(Price, Ticker)`) and passes a datetime — not a string — to the incremental
  fetch, which yfinance 1.x requires.
- **Sanity-check the feed without Telegram**:
  `TELEGRAM_ENABLED=false python run_scanner.py --once` — signals that occur
  are printed as dry-run alerts.

---

## ⚠️ Notes & disclaimer

## 🔒 Strict Deduplication Guarantee
Every alert generates a persistent composite key (`symbol|tf|kind|bar_time|level`) recorded in `data/sent_alerts.json`. Alerts are delivered **exactly once** across server restarts.
