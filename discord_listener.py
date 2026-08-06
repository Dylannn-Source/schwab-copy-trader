"""Thin wrapper around discord.py-self's Client, scoped to one channel.

Requires the `discord.py-self` package, NOT `discord.py` — both install as
the `discord` module and will conflict if both are present in the same
environment.

Automating a user account like this violates Discord's Terms of Service and
risks the account being banned if detected. This exists because the account
owner has already accepted that risk for their own account; it doesn't touch
anyone else's.

discord.py-self already retries routine Gateway disconnects internally. What
this adds is recovery from the outer case — Client.run() itself raising and
returning, which otherwise silently ends the listener thread with nothing to
restart it. On that, a fresh Client is built (a closed one can't be reused)
and reconnection is retried with exponential backoff until stop() is called.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Callable

import discord

log = logging.getLogger(__name__)

INITIAL_BACKOFF_SECONDS = 5
MAX_BACKOFF_SECONDS = 120


class DiscordAlertListener:
    def __init__(
        self,
        token: str,
        channel_id: int,
        on_message: Callable[[str], None],
        activity_log=None,
    ):
        self.token = token
        self.channel_id = int(channel_id)
        self.on_message = on_message
        self.activity_log = activity_log
        self._client: discord.Client | None = None
        self._thread: threading.Thread | None = None
        self._stopping = False

    def _log(self, level: str, message: str):
        if self.activity_log:
            self.activity_log.append(level, message)
        else:
            log.log(getattr(logging, level, logging.INFO), message)

    def _build_client(self) -> discord.Client:
        listener = self

        class _Client(discord.Client):
            async def on_ready(inner_self):
                listener._log("INFO", f"Discord listener connected as {inner_self.user}")

            async def on_message(inner_self, message):
                if message.channel.id != listener.channel_id:
                    return
                if not message.content:
                    return
                try:
                    listener.on_message(message.content)
                except Exception:
                    log.exception("Error handling Discord message %s", message.id)

        return _Client()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stopping = False
        self._thread = threading.Thread(target=self._run, daemon=True, name="discord-listener")
        self._thread.start()

    def _run(self):
        backoff = INITIAL_BACKOFF_SECONDS
        while not self._stopping:
            self._client = self._build_client()
            try:
                self._client.run(self.token)
            except Exception:
                log.exception("Discord listener crashed")

            if self._stopping:
                break
            self._log("WARNING", f"Discord connection dropped — reconnecting in {backoff}s...")
            time.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)

    def stop(self):
        self._stopping = True
        if self._client is None:
            return
        loop = self._client.loop
        if loop and loop.is_running():
            asyncio.run_coroutine_threadsafe(self._client.close(), loop)

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()
