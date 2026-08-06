"""Parses Sniper Alerts-style Discord messages into structured trade events.

Message format observed in the source channel (one message can contain
several action lines, each optionally followed by free-text commentary):

    BOUGHT DDOG 265C 7/10 5                          (long option open, no size — larger account, not actionable)
    SOLD cash secured put MU 800P 7/17 14.6          (short put open)
    SOLD covered calls CRWV 100C 7/17 1.25           (short call open)
    BOUGHT CRWV shares 85.88                         (equity open)
    SOLD 1/2 DELL 460C 7/10 6.5                      (partial close, no known total — larger account)
    SOLD 4/5 MSFT 492.5C 8/5 1.6 - 1 runner          (partial close, num = contracts to close)
    ALL OUT DDOG 265C 7/10 5.5                       (full close)
    BOUGHT SKHY 160C 8/7 4.8‼️ - 1 on small account   (small-account open, size given)

Only lines carrying the ‼️ marker are on the caller's small account, which is
the only account that states position size — everything else is skipped.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

BANG = "‼"  # ‼ — U+FE0F variation selector may or may not follow it

OPTION_TOKEN = r"(?P<strike>\d+(?:\.\d+)?)(?P<right>[CP])"
EXPIRY_TOKEN = r"(?P<expiry>\d{1,2}/\d{1,2})"
PRICE_TOKEN = r"(?P<price>\d+(?:\.\d+)?)"
SYMBOL_TOKEN = r"(?P<symbol>[A-Za-z]{1,6})"

PARTIAL_CLOSE_RE = re.compile(
    rf"^SOLD\s+(?P<num>\d+)/(?P<den>\d+)\s+{SYMBOL_TOKEN}\s+{OPTION_TOKEN}\s+{EXPIRY_TOKEN}\s+{PRICE_TOKEN}",
    re.IGNORECASE,
)
CASH_SECURED_PUT_RE = re.compile(
    rf"^SOLD\s+cash secured puts?\s+{SYMBOL_TOKEN}\s+{OPTION_TOKEN}\s+{EXPIRY_TOKEN}\s+{PRICE_TOKEN}",
    re.IGNORECASE,
)
COVERED_CALL_RE = re.compile(
    rf"^SOLD\s+covered calls?\s+{SYMBOL_TOKEN}\s+{OPTION_TOKEN}\s+{EXPIRY_TOKEN}\s+{PRICE_TOKEN}",
    re.IGNORECASE,
)
EQUITY_BUY_RE = re.compile(
    rf"^BOUGHT\s+{SYMBOL_TOKEN}\s+shares\s+{PRICE_TOKEN}",
    re.IGNORECASE,
)
ALL_OUT_RE = re.compile(
    rf"^ALL OUT\s+{SYMBOL_TOKEN}\s+{OPTION_TOKEN}\s+{EXPIRY_TOKEN}\s+{PRICE_TOKEN}",
    re.IGNORECASE,
)
LONG_OPEN_RE = re.compile(
    rf"^BOUGHT\s+{SYMBOL_TOKEN}\s+{OPTION_TOKEN}\s+{EXPIRY_TOKEN}\s+{PRICE_TOKEN}",
    re.IGNORECASE,
)
PLAIN_CLOSE_RE = re.compile(
    rf"^SOLD\s+{SYMBOL_TOKEN}\s+{OPTION_TOKEN}\s+{EXPIRY_TOKEN}\s+{PRICE_TOKEN}",
    re.IGNORECASE,
)
SMALL_ACCOUNT_QTY_RE = re.compile(r"-\s*(?P<qty>\d+)\s+on small\b", re.IGNORECASE)

ACTIONS = (
    "OPEN_LONG_OPTION",
    "OPEN_SHORT_PUT",
    "OPEN_SHORT_CALL",
    "OPEN_EQUITY",
    "CLOSE_PARTIAL",
    "CLOSE_ALL",
    "UNPARSEABLE",
)


@dataclass
class AlertEvent:
    action: str
    raw_line: str
    small_account: bool
    symbol: str | None = None
    strike: float | None = None
    right: str | None = None  # "CALL" | "PUT"
    expiry_mmdd: str | None = None
    price: float | None = None
    quantity: int | None = None       # explicit size, for opens on the small account
    close_num: int | None = None      # for CLOSE_PARTIAL — contracts to close
    close_den: int | None = None      # for CLOSE_PARTIAL — asserted original size, sanity check only

    def position_key(self) -> tuple:
        return (self.symbol, self.strike, self.right, self.expiry_mmdd)


def _right_name(letter: str) -> str:
    return "CALL" if letter.upper() == "C" else "PUT"


def _is_small_account(line: str) -> bool:
    return BANG in line


def parse_line(line: str) -> AlertEvent | None:
    """Parses a single line. Returns None for lines with no recognizable action
    (free-text commentary), so callers can just skip them."""
    line = line.strip()
    if not line:
        return None

    small = _is_small_account(line)

    m = PARTIAL_CLOSE_RE.match(line)
    if m:
        return AlertEvent(
            action="CLOSE_PARTIAL",
            raw_line=line,
            small_account=small,
            symbol=m["symbol"].upper(),
            strike=float(m["strike"]),
            right=_right_name(m["right"]),
            expiry_mmdd=m["expiry"],
            price=float(m["price"]),
            close_num=int(m["num"]),
            close_den=int(m["den"]),
        )

    m = CASH_SECURED_PUT_RE.match(line)
    if m:
        qty_m = SMALL_ACCOUNT_QTY_RE.search(line)
        return AlertEvent(
            action="OPEN_SHORT_PUT",
            raw_line=line,
            small_account=small,
            symbol=m["symbol"].upper(),
            strike=float(m["strike"]),
            right=_right_name(m["right"]),
            expiry_mmdd=m["expiry"],
            price=float(m["price"]),
            quantity=int(qty_m["qty"]) if qty_m else None,
        )

    m = COVERED_CALL_RE.match(line)
    if m:
        qty_m = SMALL_ACCOUNT_QTY_RE.search(line)
        return AlertEvent(
            action="OPEN_SHORT_CALL",
            raw_line=line,
            small_account=small,
            symbol=m["symbol"].upper(),
            strike=float(m["strike"]),
            right=_right_name(m["right"]),
            expiry_mmdd=m["expiry"],
            price=float(m["price"]),
            quantity=int(qty_m["qty"]) if qty_m else None,
        )

    m = EQUITY_BUY_RE.match(line)
    if m:
        qty_m = SMALL_ACCOUNT_QTY_RE.search(line)
        return AlertEvent(
            action="OPEN_EQUITY",
            raw_line=line,
            small_account=small,
            symbol=m["symbol"].upper(),
            price=float(m["price"]),
            quantity=int(qty_m["qty"]) if qty_m else None,
        )

    m = ALL_OUT_RE.match(line)
    if m:
        return AlertEvent(
            action="CLOSE_ALL",
            raw_line=line,
            small_account=small,
            symbol=m["symbol"].upper(),
            strike=float(m["strike"]),
            right=_right_name(m["right"]),
            expiry_mmdd=m["expiry"],
            price=float(m["price"]),
        )

    m = LONG_OPEN_RE.match(line)
    if m:
        qty_m = SMALL_ACCOUNT_QTY_RE.search(line)
        return AlertEvent(
            action="OPEN_LONG_OPTION",
            raw_line=line,
            small_account=small,
            symbol=m["symbol"].upper(),
            strike=float(m["strike"]),
            right=_right_name(m["right"]),
            expiry_mmdd=m["expiry"],
            price=float(m["price"]),
            quantity=int(qty_m["qty"]) if qty_m else None,
        )

    m = PLAIN_CLOSE_RE.match(line)
    if m:
        # "SOLD SYMBOL STRIKE±RIGHT EXPIRY PRICE" with no fraction, no cash-secured/
        # covered qualifier — the caller's shorthand for closing an entire long
        # position. Flagged UNPARSEABLE instead of guessed, since it's ambiguous
        # against "sell to open" without more examples.
        return AlertEvent(action="UNPARSEABLE", raw_line=line, small_account=small)

    return None


def parse_message(content: str) -> list[AlertEvent]:
    """A single Discord message can carry multiple independent alert lines."""
    events = []
    for line in content.splitlines():
        event = parse_line(line)
        if event is not None:
            events.append(event)
    return events


def resolve_expiry(mmdd: str, reference: date, past_slack_days: int = 25) -> date:
    """Turns a bare 'M/D' into a real date. Alerts never carry a year, so this
    assumes the current year unless that would place the expiry more than
    `past_slack_days` before the reference date, in which case it rolls to next
    year (handles alerts posted in December for January expiries)."""
    month, day = (int(part) for part in mmdd.split("/"))
    candidate = date(reference.year, month, day)
    if (reference - candidate).days > past_slack_days:
        candidate = date(reference.year + 1, month, day)
    return candidate
