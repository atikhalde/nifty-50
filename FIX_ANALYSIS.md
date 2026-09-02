# Deep Analysis: Indicator Chart vs Scanner Telegram Mismatch

## Current implementation — Fix Round 3 (2026-09-02)

This round follows the user-approved configuration: **standard pivots use
`pivLen=4`**, the separate fast tier remains `fastPivLen=3`, and both source
paths may send alerts. TradingView and Yahoo are source-tagged independently;
they are correlated rather than silently treated as identical feeds.

### Repairs made

1. **Restored the Pine source as a single valid alert path** — repaired the
   missing SSL `box.set_right` line, removed the orphaned fast-alert fragment,
   restored both FAST JSON/human alert blocks, removed duplicate trailing sweep
   alerts, and converted the JSON construction to valid escaped Pine strings.
2. **Made the standard confirmation four bars** — `abcd.txt`, Python defaults,
   examples, and tests now use `pivLen=4` (four bars on each side of the pivot;
   alert on the fourth confirmation bar). ATR remains fixed at `ta.atr(14)`.
3. **Fixed fast magnet target parity** — a fast magnet event uses Pine's
   standard `buyTgt`/`sellTgt` expression (`newBSLlvl`/`newSSLlvl`) rather than
   an unrelated nearest pool or fast pivot level.
4. **Made pool-side semantics explicit** — TradingView JSON now carries
   `pool_side`, so magnet BUY/SELL messages correctly describe HIGH/LOW anchors.
5. **Added source-aware dedupe and reconciliation** — Yahoo keys are prefixed
   `YAHOO`, TradingView webhook keys are prefixed `TRADINGVIEW`; the persistent
   `source_events` ledger reports `Cross-source confirmation` or
   `Source disagreement in: ...` while allowing both sources to alert. The
   shared state file is atomically locked/merged for concurrent live and webhook
   processes, and same-process webhook workers claim keys before sending.
6. **Aligned session filtering** — regular-session bars use `09:15 <= time <
   15:30`, including the 15:25 bar and excluding the 15:30 start.

### Important operating boundary

The TradingView webhook carries the exact chart-side OHLC-derived values. The
Yahoo path independently recomputes the engine from `^NSEI`; it can still
legitimately disagree when candle OHLC, history length, timestamps, or pool
sequence differs. In that case both source-tagged alerts remain visible and the
state ledger identifies the differing numeric fields. Exact pool IDs require
the same history or the TradingView payload.

### Verification status

Python syntax compilation, static source checks, and the complete self-test pass.
The self-test was run in an isolated environment with `requirements.txt` and
reported `ALL CHECKS PASSED`; the offline mock sample also ran successfully.
Repeat locally with:

```bash
python -m compileall -q .
python selftest.py
python run_scanner.py --mock --dump-sample
```

The historical Fix Round 2 notes below describe the earlier repair and are kept
for traceability.

## Fix Round 2 — merge-conflict repair & runtime errors (2026-09-01)

The merge in PR #15 left the tree in a half-merged state: the scanner could
not start at all (`python run_scanner.py` raised `TypeError` immediately).
Root causes found by deep analysis, all fixed:

1. **`LiveScanner.__init__` signature mangled by the merge** — the body read
   `lookback_minutes` / `market_check`, but neither was a parameter. Every
   launch died with `TypeError: unexpected keyword argument`. Both parameters
   restored (with safe defaults).
2. **`_process_new_closed_bars` structure broken** — in lookback mode
   `last_ev` was undefined (`NameError`), and the lookback `new_bars` was
   overwritten by a dead `threshold` filter below it. Restructured: lookback
   mode slices purely by window; incremental mode does baseline → threshold →
   stale-age guard → backlog cap, and advances `last_evaluated` past bars it
   deliberately skips (no re-log spam).
3. **`_emit_bar` called without its `df` argument** (`TypeError`) — needed for
   the Chart-Anchor timestamps. Fixed at the call site.
4. **Retry queue half-merged** — `live.py` called `state.add_pending()` /
   `state.drop_pending()` and `selftest.py` tested them, but `state.py` had
   neither method, and `mark()` never cleared the queue. Added both;
   `mark()` now pops the pending entry; `_emit_bar` queues failed deliveries
   so "retried until delivered" actually holds.
5. **Config keys documented but never read** — `WEBHOOK_HOST/PORT/SECRET`,
   `MAX_ALERT_AGE_MIN`, `PENDING_MAX_AGE_MIN` were in `.env.example` but
   missing from `ScannerConfig` (a custom `WEBHOOK_PORT` was silently
   ignored; `MAX_ALERT_AGE_MIN` was set by `run_scanner.py` onto a field that
   did not exist). All added with defensive parsing, and the
   `MAX_ALERT_AGE_MIN` guard is now actually enforced (live mode only — the
   lookback window bounds itself). `LOOKBACK_MINUTES=abc` no longer crashes.
6. **`--once` idled outside market hours** — it is a diagnostic cycle
   (README pre-market checklist) and now always runs once.
7. **Webhook receiver swallowed delivery failures with HTTP 200** —
   TradingView would never retry. It now answers 503 on failure and leaves
   the key unmarked, so a successful retry cannot duplicate.
8. **Feed re-downloaded a full month of bars every cycle** — added the
   incremental `_refresh()` (datetime `start`, never combined with `period`,
   one bar of overlap so Yahoo can revise the previously-open bar).
9. **`selftest.py` promised checks that did not exist** — the webhook
   formatter test, TIER 2 fast test and TIER 1 instant test were missing
   (WebhookFormatter was imported and unused); two orphan tests targeted a
   `_refresh` API that did not exist and called `_normalise` unbound. All
   fixed/added — the suite is now 18 checks, all passing.
10. **Pending CI patch applied** — `patches/0001-…` (market-hours gate that
    never gated + dedupe cache pinned to the first snapshot) is now applied
    to `.github/workflows/scanner.yml`; `patches/` removed as its README
    instructed.

Verified: `python selftest.py` (18/18 ✅), `--mock --once`, `--mock`
(continuous), `--mock --lookback`, `--dump-sample` (default + magnet
`SIG_DIR`), webhook server end-to-end (401/200/duplicate/413), corrupt-state
recovery, `prune_state.py`, and graceful no-network live runs. `pyflakes`
clean.

## Problem Reported
User observed:
- Chart indicator (Pine v5 `BSL / SSL Liquidity Start Signals — v5 LITE` in `abcd.txt`) shows BUY/SELL signals
- Scanner Telegram alerts also show BUY/SELL but not 100% matching
- Example timeline (5m NIFTY):
  - 09:45 SSL SWEPT @ 24,020.50
  - 10:25 BUY SIGNAL Fresh SSL-169 @ 24,010.55 Entry 24,061.05 SL 24,038.43 TP 24,106.30 Nearest 24,114.00
  - 11:35 BSL SWEPT @ 24,114.00
  - 12:10 SELL SIGNAL Fresh BSL-169 @ 24,142.85 Entry 24,117.75 SL 24,136.48 TP 24,080.29 Nearest 24,010.55

Signals are directionally correct but values/timing conflict. Telegram should exactly match chart.

## Root Causes Found

### 1. Outdated Branch (Major)
Current branch `arena/01a05bf8-nifty-50` was based on an old commit before magnet fix.
- `scanner/live.py` in old branch had `_level_of` and `_build_signal_msg` that **ignored magnet mode** (`SIG_DIR`)
- If TradingView chart set to `BSL→BUY · SSL→SELL` (magnet) but scanner used default, BUY/SELL would be flipped
- Main branch already fixed this, but current branch still had bug
- **Fix**: Restored `scanner/live.py` from main and improved further

### 2. Missing Data Feed Files
Branch was missing `scanner/data/yfinance_feed.py`, `scanner/data/mock.py`, `scanner/data/__init__.py`, `scanner/prune_state.py`, `.github/workflows/scanner.yml`
- Scanner couldn't run live without yfinance feed
- **Fix**: Restored from main

### 3. Telegram Message Didn't Include Actual Swing Time
Pine script draws label at **actual swing bar** (`bar_index - pivLen`) but fires alert at **confirmation bar** (`bar_index`).
- Old telegram only showed confirmation bar time: `Bar: 2026-09-01 10:25 IST`
- Chart shows label at 09:45 (actual LOW) but signal fires at 10:25
- User perceives mismatch because chart label position vs telegram bar time differ
- **Fix**: Telegram now shows BOTH:
  - `Bar: 2026-09-01 10:25 IST` (confirmation, when alert fires)
  - `Actual LOW: 2026-09-01 09:45 IST` (where label is drawn)
  - This makes the chart anchor and execution timing traceable to the chart

### 4. Magnet Handling in Message Builder
Old `_build_signal_msg`:
```python
if side == "BUY":
    pool_name = row["new_ssl_name"]  # always SSL, even in magnet mode
```
In magnet mode, BUY should come from BSL (new BSL pool), SELL from SSL.
- **Fix**: Now checks `self.params.magnet` and picks correct pool:
  - Default fade: BUY <- SSL, SELL <- BSL, BUY target = nextBSL, SELL target = nextSSL
  - Magnet: BUY <- BSL, SELL <- SSL, target = pool itself (Pine's buyTgt/sellTgt logic)

### 5. Session Filter Bug
Old filter:
```python
mask = df.index.to_series().apply(lambda ts: s <= ts.strftime("%H:%M") <= e)
```
- Included bar starting at 15:30 (ends 15:35, outside NSE)
- String comparison fragile
- **Fix**: Proper time comparison `s_t <= t < e_t`, so 15:25 included, 15:30 excluded, matching TradingView

### 6. Formatting vs Pine Exact Values
- Pine uses `format.mintick` (tick precision)
- Scanner used `_fmt_inr` with Indian grouping and 2 decimals — values same, formatting slightly different but acceptable
- More important: **values** must match — ATR, entry, SL, TP, nearest pool
- Engine follows the Pine contract:
  - ATR = Wilder RMA of TR (same as `ta.atr(14)`)
  - Pivots = strict `>` on both sides, confirmed `pivLen` bars later (non-repainting)
  - Pool lifecycle order: create BSL -> create SSL -> sweep BSL -> sweep SSL -> touch -> expiry -> signal (same as Pine)
  - SL/TP = `close ∓ atr*1.2` / `close ± atr*1.2*2.0` (same)
  - Nearest pool = `min(lv > close)` / `max(lv < close)` after sweep (same as Pine's `f_nearestAbove/Below`)
- **Fix**: Added extensive docstrings and comments explaining parity, and ensured `_build_signal_msg` uses same logic for target

### 7. Data Source Difference (Unavoidable but Documented)
- TradingView NSE:NIFTY feed vs yfinance ^NSEI may have slightly different OHLC due to exchange, adjustments, missing bars
- yfinance 5m limited to 60 days, 1m to 7 days -> pool IDs (e.g., SSL-169) will diverge from long-history TradingView chart, but signal state can only converge after a matching history window
- **Fix**: Documented in `live.py` header and README, and ensured warm-up uses full 60d/7d window so pool state converges

## Fixes Applied

### `scanner/live.py` (Complete Rewrite)
- Restored from main + improved:
  - `_process_new_closed_bars` now computes `actual_ts = df.index[pos - piv_len]` (Pine's `bar_index - pivLen`)
  - `_emit_bar` signature extended to accept `actual_ts` and `df`
  - `_build_signal_msg` now:
    - Handles magnet correctly (pool mapping, swing_word HIGH/LOW)
    - Shows both confirmation bar time and actual swing time
    - Shows target pool correctly (nearest for default, itself for magnet)
    - Format matches Pine label + alert combined
  - `_build_sweep_msg` now includes close vs level detail and matches Pine's bullish/bearish tone
  - Added detailed parity comments

### `scanner/data/yfinance_feed.py`
- Improved `_session_filter` to use proper `datetime.time` comparison, exclude 15:30 start, include 15:25

### `run_scanner.py`
- Fixed `--dump-sample` to pass `actual_ts` and `bar` to new message builders

### Restored Missing Files
- `scanner/data/__init__.py`, `mock.py`, `yfinance_feed.py`
- `scanner/prune_state.py`
- `.env.example`, `config.py` (with lookback_minutes)
- `.github/workflows/scanner.yml`

## Verification

```bash
python selftest.py  # current suite passes all parity and hardening checks
python run_scanner.py --mock --dump-sample  # shows new format with actual swing time
SIG_DIR="BSL→BUY · SSL→SELL" python run_scanner.py --mock --dump-sample  # magnet correctly flips
python run_scanner.py --mock --lookback 20 --verbose  # lookback mode works
```

Sample new telegram (default fade):
```
🟢 BUY SIGNAL — NSE:NIFTY (5m)
📌 Fresh SSL-06 pool start @ 24,189.27
Entry: 24,228.74
SL: 24,156.67  ·  TP: 24,372.88
🎯 Nearest pool: 24,411.91
Swing confirmed 4 bars after the actual LOW (non-repainting)
Bar: 2026-09-01 08:35 IST
Actual LOW: 2026-09-01 07:55 IST
```

Sample magnet:
```
🟢 BUY SIGNAL — NSE:NIFTY (5m)
📌 Fresh BSL-05 pool start @ 24,411.91
Entry: 24,352.86
SL: 24,270.59  ·  TP: 24,517.41
🎯 Target pool: 24,411.91
Swing confirmed 4 bars after the actual HIGH (non-repainting)
Bar: 2026-09-01 03:00 IST
Actual HIGH: 2026-09-01 02:20 IST
```

Now Telegram uses the same canonical fields as the chart indicator:
- Pool name & level (actual swing high/low)
- Entry = close of confirmation bar
- SL/TP from ATR at confirmation bar
- Nearest/target pool = Pine's buyTgt/sellTgt
- Swing confirmation text with correct HIGH/LOW based on mapping
- Both actual swing bar time (where label drawn) and confirmation bar time (where alert fires)

## Recommendations for high-accuracy parity

1. **Settings must match**: In TradingView indicator inputs, note PIV_LEN, ZONE_ATR_MULT, EQ_TOL_ATR, MAX_POOLS, POOL_EXPIRY, ATR_SL, RR_TARGET, SIG_DIR. Set same values in `.env` (see `.env.example`)
2. **Use 5m timeframe**: Example used 5m, scanner default TIMEFRAMES=1m,5m — ensure both chart and scanner on same TF
3. **Understand ID divergence**: Pool IDs (BSL-169) are sequence numbers from start of data. TradingView chart with months of history will have higher IDs than scanner with 60d warm-up, but signals (entry/SL/TP) still match after expiry window
4. **Check data feed**: If yfinance and TradingView OHLC differ by few paise, SL/TP will differ slightly — unavoidable, but logic identical
5. **No repaint**: Both chart and scanner fire signals only on bar close, 4 bars after actual swing by default — non-repainting confirmed
