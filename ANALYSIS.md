# Why chart signals and Telegram BUY/SELL alerts did not match 100%

Deep analysis of the Pine indicator (`abcd.txt`) vs the live scanner
(`scanner/indicators/bsl_ssl.py` + `scanner/live.py` + feed + Telegram path), with
measured reproductions. Verdict first, details below.

| # | Root cause | Symptom | Status |
|---|---|---|---|
| A | `scanner/data/` was never committed (`.gitignore` `data/` swallowed it) | fresh clone: `ModuleNotFoundError`, scanner can't even start | **FIXED** |
| B1 | Pivot **tie rule** wrong (strict on both sides vs TradingView's asymmetric rule) | **~25–30% of chart signals never alerted** + phantom extras + pool-id drift | **FIXED** |
| B2 | Magnet-mode alert text quoted the **opposite side's pool** (`nan` level) | broken BUY/SELL text when `SIG_DIR=BSL→BUY · SSL→SELL` | **FIXED** |
| B3 | Alert marked "sent" when **any one** bot delivered | if bot2 failed once, it **never** got that alert, ever | **FIXED** |
| B4 | Market gate cut off **exactly** at 15:30:00 | signals on the day's last 1m/5m bar arrived **next morning** | **FIXED** |
| C | Yahoo `^NSEI` data ≠ TradingView NSE data | residual paise/tick-level flips, timing lag | inherent — mitigated |
| D | Pine label is **backdated** 8 bars; baseline skip; sweep alerts; settings drift | things that *look* like mismatches but are by design | explained below |

All numbers in §B1 were measured with a reference implementation of TradingView's
exact pivot algorithm on tie-prone synthetic data (0.05 ticks, NIFTY-like).

---

## A. Blocking defect — the committed repo could not run

`run_scanner.py` imports `scanner.data.mock.MockFeed`; `scanner/live.py` imports
`scanner.data.yfinance_feed.YFinanceFeed`. That package **did not exist in git**:

```
$ git ls-files            # no scanner/data/* at all
ModuleNotFoundError: No module named 'scanner.data'
```

Cause: `.gitignore` contained `logs/` and `data/`. A gitignore pattern without a
leading slash matches **at any depth**, so `data/` ignored not only the runtime
`./data/` (intended — it holds `sent_alerts.json`) but also the source package
`scanner/data/`. The developer's local copy had the files; every clone broke.

Fix: anchored patterns `/data/` and `/logs/`, and recreated
`scanner/data/{__init__,yfinance_feed,mock}.py` per the README contract
(7d warm-up for 1m / 60d for 5m+, session filtering, `INTERVAL_DELTA`, `MockFeed`).

---

## B. Engine/logic divergences (fixed)

### B1 — Pivot tie rule: the big one

TradingView's `ta.pivothigh(source, 8, 8)` does **not** require the candidate to be
strictly greater than *all 16* neighbours. The platform's actual rule (canonical
replication: `window.size − array.lastindexof(array.max(window)) − 1 == rightbars`):

* candidate must be **strictly greater than all NEWER bars** (the 8 to its right);
* candidate only needs to be **≥ the OLDER bars** (the 8 to its left) — **ties with
  older bars are allowed**.

Consequence: with twin equal swing-highs inside the window (flat double tops —
extremely common on the 1-minute chart where prices move in 0.05 ticks), **TradingView
marks the NEWER twin as the pivot**. The old port used strict `>` on *both* sides, so
it marked **neither** twin → the BSL pool and its SELL signal never existed.

Measured impact (2,000–2,500 tie-prone 1m-like bars, engine run end-to-end):

| Metric | Before fix | After fix |
|---|---|---|
| pivot highs matching TV | 66/80 (14 missing) | exact |
| pivot lows matching TV | 68/81 (13 missing) | exact |
| BUY signals matching TV | 58/82 identical, **23 missing, 1 phantom** | **81/81 identical** |
| SELL signals matching TV | 61/88 identical, **25 missing, 3 phantom** | **86/86 identical** |
| sweeps matching TV | 89/123 | **123/123** |

The damage compounds: because the engine is stateful, one missing pool flips later
merge/sweep decisions — that's where the **phantom** (scanner-only) alerts and the
`BSL-14` vs `BSL-16` name drift came from.

Fix in `scanner/indicators/bsl_ssl.py::_pivots`: `>=` vs older side, `>` vs newer side,
with a docstring recording the exact rule. Covered by a parity test against a literal
reference implementation (twin case + 400-bar randomized tie series, arrays asserted
identical).

### B2 — Magnet-mode alerts quoted the wrong pool

Engine signals were correct in magnet mode, but `_build_signal_msg` always built
BUY from the SSL pool and SELL from the BSL pool (the *fade* mapping). In magnet mode
the BUY alert printed `Fresh [''] pool start @ nan` — empty name, NaN level — and
"confirmed after the actual LOW" when it was a swing HIGH. The dedupe key also used
the wrong side's level. Now direction-aware (`_level_of` takes `magnet`), and the
target in magnet mode is the fresh pool itself, exactly like the Pine label
(`buyTgt = magnet ? newBSLlvl : nextBSL`). Covered by test.

### B3 — "both bots get every alert" silently broke on partial failure

Old contract: `send()` returned `True` if **≥1** bot delivered → alert marked sent →
the failed bot never got it (and it also looked "delivered" in state). Now delivery is
tracked **per bot** (`key@bot1`, `key@bot2` in the state file); on the next scan cycle
only the missing bot(s) are retried; the master key is marked once every configured
bot has acknowledged. No duplicates, no drops. Covered by test (stubbed notifier,
bot2 fails then recovers).

### B4 — end-of-day signals slipped to the next morning

The last 1m bar (15:29→15:30) and 5m bar (15:25→15:30) close **exactly** at 15:30:00.
Polls run every 20 s; `SESSION_END=15:30` → every tick after 15:30:00 saw
"market closed" and skipped processing. The signals eventually went out — at 09:15
**the next trading day**, timestamped with yesterday's 15:30 bar. On the chart they
fired at 15:30. Fix: `EOD_GRACE_MIN` (default 6) keeps processing closed bars for a
few minutes after the session end. After the session no new bars can form, so this
can only flush late arrivals, never invent signals.

---

## C. Data-source divergence — Yahoo `^NSEI` vs TradingView NSE:NIFTY (inherent)

Even with 100% identical logic, the *inputs* differ. Expect occasional, small
disagreements here — they shrink but never reach mathematical zero with a free feed:

1. **Tick-level OHLC differences.** Yahoo aggregates trades differently from NSE's
   feed on TradingView; a bar's high/low can differ by 1 tick (₹0.05). Any decision
   sitting exactly on a threshold can then flip between chart and scanner:
   pivot comparisons, merges (`|lvl−lvl′| ≤ eqTol`), touches (zone band), sweeps
   (`high > lvl and close < lvl`).
2. **Missing or shifted 1m bars.** The engine counts *positions* (pivot confirms
   8 bars later). If Yahoo drops/delays a bar, every confirmation index behind it
   shifts by one → a signal may fire on the "wrong" bar or a marginal pivot differ.
3. **Feed latency.** Yahoo intraday data for Indian indices can lag the real tape
   (seconds to minutes), plus `SCAN_INTERVAL_SEC` adds up to 20 s. The alert
   **content** matches; the wall-clock arrival trails TradingView's bar-close alert.
4. **Pre-open / auction prints.** Yahoo sometimes includes 09:00–09:15 prints for
   indices; the feed filters strictly to 09:15–15:30 IST (bar-start times), matching
   TradingView's regular session. Unfiltered prints would shift pivot windows all day.
5. **Warm-up vs deep history (converges).** The engine is stateful; TradingView
   computes on the chart's full loaded history, the scanner warms up on what Yahoo
   allows (7d of 1m ≈ 2,600 bars; 60d of 5m ≈ 4,500 bars). Two consequences:
   * *Pool state* converges once every live pool was created after warm-up —
     bounded by `POOL_EXPIRY` (default 300 bars ≈ 1 session on 1m, ≈ 4 sessions on
     5m). Before that, expect occasional merge/new-pool differences near the seam.
   * *ATR(14) (Wilder RMA)* converges exponentially; after ~50–100 bars the
     difference is far below tick size. Entry/SL/TP may still differ by paise
     because the underlying closes themselves differ (see 1).
6. **Pool numbering is permanently offset.** `BSL-07` counts pools since the start of
   the loaded history. The scanner starts counting at warm-up → the `-NN` suffix in
   Telegram will generally **not** equal the chart's suffix. Compare by
   **direction + level + confirmation-bar time**, not by pool number.

## D. Looks like a mismatch, is by design

1. **The label is backdated.** The Pine script anchors the BUY/SELL label at the
   actual swing bar (`bar_index − pivLen`, 8 bars back) while *both* the TradingView
   alert and the bot fire on the **confirmation bar**. On the chart the label appears
   8 bars "in the past"; the alert arrives "now". That 8-bar gap is the price of
   non-repainting signals — identical behaviour on both sides.
2. **First-run baseline.** On the very first run per timeframe the scanner marks the
   current bar as processed and sends nothing for history (no spam). Signals that
   fired while the scanner was off are never re-sent. (Missed bars *between* runs are
   caught up on restart via `last_evaluated`.)
3. **Sweep alerts are a separate type.** The chart draws sweep triangles; Telegram
   gets a separate 🧹 message (`SWEEP_ALERTS=true`). Comparing only BUY/SELL counts?
   Set `SWEEP_ALERTS=false` or count 🟢/🔴 only.
4. **Timeframe mix.** The scanner alerts on *both* 1m and 5m (`TIMEFRAMES=1m,5m`).
   Comparing against a single TradingView chart (say 5m) makes the 1m alerts look
   like "extra" signals.
5. **Settings drift.** Any input changed in the indicator's settings panel on
   TradingView (pivot length, tolerances, direction, …) must be mirrored in `.env`,
   or signals legitimately differ.

## E. Verify parity after the fixes

```bash
python selftest.py                  # 11 checks incl. TV-exact pivot parity
python run_scanner.py --dump-sample # offline sample messages
python run_scanner.py               # live
```

Then, after the scanner has run ≥ `POOL_EXPIRY` bars (one session on 1m), compare
TradingView's alert log with Telegram by **(timeframe, direction, level, bar time)**.
Every entry should pair up; only pool numbers and paise-level Entry/SL/TP may differ.
