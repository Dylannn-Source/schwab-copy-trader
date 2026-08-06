"""Thin wrapper around discord.py-self's Client, scoped to one channel.

Requires the `discord.py-self` package, NOT `discord.py` — both install as
the `discord` module and will conflict if both are present in the same
environment.

Automating a user account like this violates Discord's Terms of Service and
risks the account being banned if detected. This exists because the account
owner has already accepted that risk for their own account; it doesn't touch
anyone else's.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Callable

import discord

log = logging.getLogger(__name__)


class DiscordAlertListener:
    def __init__(self, token: str, channel_id: int, on_message: Callable[[str], None]):
        self.token = token
        self.channel_id = int(channel_id)
        self.on_message = on_message
        self._client = self._build_client()
        self._thread: threading.Thread | None = None

    def _build_client(self) -> discord.Client:
        listener = self

        class _Client(discord.Client):
            async def on_ready(inner_self):
                log.info("Discord listener connected as %s", inner_self.user)

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
        self._thread = threading.Thread(target=self._run, daemon=True, name="discord-listener")
        self._thread.start()

    def _run(self):
        try:
            self._client.run(self.token)
        except Exception:
            log.exception("Discord listener crashed")

    def stop(self):
        loop = self._client.loop
        if loop and loop.is_running():
            asyncio.run_coroutine_threadsafe(self._client.close(), loop)

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()
