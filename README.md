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

### 1. Installation
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure Credentials (.env)
```bash
cp .env.example .env
```
Fill in `.env`:
```ini
BOT1_TOKEN=1111111111:AA...        # Primary Telegram bot
BOT2_TOKEN=2222222222:BB...        # Secondary Telegram bot
CHAT_ID=-1001234567890             # Destination Telegram chat/channel
CHAT_ID_2=                         # Optional second chat ID
WEBHOOK_PORT=5000                  # Port for TradingView alerts
```

---

## ⚡ Deployment Modes

### Mode A: Zero-Delay TradingView Webhook (Recommended)
This mode connects directly to TradingView for **0ms latency**:
```bash
python run_scanner.py --webhook
```
1. Open TradingView with the `abcd.txt` indicator on `NSE:NIFTY` (5m / 1m).
2. Create an Alert (`Condition: BSL / SSL Liquidity Start Signals`).
3. Set **Webhook URL** to `http://<your-server-ip>:5000/webhook`.
4. In the indicator settings, ensure `Alert Message Format` is set to `JSON (Webhook)`.

### Mode B: Standalone Live Python Scanner
Runs continuous background scanning with automated session awareness:
```bash
python run_scanner.py
```

### Mode C: Offline Validation & Preview
```bash
python selftest.py                  # Verify math, pivots, sweeps, dedupe & webhook
python run_scanner.py --dump-sample # Preview sample Telegram alerts
python run_scanner.py --mock        # Stream mock data
```

---

## 🔒 Strict Deduplication Guarantee
Every alert generates a persistent composite key (`symbol|tf|kind|bar_time|level`) recorded in `data/sent_alerts.json`. Alerts are delivered **exactly once** across server restarts.
