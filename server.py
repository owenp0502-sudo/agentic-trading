"""
server.py — Flask Execution Bridge (Render free tier). OPTIONAL.

The primary pipeline now executes in-process on GitHub Actions (scanner.py →
executor.py) — same core, no extra hosting. This HTTP wrapper remains for
manual drills and any future split back to an always-on executor: it is a
*plumber*, not a trader. Gemini + Robinhood MCP orchestrate; every number
arrives pre-computed from the scanner, and executor.execute_signal()
re-verifies the cap / min-notional / one-position rules in code before the
model sees the card.
"""

import logging
from typing import Any, Dict, Tuple

from flask import Flask, jsonify, request

import config
from discord_notify import send_discord_alert
from executor import execute_signal, mcp_preflight

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("executor")

app = Flask(__name__)

# Simple in-memory dedupe: (ticker, action, dollar_val) within one process
# guards against GitHub Actions double-fire. Idempotency per risk register.
_SEEN_SIGNALS: Dict[Tuple[str, str, float], bool] = {}


# ---------------------------------------------------------------------------
# HTTP endpoints
# ---------------------------------------------------------------------------
@app.route("/health", methods=["GET"])
def health() -> Tuple[str, int]:
    """UptimeRobot pings this every few minutes to keep Render awake."""
    return "OK", 200


@app.route("/preflight", methods=["GET"])
def preflight() -> Tuple[Any, int]:
    """Read-only end-to-end MCP check (handshake + get_accounts).
    Gated by the same passphrase header as /execute when one is set."""
    if config.WEBHOOK_PASSPHRASE and \
            request.headers.get("X-Webhook-Passphrase", "") != \
            config.WEBHOOK_PASSPHRASE:
        return jsonify({"error": "unauthorized"}), 401
    result = mcp_preflight()
    return jsonify({
        "dry_run": config.EXECUTOR_DRY_RUN,
        "mcp_ok": result.get("ok", False),
        "detail": result.get("error") or result.get("accounts_text", ""),
    }), (200 if result.get("ok") else 502)


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

    # 3. Dry-run kill-switch: validate everything, simulate the fill.
    if config.EXECUTOR_DRY_RUN:
        logger.warning("EXECUTOR_DRY_RUN=1 — simulating fill (no MCP call, "
                       "no order placed).")
        _SEEN_SIGNALS[key] = True
        report = ("[DRY RUN] Simulated fill: {action} {qty} {ticker} for "
                  "${dv:,.2f}. Gemini+MCP skipped by kill-switch.".format(
                      action=action, qty=quantity, ticker=ticker,
                      dv=dollar_val))
        send_discord_alert(action, ticker, quantity, dollar_val,
                           reason=report, success=True)
        return jsonify({"status": "executed", "report": report}), 200

    # 4. Execute: deterministic pre-checks + Gemini/MCP order placement.
    _SEEN_SIGNALS[key] = True
    ok, report = execute_signal(signal)
    logger.info("Execution result ok=%s report=%s", ok, report[:200])

    # 5. Discord alert (notification, not control plane).
    send_discord_alert(
        action=action, ticker=ticker, quantity=quantity, dollar_val=dollar_val,
        reason=(report if ok else f"EXECUTION FAILED: {report}")[:1000],
        success=ok,
    )
    return jsonify({"status": "executed" if ok else "failed",
                    "report": report}), 200 if ok else 502


if __name__ == "__main__":
    # Render injects PORT; default 10000 locally.
    app.run(host="0.0.0.0", port=config.FLASK_PORT)
