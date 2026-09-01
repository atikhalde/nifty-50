"""Faithful Python port of the TradingView Pine Script v5 indicator:

    "BSL / SSL Liquidity Start Signals — v5 LITE"   (see abcd.txt at repo root)

Signal logic replicated 1:1 with the Pine script:
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
    piv_len: int = 8              # "Swing pivot strength" (bars left+right)
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

        return cls(
            piv_len=_get("PIV_LEN", cls.piv_len, int),
            atr_len=_get("ATR_LEN", cls.atr_len, int),
            zone_atr_mult=_get("ZONE_ATR_MULT", cls.zone_atr_mult, float),
            eq_tol_atr=_get("EQ_TOL_ATR", cls.eq_tol_atr, float),
            max_pools=_get("MAX_POOLS", cls.max_pools, int),
            pool_expiry=_get("POOL_EXPIRY", cls.pool_expiry, int),
            sig_dir=_get("SIG_DIR", cls.sig_dir, str),
            atr_sl=_get("ATR_SL", cls.atr_sl, float),
            rr_target=_get("RR_TARGET", cls.rr_target, float),
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

    TradingView tie semantics (verified against the platform; equivalent to
    the canonical replication `window.size - array.lastindexof(window.max) - 1 == rightbars`):
      * the candidate must be STRICTLY greater than all NEWER bars
        (the `piv_len` bars to its right);
      * it TIES with / exceeds OLDER bars (the `piv_len` bars to its left),
        i.e. older bars may be equal but not greater.
    Consequence: with twin equal swing highs inside the pivot window, the
    NEWER twin is the pivot (the older one is shadowed). A "strict both
    sides" python port would mark NEITHER twin — silently dropping every
    signal built on flat double tops/bottoms (very common on 1m, tick 0.05).
    """
    n = len(high)
    ph = np.full(n, np.nan)
    pl = np.full(n, np.nan)
    if piv_len <= 0 or n < 2 * piv_len + 1:
        return ph, pl
    for i in range(piv_len, n - piv_len):
        # older side: strictly-greater invalidates (ties allowed)
        # newer side: greater-or-equal invalidates (ties forbidden)
        if high[i] >= high[i - piv_len:i].max() and high[i] > high[i + 1:i + piv_len + 1].max():
            ph[i + piv_len] = high[i]
        if low[i] <= low[i - piv_len:i].min() and low[i] < low[i + 1:i + piv_len + 1].min():
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
)
_BOOL_COLS = ("bsl_start", "ssl_start", "swept_bsl", "swept_ssl", "buy_sig", "sell_sig")


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
      swept_bsl, swept_ssl, swept_bsl_lvl, swept_ssl_lvl,
      next_bsl, next_ssl,
      buy_sig, sell_sig, sl_long, tp_long, sl_short, tp_short
    """
    p = p or BSLSSLParams()

    if df is None or len(df) == 0:
        idx = df.index if df is not None else None
        cols = list(_NA_FLOAT_COLS) + list(_BOOL_COLS) + ["new_bsl_name", "new_ssl_name"]
        return pd.DataFrame(columns=cols, index=idx)

    high = df["high"].to_numpy(dtype=float)
    low = df["low"].to_numpy(dtype=float)
    close = df["close"].to_numpy(dtype=float)
    n = len(df)

    atr = _atr(high, low, close, p.atr_len)
    ph, pl = _pivots(high, low, p.piv_len)

    # ---- pool storage (mirrors the Pine var arrays) ----
    b_lvl, b_bar, b_tch, b_eq, b_id, b_last, b_str = [], [], [], [], [], [], []
    s_lvl, s_bar, s_tch, s_eq, s_id, s_last, s_str = [], [], [], [], [], [], []
    b_seq, s_seq = 0, 0

    out = {k: np.full(n, np.nan) for k in _NA_FLOAT_COLS}
    flags = {k: np.zeros(n, dtype=bool) for k in _BOOL_COLS}
    names = {"new_bsl_name": [""] * n, "new_ssl_name": [""] * n}

    for j in range(n):
        a = float(atr[j])          # may be NaN (first atr_len-1 bars), like Pine
        zone_h = a * p.zone_atr_mult
        eq_tol = a * p.eq_tol_atr

        out["atr"][j] = a
        out["zone_h"][j] = zone_h
        out["eq_tol"][j] = eq_tol
        out["ph"][j] = ph[j]
        out["pl"][j] = pl[j]

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
        i = len(b_lvl) - 1
        while i >= 0:
            lvl = b_lvl[i]
            if high[j] > lvl and close[j] < lvl:          # swept
                swept_bsl = True
                swept_bsl_lvl = lvl
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
        i = len(s_lvl) - 1
        while i >= 0:
            lvl = s_lvl[i]
            if low[j] < lvl and close[j] > lvl:            # swept
                swept_ssl = True
                swept_ssl_lvl = lvl
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
        # START-POINT ENTRY SIGNALS
        # ------------------------------------------------------------
        next_bsl = min((lv for lv in b_lvl if lv > close[j]), default=np.nan)
        next_ssl = max((lv for lv in s_lvl if lv < close[j]), default=np.nan)

        buy_sig = p.show_signals and (ssl_start if not p.magnet else bsl_start)
        sell_sig = p.show_signals and (bsl_start if not p.magnet else ssl_start)

        sl_long = close[j] - a * p.atr_sl
        tp_long = close[j] + a * p.atr_sl * p.rr_target
        sl_short = close[j] + a * p.atr_sl
        tp_short = close[j] - a * p.atr_sl * p.rr_target

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

        flags["bsl_start"][j] = bsl_start
        flags["ssl_start"][j] = ssl_start
        flags["swept_bsl"][j] = swept_bsl
        flags["swept_ssl"][j] = swept_ssl
        flags["buy_sig"][j] = buy_sig
        flags["sell_sig"][j] = sell_sig

        names["new_bsl_name"][j] = new_bsl_name
        names["new_ssl_name"][j] = new_ssl_name

    res = pd.DataFrame(index=df.index)
    for k, arr in out.items():
        res[k] = arr
    for k, arr in flags.items():
        res[k] = arr
    res["new_bsl_name"] = names["new_bsl_name"]
    res["new_ssl_name"] = names["new_ssl_name"]
    return res
