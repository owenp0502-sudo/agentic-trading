"""
exits.py — Deterministic exit strategy (no LLM anywhere in this module).

The TJR pipeline enters; this module protects and exits. All broker calls are
direct, deterministic MCP tool calls — the LLM never sets exit levels, never
decides to close, and never sees these code paths.

Strategy (constants in config.py):
  * Bracket around average entry cost:
      stop   = cost * (1 - STOP_LOSS_PCT)              [long; mirrored short]
      target = cost * (1 + STOP_LOSS_PCT * TAKE_PROFIT_R)
    Default: 0.5% stop, 1.0% target (2R).
  * Attach, right after entry:
      - stop_market  GTC  → survives the session, triggers broker-side
      - limit        GFD  → take-profit at the target, expires EOD
  * Monitor every scanner run (5 min): if price already crossed stop or
    target, market-close immediately and cancel the leftover resting order.
  * 11:00 ET close-out: flatten every open position + cancel resting orders.

Fractional-shares constraint (Robinhood): decimals are only valid for
`market` orders in regular hours. Resting stop/limit orders therefore cover
whole shares only — `floor(qty)` when ≥ 1. The 5-minute monitor is the
backstop for the fractional remainder (market-close supports decimals).

Every position/order tool requires an explicit account_number (must be
`agentic_allowed=true`) — parsed from get_accounts, never hard-coded.
"""

import asyncio
import json
import logging
import math
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import config

logger = logging.getLogger(__name__)

NY_TZ = ZoneInfo(config.TIMEZONE)


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested in test_exits.py — no network)
# ---------------------------------------------------------------------------
def bracket_levels(side: str, cost: float) -> Tuple[float, float]:
    """(stop, target) for the entry side around average cost."""
    if cost <= 0:
        raise ValueError(f"cost must be positive, got {cost}")
    if side == "BUY":
        stop = cost * (1 - config.STOP_LOSS_PCT)
        target = cost * (1 + config.STOP_LOSS_PCT * config.TAKE_PROFIT_R)
    else:  # SELL (short)
        stop = cost * (1 + config.STOP_LOSS_PCT)
        target = cost * (1 - config.STOP_LOSS_PCT * config.TAKE_PROFIT_R)
    return round(stop, 4), round(target, 4)


def stop_hit(side: str, cost: float, last: float) -> bool:
    stop, _ = bracket_levels(side, cost)
    return last <= stop if side == "BUY" else last >= stop


def target_hit(side: str, cost: float, last: float) -> bool:
    _, target = bracket_levels(side, cost)
    return last >= target if side == "BUY" else last <= target


def should_flatten(now: datetime) -> bool:
    """True at/after FLATTEN_TIME in America/New_York."""
    return now.astimezone(NY_TZ).strftime("%H:%M") >= config.FLATTEN_TIME


def _fmt_price(p: float) -> str:
    return f"{p:.2f}"


# ---------------------------------------------------------------------------
# Response parsers (defensive — broker payloads vary in nesting)
# ---------------------------------------------------------------------------
def _walk(obj: Any):
    """Yield every dict anywhere in a decoded-JSON tree (any nesting)."""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def parse_account_number(accounts_raw: str) -> Optional[str]:
    """Extract the agentic brokerage account number from get_accounts text.

    Walks the full payload tree (payloads nest under wrappers like
    {"data": {"accounts": [...]}}) and only accepts accounts flagged
    agentic_allowed=true — order tools reject anything else.
    """
    candidates: List[Dict[str, Any]] = []
    for blob in _json_blobs(accounts_raw):
        for d in _walk(blob):
            if isinstance(d.get("account_number"), (str, int)):
                candidates.append(d)
            else:
                inner = d.get("account")
                if isinstance(inner, dict) and \
                        isinstance(inner.get("account_number"), (str, int)):
                    candidates.append(inner)
    agentic = [d for d in candidates
               if str(d.get("agentic_allowed", "")).lower() == "true"]
    if agentic:
        return str(agentic[0]["account_number"])
    if candidates:
        logger.warning(
            "get_accounts returned %d account(s), none agentic_allowed=true "
            "— complete the Agentic account onboarding in the consent flow.",
            len(candidates))
    return None


def parse_positions(positions_raw: str) -> List[Dict[str, Any]]:
    """Normalize positions into [{symbol, quantity, avg_cost}] (open only)."""
    out: List[Dict[str, Any]] = []
    for blob in _json_blobs(positions_raw):
        for d in _walk(blob):
            qty = _as_float(d.get("quantity"))
            if qty is None or qty <= 0:
                continue
            symbol = (d.get("symbol") or d.get("instrument_symbol")
                      or d.get("simple_symbol"))
            if not symbol and isinstance(d.get("instrument"), str):
                # Classic Robinhood pattern: instrument is a URL; the symbol
                # is not always recoverable — take the last path segment.
                symbol = d["instrument"].rstrip("/").rsplit("/", 1)[-1]
            if not symbol:
                continue
            avg = (_as_float(d.get("average_buy_price"))
                   or _as_float(d.get("average_price"))
                   or _as_float(d.get("avg_cost")))
            out.append({
                "symbol": str(symbol).upper(),
                "quantity": qty,
                "avg_cost": avg,
            })
    return out


def parse_last_price(quotes_raw: str, symbol: str) -> Optional[float]:
    """Pull last_trade_price for `symbol` from get_equity_quotes text."""
    for blob in _json_blobs(quotes_raw):
        candidates: List[Any] = []
        if isinstance(blob, dict):
            node = blob.get(symbol) or blob.get(symbol.upper())
            if node is not None:
                candidates.append(node)
        candidates.extend(_walk(blob))  # any dict anywhere in the tree
        for node in candidates:
            if not isinstance(node, dict):
                continue
            if "symbol" in node and \
                    str(node["symbol"]).upper() != symbol.upper():
                continue
            price = (_as_float(node.get("last_trade_price"))
                     or _as_float(node.get("last_price"))
                     or _as_float(node.get("last_extended_hours_trade_price")))
            if price:
                return price
    return None


def parse_open_orders(orders_raw: str) -> List[Dict[str, Any]]:
    """[{order_id, symbol, side, type}] for resting (cancellable) orders."""
    states = {"new", "queued", "confirmed", "unconfirmed", "partially_filled"}
    out: List[Dict[str, Any]] = []
    seen: set = set()
    for blob in _json_blobs(orders_raw):
        for d in _walk(blob):
            oid = d.get("order_id") or d.get("id")
            state = str(d.get("state", "")).lower()
            if oid and str(oid) not in seen and state in states:
                seen.add(str(oid))
                out.append({
                    "order_id": str(oid),
                    "symbol": str(d.get("symbol", "")).upper(),
                    "side": str(d.get("side", "")).lower(),
                    "type": str(d.get("type", "")).lower(),
                })
    return out


def _json_blobs(raw: str) -> List[Any]:
    """Best-effort: pull every JSON value out of a tool-response text blob."""
    blobs: List[Any] = []
    try:
        blobs.append(json.loads(raw))
        return blobs
    except json.JSONDecodeError:
        pass
    import re
    for m in re.finditer(r"[\[{].*?[\]}]", raw, re.DOTALL):
        try:
            blobs.append(json.loads(m.group(0)))
        except json.JSONDecodeError:
            continue
    return blobs


def _iter_items(data: Any, keys: Tuple[str, ...]):
    """Yield candidate items whether payload is a list, dict-of-list, nested."""
    if isinstance(data, list):
        yield from data
    elif isinstance(data, dict):
        found = False
        for key in keys:
            val = data.get(key)
            if isinstance(val, list):
                found = True
                yield from val
        if not found:
            yield data


def _as_float(v: Any) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Async MCP wrappers (deterministic — no Gemini)
# ---------------------------------------------------------------------------
async def _call(session: Any, name: str, args: Dict[str, Any]) -> str:
    res = await session.call_tool(name, args)
    return "\n".join(
        getattr(c, "text", "") for c in (res.content or [])
        if getattr(c, "text", None))


async def _get_account_number(session: Any) -> str:
    raw = await _call(session, "get_accounts", {})
    acct = parse_account_number(raw)
    if not acct:
        raise RuntimeError("could not resolve agentic account number "
                           "from get_accounts")
    return acct


async def _get_positions(session: Any, account: str) -> List[Dict[str, Any]]:
    return parse_positions(
        await _call(session, "get_equity_positions", {"account_number": account}))


async def _get_quote(session: Any, symbol: str) -> Optional[float]:
    return parse_last_price(
        await _call(session, "get_equity_quotes", {"symbols": [symbol]}),
        symbol)


async def _get_open_orders(session: Any, account: str,
                           symbol: Optional[str] = None) -> List[Dict[str, Any]]:
    args: Dict[str, Any] = {"account_number": account}
    if symbol:
        args["symbol"] = symbol
    return parse_open_orders(
        await _call(session, "get_equity_orders", args))


async def _place(session: Any, account: str, **order: Any) -> str:
    order["account_number"] = account
    return await _call(session, "place_equity_order", order)


async def _cancel(session: Any, account: str, order_id: str) -> str:
    return await _call(session, "cancel_equity_order", {
        "account_number": account, "order_id": order_id})


async def _attach_protections(signal: Dict[str, Any]) -> str:
    """Place the resting stop + target bracket for a fresh entry."""
    side = str(signal["action"]).upper()
    symbol = str(signal["ticker"]).upper()
    qty_total = float(signal["quantity"])
    qty_whole = math.floor(qty_total)

    if qty_whole < 1:
        return ("SKIP protections: fractional-only position (no whole shares); "
                "5-min monitor is the sole exit path")

    async with executor_session() as session:
        account = await _get_account_number(session)
        positions = await _get_positions(session, account)
        pos = next((p for p in positions if p["symbol"] == symbol), None)
        if not pos or not pos.get("avg_cost"):
            return (f"SKIP protections: no position/cost found for {symbol} "
                    f"(fill may not have settled yet; monitor will retry "
                    f"nothing — next entry requires a new signal)")
        cost = float(pos["avg_cost"])
        stop, target = bracket_levels(side, cost)

        stop_resp = await _place(
            session, account, symbol=symbol,
            side="sell" if side == "BUY" else "buy",
            type="stop_market", quantity=str(qty_whole),
            stop_price=_fmt_price(stop), time_in_force="gtc")
        target_resp = await _place(
            session, account, symbol=symbol,
            side="sell" if side == "BUY" else "buy",
            type="limit", quantity=str(qty_whole),
            limit_price=_fmt_price(target), time_in_force="gfd")

    return (f"protections attached for {symbol}: stop_market GTC @ "
            f"{_fmt_price(stop)} + limit GFD @ {_fmt_price(target)} "
            f"({qty_whole}/{qty_total:g} sh; fractional remainder exits via "
            f"monitor/flatten)")


async def _monitor_once() -> List[str]:
    """One pass: for each open position, close on stop/target hit."""
    actions: List[str] = []
    async with executor_session() as session:
        account = await _get_account_number(session)
        for pos in await _get_positions(session, account):
            symbol, qty, cost = pos["symbol"], pos["quantity"], pos["avg_cost"]
            if not cost:
                actions.append(f"{symbol}: no avg cost — skipping monitor")
                continue
            side = "BUY"  # pipeline is long-only in practice; long exit math
            last = await _get_quote(session, symbol)
            if not last:
                actions.append(f"{symbol}: no quote — skipping this pass")
                continue
            reason = None
            if stop_hit(side, cost, last):
                reason = f"STOP hit (cost {cost}, last {last})"
            elif target_hit(side, cost, last):
                reason = f"TARGET hit (cost {cost}, last {last})"
            if not reason:
                continue
            close_resp = await _place(
                session, account, symbol=symbol, side="sell",
                type="market", quantity=str(qty), time_in_force="gfd")
            actions.append(f"{symbol}: CLOSED — {reason} | {close_resp[:120]}")
            for o in await _get_open_orders(session, account, symbol):
                cancel_resp = await _cancel(session, account, o["order_id"])
                actions.append(f"{symbol}: canceled resting {o['type']} "
                               f"{o['order_id'][:8]} | {cancel_resp[:80]}")
    return actions


async def _flatten_all() -> List[str]:
    """Close every open position at market; cancel all resting orders."""
    actions: List[str] = []
    async with executor_session() as session:
        account = await _get_account_number(session)
        for o in await _get_open_orders(session, account):
            resp = await _cancel(session, account, o["order_id"])
            actions.append(f"canceled {o['type']} {o['symbol']} "
                           f"{o['order_id'][:8]} | {resp[:80]}")
        for pos in await _get_positions(session, account):
            resp = await _place(
                session, account, symbol=pos["symbol"], side="sell",
                type="market", quantity=str(pos["quantity"]),
                time_in_force="gfd")
            actions.append(f"flatten {pos['symbol']} "
                           f"({pos['quantity']:g} sh) | {resp[:120]}")
    return actions


# ---------------------------------------------------------------------------
# Session helper (reuses executor.py's context manager)
# ---------------------------------------------------------------------------
def executor_session():
    from executor import _mcp_session
    return _mcp_session()


# ---------------------------------------------------------------------------
# Sync wrappers (scanner calls these)
# ---------------------------------------------------------------------------
def attach_protections(signal: Dict[str, Any]) -> str:
    try:
        return asyncio.run(_attach_protections(signal))
    except Exception as exc:  # noqa: BLE001 — never crash the scan
        logger.exception("attach_protections failed")
        return f"protection attachment FAILED: {exc}"


def monitor_once() -> List[str]:
    try:
        return asyncio.run(_monitor_once())
    except Exception as exc:  # noqa: BLE001
        logger.exception("exit monitor failed")
        return [f"exit monitor FAILED: {exc}"]


def flatten_all() -> List[str]:
    try:
        return asyncio.run(_flatten_all())
    except Exception as exc:  # noqa: BLE001
        logger.exception("flatten failed")
        return [f"flatten FAILED: {exc}"]
