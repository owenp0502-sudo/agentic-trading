"""
executor.py — Framework-free execution core (used by both server.py and the
in-Actions pipeline).

Responsibilities, in order:
  1. Deterministic pre-checks (pure code, never the LLM):
       - 10% of cash balance cap (cash read from Robinhood MCP get_accounts)
       - min notional floor
       - one-position-at-a-time (existing equity positions == 0)
  2. LLM orchestration only: hand the pre-approved card to Gemini Flash with
     the Robinhood MCP tools bound; the model's sole job is calling
     place_equity_order with the exact numbers it was given.

The LLM never touches numbers: every value arrives pre-computed and is
re-verified in code before the model session even starts.
"""

import asyncio
import json
import logging
import re
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, Optional, Tuple

import config

logger = logging.getLogger(__name__)

EXECUTOR_SYSTEM_INSTRUCTION = (
    "You are a trade execution agent. You receive a JSON trade card and a set "
    "of Robinhood MCP tools. Your ONLY job is to place the order described, "
    "via the Robinhood MCP tool `place_equity_order`.\n"
    "Hard rules you must never break:\n"
    "1. Execute the order via `place_equity_order` on Robinhood MCP ONLY if "
    "total dollar value is <= 10% of cash balance.\n"
    "2. Never modify the ticker, side, quantity, or dollar value. No "
    "substitutions, no 'better' prices, no rounding.\n"
    "3. If the dollar value exceeds 10% of the fetched cash balance, place "
    "NOTHING and reply with the refusal reason.\n"
    "4. If any tool errors, halt and report the error. Retry at most once.\n"
    "5. Report the final order status (id, state, filled quantity) as plain text."
)


# ---------------------------------------------------------------------------
# Deterministic pre-checks (pure functions over data Robinhood reports)
# ---------------------------------------------------------------------------
def check_cap(dollar_val: float, cash: Optional[float]) -> Tuple[bool, str]:
    """10% allocation cap with 1% headroom for fee/rounding drift."""
    if cash is None:
        return False, "cash balance unavailable — refusing to size a trade"
    max_allowed = cash * config.CAPITAL_ALLOCATION_PCT
    if dollar_val > max_allowed * 1.01:
        return False, (f"exceeds 10% allocation cap "
                       f"(${max_allowed:,.2f} max of ${cash:,.2f} cash)")
    return True, ""


def check_min_notional(dollar_val: float) -> Tuple[bool, str]:
    if dollar_val < config.MIN_TRADE_NOTIONAL:
        return False, (f"dollar value ${dollar_val:,.2f} below min notional "
                       f"${config.MIN_TRADE_NOTIONAL:,.2f}")
    return True, ""


def check_no_open_positions(open_positions: Optional[int]) -> Tuple[bool, str]:
    """One position at a time — any existing equity position blocks a new one."""
    if open_positions is None:
        return False, "position count unavailable — refusing to trade blind"
    if open_positions > 0:
        return False, (f"{open_positions} position(s) already open — "
                       f"MAX_ACTIVE_POSITIONS={config.MAX_ACTIVE_POSITIONS}")
    return True, ""


# ---------------------------------------------------------------------------
# MCP plumbing
# ---------------------------------------------------------------------------
@asynccontextmanager
async def _mcp_session() -> AsyncIterator[Any]:
    """Initialized Robinhood MCP ClientSession with guaranteed teardown."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from robinhood_auth import MCP_SERVER_URL, make_authenticated_http_client

    http = await make_authenticated_http_client()
    try:
        async with streamable_http_client(
            config.ROBINHOOD_MCP_URL or MCP_SERVER_URL, http_client=http
        ) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                yield session
    finally:
        await http.aclose()


async def _fetch_account_state() -> Dict[str, Optional[float]]:
    """Read cash + open equity positions via MCP (read-only tools)."""
    async with _mcp_session() as session:

        async def _call(name: str, arguments: Dict[str, Any]) -> str:
            res = await session.call_tool(name, arguments)
            return "\n".join(
                getattr(c, "text", "") for c in (res.content or [])
                if getattr(c, "text", None))

        accounts_raw = await _call("get_accounts", {})
        positions_raw = await _call("get_equity_positions", {})

    cash: Optional[float] = None
    # Pull every JSON object out of the tool text and hunt for cash-like fields
    for blob in re.findall(r"\{.*?\}", accounts_raw, re.DOTALL):
        try:
            data = json.loads(blob)
        except json.JSONDecodeError:
            continue
        for path in (
            ("portfolio_cash",), ("cash",), ("buying_power",),
            ("account", "portfolio_cash"), ("account", "cash"),
        ):
            node: Any = data
            for key in path:
                if isinstance(node, dict) and key in node:
                    node = node[key]
                else:
                    node = None
                    break
            if isinstance(node, (int, float)):
                cash = float(node)
                break
        if cash is not None:
            break

    open_count: Optional[int] = None
    if positions_raw.strip():
        try:
            parsed = json.loads(positions_raw)
            items = parsed if isinstance(parsed, list) else (
                parsed.get("positions") or parsed.get("results") or [])
            if isinstance(items, list):
                open_count = len(items)
        except json.JSONDecodeError:
            open_count = 0 if not positions_raw.strip() else None
    else:
        open_count = 0

    return {"cash": cash, "open_positions": open_count}


async def _run_mcp_trade(prompt: str) -> str:
    """Gemini Flash drives place_equity_order through the MCP session."""
    from google import genai
    from google.genai import types

    async with _mcp_session() as session:
        client = genai.Client(api_key=config.GEMINI_API_KEY)
        response = await client.aio.models.generate_content(
            model=config.GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=EXECUTOR_SYSTEM_INSTRUCTION,
                tools=[types.Tool(mcp_client=session)],
            ),
        )
        return response.text or "(empty model response)"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def mcp_preflight() -> Dict[str, Any]:
    """Read-only handshake + get_accounts. Proves tokens/session without
    placing anything. Never raises."""
    async def _run() -> Dict[str, Any]:
        async with _mcp_session() as session:
            accounts = await session.call_tool("get_accounts", {})
            text = "\n".join(
                getattr(c, "text", "") for c in (accounts.content or [])
                if getattr(c, "text", None))
            return {"ok": True, "accounts_text": text[:500]}
    try:
        return asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def fetch_account_state() -> Dict[str, Optional[float]]:
    """Sync wrapper for the read-only cash/positions read."""
    try:
        return asyncio.run(_fetch_account_state())
    except Exception as exc:  # noqa: BLE001
        logger.error("account state read failed: %s", exc)
        return {"cash": None, "open_positions": None}


def execute_signal(signal: Dict[str, Any]) -> Tuple[bool, str]:
    """
    Execute one trade card end-to-end:
      pre-checks (deterministic) → Gemini+MCP order placement → report.

    Returns (ok, report). Never raises.
    """
    ticker = str(signal["ticker"]).upper()
    action = str(signal["action"]).upper()
    quantity = float(signal["quantity"])
    dollar_val = float(signal["dollar_val"])

    if action not in {"BUY", "SELL"} or quantity <= 0 or dollar_val <= 0:
        return False, "invalid action/quantity/dollar_val"

    # --- 1. Deterministic pre-checks against live Robinhood state ---------
    state = fetch_account_state()
    cash = state.get("cash")
    open_positions = state.get("open_positions")

    ok, reason = check_cap(dollar_val, cash)
    if not ok:
        logger.error("Pre-check FAILED (%s): %s", ticker, reason)
        return False, f"BLOCKED: {reason}"

    ok, reason = check_min_notional(dollar_val)
    if not ok:
        return False, f"BLOCKED: {reason}"

    ok, reason = check_no_open_positions(open_positions)
    if not ok:
        logger.info("Pre-check skipped trade %s: %s", ticker, reason)
        return False, f"SKIPPED: {reason}"

    # --- 2. LLM orchestration (numbers are final; model just places it) ---
    prompt = (
        "Trade card (do not modify any value):\n"
        f"ticker: {ticker}\n"
        f"side: {action.lower()}\n"
        f"quantity: {quantity} (fractional shares)\n"
        f"total dollar value: {dollar_val}\n\n"
        "Steps: 1) Fetch the account cash balance via the Robinhood MCP tool. "
        "2) Verify the total dollar value is <= 10% of that cash balance. "
        "3) If valid, place the order via place_equity_order exactly as "
        "specified. 4) Report the order status."
    )
    try:
        report = asyncio.run(_run_mcp_trade(prompt))
        return True, report
    except Exception as exc:  # noqa: BLE001
        logger.exception("MCP execution failed")
        return False, f"MCP execution error: {exc}"
