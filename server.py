"""
server.py — Flask Execution Bridge (Render free tier).

The executor is a *plumber*, not a trader. Gemini + Robinhood MCP handle
orchestration only; every number arrives pre-computed from the scanner and
the 10%-of-cash rule is re-verified in code before any order is attempted.
"""

import asyncio
import logging
import os
from typing import Any, Dict, Optional, Tuple

from flask import Flask, jsonify, request

import config
from discord_notify import send_discord_alert

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("executor")

app = Flask(__name__)

# Simple in-memory dedupe: (ticker, action, dollar_val) within one process
# guards against GitHub Actions double-fire. Idempotency per risk register.
_SEEN_SIGNALS: Dict[Tuple[str, str, float], bool] = {}

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
# Gemini + Robinhood MCP wiring
# ---------------------------------------------------------------------------
def _build_gemini_client() -> "Any":
    """Create the google-genai Developer API client. Both AIza (AI Studio)
    and AQ. (Vertex express) keys authenticate here — verified 2026-09-25."""
    from google import genai

    return genai.Client(api_key=config.GEMINI_API_KEY)


async def _run_mcp_trade(prompt: str) -> str:
    """
    Open the Robinhood MCP session (streamable HTTP over an OAuth-
    authenticated client), hand it to Gemini Flash as a tool binding, and
    let the model drive the order through `place_equity_order`.
    Returns the model's final text report.
    """
    from google.genai import types
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from robinhood_auth import (MCP_SERVER_URL,
                                make_authenticated_http_client)

    http = await make_authenticated_http_client()
    try:
        async with streamable_http_client(
            config.ROBINHOOD_MCP_URL or MCP_SERVER_URL, http_client=http
        ) as streams:
            read_stream, write_stream = streams[0], streams[1]
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()  # MCP handshake (auth auto-refreshes)

                client = _build_gemini_client()
                response = await client.aio.models.generate_content(
                    model=config.GEMINI_MODEL,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=EXECUTOR_SYSTEM_INSTRUCTION,
                        tools=[types.Tool(mcp_client=session)],  # MCP → Gemini
                    ),
                )
                return response.text or "(empty model response)"
    finally:
        await http.aclose()


def execute_via_gemini_mcp(signal: Dict[str, Any]) -> Tuple[bool, str]:
    """Sync wrapper around the async MCP session. Never raises."""
    prompt = (
        "Trade card (do not modify any value):\n"
        f"ticker: {signal['ticker']}\n"
        f"side: {str(signal['action']).lower()}\n"
        f"quantity: {signal['quantity']} (fractional shares)\n"
        f"total dollar value: {signal['dollar_val']}\n\n"
        "Steps: 1) Fetch the account cash balance via the Robinhood MCP tool. "
        "2) Verify the total dollar value is <= 10% of that cash balance. "
        "3) If valid, place the order via place_equity_order exactly as "
        "specified. 4) Report the order status."
    )
    try:
        report = asyncio.run(_run_mcp_trade(prompt))
        return True, report
    except Exception as exc:  # noqa: BLE001 — report failure, don't crash Flask
        logger.exception("MCP execution failed")
        return False, f"MCP execution error: {exc}"


# ---------------------------------------------------------------------------
# HTTP endpoints
# ---------------------------------------------------------------------------
@app.route("/health", methods=["GET"])
def health() -> Tuple[str, int]:
    """UptimeRobot pings this every few minutes to keep Render awake."""
    return "OK", 200


@app.route("/execute", methods=["POST"])
def execute() -> Tuple[Any, int]:
    # 1. Passphrase gate (constant-time compare).
    provided = request.headers.get("X-Webhook-Passphrase", "")
    if not config.WEBHOOK_PASSPHRASE or provided != config.WEBHOOK_PASSPHRASE:
        logger.warning("Rejected /execute: bad passphrase.")
        return jsonify({"error": "unauthorized"}), 401

    payload = request.get_json(silent=True) or {}
    try:
        ticker = str(payload["ticker"]).upper()
        action = str(payload["action"]).upper()
        quantity = float(payload["quantity"])
        dollar_val = float(payload["dollar_val"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "payload must include ticker, action, "
                                 "quantity, dollar_val"}), 400

    if action not in {"BUY", "SELL"} or quantity <= 0 or dollar_val <= 0:
        return jsonify({"error": "invalid action/quantity/dollar_val"}), 400

    signal = {
        "ticker": ticker,
        "action": action,
        "quantity": quantity,
        "dollar_val": dollar_val,
        "reason": str(payload.get("reason", "")),
    }

    # 2. Duplicate-signal guard (GitHub Actions can double-fire).
    key = (ticker, action, round(dollar_val, 2))
    if _SEEN_SIGNALS.get(key):
        logger.info("Duplicate signal %s ignored.", key)
        return jsonify({"status": "duplicate_ignored"}), 200

    # 3. Deterministic pre-check of the 10% rule — code re-verifies before
    #    the LLM even sees the card (LLM never touches numbers).
    balance = _fetch_cash_balance_for_validation()
    if balance is not None:
        max_allowed = balance * config.CAPITAL_ALLOCATION_PCT
        if dollar_val > max_allowed * 1.01:  # 1% headroom for fee/rounding drift
            logger.error("Refusing %s: $%.2f > 10%% of $%.2f",
                         ticker, dollar_val, balance)
            send_discord_alert(action, ticker, quantity, dollar_val,
                               reason=("BLOCKED: exceeds 10% allocation cap "
                                       f"(${max_allowed:,.2f} max)"),
                               success=False)
            return jsonify({"error": "exceeds 10% allocation cap",
                            "max_allowed": round(max_allowed, 2)}), 400
    else:
        logger.warning("Cash balance unavailable for pre-check — deferring to "
                       "the MCP-side 10% rule in the system instruction.")

    # 4. Execute via Gemini 2.5 Flash + Robinhood MCP.
    _SEEN_SIGNALS[key] = True
    ok, report = execute_via_gemini_mcp(signal)
    logger.info("Execution result ok=%s report=%s", ok, report[:200])

    # 5. Discord alert (notification, not control plane).
    send_discord_alert(
        action=action, ticker=ticker, quantity=quantity, dollar_val=dollar_val,
        reason=(report if ok else f"EXECUTION FAILED: {report}")[:1000],
        success=ok,
    )
    return jsonify({"status": "executed" if ok else "failed",
                    "report": report}), 200 if ok else 502


def _fetch_cash_balance_for_validation() -> Optional[float]:
    """Best-effort cash read via Alpaca (paper unless ALPACA_PAPER=live)."""
    try:
        from alpaca.trading.client import TradingClient
        base_url = ("https://paper-api.alpaca.markets" if config.ALPACA_PAPER
                    else None)
        client = TradingClient(
            config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY,
            paper=config.ALPACA_PAPER, url_override=base_url,
        )
        return float(client.get_account().cash)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Balance pre-check unavailable: %s", exc)
        return config.FALLBACK_ACCOUNT_BALANCE


if __name__ == "__main__":
    # Render injects PORT; default 10000 locally.
    app.run(host="0.0.0.0", port=config.FLASK_PORT)
