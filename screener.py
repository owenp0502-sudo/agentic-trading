"""
screener.py — Pre-market dynamic watchlist (top most-active US equities).

Uses Alpaca's Screener API for the top-10 most active stocks by volume,
filters penny stocks (price < $10) and always retains the SPY/QQQ anchor
ETFs. Falls back to a static liquid list if the API hiccups — the scanner
must never crash because the screener did.
"""

import logging
from typing import List, Optional

import config

logger = logging.getLogger(__name__)

# Imported defensively so a partial alpaca-py install degrades to the
# fallback watchlist instead of ImportError-ing the whole scanner.
SCREENER_AVAILABLE = False
try:
    from alpaca.data.historical.screener import ScreenerClient
    try:  # alpaca-py >= 0.36 names the request model MostActivesRequest
        from alpaca.data.historical.screener import MostActivesRequest as _MostActivesReq
    except ImportError:  # older/newer variants
        from alpaca.data.historical.screener import MostActiveRequest as _MostActivesReq
    from alpaca.data.historical.stock import StockHistoricalDataClient
    from alpaca.data.requests import StockLatestBarRequest
    SCREENER_AVAILABLE = True
except ImportError as exc:
    logger.warning("alpaca-py screener imports unavailable: %s", exc)


def _symbols_from_response(resp: object) -> List[str]:
    """Extract symbols from a MostActives response (objects or raw dicts)."""
    rows = getattr(resp, "most_actives", None)
    if rows is None and isinstance(resp, dict):
        rows = resp.get("most_actives", [])
    symbols: List[str] = []
    for row in rows or []:
        sym = getattr(row, "symbol", None)
        if sym is None and isinstance(row, dict):
            sym = row.get("symbol")
        if sym:
            symbols.append(str(sym).upper())
    return symbols


def _filter_penny_stocks(symbols: List[str]) -> List[str]:
    """Drop symbols whose latest trade price is below MIN_STOCK_PRICE."""
    if not symbols:
        return []
    client = StockHistoricalDataClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY)
    latest = client.get_latest_bars(StockLatestBarRequest(symbol_or_symbols=symbols))
    kept: List[str] = []
    for sym in symbols:
        try:
            close = float(latest[sym].close)
            if close >= config.MIN_STOCK_PRICE:
                kept.append(sym)
            else:
                logger.debug("Filtered %s — penny stock (close %.2f)", sym, close)
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            # Can't price it → can't trade it responsibly. Drop it.
            logger.debug("Dropping %s — no price data (%s)", sym, exc)
    return kept


def get_daily_dynamic_watchlist() -> List[str]:
    """
    Top-10 most active US stocks by volume (price >= $10), always anchored
    by SPY/QQQ. Falls back to config.WATCHLIST_FALLBACK on any failure.
    """
    if not SCREENER_AVAILABLE:
        logger.warning("Screener unavailable — using fallback watchlist.")
        return list(config.WATCHLIST_FALLBACK)
    if not config.ALPACA_API_KEY or not config.ALPACA_SECRET_KEY:
        logger.warning("Alpaca keys missing — using fallback watchlist.")
        return list(config.WATCHLIST_FALLBACK)

    try:
        screener = ScreenerClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY)
        request_kwargs = {"top": config.MOST_ACTIVE_COUNT}
        try:  # preferred form: explicit sort-by-volume
            from alpaca.data.enums import MostActivesBy
            request_kwargs["by"] = MostActivesBy.VOLUME
        except ImportError:
            pass
        try:
            resp = screener.get_most_actives(_MostActivesReq(**request_kwargs))
        except TypeError:
            # Schema drift — retry with the bare minimum constructor.
            resp = screener.get_most_actives(_MostActivesReq(top=config.MOST_ACTIVE_COUNT))

        symbols = _symbols_from_response(resp)[: config.MOST_ACTIVE_COUNT]
        tradable = _filter_penny_stocks(symbols)
        logger.info("Screener returned %d actives, %d after penny filter",
                    len(symbols), len(tradable))

        # Anchors first, preserve screener rank, dedupe.
        watchlist: List[str] = list(config.ANCHOR_TICKERS)
        for sym in tradable:
            if sym not in watchlist:
                watchlist.append(sym)
        return watchlist

    except Exception as exc:  # noqa: BLE001 — any API failure → static list
        logger.warning("Screener API failed (%s) — using fallback watchlist.", exc)
        return list(config.WATCHLIST_FALLBACK)
