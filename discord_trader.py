"""Copies small-account Discord alerts (‼️-marked) to a broker via a pluggable
executor. Mirrors the shape of trader.py's CopyTrader: parse, resolve
quantity, place order, log the outcome.

The executor is intentionally abstract — DryRunExecutor is the default so
this can run and be observed before it's trusted to place real orders. Swap
in a Robinhood MCP-backed executor once that connection is set up.
"""
from __future__ import annotations

import logging
from typing import Protocol

from discord_alert_parser import AlertEvent, parse_message
from discord_listener import DiscordAlertListener
from position_ledger import PositionLedger

log = logging.getLogger(__name__)


class OrderExecutor(Protocol):
    def buy_to_open(self, event: AlertEvent, quantity: int) -> None: ...
    def sell_to_open(self, event: AlertEvent, quantity: int) -> None: ...
    def sell_to_close(self, event: AlertEvent, quantity: int) -> None: ...
    def buy_to_close(self, event: AlertEvent, quantity: int) -> None: ...
    def buy_equity(self, event: AlertEvent, shares: int) -> None: ...


class DryRunExecutor:
    """Logs what would be sent instead of placing real orders."""

    def __init__(self, activity_log):
        self.log = activity_log

    def _leg(self, event: AlertEvent) -> str:
        return f"{event.symbol} {event.strike}{event.right[0]} {event.expiry_mmdd}"

    def buy_to_open(self, event, quantity):
        self.log.append("INFO", f"[DRY RUN] BUY_TO_OPEN {quantity}x {self._leg(event)} (alert price {event.price})")

    def sell_to_open(self, event, quantity):
        self.log.append("INFO", f"[DRY RUN] SELL_TO_OPEN {quantity}x {self._leg(event)} (alert price {event.price})")

    def sell_to_close(self, event, quantity):
        self.log.append("INFO", f"[DRY RUN] SELL_TO_CLOSE {quantity}x {self._leg(event)} (alert price {event.price})")

    def buy_to_close(self, event, quantity):
        self.log.append("INFO", f"[DRY RUN] BUY_TO_CLOSE {quantity}x {self._leg(event)} (alert price {event.price})")

    def buy_equity(self, event, shares):
        self.log.append("INFO", f"[DRY RUN] BUY {shares}x {event.symbol} shares (alert price {event.price})")


class DiscordCopyTrader:
    def __init__(self, config: dict, activity_log, executor: OrderExecutor | None = None):
        self.config = config
        self.log = activity_log
        self.executor = executor or DryRunExecutor(activity_log)
        self.ledger = PositionLedger(config.get("position_ledger_path", "position_ledger.json"))
        self.listener = DiscordAlertListener(
            token=config["discord_user_token"],
            channel_id=config["discord_alert_channel_id"],
            on_message=self._handle_message,
            activity_log=activity_log,
        )

    def start(self):
        self.listener.start()
        self.log.append("INFO", "Discord alert listener started.")

    def stop(self):
        self.listener.stop()
        self.log.append("INFO", "Discord alert listener stopped.")

    def is_running(self) -> bool:
        return self.listener.is_running()

    def _handle_message(self, content: str):
        for event in parse_message(content):
            try:
                self._handle_event(event)
            except Exception:
                log.exception("Error handling alert event: %r", event)

    def _handle_event(self, event: AlertEvent):
        if event.action == "UNPARSEABLE":
            self.log.append("WARNING", f"Unparseable alert line, skipped: {event.raw_line!r}")
            return
        if not event.small_account:
            # Larger-account alerts never state a size — nothing to safely mirror.
            return

        key = event.position_key()

        if event.action == "OPEN_LONG_OPTION":
            self._handle_open(event, key, "LONG", self.executor.buy_to_open)
        elif event.action in ("OPEN_SHORT_PUT", "OPEN_SHORT_CALL"):
            self._handle_open(event, key, "SHORT", self.executor.sell_to_open)
        elif event.action == "OPEN_EQUITY":
            if event.quantity is None:
                self.log.append("WARNING", f"Small-account equity buy with no share count found, skipped: {event.raw_line!r}")
                return
            self.executor.buy_equity(event, event.quantity)
        elif event.action == "CLOSE_PARTIAL":
            qty, side = self.ledger.close_partial(key, event.close_num, event.close_den)
            self._handle_close(event, qty, side)
        elif event.action == "CLOSE_ALL":
            qty, side = self.ledger.close_all(key)
            self._handle_close(event, qty, side)

    def _handle_open(self, event: AlertEvent, key: tuple, side: str, place_order):
        if event.quantity is None:
            self.log.append("WARNING", f"Small-account open with no quantity found, skipped: {event.raw_line!r}")
            return
        self.ledger.open(key, event.quantity, side)
        place_order(event, event.quantity)

    def _handle_close(self, event: AlertEvent, qty: int | None, side: str | None):
        if not qty:
            self.log.append("WARNING", f"Close with nothing tracked, skipped: {event.raw_line!r}")
            return
        if side == "SHORT":
            self.executor.buy_to_close(event, qty)
        else:
            # Default to LONG's sell-to-close — matches every position we
            # actually track opens for (side should never be unset here).
            self.executor.sell_to_close(event, qty)
