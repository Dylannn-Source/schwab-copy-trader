"""Wraps any OrderExecutor with an in-process pause switch, toggled instantly
from the dashboard's Pause/Resume buttons — no network round trip, unlike
RemoteKillSwitchExecutor. Chain both together for defense in depth: either
one being paused blocks the order.
"""
from __future__ import annotations


class LocalPauseExecutor:
    def __init__(self, inner, activity_log):
        self.inner = inner
        self.log = activity_log
        self.paused = False

    def set_paused(self, paused: bool):
        if paused == self.paused:
            return
        self.paused = paused
        self.log.append("WARNING" if paused else "INFO", f"Live trading {'PAUSED' if paused else 'RESUMED'} via dashboard.")

    def _guard(self, label: str, fn, *args):
        if self.paused:
            self.log.append("WARNING", f"PAUSED — skipped {label}")
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
