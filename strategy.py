"""
strategy.py — Core TJR Smart Money Concepts math (deterministic, no LLM).

Consumes CLOSED 5-minute bars only. If the newest row looks like a forming
(partial) bar it is dropped before any evaluation — per the risk register,
we never act on partial-bar data.

The real TJR sequence is: liquidity sweep → displacement (MSS) → FVG left
behind by that displacement. Each check therefore scans its own window:
  - Sweep:   any of the last SWEEP_SCAN_BARS bars wicked past the prior
             20-bar extreme and closed back inside (stops grabbed).
  - MSS:     the trigger (most recent closed) bar closes beyond the prior
             10-bar swing extreme, in the same direction as the sweep.
  - FVG:     any direction-consistent 3-candle imbalance at/after the sweep
             bar, >= FVG_MIN_IMBALANCE_PCT of price. Most recent wins.

Same data in → same verdict out. No network, no broker calls, no state.
"""

import logging
from datetime import datetime
from typing import Dict, Optional, Tuple
from zoneinfo import ZoneInfo

import pandas as pd

import config

logger = logging.getLogger(__name__)

NY_TZ = ZoneInfo(config.TIMEZONE)
BAR_MINUTES = 5


# ---------------------------------------------------------------------------
# Time / bar hygiene
# ---------------------------------------------------------------------------
def is_session_window(ts: datetime) -> bool:
    """True if `ts` (tz-aware) falls within 09:30–11:00 ET, Mon–Fri."""
    if ts.weekday() >= 5:  # Saturday / Sunday
        return False
    local = ts.astimezone(NY_TZ)
    hhmm = local.strftime("%H:%M")
    return config.SESSION_START <= hhmm <= config.SESSION_END


def drop_forming_bar(df: pd.DataFrame) -> pd.DataFrame:
    """
    Closed-bars-only rule: drop the trailing bar if it has not finished
    printing yet (a bar stamped 10:55 closes at 11:00).
    """
    if df.empty:
        return df
    df = df.copy()
    last_ts = df.index[-1]
    tz = getattr(last_ts, "tzinfo", None) or NY_TZ
    now = datetime.now(tz=tz)
    bar_close = (last_ts + pd.Timedelta(minutes=BAR_MINUTES)).to_pydatetime()
    if now < bar_close:
        logger.debug("Dropping forming bar stamped %s", last_ts)
        df = df.iloc[:-1]
    return df


# ---------------------------------------------------------------------------
# Check primitives
# ---------------------------------------------------------------------------
def _find_sweep(df: pd.DataFrame, scan_bars: int) -> Tuple[Optional[str], Optional[int], Optional[float]]:
    """
    Scan the last `scan_bars` closed bars for a liquidity sweep.

    A bullish sweep at bar i: low < min(lows of the SWEEP_LOOKBACK bars
    immediately before i) AND close back above that level → retail stops
    below the range were grabbed and price rejected back inside.
    Bearish is the mirror at the 20-bar high.

    Returns (direction, df-position-index of sweep bar, swept level).
    Position index is a 0-based df position (e.g. n-1 = trigger bar).
    """
    n = len(df)
    for offset in range(1, scan_bars + 1):          # most recent first
        i = n - offset                              # position index (negative)
        start = i - config.SWEEP_LOOKBACK           # 20 bars before candidate
        if start < 0:
            break
        prior = df.iloc[start:i]
        candidate = df.iloc[i]

        sweep_low = float(prior["low"].min())
        sweep_high = float(prior["high"].max())

        if (float(candidate["low"]) < sweep_low
                and float(candidate["close"]) > sweep_low):
            return "BUY", i, sweep_low
        if (float(candidate["high"]) > sweep_high
                and float(candidate["close"]) < sweep_high):
            return "SELL", i, sweep_high
    return None, None, None


def _check_mss(df: pd.DataFrame, direction: str) -> Tuple[bool, Optional[float]]:
    """
    Market Structure Shift: trigger bar CLOSES beyond the prior
    MSS_LOOKBACK-bar swing extreme, in the sweep's direction. Closing through
    the level (not just wicking) is what confirms aggressive displacement.
    """
    trigger = df.iloc[-1]
    prior = df.iloc[-(config.MSS_LOOKBACK + 1):-1]
    mss_high = float(prior["high"].max())
    mss_low = float(prior["low"].min())
    if direction == "BUY":
        return float(trigger["close"]) > mss_high, mss_high
    return float(trigger["close"]) < mss_low, mss_low


def _find_fvg(df: pd.DataFrame, direction: str, sweep_pos: int,
              price: float, scan_bars: Optional[int] = None) -> Tuple[bool, Optional[float]]:
    """
    Fair Value Gap: 3-candle imbalance.

    Bullish FVG at (c1, c2, c3):  c3.low > c1.high — price moved up so fast
    that c2's range never re-touched c1's high; the unfilled band between
    c1.high and c3.low is inefficiency price tends to revisit (entry zone).
    Bearish FVG: c3.high < c1.low (mirror).

    Scans direction-consistent windows from most recent backwards; the FVG
    must sit at/after the sweep bar (in the displacement leg, not before it).
    Gap must be >= FVG_MIN_IMBALANCE_PCT (0.15%) of the asset price.
    `scan_bars` stretches the search window (two-stage detector scans the
    full arming window so older displacement legs stay eligible).
    """
    scan_bars = scan_bars or config.FVG_SCAN_BARS
    min_gap = (config.FVG_MIN_IMBALANCE_PCT / 100.0) * price
    n = len(df)
    # Oldest→newest so "most recent wins" via overwriting.
    found = False
    fvg_price: Optional[float] = None
    for i in range(n - scan_bars, n):
        if i - 2 < 0 or i < sweep_pos:   # needs 3 bars; must be at/after sweep
            continue
        c1, _c2, c3 = df.iloc[i - 2], df.iloc[i - 1], df.iloc[i]
        if direction == "BUY":
            gap = float(c3["low"]) - float(c1["high"])
            if gap >= min_gap:
                fvg_price = (float(c3["low"]) + float(c1["high"])) / 2.0
                found = True
        else:  # SELL
            gap = float(c1["low"]) - float(c3["high"])
            if gap >= min_gap:
                fvg_price = (float(c1["low"]) + float(c3["high"])) / 2.0
                found = True
    return found, fvg_price


# ---------------------------------------------------------------------------
# Core evaluation
# ---------------------------------------------------------------------------
def evaluate_tjr_setup(df: pd.DataFrame) -> Dict[str, object]:
    """
    Evaluate the TJR SMC checklist on the most recent CLOSED 5-minute bar.

    Checklist (ALL must pass for `setup_valid: True`):
      1. Session window 09:30–11:00 ET, Mon–Fri.
      2. Liquidity sweep within the last SWEEP_SCAN_BARS bars.
      3. MSS — trigger bar closes beyond the prior 10-bar swing extreme,
         same direction as the sweep.
      4. FVG — 3-candle imbalance >= 0.15% of price, at/after the sweep bar.

    Parameters
    ----------
    df : DataFrame indexed by tz-aware timestamps with columns
         ['open', 'high', 'low', 'close', 'volume'] (case-insensitive).

    Returns
    -------
    dict: {
        'setup_valid': bool,
        'direction':  'BUY' | 'SELL' | None,
        'fvg_price':  float | None,   # midpoint of the FVG imbalance zone
        'checks':     dict,           # per-check booleans + evidence (logs)
    }
    """
    checks: Dict[str, object] = {
        "session_ok": False,
        "liquidity_sweep": False,
        "mss": False,
        "fvg": False,
        "sweep_level": None,
        "mss_level": None,
        "fvg_size": None,
    }
    invalid = {"setup_valid": False, "direction": None,
               "fvg_price": None, "checks": checks}

    if df is None or df.empty:
        checks["error"] = "empty dataframe"
        return invalid

    df = df.rename(columns={c: str(c).lower() for c in df.columns})
    needed = {"open", "high", "low", "close"}
    if not needed.issubset(df.columns):
        checks["error"] = f"missing columns, need {needed}"
        return invalid

    df = drop_forming_bar(df.sort_index())
    min_bars = max(config.MIN_BARS_REQUIRED,
                   config.SWEEP_LOOKBACK + config.SWEEP_ARM_BARS + 1)
    if len(df) < min_bars:
        checks["error"] = f"only {len(df)} closed bars, need >= {min_bars}"
        return invalid

    # --- Check 1: session window -----------------------------------------
    last_ts = df.index[-1].to_pydatetime()
    if last_ts.tzinfo is None:
        last_ts = last_ts.replace(tzinfo=NY_TZ)
    checks["session_ok"] = is_session_window(last_ts)
    if not checks["session_ok"]:
        return invalid

    price = float(df.iloc[-1]["close"])

    # --- Check 2: liquidity sweep — the ARMING window ---------------------
    # Two-stage detector: a sweep arms the setup for SWEEP_ARM_BARS bars.
    # Default 5 == strict baseline (sweep must be within 25 min); larger
    # values let an MSS that follows later still fire (SWEEP_ARM_BARS env).
    sweep_dir, sweep_pos, sweep_level = _find_sweep(df, config.SWEEP_ARM_BARS)
    checks["liquidity_sweep"] = sweep_dir is not None
    checks["sweep_level"] = sweep_level
    checks["sweep_age_bars"] = ((len(df) - 1) - sweep_pos
                                if sweep_pos is not None else None)
    if sweep_dir is None:
        return invalid

    # --- Check 3: MSS on the trigger bar, aligned with the sweep ----------
    mss_ok, mss_level = _check_mss(df, sweep_dir)
    checks["mss"] = mss_ok
    checks["mss_level"] = mss_level
    if not mss_ok:
        return invalid
    direction = sweep_dir

    # --- Check 4: FVG in the displacement leg -----------------------------
    # FVG_REQUIRED=0 trades the sweep→MSS sequence alone (frequency over
    # confirmation quality — backtest before enabling in production).
    # The FVG search window stretches with the arming window so displacement
    # legs from older sweeps stay eligible.
    fvg_ok, fvg_price = _find_fvg(
        df, direction, sweep_pos, price,
        scan_bars=max(config.FVG_SCAN_BARS, config.SWEEP_ARM_BARS))
    checks["fvg"] = fvg_ok
    checks["fvg_price"] = round(fvg_price, 4) if fvg_price is not None else None
    if config.FVG_REQUIRED and not fvg_ok:
        return invalid

    result: Dict[str, object] = {
        "setup_valid": True,
        "direction": direction,
        "fvg_price": round(fvg_price, 4) if fvg_price is not None else None,
        "checks": checks,
    }
    logger.info("TJR setup VALID: %s @ %s fvg=%s", direction, last_ts, fvg_price)
    return result
