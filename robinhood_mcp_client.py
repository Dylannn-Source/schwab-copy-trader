"""Client for Robinhood's Agentic Trading MCP server.

Connects to https://agent.robinhood.com/mcp/trading using the standard MCP
OAuth flow: the first call opens your browser for a one-time authorization
(Robinhood's own login — this code never sees your password), then caches
the resulting token locally so future runs don't need the browser step again.

Runs its own background thread with a persistent asyncio event loop and MCP
session, exposing plain synchronous methods (start/list_tools/call_tool/stop)
so it can be called from ordinary (non-async) code — e.g. discord_trader.py's
executor, which itself runs inside discord.py-self's own event loop and can't
nest another asyncio.run() call.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import webbrowser
from contextlib import AsyncExitStack
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx2
from mcp import ClientSession
from mcp.client.auth import AuthorizationCodeResult, OAuthClientProvider
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken

DEFAULT_SERVER_URL = "https://agent.robinhood.com/mcp/trading"
DEFAULT_CALLBACK_PORT = 3030


class FileTokenStorage:
    """Persists OAuth tokens + client registration to a local JSON file so the
    browser authorization step only has to happen once."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._data: dict = self._load()

    def _load(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text())
        except Exception:
            return {}

    def _save(self):
        self.path.write_text(json.dumps(self._data))
        try:
            os.chmod(self.path, 0o600)
        except Exception:
            pass

    async def get_tokens(self) -> OAuthToken | None:
        raw = self._data.get("tokens")
        return OAuthToken.model_validate(raw) if raw else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self._data["tokens"] = tokens.model_dump(mode="json")
        self._save()

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        raw = self._data.get("client_info")
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self._data["client_info"] = client_info.model_dump(mode="json")
        self._save()


class _CallbackServer:
    """One-shot local HTTP server that catches the OAuth redirect and pulls
    the authorization code out of it."""

    def __init__(self, port: int):
        self.port = port
        self.result: dict = {}

    def wait_for_callback(self, timeout: float) -> dict:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                params = parse_qs(urlparse(self.path).query)
                outer.result["code"] = params.get("code", [None])[0]
                outer.result["state"] = params.get("state", [None])[0]
                outer.result["error"] = params.get("error", [None])[0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<html><body>Robinhood authorized \xe2\x80\x94 you can close this window.</body></html>")

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", self.port), Handler)
        server.timeout = timeout
        server.handle_request()
        server.server_close()

        if self.result.get("error"):
            raise RuntimeError(f"Robinhood authorization failed: {self.result['error']}")
        if not self.result.get("code"):
            raise TimeoutError("Timed out waiting for the Robinhood authorization redirect.")
        return self.result


class RobinhoodMCPClient:
    def __init__(
        self,
        token_path: str | Path = "robinhood_mcp_token.json",
        server_url: str = DEFAULT_SERVER_URL,
        callback_port: int = DEFAULT_CALLBACK_PORT,
    ):
        self.server_url = server_url
        self.callback_port = callback_port
        self.storage = FileTokenStorage(token_path)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._session: ClientSession | None = None
        self._stack: AsyncExitStack | None = None
        self._ready = threading.Event()
        self._error: Exception | None = None

    def start(self, timeout: float = 300):
        """Connects and authenticates. Blocks until ready — on first run this
        includes the time you spend approving access in your browser."""
        self._thread = threading.Thread(target=self._run, daemon=True, name="robinhood-mcp")
        self._thread.start()
        if not self._ready.wait(timeout=timeout):
            raise TimeoutError("Timed out connecting to Robinhood's MCP server.")
        if self._error:
            raise self._error

    def _run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._connect())
            self._ready.set()
            self._loop.run_forever()
        except Exception as e:
            self._error = e
            self._ready.set()

    async def _connect(self):
        callback_server = _CallbackServer(self.callback_port)

        async def redirect_handler(authorization_url: str) -> None:
            print(f"\nOpening your browser to authorize Robinhood access:\n{authorization_url}\n")
            webbrowser.open(authorization_url)

        async def callback_handler() -> AuthorizationCodeResult:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, callback_server.wait_for_callback, 300)
            return AuthorizationCodeResult(code=result["code"], state=result["state"])

        oauth_auth = OAuthClientProvider(
            server_url=self.server_url,
            client_metadata=OAuthClientMetadata(
                client_name="discord-robinhood-copy-trader",
                redirect_uris=[f"http://127.0.0.1:{self.callback_port}/callback"],
                grant_types=["authorization_code", "refresh_token"],
            ),
            storage=self.storage,
            redirect_handler=redirect_handler,
            callback_handler=callback_handler,
        )

        http_client = httpx2.AsyncClient(auth=oauth_auth, follow_redirects=True, timeout=30)
        self._stack = AsyncExitStack()
        await self._stack.enter_async_context(http_client)
        read_stream, write_stream = await self._stack.enter_async_context(
            streamable_http_client(url=self.server_url, http_client=http_client)
        )
        self._session = await self._stack.enter_async_context(ClientSession(read_stream, write_stream))
        await self._session.initialize()

    def list_tools(self):
        assert self._session is not None and self._loop is not None, "call start() first"
        return asyncio.run_coroutine_threadsafe(self._session.list_tools(), self._loop).result(timeout=30)

    def call_tool(self, name: str, arguments: dict):
        assert self._session is not None and self._loop is not None, "call start() first"
        return asyncio.run_coroutine_threadsafe(self._session.call_tool(name, arguments), self._loop).result(timeout=60)

    def stop(self):
        if not self._loop or not self._loop.is_running():
            return

        async def _close():
            if self._stack:
                await self._stack.aclose()

        asyncio.run_coroutine_threadsafe(_close(), self._loop).result(timeout=10)
        self._loop.call_soon_threadsafe(self._loop.stop)
