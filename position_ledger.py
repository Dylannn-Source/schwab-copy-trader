"""Tracks contracts/shares opened from small-account Discord alerts.

Alerts never restate a running total, only deltas ("SOLD 4/5 ... - 1 runner",
"ALL OUT ..."), so this ledger is what turns those deltas into concrete
quantities to send to the broker. It's advisory, not authoritative — callers
should reconcile it against real broker positions periodically (e.g. via the
Robinhood MCP get_portfolio tool) since a missed or misparsed alert would
otherwise let it drift silently.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)


def _key_str(key: tuple) -> str:
    return "|".join("" if part is None else str(part) for part in key)


class PositionLedger:
    def __init__(self, state_path: str | Path = "position_ledger.json"):
        self.state_path = Path(state_path)
        self._qty: dict[str, int] = self._load()

    def _load(self) -> dict[str, int]:
        if not self.state_path.exists():
            return {}
        try:
            return json.loads(self.state_path.read_text())
        except Exception as e:
            log.warning("Could not load position ledger, starting empty: %s", e)
            return {}

    def _save(self):
        try:
            self.state_path.write_text(json.dumps(self._qty, indent=2, sort_keys=True))
        except Exception as e:
            log.warning("Could not persist position ledger: %s", e)

    def current(self, key: tuple) -> int:
        return self._qty.get(_key_str(key), 0)

    def open(self, key: tuple, qty: int) -> int:
        """Adds `qty` to the tracked position, returns the new total."""
        k = _key_str(key)
        total = self._qty.get(k, 0) + qty
        self._qty[k] = total
        self._save()
        return total

    def close_partial(self, key: tuple, num: int, den: int) -> int:
        """Closes `num` contracts. `den` is the caller's asserted original size —
        used only to sanity-check the ledger, logged on mismatch but not trusted
        over the actual tracked quantity. Returns the number of contracts to
        actually close (capped at what's tracked, so a drifted ledger can't send
        an oversized close order)."""
        k = _key_str(key)
        current = self._qty.get(k, 0)
        if current != den:
            log.warning(
                "Position ledger mismatch for %s: tracked=%d, alert asserts total=%d",
                key, current, den,
            )
        to_close = min(num, current)
        self._qty[k] = max(0, current - to_close)
        self._save()
        return to_close

    def close_all(self, key: tuple) -> int | None:
        """Closes the entire tracked position. Returns None (nothing sent to the
        broker) if the ledger has no record for this key — e.g. a typo'd strike
        in the alert, or an ALL OUT for something never seen opening — rather
        than guessing a quantity."""
        k = _key_str(key)
        current = self._qty.get(k, 0)
        if current <= 0:
            return None
        self._qty[k] = 0
        self._save()
        return current
