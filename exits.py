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
def compute_stop(side: str, cost: float,
                 structural_level: Optional[float] = None) -> float:
    """
    Stop price for the entry side.

    Preferred (config.USE_STRUCTURAL_STOP): just beyond the sweep extreme
    (TJR structure — the level that, if traded through, invalidates the
    setup), buffered by STRUCTURAL_STOP_BUFFER_PCT. The resulting distance
    is clamped to [MIN_STOP_DISTANCE_PCT, MAX_STOP_DISTANCE_PCT] of cost so
    micro-noise can't stop us out and tails stay bounded.

    Fallback: fixed STOP_LOSS_PCT from cost (clamped to the same bounds).
    """
    if cost <= 0:
        raise ValueError(f"cost must be positive, got {cost}")

    def _clamp(dist_pct: float) -> float:
        return min(max(dist_pct, config.MIN_STOP_DISTANCE_PCT),
                   config.MAX_STOP_DISTANCE_PCT)

    if (structural_level and structural_level > 0
            and config.USE_STRUCTURAL_STOP):
        buf = config.STRUCTURAL_STOP_BUFFER_PCT
        if side == "BUY":
            raw_dist = (cost - structural_level * (1 - buf)) / cost
        else:
            raw_dist = (structural_level * (1 + buf) - cost) / cost
        dist_pct = _clamp(raw_dist)
    else:
        dist_pct = _clamp(config.STOP_LOSS_PCT)

    stop = cost * (1 - dist_pct) if side == "BUY" else cost * (1 + dist_pct)
    return round(stop, 4)


def compute_target(side: str, cost: float, stop: float) -> float:
    """Target = TAKE_PROFIT_R × the actual stop distance (risk-based R)."""
    risk = abs(cost - stop)
    target = (cost + risk * config.TAKE_PROFIT_R if side == "BUY"
              else cost - risk * config.TAKE_PROFIT_R)
    return round(target, 4)


def bracket_levels(side: str, cost: float,
                   structural_level: Optional[float] = None) -> Tuple[float, float]:
    """(stop, target) around average cost — structural anchor if provided."""
    stop = compute_stop(side, cost, structural_level)
    return stop, compute_target(side, cost, stop)


def stop_hit(side: str, cost: float, last: float,
             active_stop: Optional[float] = None) -> bool:
    """Stop breached? Uses the ACTIVE (possibly ratcheted) stop when known,
    else the initial bracket from cost."""
    stop = active_stop if active_stop and active_stop > 0 \
        else compute_stop(side, cost)
    return last <= stop if side == "BUY" else last >= stop


def target_hit(side: str, cost: float, last: float) -> bool:
    """Target breached? Anchored to the INITIAL stop distance from cost —
    ratcheting the stop must never move the take-profit (otherwise a
    breakeven stop would imply a target at cost and insta-close the
    position at market)."""
    _, target = bracket_levels(side, cost)
    return last >= target if side == "BUY" else last <= target


def trailed_stop(side: str, cost: float, current_stop: float,
                 last: float) -> Optional[float]:
    """
    Ratchet policy (returns the new stop, or None to keep the current one).
    Stateless by design: the monitor reads the resting stop from the broker,
    so a crashed run never loses trail state.

      stage 1 — profit ≥ TRAIL_BREAKEVEN_R × risk → stop to breakeven (cost)
      stage 2 — post-breakeven and TRAIL_STOP_PCT > 0 → trail last price
                at that % distance

    Monotonic: only ever tightens (never below the current stop for longs,
    never above for shorts).
    """
    if cost <= 0 or current_stop <= 0 or last <= 0:
        return None
    risk = abs(cost - current_stop)
    candidate: Optional[float] = None

    if side == "BUY":
        profit = last - cost
        if risk > 0 and profit >= config.TRAIL_BREAKEVEN_R * risk:
            candidate = cost
        if current_stop >= cost and config.TRAIL_STOP_PCT > 0:
            trail = last * (1 - config.TRAIL_STOP_PCT)
            candidate = trail if candidate is None else max(candidate, trail)
        if candidate is not None and candidate > current_stop + 0.005:
            return round(candidate, 4)
    else:
        profit = cost - last
        if risk > 0 and profit >= config.TRAIL_BREAKEVEN_R * risk:
            candidate = cost
        if current_stop <= cost and config.TRAIL_STOP_PCT > 0:
            trail = last * (1 + config.TRAIL_STOP_PCT)
            candidate = trail if candidate is None else min(candidate, trail)
        if candidate is not None and candidate < current_stop - 0.005:
            return round(candidate, 4)
    return None


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
    """[{order_id, symbol, side, type, stop_price, limit_price, quantity}]
    for resting (cancellable) orders."""
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
                    "stop_price": _as_float(d.get("stop_price")),
                    "limit_price": _as_float(d.get("limit_price")),
                    "quantity": _as_float(d.get("quantity")),
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
        anchor = signal.get("stop_anchor")
        stop = compute_stop(side, cost,
                            float(anchor) if anchor else None)
        target = compute_target(side, cost, stop)

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


async def _monitor_once() -> List[Dict[str, Any]]:
    """
    One pass per open position, in priority order:
      1. Hard exit  — last price breached the ACTIVE stop or the target →
                      market-close, cancel leftover resting orders.
      2. Ratchet    — profit crossed the breakeven/trail thresholds →
                      cancel + re-place the resting stop tighter.
    Returns structured events (kind: exit|ratchet|note) so the scanner can
    log AND Discord-ping them; exits are always action=SELL (long exits).
    The resting stop order on the broker is the source of truth (stateless,
    crash-safe); when no resting stop exists, fall back to the computed
    initial bracket from cost.
    """
    events: List[Dict[str, Any]] = []
    async with executor_session() as session:
        account = await _get_account_number(session)
        open_orders = await _get_open_orders(session, account)
        for pos in await _get_positions(session, account):
            symbol, qty, cost = pos["symbol"], pos["quantity"], pos["avg_cost"]
            if not cost:
                events.append({"kind": "note", "symbol": symbol,
                               "reason": "no avg cost — skipping monitor"})
                continue
            side = "BUY"  # pipeline is long-only in practice
            last = await _get_quote(session, symbol)
            if not last:
                events.append({"kind": "note", "symbol": symbol,
                               "reason": "no quote — skipping this pass"})
                continue

            resting_stop = next(
                (o for o in open_orders
                 if o["symbol"] == symbol and o["type"] == "stop_market"
                 and o.get("stop_price")), None)
            active_stop = (resting_stop["stop_price"]
                           if resting_stop else compute_stop(side, cost))

            reason = None
            if stop_hit(side, cost, last, active_stop):
                reason = f"STOP hit (stop {active_stop}, last {last})"
            elif target_hit(side, cost, last):
                reason = f"TARGET hit (last {last})"
            if reason:
                close_resp = await _place(
                    session, account, symbol=symbol, side="sell",
                    type="market", quantity=str(qty), time_in_force="gfd")
                events.append({
                    "kind": "exit", "symbol": symbol, "action": "SELL",
                    "quantity": qty, "price": last,
                    "dollar_val": round(qty * last, 2), "reason": reason,
                    "detail": close_resp[:120]})
                for o in await _get_open_orders(session, account, symbol):
                    await _cancel(session, account, o["order_id"])
                    events.append({
                        "kind": "note", "symbol": symbol,
                        "reason": f"canceled resting {o['type']} "
                                  f"{o['order_id'][:8]}"})
                continue

            # --- Ratchet: tighten the resting stop if policy says so ------
            new_stop = trailed_stop(side, cost, active_stop, last)
            if new_stop is None:
                continue
            if resting_stop:
                await _cancel(session, account, resting_stop["order_id"])
            qty_whole = math.floor(qty)
            if qty_whole < 1:
                events.append({
                    "kind": "note", "symbol": symbol,
                    "reason": f"ratchet wanted stop {new_stop} but no whole "
                              f"shares to protect — hard-exit check active"})
                continue
            resp = await _place(
                session, account, symbol=symbol, side="sell",
                type="stop_market", quantity=str(qty_whole),
                stop_price=_fmt_price(new_stop), time_in_force="gtc")
            events.append({
                "kind": "ratchet", "symbol": symbol, "action": "SELL",
                "quantity": qty_whole,
                "reason": f"stop ratcheted {active_stop} → {new_stop}",
                "detail": resp[:100]})
    return events


def render_event(ev: Dict[str, Any]) -> str:
    """One-line human rendering for logs."""
    base = f"{ev.get('symbol', '')}: [{ev['kind']}] {ev.get('reason', '')}"
    return f"{base} | {ev['detail']}" if ev.get("detail") else base


async def _flatten_all() -> List[Dict[str, Any]]:
    """Close every open position at market; cancel all resting orders.
    Emits the same structured events as the monitor (exits = SELL)."""
    events: List[Dict[str, Any]] = []
    async with executor_session() as session:
        account = await _get_account_number(session)
        for o in await _get_open_orders(session, account):
            await _cancel(session, account, o["order_id"])
            events.append({"kind": "note", "symbol": o["symbol"],
                           "reason": f"canceled {o['type']} "
                                     f"{o['order_id'][:8]}"})
        for pos in await _get_positions(session, account):
            last = await _get_quote(session, pos["symbol"])
            resp = await _place(
                session, account, symbol=pos["symbol"], side="sell",
                type="market", quantity=str(pos["quantity"]),
                time_in_force="gfd")
            events.append({
                "kind": "exit", "symbol": pos["symbol"], "action": "SELL",
                "quantity": pos["quantity"], "price": last,
                "dollar_val": (round(pos["quantity"] * last, 2)
                               if last else None),
                "reason": f"{config.FLATTEN_TIME} ET flatten",
                "detail": resp[:120]})
    return events


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


def monitor_once() -> List[Dict[str, Any]]:
    try:
        return asyncio.run(_monitor_once())
    except Exception as exc:  # noqa: BLE001
        logger.exception("exit monitor failed")
        return [{"kind": "note", "reason": f"exit monitor FAILED: {exc}"}]


def flatten_all() -> List[Dict[str, Any]]:
    try:
        return asyncio.run(_flatten_all())
    except Exception as exc:  # noqa: BLE001
        logger.exception("flatten failed")
        return [{"kind": "note", "reason": f"flatten FAILED: {exc}"}]
