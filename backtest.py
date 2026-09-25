"""
backtest.py — Replay recent sessions through the REAL pipeline code.

Uses strategy.evaluate_tjr_setup() for signals and exits.compute_stop()/
compute_target()/trailed_stop() for management — the same modules the live
scanner runs, so tuning here transfers 1:1.

DATA-INTEGRITY PROTOCOL (mandatory — see README research log, Sep 25 2026):
A study is only valid if the detector's context window never crosses
sessions. The old continuous-index cache let the 46-bar window read the
overnight gap as a sweep+MSS+FVG, fabricating 41 "trades" on 15m bars.
Therefore:
  1. Raw bars are fetched in bulk, then SLICED per symbol-day (07:00–16:00
     ET) BEFORE resampling and BEFORE any windowing — matching the live
     scanner's per-run context.
  2. Every dataset is byte-verified (pandas assert_frame_equal) against the
     validated per-day fetch path on sampled days — at build time and again
     on every cache load. A mismatch aborts the run loudly.
  3. The simulation asserts every context window stays within one date.

Simulation rules (conservative, matched to the live cadence):
  * One position at a time (MAX_ACTIVE_POSITIONS=1): signals are simulated
    chronologically and skipped while a position is open.
  * Long-only by default — the agentic account's short path needs borrow
    support we haven't verified (--allow-shorts to include short setups).
  * Signal on closed bar i → entry at bar i+1 open (entries that would land
    at/after FLATTEN_TIME are skipped).
  * Per bar: stop checked BEFORE target; if both are touched inside one
    bar, the STOP wins (worst case).
  * Trail ratchets evaluate on bar close, exactly like the 5-min monitor.
  * Still open at FLATTEN_TIME → market-out at that bar's open (live parity).
  * Costs: Robinhood commissions are $0; slippage applies half the spread
    to each side via --slippage-bps (default 0 — IEX bars are coarse).

Run:  python backtest.py --days 10 [--symbols SPY TSLA ...] [--equity 25000]
      python backtest.py --days 60 --freq 15min --refresh
"""

import argparse
import hashlib
import logging
import pickle
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import pandas as pd

import config
import exits
from strategy import evaluate_tjr_setup

NY_TZ = ZoneInfo(config.TIMEZONE)

logging.basicConfig(level=logging.WARNING,
                    format="%(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger("backtest")


def _alpaca_client():
    from alpaca.data.historical.stock import StockHistoricalDataClient
    if not config.ALPACA_API_KEY:
        raise SystemExit("ALPACA_API_KEY missing — set .env first")
    return StockHistoricalDataClient(
        config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY)


def _normalize_bars(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """Shared bar hygiene for every data path (single source of truth):
    strip alpaca-py wrappers, lowercase columns, select OHLCV, force an
    America/New_York index. Bulk fetch and _fetch_day both funnel through
    here so byte-verification compares like with like."""
    if df is None or df.empty:
        return pd.DataFrame()
    if isinstance(df.index, pd.MultiIndex):  # alpaca-py: (symbol, timestamp)
        df = df.xs(symbol, level="symbol")
    if isinstance(df.columns, pd.MultiIndex):
        df = df.xs(symbol, level="symbol", axis=1)
    df = df.rename(columns={c: str(c).lower() for c in df.columns})
    df = df[["open", "high", "low", "close", "volume"]].sort_index()
    idx = df.index
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    df.index = idx.tz_convert(NY_TZ)
    return df


def _resample_ohlcv(df: pd.DataFrame, freq: str) -> pd.DataFrame:
    """1m bars → freq bars ("5min"/"15min"), lossless OHLCV composition."""
    return df.resample(freq).agg(
        {"open": "first", "high": "max", "low": "min",
         "close": "last", "volume": "sum"}).dropna()


def _fetch_day(client, symbol: str, day: datetime,
               freq: str = "5min") -> Optional[pd.DataFrame]:
    """One session of bars (07:00–16:00 ET), sliced per day BEFORE
    resampling to `freq` — the validated reference path. 07:00 start gives
    ~30 5m bars of pre-market history by the 09:30 open (the strategy needs
    >=26 bars and the live scanner likewise carries pre-market context)."""
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from alpaca.data.enums import DataFeed

    # tz-AWARE request bounds — naive timestamps are ambiguously
    # interpreted by broker APIs and can silently truncate the day.
    start = day.replace(hour=7, minute=0, second=0, microsecond=0,
                        tzinfo=NY_TZ)
    end = day.replace(hour=16, minute=0, second=0, microsecond=0,
                      tzinfo=NY_TZ)
    try:
        bars = client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=symbol, timeframe=TimeFrame.Minute,
            start=start, end=end, feed=DataFeed.IEX))
        df = _normalize_bars(bars.df, symbol)
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s %s: fetch failed: %s", symbol, day.date(), exc)
        return None
    if df.empty:
        return None
    day_slice = df.loc[str(day.date())].between_time("07:00", "16:00")
    if day_slice.empty:
        return None
    return _resample_ohlcv(day_slice, freq)


def _verify_dataset(dataset: Dict[Tuple[str, str], pd.DataFrame], client,
                    samples: int = 3) -> None:
    """Byte-verify sampled symbol-day frames against the validated per-day
    fetch path. Any mismatch is a hard abort — a silently contaminated
    dataset invalidates every number downstream of it."""
    import random
    rng = random.Random(42)  # deterministic sampling, reproducible logs
    keys = rng.sample(sorted(dataset.keys()), min(samples, len(dataset)))
    for sym, date in keys:
        ref = _fetch_day(client, sym, datetime.strptime(date, "%Y-%m-%d"))
        mine = dataset[(sym, date)]
        if ref is None:
            continue  # holiday in the reference path — nothing to compare
        try:
            pd.testing.assert_frame_equal(mine, ref)
        except AssertionError:
            print(f"VERIFY MISMATCH: {sym} {date}: bulk={mine.shape} "
                  f"ref={ref.shape} bulk[{mine.index.min()}.."
                  f"{mine.index.max()}] ref[{ref.index.min()}.."
                  f"{ref.index.max()}]", flush=True)
            raise
    print(f"dataset verified vs _fetch_day ({len(keys)} samples) OK", flush=True)


def _get_dataset(client, symbols: List[str], calendar: List[datetime],
                 freq: str, refresh: bool) -> Dict[Tuple[str, str], pd.DataFrame]:
    """Per-symbol-day 5m/15m frames, built via the protocol and cached.

    Bulk-fetch once per symbol (fast), then slice per day 07:00–16:00 ET
    BEFORE resampling so no context window can ever cross a session.
    Cache key covers symbols+span+freq; loads are re-verified (2 samples)
    so a stale/poisoned cache fails loudly instead of lying.
    """
    import random
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from alpaca.data.enums import DataFeed


    start_date = calendar[0].date().isoformat()
    end_date = calendar[-1].date().isoformat()
    key_raw = f"{'|'.join(sorted(symbols))}|{start_date}|{end_date}|{freq}"
    key = hashlib.sha1(key_raw.encode()).hexdigest()[:12]
    cache_path = Path("/tmp") / f"tjr_bt_{key}.pkl"

    if not refresh and cache_path.exists():
        try:
            dataset = pickle.loads(cache_path.read_bytes())
            _verify_dataset(dataset, client, samples=2)
            print(f"dataset loaded from {cache_path} "
                  f"({len(dataset)} symbol-days, verified)", flush=True)
            return dataset
        except Exception as exc:  # noqa: BLE001 — rebuild on any doubt
            logger.warning("cache load/verify failed (%s) — rebuilding", exc)

    t0 = time.time()
    span_start = datetime.fromisoformat(start_date).replace(tzinfo=NY_TZ)
    span_end = datetime.fromisoformat(end_date).replace(
        hour=18, tzinfo=NY_TZ)
    dataset: Dict[Tuple[str, str], pd.DataFrame] = {}
    for symbol in symbols:
        df = pd.DataFrame()
        for attempt in (1, 2, 3):  # retry w/ backoff — bulk calls rate-limit
            try:
                bars = client.get_stock_bars(StockBarsRequest(
                    symbol_or_symbols=symbol, timeframe=TimeFrame.Minute,
                    start=span_start.replace(hour=4), end=span_end,
                    feed=DataFeed.IEX))
                df = _normalize_bars(bars.df, symbol)
                break
            except Exception as exc:  # noqa: BLE001
                wait = 5 * attempt
                logger.warning("%s: bulk fetch attempt %d failed: %s — "
                               "retrying in %ds", symbol, attempt, exc, wait)
                time.sleep(wait)
        if df.empty:
            # A missing symbol silently halves the sample — refuse to run.
            raise SystemExit(
                f"DATA PROTOCOL VIOLATION: no bars for {symbol} after "
                f"retries — refusing to build a partial dataset")
        for date in sorted({ts.date() for ts in df.index}):
            day_slice = df.loc[str(date)].between_time("07:00", "16:00")
            if day_slice.empty:
                continue
            f = _resample_ohlcv(day_slice, freq)
            if not f.empty:
                dataset[(symbol, str(date))] = f
        days = len({dt for (s, dt), _f in dataset.items() if s == symbol})
        print(f"  {symbol}: {len(df)} raw bars, {days} days "
              f"({time.time() - t0:.0f}s)", flush=True)
        time.sleep(1.0)  # be polite to the free IEX feed between symbols

    _verify_dataset(dataset, client, samples=3)
    cache_path.write_bytes(pickle.dumps(dataset))
    print(f"dataset: {len(dataset)} symbol-day {freq} frames cached → "
          f"{cache_path} ({time.time() - t0:.0f}s)", flush=True)
    return dataset


def _simulate_long(bars: pd.DataFrame, sig_idx: int, sweep_level: Optional[float],
                   equity: float, slip: float) -> Optional[Dict[str, Any]]:
    """Walk one long position from entry (sig_idx+1 open) to exit."""
    if sig_idx + 1 >= len(bars):
        return None
    entry = float(bars.iloc[sig_idx + 1]["open"]) * (1 + slip)
    if entry <= 0:
        return None
    stop = exits.compute_stop("BUY", entry, sweep_level)
    target = exits.compute_target("BUY", entry, stop)
    risk = entry - stop
    if risk <= 0:
        return None
    qty = (equity * config.CAPITAL_ALLOCATION_PCT) / entry

    exit_price: Optional[float] = None
    reason, exit_ts = None, None
    bars_held = 0
    for j in range(sig_idx + 1, len(bars)):
        bar = bars.iloc[j]
        ts = bar.name.to_pydatetime()
        if ts.astimezone(NY_TZ).strftime("%H:%M") >= config.FLATTEN_TIME:
            exit_price, reason, exit_ts = float(bar["open"]) * (1 - slip), \
                "FLATTEN", ts
            break
        low, high, close = (float(bar["low"]), float(bar["high"]),
                            float(bar["close"]))
        if low <= stop:                       # stop checked first (worst case)
            exit_price, reason, exit_ts = stop * (1 - slip), "STOP", ts
            break
        if high >= target:
            exit_price, reason, exit_ts = target * (1 - slip), "TARGET", ts
            break
        bars_held += 1
        # Time stop: flat position after N bars exits on this bar's close.
        if config.TIME_STOP_BARS and bars_held >= config.TIME_STOP_BARS:
            exit_price, reason, exit_ts = close * (1 - slip), \
                f"TIME-{config.TIME_STOP_BARS}", ts
            break
        new_stop = exits.trailed_stop("BUY", entry, stop, close)
        if new_stop is not None:
            stop = new_stop                   # monitor parity: ratchet on close

    if exit_price is None:                    # data ended mid-position
        last = bars.iloc[-1]
        exit_price = float(last["close"]) * (1 - slip)
        reason, exit_ts = "EOD-DATA", last.name.to_pydatetime()

    return {
        "entry": entry, "exit": exit_price, "initial_stop": stop,
        "target": target, "risk": risk, "reason": reason, "qty": qty,
        "entry_ts": bars.iloc[sig_idx + 1].name.to_pydatetime(),
        "exit_ts": exit_ts,
    }


def run(days: int, symbols: List[str], equity: float, allow_shorts: bool,
        slip_bps: float, session_start: Optional[str] = None,
        session_end: Optional[str] = None, no_fvg: bool = False,
        max_stop_pct: Optional[float] = None,
        arm_bars: Optional[int] = None, freq: str = "5min",
        refresh: bool = False, vol_mult: Optional[float] = None,
        tp_r: Optional[float] = None,
        time_stop_bars: Optional[int] = None) -> None:
    # Optional experiment overrides (restored after the run)
    saved = (config.SESSION_START, config.SESSION_END, config.FVG_REQUIRED,
             config.MAX_STOP_DISTANCE_PCT, config.SWEEP_ARM_BARS,
             config.VOLUME_CONFIRM, config.VOL_MULT,
             config.TAKE_PROFIT_R, config.TIME_STOP_BARS)
    if session_start:
        config.SESSION_START = session_start
    if session_end:
        config.SESSION_END = session_end
    if no_fvg:
        config.FVG_REQUIRED = False
    if max_stop_pct is not None:
        config.MAX_STOP_DISTANCE_PCT = max_stop_pct
    if arm_bars is not None:
        config.SWEEP_ARM_BARS = arm_bars
    if vol_mult is not None:  # enabling a multiplier turns the filter on
        config.VOLUME_CONFIRM = True
        config.VOL_MULT = vol_mult
    if tp_r is not None:
        config.TAKE_PROFIT_R = tp_r
    if time_stop_bars is not None:
        config.TIME_STOP_BARS = time_stop_bars
    try:
        _run_inner(days, symbols, equity, allow_shorts, slip_bps, freq,
                   refresh)
    finally:
        (config.SESSION_START, config.SESSION_END, config.FVG_REQUIRED,
         config.MAX_STOP_DISTANCE_PCT, config.SWEEP_ARM_BARS,
         config.VOLUME_CONFIRM, config.VOL_MULT,
         config.TAKE_PROFIT_R, config.TIME_STOP_BARS) = saved


def _run_inner(days: int, symbols: List[str], equity: float,
               allow_shorts: bool, slip_bps: float, freq: str,
               refresh: bool) -> None:
    client = _alpaca_client()
    slip = slip_bps / 10_000.0
    today = datetime.now(tz=NY_TZ).replace(hour=12, minute=0,
                                           second=0, microsecond=0)
    calendar = [today - timedelta(days=k) for k in range(days, 0, -1)
                if (today - timedelta(days=k)).weekday() < 5]

    min_bars = max(config.MIN_BARS_REQUIRED,
                   config.SWEEP_LOOKBACK + config.SWEEP_ARM_BARS + 1)

    # Pass 1: collect every valid signal across symbols/days.
    # Context fidelity: the live scanner feeds evaluate_tjr_setup a rolling
    # ~46-bar window (fetch_5m_bars: 40-bar lookback + 6-bar buffer), NOT the
    # whole day. Mirror that exactly — otherwise history depth differs from
    # production and sweep levels shift. Frames are per-symbol-day (protocol),
    # and the same-date guard below makes cross-session leakage impossible.
    window_bars = 40 + 6
    dataset = _get_dataset(client, symbols, calendar, freq, refresh)
    signals: List[Dict[str, Any]] = []
    for (symbol, date), df in dataset.items():
        n = len(df)
        for i in range(min_bars - 1, n - 1):
            entry_ts = df.index[i + 1].to_pydatetime().astimezone(NY_TZ)
            if entry_ts.strftime("%H:%M") >= config.FLATTEN_TIME:
                break  # no entries that would land at/after flatten
            window = df.iloc[max(0, i + 1 - window_bars):i + 1]
            if len(window) < min_bars:
                continue  # live scanner would also skip (not enough bars)
            # PROTOCOL GUARD: context must never cross a session. Per-day
            # slicing makes this structural; the assert turns any future
            # regression into a loud failure instead of silent garbage.
            assert len({ts.date() for ts in window.index}) == 1, \
                f"context window crosses sessions for {symbol} {date}"
            setup = evaluate_tjr_setup(window)
            if not setup["setup_valid"]:
                continue
            if setup["direction"] == "SELL" and not allow_shorts:
                continue
            signals.append({
                "symbol": symbol, "date": date,
                "sig_idx": i, "df": df,
                "sweep": setup["checks"].get("sweep_level"),
                "direction": setup["direction"],
                "entry_ts": entry_ts,
            })

    # Pass 2: simulate chronologically; one position at a time
    signals.sort(key=lambda s: s["entry_ts"])
    busy_until: Optional[datetime] = None
    trades: List[Dict[str, Any]] = []
    for sig in signals:
        if busy_until is not None and sig["entry_ts"] < busy_until:
            continue  # MAX_ACTIVE_POSITIONS=1 — executor would skip this
        if sig["direction"] == "SELL":
            continue  # long-exit engine only; shorts recorded as skipped
        result = _simulate_long(sig["df"], sig["sig_idx"], sig["sweep"],
                                equity, slip)
        if result is None:
            continue
        result.update({"date": sig["date"], "symbol": sig["symbol"],
                       "direction": sig["direction"]})
        trades.append(result)
        busy_until = result["exit_ts"]

    _report(trades, equity, allow_shorts, slip_bps)


def _report(trades: List[Dict[str, Any]], equity: float, allow_shorts: bool,
            slip_bps: float) -> None:
    stop_desc = ("structural(sweep)" if config.USE_STRUCTURAL_STOP
                 else f"{config.STOP_LOSS_PCT:.2%}")
    print("=" * 78)
    print(f"TJR backtest — stop={stop_desc} (clamped "
          f"{config.MIN_STOP_DISTANCE_PCT:.2%}–"
          f"{config.MAX_STOP_DISTANCE_PCT:.2%}), "
          f"TP={config.TAKE_PROFIT_R:g}R, trail_BE_R={config.TRAIL_BREAKEVEN_R:g}, "
          f"trail_pct={config.TRAIL_STOP_PCT:g}, flatten={config.FLATTEN_TIME} ET, "
          f"slippage={slip_bps:g}bps")
    print(f"long-only={not allow_shorts}, sizing=10% of ${equity:,.0f}/trade, "
          f"one position at a time, stop-before-target inside bars")
    print("=" * 78)

    if not trades:
        print("No trades simulated in the window. Widen --days/--symbols or "
              "relax filters.")
        return

    bal = equity
    peak = equity
    max_dd = 0.0
    wins = 0
    r_sum = 0.0
    print(f"{'date':<11}{'sym':<6}{'entry':>8}{'exit':>8}{'stop':>8}"
          f"{'R':>6}{'reason':<9}{'pnl':>10}{'balance':>12}")
    for t in trades:
        pnl = (t["exit"] - t["entry"]) * t["qty"]
        r = (t["exit"] - t["entry"]) / t["risk"]
        r_sum += r
        wins += 1 if pnl > 0 else 0
        bal += pnl
        peak = max(peak, bal)
        max_dd = max(max_dd, peak - bal)
        print(f"{t['date']:<11}{t['symbol']:<6}{t['entry']:>8.2f}"
              f"{t['exit']:>8.2f}{t['initial_stop']:>8.2f}{r:>6.2f}"
              f"{t['reason']:<9}{pnl:>10.2f}{bal:>12.2f}")

    n = len(trades)
    pnls = [(t["exit"] - t["entry"]) * t["qty"] for t in trades]
    gross_win = sum(p for p in pnls if p > 0)
    gross_loss = abs(sum(p for p in pnls if p <= 0))
    print("-" * 78)
    print(f"trades={n}  win-rate={wins / n:.0%}  avg-R={r_sum / n:.2f}  "
          f"total-pnl=${bal - equity:,.2f}  final=${bal:,.2f}  "
          f"max-DD=${max_dd:,.2f}")
    if gross_loss:
        print(f"profit-factor={gross_win / gross_loss:.2f}  "
              f"(gross win ${gross_win:,.2f} / gross loss ${gross_loss:,.2f})")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="TJR exit-rule backtester")
    ap.add_argument("--days", type=int, default=10,
                    help="calendar days back (weekends skipped)")
    ap.add_argument("--symbols", nargs="*", default=config.WATCHLIST_FALLBACK)
    ap.add_argument("--equity", type=float, default=25000.0)
    ap.add_argument("--allow-shorts", action="store_true",
                    help="also collect SELL setups (still simulated long-only)")
    ap.add_argument("--slippage-bps", type=float, default=0.0,
                    help="round-trip slippage in basis points (e.g. 2)")
    ap.add_argument("--no-fvg", action="store_true",
                    help="run the sweep→MSS variant (skip the FVG checklist "
                         "item) for frequency comparison")
    ap.add_argument("--session-start", type=str, default=None,
                    help="override session start HH:MM ET (e.g. 09:30)")
    ap.add_argument("--session-end", type=str, default=None,
                    help="override session end HH:MM ET (e.g. 16:00 — "
                         "last entry must still respect --flatten-time")
    ap.add_argument("--flatten-time", type=str, default=None,
                    help="override close-out time HH:MM ET")
    ap.add_argument("--max-stop-pct", type=float, default=None,
                    help="override the structural stop clamp, e.g. 0.03")
    ap.add_argument("--arm-bars", type=int, default=None,
                    help="two-stage detector: sweep arms the setup for N "
                         "bars (default: config value, strict = 5)")
    ap.add_argument("--freq", type=str, default="5min",
                    choices=["5min", "15min"],
                    help="bar timeframe (protocol applies to either)")
    ap.add_argument("--refresh", action="store_true",
                    help="rebuild the dataset cache instead of loading it")
    ap.add_argument("--vol-mult", type=float, default=None,
                    help="volume confirmation: trigger bar must be N× the "
                         "prior-20-bar mean volume (e.g. 1.5)")
    ap.add_argument("--tp-r", type=float, default=None,
                    help="override TAKE_PROFIT_R (e.g. 1.0 for 1R targets)")
    ap.add_argument("--time-stop", type=int, default=None,
                    help="exit flat positions after N bars (0 = hold to "
                         "flatten, the default)")
    args = ap.parse_args()
    if args.flatten_time:
        config.FLATTEN_TIME = args.flatten_time
    run(args.days, [s.upper() for s in args.symbols], args.equity,
        args.allow_shorts, args.slippage_bps, args.session_start,
        args.session_end, args.no_fvg, args.max_stop_pct, args.arm_bars,
        args.freq, args.refresh, args.vol_mult, args.tp_r, args.time_stop)
