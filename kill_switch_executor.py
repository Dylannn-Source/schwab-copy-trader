"""Wraps any OrderExecutor with a remote kill switch.

Polls a small public URL (cached, checked at most once every poll_seconds)
before every order and refuses to place it if the flag says paused — so you
can message Claude to flip that flag and stop live trading without needing
to reach the machine this is running on directly.

This is a convenience layer, not the primary safety net — it depends on
network access and on Claude still having push access to the repo hosting
the flag. Keep a direct kill switch (SSH, Screen Sharing) as the real
fallback.
"""
from __future__ import annotations

import time
import urllib.request


class RemoteKillSwitchExecutor:
    def __init__(self, inner, flag_url: str, activity_log, poll_seconds: float = 20):
        self.inner = inner
        self.flag_url = flag_url
        self.log = activity_log
        self.poll_seconds = poll_seconds
        self._cached_paused = False
        self._last_checked = 0.0

    def _paused(self) -> bool:
        now = time.time()
        if now - self._last_checked >= self.poll_seconds:
            self._last_checked = now
            try:
                with urllib.request.urlopen(self.flag_url, timeout=5) as resp:
                    content = resp.read().decode().strip().lower()
                was_paused = self._cached_paused
                self._cached_paused = content == "true"
                if self._cached_paused and not was_paused:
                    self.log.append("WARNING", "Remote pause flag is now TRUE — live orders will be skipped.")
                elif was_paused and not self._cached_paused:
                    self.log.append("INFO", "Remote pause flag is now FALSE — live orders resumed.")
            except Exception as e:
                self.log.append("WARNING", f"Could not check remote pause flag, keeping last known state: {e}")
        return self._cached_paused

    def _guard(self, label: str, fn, *args):
        if self._paused():
            self.log.append("WARNING", f"PAUSED remotely — skipped {label}")
            return
        fn(*args)

    def buy_to_open(self, event, quantity):
        self._guard("BUY_TO_OPEN", self.inner.buy_to_open, event, quantity)

    def sell_to_open(self, event, quantity):
        self._guard("SELL_TO_OPEN", self.inner.sell_to_open, event, quantity)

    def sell_to_close(self, event, quantity):
        self._guard("SELL_TO_CLOSE", self.inner.sell_to_close, event, quantity)

    def buy_to_close(self, event, quantity):
        self._guard("BUY_TO_CLOSE", self.inner.buy_to_close, event, quantity)

    def buy_equity(self, event, shares):
        self._guard("BUY_EQUITY", self.inner.buy_equity, event, shares)
