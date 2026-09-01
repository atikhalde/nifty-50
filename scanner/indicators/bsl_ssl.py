"""Faithful Python port of the TradingView Pine Script v5 indicator:

    "BSL / SSL Liquidity Start Signals — v5 LITE"   (see abcd.txt at repo root)

Signal logic replicated 1:1 with the Pine script, plus a 3-tier Multi-Speed overlay:

  * ATR      -> ta.atr(14)  (Wilder RMA of True Range)
  * Pivots   -> ta.pivothigh(high, pivLen, pivLen) / ta.pivotlow(low, pivLen, pivLen).
                A swing high at bar *i* is CONFIRMED pivLen bars later (bar i+pivLen),
                so the signal fires on the confirmation bar -> non-repainting,
                exactly like the Pine alert (freq_once_per_bar_close).
  * Pools    -> a fresh confirmed swing opens a BSL (above price) / SSL (below price)
                pool. A swing within eqTol of an existing pool level MERGES into that
                pool (touch count +1) and does NOT fire a signal.
                Pools are swept when price trades through the level and closes back
                inside (BSL: high>lvl and close<lvl ; SSL: low<lvl and close>lvl),
                and expire after pool_expiry bars. max_pools live pools per side.
  * Signals  -> DEFAULT (fade): fresh BSL -> SELL, fresh SSL -> BUY
                MAGNET       : fresh BSL -> BUY,  fresh SSL -> SELL
  * SL / TP  -> sl = close -/+ atr*atr_sl ; tp = close -/+ atr*atr_sl*rr_target

  Multi-Speed tiers (all three can fire independently on the same tape):
  * TIER 3 STANDARD  piv_len=8  — original non-repainting swing, macro target tracking
  * TIER 2 FAST      fast_piv_len=3  — 62% faster entries (15m on 5m, 3m on 1m)
  * TIER 1 INSTANT   0-bar lag on sweep candle close — wick SL, 1:2 R:R TP

The per-bar order of operations matches the script exactly:
    BSL create -> SSL create -> BSL sweep/touch/expiry -> SSL sweep/touch/expiry
    -> signals
"""

from __future__ import annotations

import os
from dataclasses import dataclass
import math

import numpy as np
import pandas as pd

# ------------------------------------------------------------------------------
# Parameters (defaults identical to the Pine script's inputs)
# ------------------------------------------------------------------------------


@dataclass
class BSLSSLParams:
    piv_len: int = 8              # TIER 3 "Swing pivot strength" (bars left+right)
    fast_piv_len: int = 3         # TIER 2 fast swing (15m on 5m, 3m on 1m) — 62% faster
    atr_len: int = 14             # ta.atr length
    zone_atr_mult: float = 0.25   # "Zone thickness (x ATR)"
    eq_tol_atr: float = 0.15      # "Equal H/L tolerance (x ATR)"
    max_pools: int = 12           # "Max live pools per side"
    pool_expiry: int = 300        # "Pool expiry (bars)"
    sig_dir: str = "BSL→SELL · SSL→BUY"   # signal direction mapping
    atr_sl: float = 1.2           # "Stop loss (x ATR)"
    rr_target: float = 2.0        # "Reward:Risk target"
    show_liq: bool = True         # "Enable liquidity engine"
    show_signals: bool = True     # "Show BUY / SELL on new pools"
    fast_signals: bool = True     # TIER 2 fast-swing entries
    instant_sweep_trades: bool = True  # TIER 1 0-bar lag sweep trades

    @property
    def magnet(self) -> bool:
        """True when the alternative 'BSL→BUY · SSL→SELL' mapping is selected."""
        d = self.sig_dir.replace("->", "→")
        return "BSL→BUY" in d

    @classmethod
    def from_env(cls) -> "BSLSSLParams":
        """Build params from environment variables (values from .env)."""

        def _get(name: str, current, cast):
            v = os.getenv(name)
            if v is None or not v.strip():
                return current
            try:
                return cast(v.strip())
            except ValueError:
                return current

        def _get_bool(name: str, current: bool) -> bool:
            v = os.getenv(name)
            if v is None or not v.strip():
                return current
            return v.strip().lower() in ("1", "true", "yes", "on")

        return cls(
            piv_len=_get("PIV_LEN", cls.piv_len, int),
            fast_piv_len=_get("FAST_PIV_LEN", cls.fast_piv_len, int),
            atr_len=_get("ATR_LEN", cls.atr_len, int),
            zone_atr_mult=_get("ZONE_ATR_MULT", cls.zone_atr_mult, float),
            eq_tol_atr=_get("EQ_TOL_ATR", cls.eq_tol_atr, float),
            max_pools=_get("MAX_POOLS", cls.max_pools, int),
            pool_expiry=_get("POOL_EXPIRY", cls.pool_expiry, int),
            sig_dir=_get("SIG_DIR", cls.sig_dir, str),
            atr_sl=_get("ATR_SL", cls.atr_sl, float),
            rr_target=_get("RR_TARGET", cls.rr_target, float),
            fast_signals=_get_bool("FAST_SIGNALS", cls.fast_signals),
            instant_sweep_trades=_get_bool("INSTANT_SWEEP_TRADES", cls.instant_sweep_trades),
        )


# ------------------------------------------------------------------------------
# Building blocks (mirror the Pine built-ins used by the script)
# ------------------------------------------------------------------------------


def _true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    n = len(high)
    tr = np.empty(n)
    tr[0] = high[0] - low[0]
    for i in range(1, n):
        pc = close[i - 1]
        tr[i] = max(high[i] - low[i], abs(high[i] - pc), abs(low[i] - pc))
    return tr


def _wilders_rma(values: np.ndarray, length: int) -> np.ndarray:
    """Wilder's RMA - the moving average used by ta.atr() in Pine v5."""
    n = len(values)
    out = np.full(n, np.nan)
    if n < length or length <= 0:
        return out
    out[length - 1] = float(np.mean(values[:length]))
    alpha = 1.0 / length
    for i in range(length, n):
        out[i] = out[i - 1] * (1.0 - alpha) + values[i] * alpha
    return out


def _atr(high, low, close, length: int) -> np.ndarray:
    return _wilders_rma(_true_range(high, low, close), length)


def _pivots(high: np.ndarray, low: np.ndarray, piv_len: int):
    """Return (ph, pl) arrays where a confirmed pivot value appears on the
    CONFIRMATION bar (bar i+piv_len), like Pine's ta.pivothigh/pivotlow.
    Ties are strict (a pivot must be strictly higher/lower than piv_len bars
    on each side) - matching TradingView behaviour.
    """
    n = len(high)
    ph = np.full(n, np.nan)
    pl = np.full(n, np.nan)
    if piv_len <= 0 or n < 2 * piv_len + 1:
        return ph, pl
    for i in range(piv_len, n - piv_len):
        if high[i] > high[i - piv_len:i].max() and high[i] > high[i + 1:i + piv_len + 1].max():
            ph[i + piv_len] = high[i]
        if low[i] < low[i - piv_len:i].min() and low[i] < low[i + 1:i + piv_len + 1].min():
            pl[i + piv_len] = low[i]
    return ph, pl


def _strength(eq: bool, tch: int) -> int:
    v = 1 + (1 if eq else 0) + (1 if tch >= 2 else 0) + (1 if tch >= 4 else 0)
    return min(v, 5)


def _zone_name(eq: bool, buyside: bool, pid: int) -> str:
    kind = ("EQH" if buyside else "EQL") if eq else ("BSL" if buyside else "SSL")
    return f"{kind}-{pid:02d}"


# ------------------------------------------------------------------------------
# The engine
# ------------------------------------------------------------------------------

_NA_FLOAT_COLS = (
    "ph", "pl", "atr", "zone_h", "eq_tol",
    "new_bsl_lvl", "new_ssl_lvl", "swept_bsl_lvl", "swept_ssl_lvl",
    "next_bsl", "next_ssl",
    "sl_long", "tp_long", "sl_short", "tp_short",
    "ph_fast", "pl_fast",
    "fast_new_bsl_lvl", "fast_new_ssl_lvl",
    "fast_sl_long", "fast_tp_long", "fast_sl_short", "fast_tp_short",
    "inst_sl_long", "inst_tp_long", "inst_sl_short", "inst_tp_short",
    "inst_pool_lvl",
)
_BOOL_COLS = (
    "bsl_start", "ssl_start", "swept_bsl", "swept_ssl", "buy_sig", "sell_sig",
    "fast_bsl_start", "fast_ssl_start", "fast_buy_sig", "fast_sell_sig",
    "inst_buy_sig", "inst_sell_sig",
)
_NAME_COLS = (
    "new_bsl_name", "new_ssl_name",
    "fast_new_bsl_name", "fast_new_ssl_name",
    "swept_bsl_name", "swept_ssl_name",
)


def compute_signals(df: pd.DataFrame, p: BSLSSLParams | None = None) -> pd.DataFrame:
    """Run the BSL/SSL engine over a full OHLC dataframe.

    Parameters
    ----------
    df : DataFrame with columns open, high, low, close (any index).
    p   : BSLSSLParams (defaults = identical to the Pine script's inputs).

    Returns
    -------
    DataFrame (same index as df) with one row per bar and columns:
      ph, pl, atr, zone_h, eq_tol,
      bsl_start, ssl_start, new_bsl_lvl, new_ssl_lvl, new_bsl_name, new_ssl_name,
      swept_bsl, swept_ssl, swept_bsl_lvl, swept_ssl_lvl, swept_bsl_name, swept_ssl_name,
      next_bsl, next_ssl,
      buy_sig, sell_sig, sl_long, tp_long, sl_short, tp_short,
      ph_fast, pl_fast, fast_bsl_start, fast_ssl_start,
      fast_new_bsl_lvl, fast_new_ssl_lvl, fast_new_bsl_name, fast_new_ssl_name,
      fast_buy_sig, fast_sell_sig, fast_sl_long, fast_tp_long, fast_sl_short, fast_tp_short,
      inst_buy_sig, inst_sell_sig, inst_sl_long, inst_tp_long, inst_sl_short, inst_tp_short,
      inst_pool_lvl
    """
    p = p or BSLSSLParams()

    if df is None or len(df) == 0:
        idx = df.index if df is not None else None
        cols = list(_NA_FLOAT_COLS) + list(_BOOL_COLS) + list(_NAME_COLS)
        return pd.DataFrame(columns=cols, index=idx)

    high = df["high"].to_numpy(dtype=float)
    low = df["low"].to_numpy(dtype=float)
    close = df["close"].to_numpy(dtype=float)
    n = len(df)

    atr = _atr(high, low, close, p.atr_len)
    ph, pl = _pivots(high, low, p.piv_len)
    ph_f, pl_f = _pivots(high, low, p.fast_piv_len) if p.fast_piv_len > 0 else (
        np.full(n, np.nan), np.full(n, np.nan)
    )

    # ---- pool storage (mirrors the Pine var arrays) ----
    b_lvl, b_bar, b_tch, b_eq, b_id, b_last, b_str = [], [], [], [], [], [], []
    s_lvl, s_bar, s_tch, s_eq, s_id, s_last, s_str = [], [], [], [], [], [], []
    b_seq, s_seq = 0, 0

    out = {k: np.full(n, np.nan) for k in _NA_FLOAT_COLS}
    flags = {k: np.zeros(n, dtype=bool) for k in _BOOL_COLS}
    names = {k: [""] * n for k in _NAME_COLS}

    for j in range(n):
        a = float(atr[j])          # may be NaN (first atr_len-1 bars), like Pine
        zone_h = a * p.zone_atr_mult
        eq_tol = a * p.eq_tol_atr

        out["atr"][j] = a
        out["zone_h"][j] = zone_h
        out["eq_tol"][j] = eq_tol
        out["ph"][j] = ph[j]
        out["pl"][j] = pl[j]
        out["ph_fast"][j] = ph_f[j]
        out["pl_fast"][j] = pl_f[j]

        # ------------------------------------------------------------
        # CREATE BUY-SIDE POOL  (fresh confirmed swing high = START)
        # ------------------------------------------------------------
        bsl_start = False
        new_bsl_lvl = np.nan
        new_bsl_name = ""
        if p.show_liq and not math.isnan(ph[j]):
            lvl = ph[j]
            pbar = j - p.piv_len
            is_eq = False
            for i in range(len(b_lvl) - 1, -1, -1):
                if abs(b_lvl[i] - lvl) <= eq_tol:      # NaN eq_tol -> False (matches Pine)
                    is_eq = True
                    b_eq[i] = True
                    b_tch[i] += 1
                    b_str[i] = _strength(True, b_tch[i])
                    break
            if not is_eq:
                b_seq += 1
                b_lvl.append(lvl)
                b_bar.append(pbar)
                b_tch.append(1)
                b_eq.append(False)
                b_id.append(b_seq)
                b_last.append(j)
                b_str.append(_strength(False, 1))
                bsl_start = True
                new_bsl_lvl = lvl
                new_bsl_name = _zone_name(False, True, b_seq)
                while len(b_lvl) > p.max_pools:
                    for arr in (b_lvl, b_bar, b_tch, b_eq, b_id, b_last, b_str):
                        arr.pop(0)

        # ------------------------------------------------------------
        # CREATE SELL-SIDE POOL (fresh confirmed swing low = START)
        # ------------------------------------------------------------
        ssl_start = False
        new_ssl_lvl = np.nan
        new_ssl_name = ""
        if p.show_liq and not math.isnan(pl[j]):
            lvl = pl[j]
            pbar = j - p.piv_len
            is_eq = False
            for i in range(len(s_lvl) - 1, -1, -1):
                if abs(s_lvl[i] - lvl) <= eq_tol:
                    is_eq = True
                    s_eq[i] = True
                    s_tch[i] += 1
                    s_str[i] = _strength(True, s_tch[i])
                    break
            if not is_eq:
                s_seq += 1
                s_lvl.append(lvl)
                s_bar.append(pbar)
                s_tch.append(1)
                s_eq.append(False)
                s_id.append(s_seq)
                s_last.append(j)
                s_str.append(_strength(False, 1))
                ssl_start = True
                new_ssl_lvl = lvl
                new_ssl_name = _zone_name(False, False, s_seq)
                while len(s_lvl) > p.max_pools:
                    for arr in (s_lvl, s_bar, s_tch, s_eq, s_id, s_last, s_str):
                        arr.pop(0)

        # ------------------------------------------------------------
        # SWEEP / TOUCH / EXPIRY — BUY-SIDE
        # ------------------------------------------------------------
        swept_bsl = False
        swept_bsl_lvl = np.nan
        swept_bsl_name = ""
        i = len(b_lvl) - 1
        while i >= 0:
            lvl = b_lvl[i]
            if high[j] > lvl and close[j] < lvl:          # swept
                swept_bsl = True
                swept_bsl_lvl = lvl
                swept_bsl_name = _zone_name(b_eq[i], True, b_id[i])
                for arr in (b_lvl, b_bar, b_tch, b_eq, b_id, b_last, b_str):
                    arr.pop(i)
            else:
                if (high[j] >= lvl - zone_h * 0.5 and high[j] <= lvl + zone_h * 0.5
                        and (j - b_last[i]) > 3):          # touch
                    b_tch[i] += 1
                    b_last[i] = j
                    b_str[i] = _strength(b_eq[i], b_tch[i])
                if j - b_bar[i] > p.pool_expiry:           # expiry
                    for arr in (b_lvl, b_bar, b_tch, b_eq, b_id, b_last, b_str):
                        arr.pop(i)
            i -= 1

        # ------------------------------------------------------------
        # SWEEP / TOUCH / EXPIRY — SELL-SIDE
        # ------------------------------------------------------------
        swept_ssl = False
        swept_ssl_lvl = np.nan
        swept_ssl_name = ""
        i = len(s_lvl) - 1
        while i >= 0:
            lvl = s_lvl[i]
            if low[j] < lvl and close[j] > lvl:            # swept
                swept_ssl = True
                swept_ssl_lvl = lvl
                swept_ssl_name = _zone_name(s_eq[i], False, s_id[i])
                for arr in (s_lvl, s_bar, s_tch, s_eq, s_id, s_last, s_str):
                    arr.pop(i)
            else:
                if (low[j] <= lvl + zone_h * 0.5 and low[j] >= lvl - zone_h * 0.5
                        and (j - s_last[i]) > 3):          # touch
                    s_tch[i] += 1
                    s_last[i] = j
                    s_str[i] = _strength(s_eq[i], s_tch[i])
                if j - s_bar[i] > p.pool_expiry:           # expiry
                    for arr in (s_lvl, s_bar, s_tch, s_eq, s_id, s_last, s_str):
                        arr.pop(i)
            i -= 1

        # ------------------------------------------------------------
        # START-POINT ENTRY SIGNALS  (TIER 3 STANDARD, piv_len)
        # ------------------------------------------------------------
        next_bsl = min((lv for lv in b_lvl if lv > close[j]), default=np.nan)
        next_ssl = max((lv for lv in s_lvl if lv < close[j]), default=np.nan)

        buy_sig = p.show_signals and (ssl_start if not p.magnet else bsl_start)
        sell_sig = p.show_signals and (bsl_start if not p.magnet else ssl_start)

        sl_long = close[j] - a * p.atr_sl
        tp_long = close[j] + a * p.atr_sl * p.rr_target
        sl_short = close[j] + a * p.atr_sl
        tp_short = close[j] - a * p.atr_sl * p.rr_target

        # ------------------------------------------------------------
        # TIER 2 — FAST SWING PIVOTS (fast_piv_len = 3 → 62% faster)
        # Independent confirmation; macro targets still come from the
        # intact piv_len=8 pool book (next_bsl / next_ssl).
        # ------------------------------------------------------------
        fast_bsl_start = False
        fast_ssl_start = False
        fast_new_bsl_lvl = np.nan
        fast_new_ssl_lvl = np.nan
        fast_new_bsl_name = ""
        fast_new_ssl_name = ""
        if p.show_liq and p.fast_piv_len > 0 and not math.isnan(ph_f[j]):
            fast_bsl_start = True
            fast_new_bsl_lvl = float(ph_f[j])
            fast_new_bsl_name = "FAST-BSL"
        if p.show_liq and p.fast_piv_len > 0 and not math.isnan(pl_f[j]):
            fast_ssl_start = True
            fast_new_ssl_lvl = float(pl_f[j])
            fast_new_ssl_name = "FAST-SSL"

        fast_buy_sig = (
            p.show_signals and p.fast_signals
            and (fast_ssl_start if not p.magnet else fast_bsl_start)
        )
        fast_sell_sig = (
            p.show_signals and p.fast_signals
            and (fast_bsl_start if not p.magnet else fast_ssl_start)
        )
        fast_sl_long = sl_long
        fast_tp_long = tp_long
        fast_sl_short = sl_short
        fast_tp_short = tp_short

        # ------------------------------------------------------------
        # TIER 1 — INSTANT SWEEP TRADES (0-bar lag)
        # Execute on the sweep candle close. SL = sweep-candle wick,
        # TP = 1:2 R:R from that wick risk.
        # SSL sweep (bullish reclaim) → BUY ; BSL sweep (bearish reject) → SELL
        # ------------------------------------------------------------
        inst_buy_sig = bool(p.instant_sweep_trades) and swept_ssl
        inst_sell_sig = bool(p.instant_sweep_trades) and swept_bsl
        wick_risk_long = close[j] - low[j]
        wick_risk_short = high[j] - close[j]
        inst_sl_long = low[j]
        inst_tp_long = close[j] + p.rr_target * wick_risk_long
        inst_sl_short = high[j]
        inst_tp_short = close[j] - p.rr_target * wick_risk_short
        if inst_buy_sig:
            inst_pool_lvl = swept_ssl_lvl
        elif inst_sell_sig:
            inst_pool_lvl = swept_bsl_lvl
        else:
            inst_pool_lvl = np.nan

        out["new_bsl_lvl"][j] = new_bsl_lvl
        out["new_ssl_lvl"][j] = new_ssl_lvl
        out["swept_bsl_lvl"][j] = swept_bsl_lvl
        out["swept_ssl_lvl"][j] = swept_ssl_lvl
        out["next_bsl"][j] = next_bsl
        out["next_ssl"][j] = next_ssl
        out["sl_long"][j] = sl_long
        out["tp_long"][j] = tp_long
        out["sl_short"][j] = sl_short
        out["tp_short"][j] = tp_short
        out["fast_new_bsl_lvl"][j] = fast_new_bsl_lvl
        out["fast_new_ssl_lvl"][j] = fast_new_ssl_lvl
        out["fast_sl_long"][j] = fast_sl_long
        out["fast_tp_long"][j] = fast_tp_long
        out["fast_sl_short"][j] = fast_sl_short
        out["fast_tp_short"][j] = fast_tp_short
        out["inst_sl_long"][j] = inst_sl_long
        out["inst_tp_long"][j] = inst_tp_long
        out["inst_sl_short"][j] = inst_sl_short
        out["inst_tp_short"][j] = inst_tp_short
        out["inst_pool_lvl"][j] = inst_pool_lvl

        flags["bsl_start"][j] = bsl_start
        flags["ssl_start"][j] = ssl_start
        flags["swept_bsl"][j] = swept_bsl
        flags["swept_ssl"][j] = swept_ssl
        flags["buy_sig"][j] = buy_sig
        flags["sell_sig"][j] = sell_sig
        flags["fast_bsl_start"][j] = fast_bsl_start
        flags["fast_ssl_start"][j] = fast_ssl_start
        flags["fast_buy_sig"][j] = fast_buy_sig
        flags["fast_sell_sig"][j] = fast_sell_sig
        flags["inst_buy_sig"][j] = inst_buy_sig
        flags["inst_sell_sig"][j] = inst_sell_sig

        names["new_bsl_name"][j] = new_bsl_name
        names["new_ssl_name"][j] = new_ssl_name
        names["fast_new_bsl_name"][j] = fast_new_bsl_name
        names["fast_new_ssl_name"][j] = fast_new_ssl_name
        names["swept_bsl_name"][j] = swept_bsl_name
        names["swept_ssl_name"][j] = swept_ssl_name

    res = pd.DataFrame(index=df.index)
    for k, arr in out.items():
        res[k] = arr
    for k, arr in flags.items():
        res[k] = arr
    for k, arr in names.items():
        res[k] = arr
    return res
