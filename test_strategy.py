"""
test_strategy.py — Deterministic unit tests for the TJR SMC checklist.

Same bars in → same verdict out. Run:  python -m pytest test_strategy.py -q
(also runnable standalone:  python test_strategy.py)
"""

import math
from datetime import datetime, timedelta
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

import pandas as pd

import config
import strategy
from config import TIMEZONE

NY = ZoneInfo(TIMEZONE)


# ---------------------------------------------------------------------------
# Bar factory
# ---------------------------------------------------------------------------
def _session_bars(
    n: int,
    start: datetime,
    seed: float = 100.0,
    overrides: Optional[Tuple[int, dict]] = None,
) -> pd.DataFrame:
    """
    Build a flat, quiet 5m series (low volatility → no sweeps/FVGs by
    default), then apply per-bar overrides: {pos: {col: value}} where pos
    is a negative pandas index (-1 = newest).
    """
    rows: List[dict] = []
    ts = start
    for i in range(n):
        wobble = 0.01 * math.sin(i)  # deterministic, sub-tick
        rows.append({
            "open": seed + wobble,
            "high": seed + wobble + 0.02,
            "low": seed + wobble - 0.02,
            "close": seed + wobble,
            "volume": 1_000_000,
        })
    df = pd.DataFrame(rows)
    if overrides:
        pos, fields = overrides
        for col, val in fields.items():
            df.iloc[pos, df.columns.get_loc(col)] = val
    index = pd.DatetimeIndex(
        [start + timedelta(minutes=5 * i) for i in range(n)], name="timestamp"
    )
    if index.tz is None:
        index = index.tz_localize(NY)
    df.index = index
    return df


def _bullish_setup(when: Optional[datetime] = None) -> pd.DataFrame:
    """
    Construct a textbook bullish sequence ending at `when`:
    flat range → sweep bar (wicks below 20-bar low, closes back inside)
    → displacement leg (closes above the 10-bar swing high, leaves a
    3-candle FVG between the sweep bar's high and the trigger leg's low).
    """
    when = when or datetime(2026, 9, 24, 10, 30, tzinfo=NY)  # Thu, mid-window
    n = strategy_min_bars() + 5
    df = _session_bars(n, when - timedelta(minutes=5 * (n - 1)))
    # pos -5: sweep bar — low 99.00 < 20-bar low (~99.96), close back inside
    df.iloc[-5, df.columns.get_loc("low")] = 99.00
    df.iloc[-5, df.columns.get_loc("close")] = 100.05
    # pos -4..-1: displacement leg — closes ride above the 10-bar swing high;
    # bull FVG between -4.high (100.60) and -2.low (100.76) = 0.16 >= 0.15%.
    df.iloc[-4, df.columns.get_loc("high")] = 100.60
    df.iloc[-4, df.columns.get_loc("low")] = 100.00
    df.iloc[-4, df.columns.get_loc("close")] = 100.55
    df.iloc[-3, df.columns.get_loc("low")] = 100.58
    df.iloc[-3, df.columns.get_loc("high")] = 100.75
    df.iloc[-3, df.columns.get_loc("close")] = 100.72
    df.iloc[-2, df.columns.get_loc("low")] = 100.76
    df.iloc[-2, df.columns.get_loc("high")] = 100.80
    df.iloc[-2, df.columns.get_loc("close")] = 100.78
    df.iloc[-1, df.columns.get_loc("low")] = 100.74   # <= -3.high → no 2nd FVG
    df.iloc[-1, df.columns.get_loc("high")] = 100.85
    df.iloc[-1, df.columns.get_loc("close")] = 100.83  # > 10-bar high → MSS
    return df


def strategy_min_bars() -> int:
    from config import (MIN_BARS_REQUIRED, SWEEP_LOOKBACK, SWEEP_SCAN_BARS)
    return max(MIN_BARS_REQUIRED, SWEEP_LOOKBACK + SWEEP_SCAN_BARS + 1)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_bullish_setup_fires() -> None:
    df = _bullish_setup()
    res = strategy.evaluate_tjr_setup(df)
    assert res["setup_valid"] is True, res
    assert res["direction"] == "BUY"
    # FVG midpoint must sit between candle1.high (100.60) and candle3.low (100.76)
    assert 100.60 < res["fvg_price"] < 100.76


def test_bearish_setup_fires() -> None:
    when = datetime(2026, 9, 24, 10, 30, tzinfo=NY)
    n = strategy_min_bars() + 5
    df = _session_bars(n, when - timedelta(minutes=5 * (n - 1)))
    # pos -5: sweep above the 20-bar high (~100.03), close back inside
    df.iloc[-5, df.columns.get_loc("high")] = 101.00
    df.iloc[-5, df.columns.get_loc("close")] = 99.95
    # pos -4..-1: displacement down; bear FVG between -4.low (99.45) and
    # -2.high (99.25) = 0.20 >= 0.15%.
    df.iloc[-4, df.columns.get_loc("low")] = 99.45
    df.iloc[-4, df.columns.get_loc("high")] = 99.92
    df.iloc[-4, df.columns.get_loc("close")] = 99.50
    df.iloc[-3, df.columns.get_loc("high")] = 99.44
    df.iloc[-3, df.columns.get_loc("low")] = 99.35
    df.iloc[-3, df.columns.get_loc("close")] = 99.38
    df.iloc[-2, df.columns.get_loc("high")] = 99.25
    df.iloc[-2, df.columns.get_loc("low")] = 99.20
    df.iloc[-2, df.columns.get_loc("close")] = 99.22
    df.iloc[-1, df.columns.get_loc("high")] = 99.24
    df.iloc[-1, df.columns.get_loc("low")] = 99.15
    df.iloc[-1, df.columns.get_loc("close")] = 99.10  # < 10-bar low → MSS
    res = strategy.evaluate_tjr_setup(df)
    assert res["setup_valid"] is True, res
    assert res["direction"] == "SELL"
    assert 99.25 < res["fvg_price"] < 99.45


def test_no_setup_in_quiet_range() -> None:
    when = datetime(2026, 9, 24, 10, 30, tzinfo=NY)
    n = strategy_min_bars() + 4
    df = _session_bars(n, when - timedelta(minutes=5 * (n - 1)))
    res = strategy.evaluate_tjr_setup(df)
    assert res["setup_valid"] is False
    assert res["checks"]["liquidity_sweep"] is False


def test_outside_session_rejected() -> None:
    df = _bullish_setup(when=datetime(2026, 9, 24, 13, 30, tzinfo=NY))  # 1:30 PM
    res = strategy.evaluate_tjr_setup(df)
    assert res["setup_valid"] is False
    assert res["checks"]["session_ok"] is False


def test_weekend_rejected() -> None:
    df = _bullish_setup(when=datetime(2026, 9, 26, 10, 0, tzinfo=NY))  # Saturday
    res = strategy.evaluate_tjr_setup(df)
    assert res["setup_valid"] is False
    assert res["checks"]["session_ok"] is False


def test_insufficient_bars_rejected() -> None:
    when = datetime(2026, 9, 24, 10, 30, tzinfo=NY)
    df = _bullish_setup(when)
    res = strategy.evaluate_tjr_setup(df.iloc[:-10])  # starve the history
    assert res["setup_valid"] is False
    assert "error" in res["checks"]


def test_forming_bar_dropped() -> None:
    """Invariant: appending a forming (unclosed) bar must NEVER change the
    verdict computed on the closed history."""
    df = _bullish_setup()
    res_closed = strategy.evaluate_tjr_setup(df)
    now_row = pd.DataFrame(
        [{"open": 100.6, "high": 100.9, "low": 100.2, "close": 100.4,
          "volume": 500_000}],
        index=pd.DatetimeIndex([pd.Timestamp.now(tz=NY)], name="timestamp"),
    )
    res_with_forming = strategy.evaluate_tjr_setup(pd.concat([df, now_row]))
    assert res_with_forming == res_closed
    assert res_closed["setup_valid"] is True, res_closed


def _delayed_setup(arm_bars: int = 12) -> pd.DataFrame:
    """Two-stage variant: sweep 10 bars ago, displacement NOW.

    Strict arm (5) never sees the sweep → no setup. Wide arm (≥10) fires:
    sweep @ -10 → quiet drift → displacement leg -3..-1 (MSS close + FVG
    between -3.high 100.30 and -2.low 100.48).
    """
    n = max(25, 20 + arm_bars + 1) + 2
    when = datetime(2026, 9, 24, 10, 30, tzinfo=NY)
    df = _session_bars(n, when - timedelta(minutes=5 * (n - 1)))
    loc = df.columns.get_loc
    df.iloc[-10, loc("low")] = 99.00                      # sweep low
    df.iloc[-10, loc("high")] = 100.01
    df.iloc[-10, loc("close")] = 100.02                   # back inside
    for p in range(-9, -3):                               # quiet drift up
        df.iloc[p, loc("close")] = 100.10 + 0.02 * (p + 9)
        df.iloc[p, loc("high")] = df.iloc[p, loc("close")] + 0.02
        df.iloc[p, loc("low")] = df.iloc[p, loc("close")] - 0.02
    # displacement leg: MSS close @ -1, FVG -3.high → -2.low = 0.18
    df.iloc[-3, loc("high")] = 100.30
    df.iloc[-3, loc("low")] = 100.26
    df.iloc[-3, loc("close")] = 100.28
    df.iloc[-2, loc("low")] = 100.48
    df.iloc[-2, loc("high")] = 100.52
    df.iloc[-2, loc("close")] = 100.50
    df.iloc[-1, loc("low")] = 100.46
    df.iloc[-1, loc("high")] = 100.60
    df.iloc[-1, loc("close")] = 100.55
    return df


def test_two_stage_delayed_chain_fires_with_wide_arm() -> None:
    old = config.SWEEP_ARM_BARS
    try:
        config.SWEEP_ARM_BARS = 12
        res = strategy.evaluate_tjr_setup(_delayed_setup(12))
        assert res["setup_valid"] is True, res
        assert res["direction"] == "BUY"
        assert res["checks"]["sweep_age_bars"] == 9
    finally:
        config.SWEEP_ARM_BARS = old


def test_delayed_chain_rejected_by_strict_arm() -> None:
    old = config.SWEEP_ARM_BARS
    try:
        config.SWEEP_ARM_BARS = 5
        res = strategy.evaluate_tjr_setup(_delayed_setup(12))
        assert res["setup_valid"] is False
        assert res["checks"]["liquidity_sweep"] is False
    finally:
        config.SWEEP_ARM_BARS = old


def test_volume_confirmation_filters_quiet_triggers() -> None:
    """Same textbook setup, trigger volume varied: high vol fires (with the
    filter on), mean vol is rejected, and defaults leave it off."""
    old = (config.VOLUME_CONFIRM, config.VOL_MULT)
    try:
        df = _bullish_setup()
        df.iloc[-1, df.columns.get_loc("volume")] = 3_000_000  # 3× baseline
        config.VOLUME_CONFIRM, config.VOL_MULT = True, 1.5
        res = strategy.evaluate_tjr_setup(df)
        assert res["setup_valid"] is True, res
        assert res["checks"]["vol_ratio"] >= 1.5

        df2 = _bullish_setup()
        df2.iloc[-1, df2.columns.get_loc("volume")] = 1_000_000  # 1× baseline
        res2 = strategy.evaluate_tjr_setup(df2)
        assert res2["setup_valid"] is False
        assert res2["checks"]["volume_confirm"] is False
    finally:
        config.VOLUME_CONFIRM, config.VOL_MULT = old


def test_volume_confirmation_off_by_default() -> None:
    old = config.VOLUME_CONFIRM
    try:
        config.VOLUME_CONFIRM = False
        df = _bullish_setup()
        df.iloc[-1, df.columns.get_loc("volume")] = 1  # absurdly low
        res = strategy.evaluate_tjr_setup(df)
        assert res["setup_valid"] is True  # filter off → no effect
    finally:
        config.VOLUME_CONFIRM = old


def test_volume_confirmation_fails_closed_on_bad_data() -> None:
    old = config.VOLUME_CONFIRM
    try:
        config.VOLUME_CONFIRM, config.VOL_MULT = True, 1.5
        df = _bullish_setup()
        df.iloc[-1, df.columns.get_loc("volume")] = None  # corrupt trigger
        res = strategy.evaluate_tjr_setup(df)
        assert res["setup_valid"] is False
        assert res["checks"]["volume_confirm"] is False
    finally:
        config.VOLUME_CONFIRM = old


def test_sweep_without_mss_fails() -> None:
    when = datetime(2026, 9, 24, 10, 30, tzinfo=NY)
    n = strategy_min_bars() + 5
    df = _session_bars(n, when - timedelta(minutes=5 * (n - 1)))
    df.iloc[-5, df.columns.get_loc("low")] = 99.00       # sweep
    df.iloc[-5, df.columns.get_loc("close")] = 100.05    # close back inside
    # No displacement: closes stay inside the 10-bar range → MSS fails.
    res = strategy.evaluate_tjr_setup(df)
    assert res["setup_valid"] is False
    assert res["checks"]["liquidity_sweep"] is True
    assert res["checks"]["mss"] is False


def test_fvg_below_min_size_fails() -> None:
    when = datetime(2026, 9, 24, 10, 30, tzinfo=NY)
    n = strategy_min_bars() + 5
    df = _session_bars(n, when - timedelta(minutes=5 * (n - 1)))
    df.iloc[-5, df.columns.get_loc("low")] = 99.00
    df.iloc[-5, df.columns.get_loc("close")] = 100.05    # sweep ✓
    df.iloc[-5, df.columns.get_loc("high")] = 100.62    # cap: no sweep-bar FVG
    df.iloc[-4, df.columns.get_loc("high")] = 100.60
    df.iloc[-4, df.columns.get_loc("low")] = 100.00
    df.iloc[-4, df.columns.get_loc("close")] = 100.55
    df.iloc[-3, df.columns.get_loc("low")] = 100.58
    df.iloc[-3, df.columns.get_loc("high")] = 100.75
    df.iloc[-3, df.columns.get_loc("close")] = 100.72
    df.iloc[-2, df.columns.get_loc("low")] = 100.68      # gap vs -4.high = 0.08
    df.iloc[-2, df.columns.get_loc("high")] = 100.80
    df.iloc[-2, df.columns.get_loc("close")] = 100.78
    df.iloc[-1, df.columns.get_loc("low")] = 100.76      # gap vs -3.high = 0.01
    df.iloc[-1, df.columns.get_loc("high")] = 100.85
    df.iloc[-1, df.columns.get_loc("close")] = 100.83    # MSS ✓
    res = strategy.evaluate_tjr_setup(df)
    assert res["setup_valid"] is False
    assert res["checks"]["liquidity_sweep"] is True
    assert res["checks"]["mss"] is True
    assert res["checks"]["fvg"] is False   # both gaps < 0.15% of price


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    raise SystemExit(1 if failures else 0)
