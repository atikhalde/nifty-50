# BSL/SSL Liquidity Scanner — NSE:NIFTY (High-Accuracy Chart Parity)

A high-precision Python scanner and Webhook receiver that replicates the TradingView Pine indicator
**"BSL / SSL Liquidity Start Signals — v5 LITE"** (`abcd.txt` in this repo) with a canonical payload shared by the chart, webhook, and Yahoo comparison paths, scans **only the NSE:NIFTY index** (`^NSEI` via yfinance or direct TradingView webhook) on **1m & 5m** charts, and sends the **exact same BUY/SELL/Sweep signals** to **two Telegram bots** simultaneously.

---

## 📡 Indicator Engine vs Scanner Parity

| Feature | TradingView Pine Script (`abcd.txt`) | Scanner Python Engine (`scanner/`) |
|---|---|---|
| **ATR** | `ta.atr(14)` (Wilder RMA of True Range) | Exact `_wilders_rma` with $\alpha = 1/14$ |
| **Pivot Detection** | `ta.pivothigh(high, 4, 4)` / `ta.pivotlow(low, 4, 4)` | Strict pivot confirmed **4 bars later** (non-repainting) |
| **Multi-Speed TIER 3** | Standard `pivLen=4` labels + webhook | Original intact swing, macro `nextBSL`/`nextSSL` target tracking |
| **Multi-Speed TIER 2** | Fast `fastPivLen=3` labels + webhook (15m on 5m, 3m on 1m) | 25% faster than the 4-bar default, same ATR SL/TP, still aims at standard pools |
| **Multi-Speed TIER 1** | Instant sweep labels on the sweep candle (0-bar lag) | Wick SL + 1:2 R:R TP, executed on sweep close |
| **Pool Creation** | Fresh swing high $\rightarrow$ BSL, Fresh swing low $\rightarrow$ SSL | Identical; equal swings within `eqTol` merge into existing pool without firing a signal |
| **Pool Lifecycle** | Sweep (`high > lvl && close < lvl` / `low < lvl && close > lvl`), Touch, Expiry (300 bars), Max 12 pools/side | Same order per bar: create $\rightarrow$ sweep $\rightarrow$ touch $\rightarrow$ expiry $\rightarrow$ signal |
| **Signal Direction** | Default Fade: BSL $\rightarrow$ **SELL**, SSL $\rightarrow$ **BUY** (Magnet: BSL $\rightarrow$ **BUY**, SSL $\rightarrow$ **SELL**) | Configurable via `SIG_DIR` in `.env` |
| **SL / TP Math** | `close ∓ atr × 1.2` / `close ± atr × 1.2 × 2.0` | Exact formula using confirmation bar `close` and `atr` |
| **Timestamps** | Label at Swing Bar (`bar_index - pivLen`), Alert at Confirmation / Sweep Bar | Telegram + webhook show **BOTH** for all 3 tiers: Chart Anchor & Execution Bar |

---

## 🚀 Modes of Operation

### Mode 1: Zero-Delay Direct TradingView Webhook (Recommended)
TradingView alerts send webhook requests instantly to the scanner when a bar closes.
TradingView and Yahoo alerts are source-tagged separately. Both may deliver an alert, while the persisted `source_events` ledger marks matching events as confirmed and exposes numeric conflicts instead of silently merging them.
- 0 second delay (instant alerts)
- Exact TradingView price matching for webhook-originated alerts; Yahoo remains feed-dependent
- In TradingView, create the alert with **Condition → Any alert() function call** and
  `Once Per Bar Close` to receive the rich JSON payload. The named `alertcondition`
  entries are lightweight manual conditions and do not carry the numeric JSON fields.
```bash
python run_scanner.py --webhook
```

### Mode 2: Automated Live Scanner (yfinance + Dual Telegram)
Continuously polls Yahoo Finance for newly closed 1m/5m bars during NSE hours (09:15–15:30 IST).
```bash
python run_scanner.py
```

To operate both feeds, run the webhook listener and the live scanner as
separate processes with the same `STATE_FILE`. The persistent ledger uses a
file lock and merges sent keys/correlation events, so the sources can alert
independently without overwriting each other.

### Mode 3: GitHub Actions Cloud Runner
Runs periodically via scheduled workflow in `.github/workflows/scanner.yml` with state persistence.
```bash
python run_scanner.py --lookback 20
```

---

## 📱 Telegram Alert Formats

### 🟢 BUY Signal (SSL Start)
```
🟢 BUY SIGNAL — NSE:NIFTY (5m)
📌 Fresh SSL-06 pool start @ 24,189.27
💵 Entry: 24,228.74 (Closed Confirmation Bar)
🛑 SL: 24,156.67  ·  🎯 TP: 24,372.88 (1:2 R:R)
🎯 Nearest pool: 24,411.91
📍 Chart Anchor (Swing Low): 2026-09-01 08:15 IST
⚡ Swing confirmed 4 bars after actual LOW (non-repainting)
🕒 Execution Bar: 2026-09-01 08:35 IST
Bar: 2026-09-01 08:35 IST
```

### 🔴 SELL Signal (BSL Start)
```
🔴 SELL SIGNAL — NSE:NIFTY (5m)
📌 Fresh BSL-05 pool start @ 24,411.91
💵 Entry: 24,352.86 (Closed Confirmation Bar)
🛑 SL: 24,435.13  ·  🎯 TP: 24,188.31 (1:2 R:R)
🎯 Nearest pool: 24,164.77
📍 Chart Anchor (Swing High): 2026-09-01 02:40 IST
⚡ Swing confirmed 4 bars after actual HIGH (non-repainting)
🕒 Execution Bar: 2026-09-01 03:00 IST
Bar: 2026-09-01 03:00 IST
```

### Strict no-duplicate guarantee
Every alert gets a source-specific unique key (`source|symbol|tf|kind|bar-time|level`) persisted to
`data/sent_alerts.json`. A signal is sent **exactly once per source** — never repeated
by that source, even across scanner restarts. The `source_events` ledger correlates
TradingView and Yahoo alerts and marks matching values as confirmed or conflicting.
Failed deliveries are retried until delivered.

---

## ⚙️ Configuration (.env)

| Variable | Default | Meaning |
|---|---|---|
| `SYMBOL` / `DISPLAY_SYMBOL` | `^NSEI` / `NSE:NIFTY` | only the NIFTY index |
| `TIMEFRAMES` | `1m,5m` | comma-separated intervals |
| `SCAN_INTERVAL_SEC` | `20` | scan cadence |
| `MARKET_HOURS_ONLY` | `true` | scan only 09:15–15:30 IST Mon–Fri |
| `SESSION_START/END` | `09:15` / `15:30` | NSE session |
| `SWEEP_ALERTS` | `true` | send separate 🧹 sweep alerts |
| `MAX_ALERT_AGE_MIN` | `10` | never alert on closed bars older than this (live mode; 0 = off) |
| `PENDING_MAX_AGE_MIN` | `30` | retry failed Telegram deliveries this long, then drop |
| `WEBHOOK_HOST` / `WEBHOOK_PORT` / `WEBHOOK_SECRET` | `0.0.0.0` / `5000` / — | webhook receiver binding + shared secret |
| `PIV_LEN` | `4` | swing pivot strength (bars each side); signal fires 4 bars after swing |
| `ALERT_SOURCE` | `YAHOO` | source tag for the live path; webhook is always `TRADINGVIEW` |
| `ATR_LEN` | fixed `14` | Pine uses `ta.atr(14)`; not a configurable chart input |
| `ZONE_ATR_MULT` | `0.25` | zone thickness × ATR |
| `EQ_TOL_ATR` | `0.15` | equal H/L merge tolerance × ATR |
| `MAX_POOLS` | `12` | max live pools per side |
| `POOL_EXPIRY` | `300` | pool expiry in bars |
| `SIG_DIR` | `BSL→SELL · SSL→BUY` | or `BSL→BUY · SSL→SELL` (magnet) |
| `ATR_SL` / `RR_TARGET` | `1.2` / `2.0` | SL and TP multiples |
| `SCANNER_TZ` | `Asia/Kolkata` | exchange timezone — **overrides `TZ`** |

> ⏰ **Timezone gotcha.** Many Docker images and CI runners export `TZ=UTC`,
> which would shift the whole 09:15–15:30 session window by 5h30m and make the
> scanner think the market is closed all day. Prefer `SCANNER_TZ=Asia/Kolkata`;
> it always wins over `TZ`. The scanner warns at startup if the two disagree.

Every value is parsed defensively: a typo (`SCAN_INTERVAL_SEC=abc`, an
unsupported timeframe, `9:15` instead of `09:15`) logs a warning and falls back
to the documented default instead of crashing during market hours.

> ⚠️ **Settings must match your TradingView chart.** If you changed any input
> in the indicator's settings panel, set the same value in `.env` — otherwise
> signals will differ.

---

## ⚙️ Configuration (`.env`)

```ini
# Telegram Tokens
BOT1_TOKEN=1111111111:AA...
BOT2_TOKEN=2222222222:BB...
CHAT_ID=-1001234567890
CHAT_ID_2=

# Webhook Server
WEBHOOK_HOST=0.0.0.0
WEBHOOK_PORT=5000
WEBHOOK_SECRET=

# Market Parameters
SYMBOL=^NSEI
DISPLAY_SYMBOL=NSE:NIFTY
TIMEFRAMES=1m,5m
MARKET_HOURS_ONLY=true
SESSION_START=09:15
SESSION_END=15:30
TZ=Asia/Kolkata

# Indicator Settings
PIV_LEN=4
ALERT_SOURCE=YAHOO
# ATR is fixed at 14 to match ta.atr(14)
ZONE_ATR_MULT=0.25
EQ_TOL_ATR=0.15
MAX_POOLS=12
POOL_EXPIRY=300
SIG_DIR=BSL→SELL · SSL→BUY
ATR_SL=1.2
RR_TARGET=2.0
SWEEP_ALERTS=true
```

---

## ✅ Pre-market checklist

Run this a few minutes before 09:15 IST — it catches every failure mode that
would otherwise surface as a missed signal:

```bash
python selftest.py                 # 18 checks: engine parity + live hardening
python run_scanner.py --once       # verifies feed + Telegram tokens, sets baseline
tail -f logs/scanner.log
```

`--once` pings Telegram's `getMe` for both bots, so a bad token or wrong
`CHAT_ID` is reported immediately instead of at the first live signal.

### How the scanner behaves when things go wrong

| Failure | Behaviour |
|---|---|
| Yahoo down / network blip | 3 retries with backoff, then the last good frame is reused; the cycle never dies |
| Feed frozen during session | Logs a loud `feed appears STALE` warning |
| Corrupt bars (NaN, zero, high<low) | Dropped before they reach the engine |
| Telegram 429 rate limit | Honours `retry_after`, then retries |
| Telegram delivery fails | Alert is **not** marked sent — retried next cycle |
| Bad token / wrong chat | Logged as a config error (no pointless retries) |
| Corrupt or tz-naive state file | Re-baselines instead of crashing |
| Long outage / big backlog | Only the newest 5 closed bars alert — no burst of stale signals |
| Scanner restart | Dedupe ledger persists; a delivered alert never repeats |

---

## ⚠️ Notes & disclaimer

Run the offline verification suite:
```bash
python selftest.py
python run_scanner.py --dump-sample
```
