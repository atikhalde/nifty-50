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
even across scanner restarts. Failed deliveries are retried until delivered.

---

## ⚙️ Configuration (.env)

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

## 📁 Project layout

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

---

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

- **yfinance** is free and unofficial; Yahoo can throttle — if you see
  repeated fetch errors, raise `SCAN_INTERVAL_SEC`.
- The engine is validated by `selftest.py` against hand-traced expectations of
  the Pine logic (pivot timing, merge, sweep, expiry, magnet, SL/TP, dedupe).
- Intraday bars from yfinance are filtered to the NSE session
  `09:15–15:30` to match TradingView.
- **Educational use only.** Not investment advice. Trading involves risk.
