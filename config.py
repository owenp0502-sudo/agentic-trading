"""
config.py — Global constants, API keys, and environment variables.

ALL secrets flow through environment variables (local .env via python-dotenv,
GitHub Secrets on Actions, Render env vars in production). Nothing sensitive
is ever hard-coded here.
"""

import os
import logging
from typing import Dict, Optional

try:  # Optional: only local runs need .env loading; CI/Render inject real vars
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logger = logging.getLogger(__name__)


def _get_env(key: str, default: str = "") -> str:
    """Fetch an env var, trimming whitespace. Never raises."""
    return os.environ.get(key, default).strip()


# ---------------------------------------------------------------------------
# API keys / secrets
# ---------------------------------------------------------------------------
ALPACA_API_KEY: str = _get_env("ALPACA_API_KEY")
ALPACA_SECRET_KEY: str = _get_env("ALPACA_SECRET_KEY")

GEMINI_API_KEY: str = _get_env("GEMINI_API_KEY")

DISCORD_WEBHOOK_URL: str = _get_env("DISCORD_WEBHOOK_URL")
DISCORD_USER_ID: str = _get_env("DISCORD_USER_ID")

RENDER_WEBHOOK_URL: str = _get_env("RENDER_WEBHOOK_URL").rstrip("/")
WEBHOOK_PASSPHRASE: str = _get_env("WEBHOOK_PASSPHRASE")

# Robinhood MCP endpoint + optional auth headers (JSON string, e.g.
# '{"Authorization": "Bearer ..."}' if the chosen MCP server requires them).
ROBINHOOD_MCP_URL: str = _get_env(
    "ROBINHOOD_MCP_URL", "https://agent.robinhood.com/mcp/trading"
)
ROBINHOOD_MCP_HEADERS: Dict[str, str] = {}
_raw_headers = _get_env("ROBINHOOD_MCP_HEADERS")
if _raw_headers:
    try:
        import json

        parsed = json.loads(_raw_headers)
        if isinstance(parsed, dict):
            ROBINHOOD_MCP_HEADERS = {str(k): str(v) for k, v in parsed.items()}
    except (ValueError, TypeError) as exc:
        logger.warning("ROBINHOOD_MCP_HEADERS is not valid JSON — ignoring: %s", exc)

# ---------------------------------------------------------------------------
# Gemini model routing
# ---------------------------------------------------------------------------
# Keys starting with "AQ." are Vertex AI express-mode keys; anything else is
# treated as a Gemini Developer API (AI Studio) key.
GEMINI_MODEL: str = _get_env("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_IS_VERTEX: bool = GEMINI_API_KEY.startswith("AQ.")

# ---------------------------------------------------------------------------
# Risk & sizing constants (hard-coded guardrails — code cannot talk its way
# around these; they mirror the Capital & Sizing Plan)
# ---------------------------------------------------------------------------
CAPITAL_ALLOCATION_PCT: float = 0.10   # Strict 10% of cash balance per trade
MAX_ACTIVE_POSITIONS: int = 1          # One position at a time
MIN_TRADE_NOTIONAL: float = 1.0        # Robinhood minimum order value ($)

# ---------------------------------------------------------------------------
# Session window (all logic uses America/New_York; never local machine time)
# ---------------------------------------------------------------------------
TIMEZONE: str = "America/New_York"
SESSION_START: str = "09:30"           # NY open
SESSION_END: str = "11:00"             # End of tradeable window
FALLBACK_ACCOUNT_BALANCE: Optional[float] = (
    float(_get_env("ACCOUNT_BALANCE")) if _get_env("ACCOUNT_BALANCE") else None
)

# ---------------------------------------------------------------------------
# Strategy parameters
# ---------------------------------------------------------------------------
SWEEP_LOOKBACK: int = 20               # Bars scanned for liquidity (stops) grabs
SWEEP_SCAN_BARS: int = 5               # Sweep must occur within last N bars (25 min)
MSS_LOOKBACK: int = 10                 # Swing-point window for structure shift
FVG_SCAN_BARS: int = 6                 # FVG searched within last N bars (disp. leg)
FVG_MIN_IMBALANCE_PCT: float = 0.15    # Min FVG size as % of asset price
MIN_BARS_REQUIRED: int = 25            # Enough history for 20-bar sweep lookback

# ---------------------------------------------------------------------------
# Screener parameters
# ---------------------------------------------------------------------------
MOST_ACTIVE_COUNT: int = 10            # Top-N most active stocks to keep
MIN_STOCK_PRICE: float = 10.0          # Filter out penny stocks
ANCHOR_TICKERS: list = ["SPY", "QQQ"]  # Always retained index ETF anchors
WATCHLIST_FALLBACK: list = ["SPY", "QQQ", "NVDA", "TSLA", "AMD", "AAPL"]

# ---------------------------------------------------------------------------
# Networking
# ---------------------------------------------------------------------------
HTTP_TIMEOUT_SECONDS: int = 15
FLASK_PORT: int = int(_get_env("PORT", "10000"))  # Render exposes PORT

# Environment marker: "paper" (default) makes the TradingClient read-only-safe
# against paper accounts; set to "live" deliberately in Render to read real cash.
ALPACA_PAPER: bool = _get_env("ALPACA_PAPER", "paper").lower() != "live"
