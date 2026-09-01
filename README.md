# BSL/SSL Liquidity Scanner — NSE:NIFTY

A live Python scanner that replicates the TradingView Pine indicator
**"BSL / SSL Liquidity Start Signals — v5 LITE"** (`abcd.txt` in this repo)
**1:1**, scans **only the NSE:NIFTY index** (`^NSEI` via yfinance) on
**1m & 5m** charts, and sends the **exact same BUY/SELL signals** to **two
Telegram bots** (both receive every alert).

---

## 📡 What the indicator does (and what the scanner replicates exactly)

| Piece | Pine script | This scanner |
|---|---|---|
| ATR | `ta.atr(14)` (Wilder RMA of TR) | `scanner/indicators/bsl_ssl.py` |
| Swing detection | `ta.pivothigh/low(_, 8, 8)` | strict pivot, confirmed **8 bars later** (non-repainting) |
| Pool creation | fresh swing high → BSL, fresh swing low → SSL | same, incl. **equal-merge** within `eqTol` (no signal) |
| Pool lifecycle | sweep (`high>lvl and close<lvl` / `low<lvl and close>lvl`), touch, expiry **300 bars**, max **12**/side | same, same order per bar (create → sweep → touch → expiry → signal) |
| Signal mapping | **default fade**: new BSL → **SELL**, new SSL → **BUY** | same (magnet `BSL→BUY · SSL→SELL` available in `.env`) |
| SL / TP | `close ∓ atr×1.2` / `close ± atr×1.2×2.0` | same |
| Alert timing | `freq_once_per_bar_close` | one alert per signal bar, **strictly deduplicated forever** |

Signals fire on the **confirmation bar** (8 bars after the actual swing),
exactly like the indicator's alert — **no repainting**.

---

## 🚀 Quick start

### 1. Install

```bash
python3 -m venv .venv
. .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure Telegram

```bash
cp .env.example .env
```

Edit `.env` and fill in:

```ini
BOT1_TOKEN=1111111111:AA...        # from @BotFather
BOT2_TOKEN=2222222222:BB...        # your second bot
CHAT_ID=-1001234567890             # your chat/group/channel ID
CHAT_ID_2=                         # optional: chat for bot 2 (defaults to CHAT_ID)
```

`.env` is git-ignored — your tokens never enter the repo.

### 3. Sanity-check offline (no network / no Telegram needed)

```bash
python selftest.py                          # engine parity tests
python run_scanner.py --dump-sample         # preview exact Telegram messages
python run_scanner.py --mock                # streaming demo on synthetic data
```

### 4. Run live

```bash
python run_scanner.py
```

The scanner:
- warms up with ~7 days of 1m bars / 60 days of 5m bars so the pool state
  converges to your TradingView chart, then sets a baseline (no history spam);
- checks every **20s** (configurable) for newly closed 1m/5m bars;
- evaluates the BSL/SSL engine and sends **BUY / SELL / sweep** alerts to
  **both bots** as soon as a bar closes;
- runs only during NSE hours `09:15–15:30 IST`, Mon–Fri
  (`MARKET_HOURS_ONLY=false` to disable).

---

## 📱 Alert formats

**BUY (SSL start):**
```
🟢 BUY SIGNAL — NSE:NIFTY (5m)
📌 Fresh SSL-05 pool start @ 24,310.00
Entry: 24,280.00
SL: 24,205.00  ·  TP: 24,430.00
🎯 Nearest pool: 24,300.00
Swing confirmed 8 bars after the actual LOW (non-repainting)
Bar: 2026-09-01 14:35 IST
```

**SELL (BSL start):** 🔴 identical structure, SL/TP mirrored.

**Sweeps** (separate alert type, `SWEEP_ALERTS=true`):
```
🧹 SSL SWEPT — NSE:NIFTY (5m)
📈 Sell-side liquidity pool @ 24,150.00 was swept — bullish (price reclaimed the level)
Bar: 2026-09-01 14:40 IST
```

### Strict no-duplicate guarantee
Every alert gets a unique key (`symbol|tf|kind|bar-time|level`) persisted to
`data/sent_alerts.json`. A signal is sent **exactly once** — never repeated,
even across scanner restarts. Failed deliveries are queued and retried each
cycle for up to `PENDING_MAX_AGE_MIN` (default 30 min); after that they are
dropped with a warning — a signal that stale is more dangerous than a missed
one in live markets.

### Live-market safety guards
- **Stale-bar guard** (`MAX_ALERT_AGE_MIN`, default 10): after a restart with
  an old state file or a data outage, catch-up bars older than this are
  evaluated but **never alerted** — no spam of untradable old signals.
- **Fail-safe feed**: if Yahoo throttles or the network blips, the last good
  cached bars are used and the cycle continues — one bad fetch never crashes
  the scanner or a scan cycle.
- **Closed bars only**: a bar is processed only once `bar_time + interval <= now`
  (mirrors `alert.freq_once_per_bar_close`), and yfinance's stray `15:30`
  auction bar is dropped to match TradingView's session.

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
| `MAX_ALERT_AGE_MIN` | `10` | never alert bars older than this (restart/outage guard) |
| `PENDING_MAX_AGE_MIN` | `30` | retry window for failed Telegram deliveries |
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

## 📁 Project layout

```
abcd.txt                        # the original Pine Script (reference)
run_scanner.py                  # entry point
config.py                       # env-based configuration
selftest.py                     # offline engine parity tests
scanner/
  indicators/bsl_ssl.py         # 1:1 Python port of the indicator
  data/yfinance_feed.py         # yfinance feed (warm-up + incremental)
  data/mock.py                  # synthetic data for offline preview
  alerts/telegram.py            # dual-bot notifier
  live.py                       # scan loop, dedupe, message building
  state.py                      # persistent no-repeat dedupe
```

---

## ⚠️ Notes & disclaimer

- **yfinance** is free and unofficial; Yahoo can throttle — if you see
  repeated fetch errors, raise `SCAN_INTERVAL_SEC`.
- The engine is validated by `selftest.py` against hand-traced expectations of
  the Pine logic (pivot timing, merge, sweep, expiry, magnet, SL/TP, dedupe).
- Intraday bars from yfinance are filtered to the NSE session
  `09:15–15:30` to match TradingView.
- **Educational use only.** Not investment advice. Trading involves risk.
