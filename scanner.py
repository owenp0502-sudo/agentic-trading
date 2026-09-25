"""
scanner.py — Event scanner executed by GitHub Actions every 5 minutes.

Flow: watchlist → 5m bars (closed only) → TJR setup check → 10% position
sizing → in-process execution via executor.py (deterministic pre-checks →
Gemini+Robinhood MCP placement → Discord alert). The scanner NEVER talks to
the broker directly and the LLM never touches numbers. Fully stateless per
run; idempotency comes from the executor's pre-checks.

Drill mode: `python scanner.py --selftest` runs a synthetic end-to-end check
(no session gate) — deterministic guardrail unit checks plus one plumbing
card. Under EXECUTOR_DRY_RUN=1 the plumbing card is simulated; set it to 0
deliberately for a real end-to-end placement.
"""

import logging
import os
import sys
from datetime import datetime
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

import pandas as pd

import config
import exits
from discord_notify import send_discord_alert
from screener import get_daily_dynamic_watchlist
from strategy import evaluate_tjr_setup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("scanner")

SCANNER_AVAILABLE = False
try:
    from alpaca.data.historical.stock import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from alpaca.data.enums import DataFeed
    SCANNER_AVAILABLE = True
except ImportError as exc:
    logger.error("alpaca-py unavailable: %s — scanner cannot fetch bars.", exc)


def fetch_5m_bars(client: "StockHistoricalDataClient", symbol: str,
                  lookback_bars: int = 40) -> Optional[pd.DataFrame]:
    """Fetch recent 5-minute bars (IEX feed = free tier) as a DataFrame."""
    end = pd.Timestamp.now(tz=config.TIMEZONE)
    # 40 bars ≈ 3.3 hours: comfortably covers the 20-bar sweep lookback
    # plus pre-market context, without pulling a whole day.
    start = end - pd.Timedelta(minutes=5 * (lookback_bars + 6))
    request = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Minute,
        start=start,
        end=end,
        feed=DataFeed.IEX,           # free data feed
    )
    bars = client.get_stock_bars(request)
    df = bars.df
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):  # multi-symbol responses
        df = df.xs(symbol, level="symbol")
    df = df.rename(columns={c: str(c).lower() for c in df.columns})
    return df[["open", "high", "low", "close", "volume"]]


def get_cash_balance() -> Optional[float]:
    """
    Read the account's cash balance via the Alpaca TRADING API. Paper by
    default (config.ALPACA_PAPER). Returns None on failure — the caller
    then refuses to size a trade rather than guessing.
    """
    try:
        from alpaca.trading.client import TradingClient
        base_url = ("https://paper-api.alpaca.markets" if config.ALPACA_PAPER
                    else None)
        client = TradingClient(
            config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY,
            paper=config.ALPACA_PAPER, url_override=base_url,
        )
        account = client.get_account()
        return float(account.cash)
    except Exception as exc:  # noqa: BLE001
        logger.error("Could not read account cash: %s", exc)
        return None


def execute_in_process(signal: Dict[str, object]) -> bool:
    """
    Execute the trade card in-process (GitHub Actions pipeline):
    deterministic pre-checks → Gemini+MCP placement → Discord alert.
    Under EXECUTOR_DRY_RUN the fill is simulated (alert path still fires).
    """
    import executor as executor_core

    if config.EXECUTOR_DRY_RUN:
        logger.warning("EXECUTOR_DRY_RUN=1 — simulating fill (no MCP call, "
                       "no order placed).")
        report = ("[DRY RUN] Simulated fill: {action} {qty} {ticker} for "
                  "${dv:,.2f}.".format(action=signal["action"],
                                       qty=signal["quantity"],
                                       ticker=signal["ticker"],
                                       dv=signal["dollar_val"]))
        send_discord_alert(str(signal["action"]), str(signal["ticker"]),
                           float(signal["quantity"]),
                           float(signal["dollar_val"]),
                           reason=report, success=True)
        return True

    ok, report = executor_core.execute_signal(signal)
    logger.info("Execution result ok=%s report=%s", ok, report[:200])
    alert_reason = report if ok else f"EXECUTION FAILED: {report}"
    if ok:
        # Deterministic bracket: resting stop (GTC) + target (GFD), sized to
        # whole shares (fractional remainder is handled by monitor/flatten).
        prot = exits.attach_protections(signal)
        logger.info("Protections: %s", prot)
        alert_reason = f"{report} | {prot}"[:1000]
    send_discord_alert(
        action=str(signal["action"]), ticker=str(signal["ticker"]),
        quantity=float(signal["quantity"]),
        dollar_val=float(signal["dollar_val"]),
        reason=alert_reason,
        success=ok,
    )
    return ok


def _selftest() -> int:
    """
    Synthetic end-to-end drill, independent of the 09:30–11:00 session gate.
    1. Deterministic guardrail unit checks (no network).
    2. One plumbing card through execute_in_process → Discord alert +
       dry-run simulation (or the real path when EXECUTOR_DRY_RUN=0).
    Exit 0 = all green.
    """
    import executor as executor_core

    failures = 0

    def _check(name: str, ok: bool) -> None:
        nonlocal failures
        print(("PASS" if ok else "FAIL") + "  " + name)
        if not ok:
            failures += 1

    print("=== scanner selftest ===")

    # 1. deterministic guardrails (pure functions, no network)
    _check("cap allows $10 at 10% of $100 cash",
           executor_core.check_cap(10.0, 100.0)[0])
    _check("cap blocks $10.20 (beyond 1% headroom of $10.10)",
           not executor_core.check_cap(10.20, 100.0)[0])
    _check("cap blocks $102 at 10% of $100 cash",
           not executor_core.check_cap(102.0, 100.0)[0])
    _check("cap refuses when cash is unknown",
           not executor_core.check_cap(5.0, None)[0])
    _check("min notional blocks $0.50",
           not executor_core.check_min_notional(0.5)[0])
    _check("one-position rule allows 0 open",
           executor_core.check_no_open_positions(0)[0])
    _check("one-position rule blocks 2 open",
           not executor_core.check_no_open_positions(2)[0])
    _check("one-position rule refuses when unknown",
           not executor_core.check_no_open_positions(None)[0])

    # 2. plumbing card (alert + dry-run sim, or real path if kill-switch off)
    card: Dict[str, object] = {
        "ticker": "SPY", "action": "BUY", "quantity": 1.0,
        "dollar_val": 100.0, "reason": "selftest plumbing drill",
    }
    _check("plumbing card executed (alert fired)", execute_in_process(card))

    print(f"=== selftest {'ALL GREEN' if failures == 0 else 'FAILED'} ===")
    return 1 if failures else 0


def main() -> None:
    logger.info("=== TJR scanner run started ===")

    if not SCANNER_AVAILABLE:
        logger.error("alpaca-py missing — aborting run.")
        return
    if not config.GEMINI_API_KEY:
        logger.error("GEMINI_API_KEY not set — cannot execute signals.")
        return

    # --- Close-out: at/after FLATTEN_TIME (11:00 ET) flatten everything ---
    # (The 11:00 and 11:05 crons land here; also any manual run late.)
    now = datetime.now(tz=ZoneInfo(config.TIMEZONE))
    if exits.should_flatten(now):
        logger.info("At/after %s ET — close-out flatten.", config.FLATTEN_TIME)
        actions = exits.flatten_all()
        for line in actions:
            logger.info("FLATTEN: %s", line)
        if actions:
            send_discord_alert(
                action="SELL", ticker="PORTFOLIO", quantity=None,
                dollar_val=None,
                reason=("11:00 close-out flatten: "
                        + " | ".join(actions))[:1000], success=True)
        else:
            logger.info("Nothing open — no flatten needed.")
        return

    # --- Exit monitor: deterministic stop/target check before new entries ---
    for line in exits.monitor_once():
        logger.info("EXIT MONITOR: %s", line)

    watchlist: List[str] = get_daily_dynamic_watchlist()
    logger.info("Watchlist: %s", watchlist)

    # One read-only account fetch per run: sizing and the max-position guard
    # use the SAME account the executor's cap check will verify (Robinhood).
    import executor as executor_core
    account_state = executor_core.fetch_account_state()
    rh_cash = account_state.get("cash")
    rh_open = account_state.get("open_positions")
    if rh_open is not None and rh_open >= config.MAX_ACTIVE_POSITIONS:
        logger.info("%d open position(s) — max reached, skipping scan.",
                    rh_open)
        return
    if rh_cash is None:
        logger.warning("Robinhood cash unavailable — falling back to Alpaca "
                       "for sizing only (executor cap check still applies).")

    data_client = StockHistoricalDataClient(
        config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY
    )
    open_positions = rh_open or 0  # executor re-validates anyway

    for symbol in watchlist:
        if open_positions >= config.MAX_ACTIVE_POSITIONS:
            logger.info("Max active positions reached — stopping scan.")
            break
        try:
            df = fetch_5m_bars(data_client, symbol)
            if df is None or df.empty:
                logger.warning("No bars returned for %s — skipping.", symbol)
                continue

            setup = evaluate_tjr_setup(df)
            if not setup["setup_valid"]:
                logger.debug("%s: no setup — %s", symbol, setup["checks"])
                continue

            # --- Position sizing: strict 10% of cash ----------------------
            # trade_budget = balance * CAPITAL_ALLOCATION_PCT (0.10)
            # quantity     = trade_budget / last_close, 4dp fractional shares
            balance = rh_cash if rh_cash is not None else get_cash_balance()
            if balance is None:
                logger.error("No balance available — skipping %s signal.", symbol)
                continue
            trade_budget = balance * config.CAPITAL_ALLOCATION_PCT
            stock_price = float(df["close"].iloc[-1])
            if stock_price <= 0 or trade_budget < config.MIN_TRADE_NOTIONAL:
                logger.warning("Budget $%.2f below min notional — skipping %s.",
                               trade_budget, symbol)
                continue
            quantity = round(trade_budget / stock_price, 4)

            signal = {
                "ticker": symbol,
                "action": setup["direction"],           # 'BUY' | 'SELL'
                "quantity": quantity,
                "dollar_val": round(quantity * stock_price, 2),
                "stock_price": stock_price,
                "fvg_price": setup["fvg_price"],
                # Structural stop anchor: the swept liquidity extreme. If
                # price trades back through it, the setup is invalidated —
                # the bracket stop sits just beyond it (clamped).
                "stop_anchor": setup["checks"].get("sweep_level"),
                "reason": (
                    f"TJR {setup['direction']}: liquidity sweep + MSS + FVG "
                    f"@ {setup['fvg_price']} on 5m"
                ),
            }
            logger.info("SIGNAL %s: %s", symbol, signal)

            if execute_in_process(signal):
                open_positions += 1

        except Exception as exc:  # noqa: BLE001 — one symbol must not kill the scan
            logger.error("Error processing %s: %s", symbol, exc)

    logger.info("=== Scanner run complete (%d signal(s) sent) ===", open_positions)


if __name__ == "__main__":
    if "--flatten" in sys.argv:
        print("Manual flatten requested (deterministic, no LLM):")
        for line in exits.flatten_all():
            print(" ", line)
        raise SystemExit(0)
    if "--selftest" in sys.argv or os.environ.get("SCANNER_SELFTEST", "") == "1":
        raise SystemExit(_selftest())
    main()
