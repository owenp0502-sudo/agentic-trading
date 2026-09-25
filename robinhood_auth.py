"""
robinhood_auth.py — Robinhood Trading MCP OAuth2 (one-time bootstrap + runtime).

The official Robinhood Trading MCP (agent.robinhood.com/mcp/trading) requires
OAuth2: you sign in + consent in a desktop browser, open the dedicated
"Agentic account" when prompted, and the broker redirects back with an
authorization code. This module implements the MCP-spec OAuth client:

  * `python robinhood_auth.py`  → one-time CLI bootstrap: opens your browser,
    you approve, paste the redirect URL, tokens are saved locally and a
    live MCP initialize + tool-listing proves the connection end to end.
  * `make_authenticated_http_client()` → runtime factory for server.py;
    builds an httpx2 client whose auth handler loads saved tokens and
    auto-refreshes them against Robinhood's token endpoint.

Tokens are stored at ROBINHOOD_TOKEN_STORE (default ~/.tjr_bot/robinhood_tokens.json,
mode 0600) and travel to Render via its ROBINHOOD_TOKEN_STORE_JSON env var
(paste the file's contents after bootstrapping locally).
"""

import asyncio
import json
import logging
import os
import stat
import sys
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

MCP_SERVER_URL = "https://agent.robinhood.com/mcp/trading"
LOCAL_REDIRECT_HOST = "localhost"
LOCAL_REDIRECT_PORT = 9876
LOCAL_REDIRECT_PATH = "/callback"
LOCAL_REDIRECT_URI = f"http://{LOCAL_REDIRECT_HOST}:{LOCAL_REDIRECT_PORT}{LOCAL_REDIRECT_PATH}"


# ---------------------------------------------------------------------------
# Token storage (MCP TokenStorage protocol, JSON file backed)
# ---------------------------------------------------------------------------
def _store_path() -> Path:
    env = os.environ.get("ROBINHOOD_TOKEN_STORE", "").strip()
    if env:
        return Path(env)
    path = Path.home() / ".tjr_bot" / "robinhood_tokens.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


class JSONTokenStorage:
    """Persists OAuth tokens + dynamic client info to a local JSON file."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or _store_path()

    def _read(self) -> Dict[str, Any]:
        try:
            return json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError, PermissionError):
            return {}

    def _write(self, data: Dict[str, Any]) -> None:
        self.path.write_text(json.dumps(data, indent=2))
        try:  # best-effort 0600 — tokens at rest
            self.path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass

    # --- TokenStorage protocol (async) ---
    async def get_tokens(self) -> Optional[Any]:
        from mcp.shared.auth import OAuthToken
        raw = self._read().get("tokens")
        return OAuthToken.model_validate(raw) if raw else None

    async def set_tokens(self, tokens: Any) -> None:
        data = self._read()
        data["tokens"] = tokens.model_dump(mode="json")
        self._write(data)

    async def get_client_info(self) -> Optional[Any]:
        from mcp.shared.auth import OAuthClientInformationFull
        raw = self._read().get("client_info")
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def set_client_info(self, client_info: Any) -> None:
        data = self._read()
        data["client_info"] = client_info.model_dump(mode="json")
        self._write(data)


def load_tokens_as_env_json() -> Optional[str]:
    """Read the local token file so it can be pasted into Render's env."""
    raw = _read_tokens()
    return json.dumps(raw) if raw else None


def _read_tokens() -> Dict[str, Any]:
    try:
        return json.loads(_store_path().read_text())
    except (FileNotFoundError, json.JSONDecodeError, PermissionError):
        return {}


# ---------------------------------------------------------------------------
# Local CLI handlers (browser + paste flow)
# ---------------------------------------------------------------------------
async def _local_redirect_handler(url: str) -> None:
    print("\n" + "=" * 72)
    print("ROBINHOOD AUTHORIZATION REQUIRED")
    print("=" * 72)
    print("\n1. Your browser is opening the Robinhood sign-in / consent page.")
    print("   (If it doesn't, open this URL manually — it is printed below.)")
    print("2. Sign in, APPROVE access, and complete the Agentic account")
    print("   onboarding if prompted (desktop browser required by Robinhood).")
    print("3. Robinhood redirects to a page that FAILS TO LOAD")
    print(f"   ({LOCAL_REDIRECT_URI}?code=...) — that is expected.")
    print("4. Copy the FULL URL from the browser address bar.\n")
    print("AUTH URL:\n" + url + "\n")
    try:  # browser is a convenience, not a requirement
        import webbrowser
        webbrowser.open(url)
    except Exception:  # noqa: BLE001
        pass


async def _local_paste_callback() -> Any:
    from mcp.shared.auth import AuthorizationCodeResult
    while True:
        pasted = input("Paste the full redirect URL here: ").strip()
        parsed = urllib.parse.urlparse(pasted)
        query = urllib.parse.parse_qs(parsed.query or ("?" + parsed.fragment)[1:])
        code = (query.get("code") or [None])[0]
        state = (query.get("state") or [None])[0]
        if not code:
            print("  ✗ No 'code' parameter found — paste the complete URL "
                  "Robinhood redirected to (starts with "
                  f"{LOCAL_REDIRECT_URI}).\n")
            continue
        return AuthorizationCodeResult(code=code, state=state, iss=None)


def _client_metadata() -> Any:
    from mcp.shared.auth import OAuthClientMetadata
    return OAuthClientMetadata(
        client_name="TJR SMC Executor",
        redirect_uris=[LOCAL_REDIRECT_URI],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="none",  # public client + PKCE
        scope="openid profile email",
    )


def _build_provider(storage: Any) -> Any:
    from mcp.client.auth import OAuthClientProvider
    return OAuthClientProvider(
        server_url=MCP_SERVER_URL,
        client_metadata=_client_metadata(),
        storage=storage,
        redirect_handler=_local_redirect_handler,
        callback_handler=_local_paste_callback,
    )


# ---------------------------------------------------------------------------
# Runtime: authenticated httpx2 client for server.py
# ---------------------------------------------------------------------------
async def make_authenticated_http_client() -> Any:
    """
    httpx2.AsyncClient wired with the MCP OAuth provider. Tokens load from
    storage (env JSON on Render, file locally); refreshes are automatic.
    Pass this into streamable_http_client(..., http_client=...).
    """
    import httpx2

    if os.environ.get("ROBINHOOD_TOKEN_STORE_JSON", "").strip():
        # Render: tokens injected as env var JSON → seed the file store once.
        env_store = os.environ.get("ROBINHOOD_TOKEN_STORE", "").strip()
        target = Path(env_store) if env_store else _store_path()
        if not target.exists():
            try:
                seed = json.loads(os.environ["ROBINHOOD_TOKEN_STORE_JSON"])
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(seed))
                target.chmod(stat.S_IRUSR | stat.S_IWUSR)
                logger.info("Seeded Robinhood tokens from env into %s", target)
            except (json.JSONDecodeError, OSError) as exc:
                logger.error("Could not seed token store: %s", exc)

    # Fail fast if no tokens exist: the OAuth provider's interactive
    # handlers are CLI-only (browser + paste) and must never hang the
    # executor server. Bootstrap once with `python robinhood_auth.py`.
    if await JSONTokenStorage().get_tokens() is None:
        raise RuntimeError(
            "No Robinhood MCP tokens found. Run `python robinhood_auth.py` "
            "locally once (browser consent), then provide them to the "
            "server via ROBINHOOD_TOKEN_STORE_JSON."
        )

    provider = _build_provider(JSONTokenStorage())
    return httpx2.AsyncClient(auth=provider, timeout=60.0)


# ---------------------------------------------------------------------------
# CLI: python robinhood_auth.py
# ---------------------------------------------------------------------------
async def _bootstrap() -> int:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    storage = JSONTokenStorage()
    if await storage.get_tokens():
        print("Existing Robinhood tokens found — reconnecting with them.")
        print("(Delete the file to re-consent: " + str(storage.path) + ")\n")

    provider = _build_provider(storage)
    import httpx2
    http = httpx2.AsyncClient(auth=provider, timeout=60.0)
    try:
        async with streamable_http_client(MCP_SERVER_URL, http_client=http) as streams:
            read_stream, write_stream = streams[0], streams[1]
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                tools = await session.list_tools()
                names: List[str] = [t.name for t in tools.tools]
        print("\n✅ CONNECTED — MCP session initialized, tokens saved to: "
              + str(storage.path))
        print("\nTools exposed by Robinhood Trading MCP:")
        for name in names:
            print("  •", name)
        if not any("place" in n.lower() or "order" in n.lower() for n in names):
            print("\n⚠️  No obvious order-placement tool found — check the "
                  "names above; server.py's system instruction references "
                  "place_equity_order and may need updating.")
        print("\nNEXT STEP for Render: paste the JSON below into an env var "
              "named ROBINHOOD_TOKEN_STORE_JSON:\n")
        print(load_tokens_as_env_json())
        return 0
    except Exception as exc:  # noqa: BLE001 — bootstrap diagnostics
        logger.exception("Bootstrap failed")
        print(f"\n✗ Bootstrap failed: {exc}")
        print("Common causes: consent not completed in browser, mobile-only "
              "flow (Robinhood requires desktop), or the redirect URL was "
              "edited before pasting.")
        return 1
    finally:
        await http.aclose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s | %(name)s | %(message)s")
    raise SystemExit(asyncio.run(_bootstrap()))
