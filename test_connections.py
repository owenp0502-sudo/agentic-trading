"""
test_connections.py — Read-only smoke tests for every external service.

Verifies credentials and reachability WITHOUT placing any order:
  1. Alpaca paper account   (GET /v2/account — falls back to live check on 401)
  2. Alpaca screener        (GET most-actives)
  3. Gemini / Vertex key    (1-token generateContent)
  4. Robinhood MCP endpoint (JSON-RPC initialize handshake only)

Run:  python test_connections.py
"""

import json
import sys
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Tuple


def load_env(path: str = ".env") -> Dict[str, str]:
    env: Dict[str, str] = {}
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    env[key.strip()] = val.strip()
    except FileNotFoundError:
        pass
    return env


ENV = {**load_env(), **{k: v for k, v in __import__("os").environ.items()
                        if v and k.isupper()}}


def request(method: str, url: str, headers: Optional[Dict[str, str]] = None,
            body: Optional[Dict[str, Any]] = None,
            timeout: int = 15) -> Tuple[int, str]:
    """Minimal HTTP helper returning (status, truncated body)."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    for key, val in (headers or {}).items():
        req.add_header(key, val)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode(errors="replace")[:400]
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(errors="replace")[:400]
    except Exception as exc:  # noqa: BLE001 — diagnostics must not crash
        return -1, f"{type(exc).__name__}: {exc}"


def mask(secret: str) -> str:
    return secret[:4] + "…" if secret else "(missing)"


RESULTS: list = []


def report(name: str, ok: bool, detail: str) -> None:
    RESULTS.append(ok)
    print(f"{'✅' if ok else '❌'} {name}: {detail}")


# ---------------------------------------------------------------------------
def check_alpaca_account() -> None:
    key, secret = ENV.get("ALPACA_API_KEY", ""), ENV.get("ALPACA_SECRET_KEY", "")
    if not key or not secret:
        return report("Alpaca account", False, "keys missing in .env")
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    status, body = request("GET", "https://paper-api.alpaca.markets/v2/account",
                           headers)
    if status == 200:
        try:
            acct = json.loads(body + '"')  # body may be truncated; re-fetch below
        except Exception:  # noqa: BLE001
            acct = {}
        # Re-fetch without truncation for the cash figure.
        req = urllib.request.Request(
            "https://paper-api.alpaca.markets/v2/account", headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            acct = json.loads(resp.read().decode())
        report("Alpaca account (paper)", True,
               f"cash=${float(acct.get('cash', 0)):,.2f} "
               f"status={acct.get('status')}")
        return
    if status == 401 or status == 403:
        # Keys might be live-environment keys — try live (read-only GET).
        lstatus, lbody = request("GET", "https://api.alpaca.markets/v2/account",
                                 headers)
        if lstatus == 200:
            report("Alpaca account", True,
                   "keys are LIVE-environment keys — set ALPACA_PAPER=live "
                   "or generate paper keys")
        else:
            report("Alpaca account", False,
                   f"paper={status}, live={lstatus} — check the keys")
    else:
        report("Alpaca account", False, f"HTTP {status}: {body[:150]}")


def check_alpaca_screener() -> None:
    key, secret = ENV.get("ALPACA_API_KEY", ""), ENV.get("ALPACA_SECRET_KEY", "")
    if not key or not secret:
        return report("Alpaca screener", False, "keys missing")
    status, body = request(
        "GET", "https://data.alpaca.markets/v1beta1/screener/stocks/"
               "most-actives?top=5",
        {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret,
         "Accept": "application/json"})
    if status == 200:
        try:
            rows = json.loads(body + '"')  # may be truncated; parse loosely
        except Exception:  # noqa: BLE001
            rows = {}
        report("Alpaca screener", True, "most-actives reachable "
               f"(body starts: {body[:80]})")
    elif status in (401, 403):
        report("Alpaca screener", False,
               "401/403 — free plan may lack screener access; the bot falls "
               "back to the static watchlist automatically")
    else:
        report("Alpaca screener", False, f"HTTP {status}: {body[:150]}")


def check_gemini() -> None:
    key = ENV.get("GEMINI_API_KEY", "")
    model = ENV.get("GEMINI_MODEL", "gemini-3.5-flash")
    if not key:
        return report("Gemini", False, "GEMINI_API_KEY missing")
    # Both AIza and AQ. (express) keys work on the Developer API endpoint.
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model}:generateContent")
    headers = {"x-goog-api-key": key, "Content-Type": "application/json"}
    status, body = request("POST", url, headers, {
        "contents": [{"parts": [{"text": "Reply with exactly: OK"}]}],
        "generationConfig": {"maxOutputTokens": 800},
    })
    if status == 200:
        report("Gemini", True, f"{model} responded via "
               f"{'Vertex express' if key.startswith('AQ.') else 'AI Studio'}")
    else:
        report("Gemini", False, f"HTTP {status}: {body[:200]}")


def check_robinhood_mcp() -> None:
    url = ENV.get("ROBINHOOD_MCP_URL", "")
    if not url:
        return report("Robinhood MCP", False, "ROBINHOOD_MCP_URL missing")
    # MCP streamable-HTTP initialize handshake — read-only, places nothing.
    status, body = request("POST", url, {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "tjr-conn-check", "version": "0.1"},
        },
    })
    if status == 200:
        if "robinhood" in body.lower() or "serverInfo" in body:
            report("Robinhood MCP", True, "initialize handshake succeeded — "
                   f"server replied: {body[:150]}")
        else:
            report("Robinhood MCP", True,
                   f"HTTP 200 (verify payload): {body[:150]}")
    elif status in (401, 403):
        report("Robinhood MCP", False, "endpoint live but requires auth "
               "headers — set ROBINHOOD_MCP_HEADERS JSON in .env")
    elif status == 404:
        report("Robinhood MCP", False, "404 — path not found; endpoint may "
               "have moved or need a session route")
    else:
        report("Robinhood MCP", False, f"HTTP {status}: {body[:150]}")


if __name__ == "__main__":
    print("=== TJR bot connection checks (read-only, no orders) ===\n")
    check_alpaca_account()
    check_alpaca_screener()
    check_gemini()
    check_robinhood_mcp()
    passed = sum(RESULTS)
    print(f"\n{passed}/{len(RESULTS)} services verified.")
    sys.exit(0 if passed == len(RESULTS) else 1)
