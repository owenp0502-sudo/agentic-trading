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


def _get_env_float(key: str, default: float) -> float:
    """Fetch a float env var; fall back to default on missing/garbage."""
    raw = _get_env(key)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("%s=%r is not a float — using default %s",
                       key, raw, default)
        return default


def _get_env_int(key: str, default: int) -> int:
    """Fetch an int env var; fall back to default on missing/garbage."""
    raw = _get_env(key)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("%s=%r is not an int — using default %s",
                       key, raw, default)
        return default


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
# NOTE: both AIza (AI Studio) and AQ. (Vertex express) keys work via the
# Developer API endpoint (generativelanguage.googleapis.com) — verified
# empirically 2026-09-25. gemini-2.5-flash is retired for new users.
# ("or" fallback so an empty-but-present env var can't blank the model.)
GEMINI_MODEL: str = _get_env("GEMINI_MODEL") or "gemini-3.5-flash"

# ---------------------------------------------------------------------------
# Risk & sizing constants (hard-coded guardrails — code cannot talk its way
# around these; they mirror the Capital & Sizing Plan)
# ---------------------------------------------------------------------------
CAPITAL_ALLOCATION_PCT: float = 0.10   # Strict 10% of cash balance per trade
MAX_ACTIVE_POSITIONS: int = 1          # One position at a time
MIN_TRADE_NOTIONAL: float = 1.0        # Robinhood minimum order value ($)

# ---------------------------------------------------------------------------
# Exit strategy (deterministic — the LLM never sets exit levels)
# ---------------------------------------------------------------------------
# Stop distance as a fraction of entry cost — fallback when no structural
# anchor is available. Target = TAKE_PROFIT_R × the actual stop distance.
STOP_LOSS_PCT: float = _get_env_float("STOP_LOSS_PCT", 0.005)   # 0.5%
TAKE_PROFIT_R: float = _get_env_float("TAKE_PROFIT_R", 2.0)     # 2R target

# Require the FVG checklist item. Turning it off trades the sweep→MSS
# sequence alone (higher frequency, unproven quality — backtest first:
#   python backtest.py --no-fvg --days 20
FVG_REQUIRED: bool = _get_env(
    "FVG_REQUIRED", "1").lower() not in ("0", "false", "no")

# Displacement volume confirmation: trigger-bar volume ≥ VOL_MULT × mean
# volume of the prior VOL_LOOKBACK bars. OFF by default — new filter, live
# behavior unchanged until deliberately enabled (backtest: --vol-mult 1.5).
VOLUME_CONFIRM: bool = _get_env(
    "VOLUME_CONFIRM", "0").lower() in ("1", "true", "yes")
VOL_MULT: float = _get_env_float("VOL_MULT", 1.5)
VOL_LOOKBACK: int = _get_env_int("VOL_LOOKBACK", 20)

# Anchor the stop to the sweep level (TJR structure) when the scanner
# provides one: just beyond the swept extreme, with the distance clamped
# to [MIN, MAX] % of cost so noise can't stop us out and tails are bounded.
USE_STRUCTURAL_STOP: bool = _get_env(
    "USE_STRUCTURAL_STOP", "1").lower() not in ("0", "false", "no")
STRUCTURAL_STOP_BUFFER_PCT: float = 0.0005   # 5bps beyond the sweep extreme
MIN_STOP_DISTANCE_PCT: float = 0.001         # 0.1% — noise floor
MAX_STOP_DISTANCE_PCT: float = 0.02          # 2.0% — tail-risk bound

# Trailing policy (the monitor ratchets the resting stop; never loosens):
#   stage 1 — once price moves TRAIL_BREAKEVEN_R × risk in our favor,
#             stop moves to entry (breakeven). 0 disables.
#   stage 2 — if TRAIL_STOP_PCT > 0, stop then trails last price at that
#             % distance. 0 disables (breakeven-only by default).
TRAIL_BREAKEVEN_R: float = _get_env_float("TRAIL_BREAKEVEN_R", 1.0)
TRAIL_STOP_PCT: float = _get_env_float("TRAIL_STOP_PCT", 0.0)

# Time stop: if the position is still open after TIME_STOP_BARS bars and
# has not hit stop/target, exit on that bar's close. 0 disables (default:
# hold to FLATTEN_TIME). Rationale: the signal book's winners historically
# decay into flatten-time exits — see README research log.
TIME_STOP_BARS: int = _get_env_int("TIME_STOP_BARS", 0)

# Time (America/New_York, HH:MM) at/after which the close-out run flattens
# every open position and cancels resting orders (11:00 + 11:05 crons).
FLATTEN_TIME: str = _get_env("FLATTEN_TIME", "11:00")

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
SWEEP_ARM_BARS: int = _get_env_int(    # Two-stage detector: sweep 'arms' the
    "SWEEP_ARM_BARS", 5)               # setup for N bars; MSS while armed
                                       # fires. 5 == strict baseline (MSS bar
                                       # coincides with sweep window).
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

# Execution kill-switch: when "1"/"true", /execute still validates the full
# flow (passphrase gate, 10% cap pre-check, dedupe) but SKIPS Gemini + MCP and
# returns a simulated fill. Default off — flip deliberately for first
# deployments and drills.
EXECUTOR_DRY_RUN: bool = _get_env("EXECUTOR_DRY_RUN", "0").lower() in ("1", "true", "yes")
