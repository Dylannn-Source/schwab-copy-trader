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
import time
import webbrowser
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


INITIAL_BACKOFF_SECONDS = 5
MAX_BACKOFF_SECONDS = 120


class RobinhoodMCPClient:
    def __init__(
        self,
        token_path: str | Path = "robinhood_mcp_token.json",
        server_url: str = DEFAULT_SERVER_URL,
        callback_port: int = DEFAULT_CALLBACK_PORT,
        activity_log=None,
    ):
        self.server_url = server_url
        self.callback_port = callback_port
        self.storage = FileTokenStorage(token_path)
        self.activity_log = activity_log
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._session: ClientSession | None = None
        self._stop_event: asyncio.Event | None = None
        self._ready = threading.Event()
        self._error: Exception | None = None
        self._stopping = False

    def _log(self, level: str, message: str):
        if self.activity_log:
            self.activity_log.append(level, message)
        else:
            print(f"[{level}] {message}")

    def start(self, timeout: float = 300):
        """Connects and authenticates. Blocks until ready — on first run this
        includes the time you spend approving access in your browser. If the
        connection later drops (network blip, server restart), reconnection is
        retried automatically in the background with exponential backoff —
        no need to call start() again."""
        self._thread = threading.Thread(target=self._run, daemon=True, name="robinhood-mcp")
        self._thread.start()
        if not self._ready.wait(timeout=timeout):
            raise TimeoutError("Timed out connecting to Robinhood's MCP server.")
        if self._error:
            raise self._error

    def _run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        backoff = INITIAL_BACKOFF_SECONDS

        while not self._stopping:
            self._session = None
            try:
                self._loop.run_until_complete(self._connect_and_serve())
                break  # _connect_and_serve only returns normally after a clean stop()
            except Exception as e:
                self._error = e
                if not self._ready.is_set():
                    # Never connected successfully even once — a hard failure
                    # (bad config, auth denied). Surface it to start() and stop;
                    # this isn't a transient drop worth retrying forever.
                    self._ready.set()
                    return
                self._log("ERROR", f"Robinhood MCP connection lost: {e}")

            if self._stopping:
                break
            self._log("WARNING", f"Reconnecting to Robinhood MCP in {backoff}s...")
            time.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)

    async def _connect_and_serve(self):
        # The whole connection lifetime — open, serve tool calls, close — has to
        # run as a single asyncio Task. streamable_http_client's internal task
        # group ties its cancel scope to whichever Task entered it; closing it
        # from a different Task (e.g. one spun up later by
        # run_coroutine_threadsafe for a separate stop() call) raises
        # "Attempted to exit cancel scope in a different task than it was
        # entered in". Blocking on self._stop_event here, inside the same
        # `async with`, keeps entry and exit in that one Task.
        self._stop_event = asyncio.Event()
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

        # Deliberately not caught here — exceptions propagate up to _run(),
        # which is what decides whether to retry or give up.
        async with httpx2.AsyncClient(auth=oauth_auth, follow_redirects=True, timeout=30) as http_client:
            async with streamable_http_client(url=self.server_url, http_client=http_client) as (
                read_stream,
                write_stream,
            ):
                async with ClientSession(read_stream, write_stream) as session:
                    self._session = session
                    await session.initialize()
                    was_ready = self._ready.is_set()
                    self._ready.set()
                    if was_ready:
                        self._log("INFO", "Reconnected to Robinhood MCP.")
                    await self._stop_event.wait()

    def list_tools(self):
        if self._session is None:
            raise RuntimeError("Not currently connected to Robinhood MCP (reconnecting in the background).")
        return asyncio.run_coroutine_threadsafe(self._session.list_tools(), self._loop).result(timeout=30)

    def call_tool(self, name: str, arguments: dict):
        if self._session is None:
            raise RuntimeError("Not currently connected to Robinhood MCP (reconnecting in the background).")
        return asyncio.run_coroutine_threadsafe(self._session.call_tool(name, arguments), self._loop).result(timeout=60)

    def stop(self):
        self._stopping = True
        if not self._loop or not self._thread or not self._thread.is_alive():
            return
        if self._stop_event:
            self._loop.call_soon_threadsafe(self._stop_event.set)
        self._thread.join(timeout=10)
