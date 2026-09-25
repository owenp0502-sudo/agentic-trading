"""
backtest.py — Replay recent sessions through the REAL pipeline code.

Uses strategy.evaluate_tjr_setup() for signals and exits.compute_stop()/
compute_target()/trailed_stop() for management — the same modules the live
scanner runs, so tuning here transfers 1:1.

Simulation rules (conservative, matched to the live cadence):
  * One position at a time (MAX_ACTIVE_POSITIONS=1): signals are simulated
    chronologically and skipped while a position is open.
  * Long-only by default — the agentic account's short path needs borrow
    support we haven't verified (--allow-shorts to include short setups).
  * Signal on closed bar i → entry at bar i+1 open (entries that would land
    at/after FLATTEN_TIME are skipped).
  * Per 5m bar: stop checked BEFORE target; if both are touched inside one
    bar, the STOP wins (worst case).
  * Trail ratchets evaluate on bar close, exactly like the 5-min monitor.
  * Still open at FLATTEN_TIME → market-out at that bar's open (live parity).
  * Costs: Robinhood commissions are $0; slippage applies half the spread
    to each side via --slippage-bps (default 0 — 5m IEX bars are coarse).

Run:  python backtest.py --days 10 [--symbols SPY TSLA ...] [--equity 25000]
"""

import argparse
import logging
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
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


def _fetch_day(client, symbol: str, day: datetime) -> Optional[pd.DataFrame]:
    """One session of 1-minute bars (07:00–16:00 ET, resampled to 5m).

    Starts at 07:00 so ~30 bars of pre-market history exist by the 09:30
    open — the strategy needs >=26 bars before it evaluates anything, and
    the live scanner likewise carries pre-market context into the window.
    """
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from alpaca.data.enums import DataFeed

    start = day.replace(hour=7, minute=0, second=0, microsecond=0)
    end = day.replace(hour=16, minute=0, second=0, microsecond=0)
    try:
        bars = client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=symbol, timeframe=TimeFrame.Minute,
            start=start, end=end, feed=DataFeed.IEX))
        df = bars.df
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s %s: fetch failed: %s", symbol, day.date(), exc)
        return None
    if df is None or df.empty:
        return None
    if isinstance(df.index, pd.MultiIndex):  # alpaca-py: (symbol, timestamp)
        df = df.xs(symbol, level="symbol")
    if isinstance(df.columns, pd.MultiIndex):
        df = df.xs(symbol, level="symbol", axis=1)
    df = df.rename(columns={c: str(c).lower() for c in df.columns})
    df = df[["open", "high", "low", "close", "volume"]].sort_index()
    return df.resample("5min").agg(
        {"open": "first", "high": "max", "low": "min",
         "close": "last", "volume": "sum"}).dropna()


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
        max_stop_pct: Optional[float] = None) -> None:
    # Optional experiment overrides (restored after the run)
    saved = (config.SESSION_START, config.SESSION_END, config.FVG_REQUIRED,
             config.MAX_STOP_DISTANCE_PCT)
    if session_start:
        config.SESSION_START = session_start
    if session_end:
        config.SESSION_END = session_end
    if no_fvg:
        config.FVG_REQUIRED = False
    if max_stop_pct is not None:
        config.MAX_STOP_DISTANCE_PCT = max_stop_pct
    try:
        _run_inner(days, symbols, equity, allow_shorts, slip_bps)
    finally:
        (config.SESSION_START, config.SESSION_END, config.FVG_REQUIRED,
         config.MAX_STOP_DISTANCE_PCT) = saved


def _run_inner(days: int, symbols: List[str], equity: float,
               allow_shorts: bool, slip_bps: float) -> None:
    client = _alpaca_client()
    slip = slip_bps / 10_000.0
    today = datetime.now(tz=NY_TZ).replace(hour=12, minute=0,
                                           second=0, microsecond=0)
    calendar = [today - timedelta(days=k) for k in range(days, 0, -1)
                if (today - timedelta(days=k)).weekday() < 5]

    min_bars = max(config.MIN_BARS_REQUIRED,
                   config.SWEEP_LOOKBACK + config.SWEEP_SCAN_BARS + 1)

    # Pass 1: collect every valid signal across symbols/days.
    # Context fidelity: the live scanner feeds evaluate_tjr_setup a rolling
    # ~46-bar window (fetch_5m_bars: 40-bar lookback + 6-bar buffer), NOT the
    # whole day. Mirror that exactly — otherwise history depth differs from
    # production and sweep levels shift.
    window_bars = 40 + 6
    signals: List[Dict[str, Any]] = []
    for symbol in symbols:
        for day in calendar:
            df = _fetch_day(client, symbol, day)
            time.sleep(0.2)  # be polite to the free IEX feed
            if df is None or len(df) < min_bars + 2:
                continue  # holiday or thin session
            for i in range(min_bars - 1, len(df) - 1):
                entry_ts = df.index[i + 1].to_pydatetime().astimezone(NY_TZ)
                if entry_ts.strftime("%H:%M") >= config.FLATTEN_TIME:
                    break  # no entries that would land at/after flatten
                window = df.iloc[max(0, i + 1 - window_bars):i + 1]
                if len(window) < min_bars:
                    continue  # live scanner would also skip (not enough bars)
                setup = evaluate_tjr_setup(window)
                if not setup["setup_valid"]:
                    continue
                if setup["direction"] == "SELL" and not allow_shorts:
                    continue
                signals.append({
                    "symbol": symbol, "date": str(day.date()),
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
    args = ap.parse_args()
    if args.flatten_time:
        config.FLATTEN_TIME = args.flatten_time
    run(args.days, [s.upper() for s in args.symbols], args.equity,
        args.allow_shorts, args.slippage_bps, args.session_start,
        args.session_end, args.no_fvg, args.max_stop_pct)
