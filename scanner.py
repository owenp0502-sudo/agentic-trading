"""
scanner.py — Event scanner executed by GitHub Actions every 5 minutes.

Flow: watchlist → 5m bars (closed only) → TJR setup check → 10% position
sizing → POST to the Render executor. The scanner NEVER talks to a broker;
it only computes and signals. Fully idempotent per run (stateless).
"""

import logging
import os
from typing import Dict, List, Optional

import pandas as pd
import requests

import config
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


def send_to_executor(signal: Dict[str, object]) -> bool:
    """POST the trade card to the Render executor. One retry on failure."""
    url = f"{config.RENDER_WEBHOOK_URL}/execute"
    headers = {
        "X-Webhook-Passphrase": config.WEBHOOK_PASSPHRASE,
        "Content-Type": "application/json",
    }
    for attempt in (1, 2):
        try:
            resp = requests.post(
                url, json=signal, headers=headers,
                timeout=config.HTTP_TIMEOUT_SECONDS,
            )
            if resp.status_code == 200:
                logger.info("Executor accepted %s %s",
                            signal["action"], signal["ticker"])
                return True
            logger.error("Executor rejected (HTTP %s): %s",
                         resp.status_code, resp.text[:300])
        except requests.RequestException as exc:
            logger.error("POST to executor failed (attempt %d): %s", attempt, exc)
    return False


def main() -> None:
    logger.info("=== TJR scanner run started ===")

    if not SCANNER_AVAILABLE:
        logger.error("alpaca-py missing — aborting run.")
        return
    if not config.RENDER_WEBHOOK_URL or not config.WEBHOOK_PASSPHRASE:
        logger.error("RENDER_WEBHOOK_URL / WEBHOOK_PASSPHRASE not set — aborting.")
        return

    watchlist: List[str] = get_daily_dynamic_watchlist()
    logger.info("Watchlist: %s", watchlist)

    data_client = StockHistoricalDataClient(
        config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY
    )
    open_positions = 0  # scanner-side counter (executor re-validates anyway)

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
            balance = get_cash_balance()
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
                "reason": (
                    f"TJR {setup['direction']}: liquidity sweep + MSS + FVG "
                    f"@ {setup['fvg_price']} on 5m"
                ),
            }
            logger.info("SIGNAL %s: %s", symbol, signal)

            if send_to_executor(signal):
                open_positions += 1

        except Exception as exc:  # noqa: BLE001 — one symbol must not kill the scan
            logger.error("Error processing %s: %s", symbol, exc)

    logger.info("=== Scanner run complete (%d signal(s) sent) ===", open_positions)


if __name__ == "__main__":
    main()
