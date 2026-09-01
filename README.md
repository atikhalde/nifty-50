# BSL/SSL Liquidity Scanner — NSE:NIFTY (Zero Delay & 100% Chart Parity)

A high-precision Python scanner and Webhook receiver that replicates the TradingView Pine indicator
**"BSL / SSL Liquidity Start Signals — v5 LITE"** (`abcd.txt` in this repo) **100% 1:1**, scans **only the NSE:NIFTY index** (`^NSEI` via yfinance or direct TradingView webhook) on **1m & 5m** charts, and sends the **exact same BUY/SELL/Sweep signals** to **two Telegram bots** simultaneously.

---

## 📡 Indicator Engine vs Scanner Parity

| Feature | TradingView Pine Script (`abcd.txt`) | Scanner Python Engine (`scanner/`) |
|---|---|---|
| **ATR** | `ta.atr(14)` (Wilder RMA of True Range) | Exact `_wilders_rma` with $\alpha = 1/14$ |
| **Pivot Detection** | `ta.pivothigh(high, 8, 8)` / `ta.pivotlow(low, 8, 8)` | Strict pivot confirmed **8 bars later** (non-repainting) |
| **Multi-Speed TIER 3** | Standard `pivLen=8` labels + webhook | Original intact swing, macro `nextBSL`/`nextSSL` target tracking |
| **Multi-Speed TIER 2** | Fast `fastPivLen=3` labels + webhook (15m on 5m, 3m on 1m) | 62% faster entries, same ATR SL/TP, still aims at standard pools |
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
- 0 second delay (instant alerts)
- Exact TradingView price matching
```bash
python run_scanner.py --webhook
```

### Mode 2: Automated Live Scanner (yfinance + Dual Telegram)
Continuously polls Yahoo Finance for newly closed 1m/5m bars during NSE hours (09:15–15:30 IST).
```bash
python run_scanner.py
```

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
📍 Chart Anchor (Swing Low): 2026-09-01 07:55 IST
⚡ Swing confirmed 8 bars after actual LOW (non-repainting)
Bar: 2026-09-01 08:35 IST
```

### 🔴 SELL Signal (BSL Start)
```
🔴 SELL SIGNAL — NSE:NIFTY (5m)
📌 Fresh BSL-05 pool start @ 24,411.91
💵 Entry: 24,352.86 (Closed Confirmation Bar)
🛑 SL: 24,435.13  ·  🎯 TP: 24,188.31 (1:2 R:R)
🎯 Nearest pool: 24,164.77
📍 Chart Anchor (Swing High): 2026-09-01 02:20 IST
⚡ Swing confirmed 8 bars after actual HIGH (non-repainting)
Bar: 2026-09-01 03:00 IST
```

### 🧹 Liquidity Sweep Alert
```
🧹 SSL SWEPT (Bullish Reclaim) — NSE:NIFTY (5m)
📈 Sell-side liquidity pool @ 24,020.50 was swept — bullish (price reclaimed)
Close 24,025.10 > level 24,020.50
📍 Chart Marker: ▲ Green Triangle below bar
Bar: 2026-09-01 09:45 IST
```

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
PIV_LEN=8
ATR_LEN=14
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

## 🧪 Parity Self-Test

Run the offline verification suite:
```bash
python selftest.py
python run_scanner.py --dump-sample
```
