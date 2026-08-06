"""Tracks contracts/shares opened from small-account Discord alerts.

Alerts never restate a running total, only deltas ("SOLD 4/5 ... - 1 runner",
"ALL OUT ..."), so this ledger is what turns those deltas into concrete
quantities to send to the broker. It also tracks whether each position is
LONG (bought calls/puts — closes with a sell) or SHORT (cash-secured puts,
covered calls — closes with a buy), since "ALL OUT"/"SOLD n/m" use identical
wording for both but need opposite order sides.

It's advisory, not authoritative — callers should reconcile it against real
broker positions periodically (e.g. via the Robinhood MCP get_portfolio tool)
since a missed or misparsed alert would otherwise let it drift silently.
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
        self._positions: dict[str, dict] = self._load()

    def _load(self) -> dict[str, dict]:
        if not self.state_path.exists():
            return {}
        try:
            return json.loads(self.state_path.read_text())
        except Exception as e:
            log.warning("Could not load position ledger, starting empty: %s", e)
            return {}

    def _save(self):
        try:
            self.state_path.write_text(json.dumps(self._positions, indent=2, sort_keys=True))
        except Exception as e:
            log.warning("Could not persist position ledger: %s", e)

    def current(self, key: tuple) -> int:
        return self._positions.get(_key_str(key), {}).get("qty", 0)

    def open(self, key: tuple, qty: int, side: str) -> int:
        """Adds `qty` to the tracked position, returns the new total.

        `side` is "LONG" (bought — closes with a sell) or "SHORT" (sold to
        open — closes with a buy). If this key already has a tracked position
        with the opposite side, that's a real inconsistency (e.g. two
        differently-parsed alerts for the same contract) — logged, and the
        new side wins, since it's the more recent alert.
        """
        k = _key_str(key)
        existing = self._positions.get(k)
        if existing and existing.get("side") != side and existing.get("qty", 0) > 0:
            log.warning(
                "Position ledger side mismatch for %s: tracked side=%s, new open is side=%s",
                key, existing.get("side"), side,
            )
        total = (existing.get("qty", 0) if existing else 0) + qty
        self._positions[k] = {"qty": total, "side": side}
        self._save()
        return total

    def close_partial(self, key: tuple, num: int, den: int) -> tuple[int, str | None]:
        """Closes `num` contracts. `den` is the caller's asserted original size —
        used only to sanity-check the ledger, logged on mismatch but not trusted
        over the actual tracked quantity. Returns (quantity to actually close,
        side of the position) — quantity capped at what's tracked, so a drifted
        ledger can't send an oversized close order. side is None if nothing was
        tracked for this key."""
        k = _key_str(key)
        entry = self._positions.get(k)
        current = entry.get("qty", 0) if entry else 0
        side = entry.get("side") if entry else None
        if current != den:
            log.warning(
                "Position ledger mismatch for %s: tracked=%d, alert asserts total=%d",
                key, current, den,
            )
        to_close = min(num, current)
        if entry:
            entry["qty"] = max(0, current - to_close)
        self._save()
        return to_close, side

    def close_all(self, key: tuple) -> tuple[int | None, str | None]:
        """Closes the entire tracked position. Returns (None, None) if the
        ledger has no record for this key — e.g. a typo'd strike in the alert,
        or an ALL OUT for something never seen opening — rather than guessing
        a quantity."""
        k = _key_str(key)
        entry = self._positions.get(k)
        current = entry.get("qty", 0) if entry else 0
        if current <= 0:
            return None, None
        side = entry.get("side")
        entry["qty"] = 0
        self._save()
        return current, side
