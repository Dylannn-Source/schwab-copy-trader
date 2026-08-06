"""Places real Robinhood orders for parsed Discord alerts.

Resolves each alert's (symbol, strike, right, expiry) into a Robinhood
option contract UUID via get_option_chains -> get_option_instruments (caching
both lookups, since the same contract is referenced again on close), then
places a single-leg market order for it. Equity buys go straight to
place_equity_order.
"""
from __future__ import annotations

import uuid
from datetime import date

from discord_alert_parser import AlertEvent, resolve_expiry


def _summarize(result) -> str:
    data = (result.structured_content or {}).get("data")
    if data is not None:
        return str(data)
    if result.content:
        block = result.content[0]
        return getattr(block, "text", str(block))[:300]
    return "(no detail)"


class RobinhoodExecutor:
    def __init__(self, mcp_client, account_number: str, activity_log):
        self.client = mcp_client
        self.account_number = account_number
        self.log = activity_log
        self._chain_id_cache: dict[str, str] = {}
        self._option_id_cache: dict[tuple, str] = {}

    def _leg(self, event: AlertEvent) -> str:
        return f"{event.symbol} {event.strike}{event.right[0]} {event.expiry_mmdd}"

    def _resolve_chain_id(self, symbol: str) -> str:
        if symbol in self._chain_id_cache:
            return self._chain_id_cache[symbol]
        result = self.client.call_tool("get_option_chains", {"underlying_symbol": symbol})
        chains = (result.structured_content or {}).get("data", {}).get("chains", [])
        if not chains:
            raise RuntimeError(f"No option chain found for {symbol}")
        chain_id = chains[0]["id"]
        self._chain_id_cache[symbol] = chain_id
        return chain_id

    def _resolve_option_id(self, event: AlertEvent) -> str:
        key = event.position_key()
        if key in self._option_id_cache:
            return self._option_id_cache[key]

        chain_id = self._resolve_chain_id(event.symbol)
        expiry_date = resolve_expiry(event.expiry_mmdd, date.today())

        result = self.client.call_tool("get_option_instruments", {
            "chain_id": chain_id,
            "expiration_dates": expiry_date.isoformat(),
            "strike_price": f"{event.strike:.4f}",
            "type": event.right.lower(),
        })
        instruments = (result.structured_content or {}).get("data", {}).get("instruments", [])
        if not instruments:
            raise RuntimeError(f"No option contract found for {self._leg(event)} (expiry {expiry_date.isoformat()})")
        option_id = instruments[0]["id"]
        self._option_id_cache[key] = option_id
        return option_id

    def _place_option(self, event: AlertEvent, quantity: int, side: str, position_effect: str):
        label = f"{side.upper()}_TO_{position_effect.upper()}"
        try:
            option_id = self._resolve_option_id(event)
        except Exception as e:
            self.log.append("ERROR", f"ORDER FAILED {label} {self._leg(event)} — could not resolve contract: {e}")
            return

        try:
            result = self.client.call_tool("place_option_order", {
                "account_number": self.account_number,
                "legs": [{
                    "option_id": option_id,
                    "side": side,
                    "position_effect": position_effect,
                    "ratio_quantity": 1,
                }],
                "type": "market",
                "quantity": str(quantity),
                "ref_id": str(uuid.uuid4()),
            })
            self.log.append("INFO", f"ORDER PLACED {label} {quantity}x {self._leg(event)} — {_summarize(result)}")
        except Exception as e:
            self.log.append("ERROR", f"ORDER FAILED {label} {quantity}x {self._leg(event)}: {e}")

    def buy_to_open(self, event: AlertEvent, quantity: int):
        self._place_option(event, quantity, "buy", "open")

    def sell_to_open(self, event: AlertEvent, quantity: int):
        self._place_option(event, quantity, "sell", "open")

    def sell_to_close(self, event: AlertEvent, quantity: int):
        self._place_option(event, quantity, "sell", "close")

    def buy_to_close(self, event: AlertEvent, quantity: int):
        self._place_option(event, quantity, "buy", "close")

    def buy_equity(self, event: AlertEvent, shares: int):
        try:
            result = self.client.call_tool("place_equity_order", {
                "account_number": self.account_number,
                "symbol": event.symbol,
                "side": "buy",
                "type": "market",
                "quantity": str(shares),
                "ref_id": str(uuid.uuid4()),
            })
            self.log.append("INFO", f"ORDER PLACED BUY {shares}x {event.symbol} shares — {_summarize(result)}")
        except Exception as e:
            self.log.append("ERROR", f"ORDER FAILED BUY {shares}x {event.symbol} shares: {e}")
