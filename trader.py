import collections
import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from schwab.client import Client
from schwab.orders import options as opt
from schwab.utils import Utils

log = logging.getLogger(__name__)

OPTION_INSTRUCTIONS = {
    "BUY_TO_OPEN": opt.option_buy_to_open_market,
    "SELL_TO_CLOSE": opt.option_sell_to_close_market,
    "BUY_TO_CLOSE": opt.option_buy_to_close_market,
    "SELL_TO_OPEN": opt.option_sell_to_open_market,
}

TERMINAL_ORDER_STATUSES = {
    Client.Order.Status.FILLED,
    Client.Order.Status.REJECTED,
    Client.Order.Status.CANCELED,
    Client.Order.Status.EXPIRED,
}


class ActivityLog:
    def __init__(self, maxlen=500):
        self._entries = collections.deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def append(self, level: str, message: str):
        entry = {
            "time": datetime.now().strftime("%H:%M:%S"),
            "level": level,
            "message": message,
        }
        with self._lock:
            self._entries.append(entry)
        log.log(getattr(logging, level, logging.INFO), message)

    def entries(self) -> list[dict]:
        with self._lock:
            return list(self._entries)


class CopyTrader:
    def __init__(self, config: dict, client, activity_log: ActivityLog):
        self.config = config
        self.client = client
        self.log = activity_log
        self.leader = config["leader_account_hash"]
        self.followers = config["follower_account_hashes"]
        self.multiplier = float(config.get("size_multiplier", 1.0))
        self.poll_interval = int(config.get("poll_interval_seconds", 5))
        self.lookback_hours = int(config.get("lookback_hours", 2))
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

        # Persisted so a restart can tell "already replicated in a previous
        # session" apart from "genuinely new, happened while we were down" —
        # without this, every restart blindly re-seeds recent fills as
        # already-handled and silently drops trades that occurred just
        # before startup.
        self.state_path = Path(config.get("state_path", "trader_state.json"))
        self._is_first_run = not self.state_path.exists()
        self.seen_order_ids: set[str] = self._load_state()

    # ------------------------------------------------------------------
    # Control
    # ------------------------------------------------------------------

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="copy-trader")
        self._thread.start()
        self.log.append("INFO", f"Trader started — multiplier={self.multiplier}x")

    def stop(self):
        self._stop_event.set()
        self.log.append("INFO", "Trader stopped.")

    def is_running(self) -> bool:
        return (
            self._thread is not None
            and self._thread.is_alive()
            and not self._stop_event.is_set()
        )

    def set_multiplier(self, value: float):
        self.multiplier = max(0.1, round(float(value), 2))
        self.log.append("INFO", f"Multiplier updated to {self.multiplier}x")

    # ------------------------------------------------------------------
    # Persisted state
    # ------------------------------------------------------------------

    def _load_state(self) -> set[str]:
        if self._is_first_run:
            return set()
        try:
            data = json.loads(self.state_path.read_text())
            return set(data.get("processed_order_ids", []))
        except Exception as e:
            self.log.append("WARNING", f"Could not load trader state, starting empty: {e}")
            return set()

    def _save_state(self):
        try:
            self.state_path.write_text(json.dumps({"processed_order_ids": sorted(self.seen_order_ids)}))
        except Exception as e:
            self.log.append("WARNING", f"Could not persist trader state: {e}")

    def _mark_seen(self, order_id: str):
        self.seen_order_ids.add(order_id)
        self._save_state()

    # ------------------------------------------------------------------
    # Positions
    # ------------------------------------------------------------------

    def get_positions(self, account_hash: str) -> list[dict]:
        resp = self.client.get_account(account_hash, fields=Client.Account.Fields.POSITIONS)
        resp.raise_for_status()
        data = resp.json()
        raw = data.get("securitiesAccount", {}).get("positions", [])
        result = []
        for pos in raw:
            inst = pos.get("instrument", {})
            if inst.get("assetType") != "OPTION":
                continue
            qty = pos.get("longQuantity", 0) - pos.get("shortQuantity", 0)
            result.append({
                "symbol": inst.get("symbol", ""),
                "description": inst.get("description", ""),
                "quantity": qty,
                "market_value": round(pos.get("marketValue", 0), 2),
                "average_price": round(pos.get("averagePrice", 0), 2),
                "day_pnl": round(pos.get("currentDayProfitLoss", 0), 2),
            })
        return result

    # ------------------------------------------------------------------
    # Poll loop
    # ------------------------------------------------------------------

    def _run(self):
        try:
            self._catch_up()
        except Exception as e:
            self.log.append("ERROR", f"Failed to seed orders: {e}")

        while not self._stop_event.is_set():
            try:
                self._poll()
            except Exception as e:
                self.log.append("ERROR", f"Poll error: {e}")
            self._stop_event.wait(self.poll_interval)

    def _catch_up(self):
        fills = self._filled_orders()
        if self._is_first_run:
            # No persisted state — this is the very first time this leader
            # account has been tracked, so treat existing fills as pre-existing
            # history rather than trades to replicate.
            for order in fills:
                self._mark_seen(str(order["orderId"]))
            self.log.append("INFO", f"First run — seeded {len(self.seen_order_ids)} existing fills without replicating.")
            return

        replicated = 0
        for order in fills:
            oid = str(order["orderId"])
            if oid not in self.seen_order_ids:
                self._mark_seen(oid)
                replicated += 1
                self.log.append("INFO", f"Unreplicated fill from before restart — orderId={oid}")
                self._replicate(order)
        self.log.append("INFO", f"Resumed with {len(self.seen_order_ids)} known fills ({replicated} replicated on startup).")

    def _poll(self):
        for order in self._filled_orders():
            oid = str(order["orderId"])
            if oid not in self.seen_order_ids:
                self._mark_seen(oid)
                self.log.append("INFO", f"New fill detected — orderId={oid}")
                self._replicate(order)

    def _filled_orders(self) -> list[dict]:
        now = datetime.now(timezone.utc)
        since = now - timedelta(hours=self.lookback_hours)
        resp = self.client.get_orders_for_account(
            self.leader,
            from_entered_datetime=since,
            to_entered_datetime=now,
            status=Client.Order.Status.FILLED,
        )
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Replication
    # ------------------------------------------------------------------

    def _replicate(self, order: dict):
        legs = order.get("orderLegCollection", [])
        if len(legs) > 1:
            self.log.append(
                "WARNING",
                f"Multi-leg (spread) order skipped — orderId={order.get('orderId')}. "
                "Spread support not yet implemented.",
            )
            return
        if legs:
            self._replicate_single(legs[0])

    def _follower_qty(self, leader_qty: float) -> int:
        return max(1, round(leader_qty * self.multiplier))

    def _replicate_single(self, leg: dict):
        inst = leg.get("instrument", {})
        if inst.get("assetType") != "OPTION":
            self.log.append(
                "WARNING",
                f"Non-option leg skipped — assetType={inst.get('assetType')} "
                f"symbol={inst.get('symbol')}",
            )
            return
        symbol = inst["symbol"]
        instruction = leg["instruction"]
        leader_qty = leg["quantity"]
        follower_qty = self._follower_qty(leader_qty)

        builder = OPTION_INSTRUCTIONS.get(instruction)
        if builder is None:
            self.log.append("WARNING", f"Unknown instruction {instruction} — skipped")
            return

        # Place on every follower account back-to-back first, then verify fills —
        # verifying one account's fill before placing the next would delay
        # execution on the remaining followers.
        placements = [
            (account_hash, self.client.place_order(account_hash, builder(symbol, follower_qty)))
            for account_hash in self.followers
        ]
        for account_hash, resp in placements:
            self._confirm_placement(account_hash, resp, instruction, symbol, leader_qty, follower_qty)

    def _confirm_placement(self, account_hash, resp, instruction, symbol, leader_qty, follower_qty):
        tag = account_hash[:8]
        if resp.status_code not in (200, 201):
            self.log.append(
                "ERROR",
                f"ORDER FAILED [{tag}]  {instruction} {symbol}  "
                f"HTTP {resp.status_code}: {resp.text[:300]}",
            )
            return

        order_id = Utils(self.client, account_hash).extract_order_id(resp)
        status, order_data = self._await_fill(account_hash, order_id)

        if status == Client.Order.Status.FILLED:
            self.log.append(
                "INFO",
                f"REPLICATED [{tag}]  {instruction} {symbol}  "
                f"leader={int(leader_qty)}  follower={follower_qty}  ({self.multiplier}x)",
            )
        elif status in TERMINAL_ORDER_STATUSES:
            self.log.append(
                "ERROR",
                f"ORDER {status.name} [{tag}]  {instruction} {symbol}  orderId={order_id}  "
                f"detail={self._order_detail(order_data)}",
            )
        else:
            self.log.append(
                "WARNING",
                f"ORDER SUBMITTED but not confirmed filled [{tag}]  {instruction} {symbol}  "
                f"orderId={order_id}  status={status}. Check the follower account manually.",
            )

    @staticmethod
    def _order_detail(order_data: dict | None) -> str:
        if not order_data:
            return "no order detail available"
        fields = (
            "statusDescription", "cancelTime", "orderType", "session",
            "duration", "complexOrderStrategyType", "quantity",
            "filledQuantity", "remainingQuantity",
        )
        present = {k: order_data[k] for k in fields if k in order_data}
        return json.dumps(present) if present else "no order detail available"

    def _await_fill(self, account_hash, order_id, attempts=6, delay=1.0):
        # place_order() returning 200/201 only means Schwab accepted the request,
        # not that it filled — this confirms the actual terminal status and
        # returns the raw order data so rejections can be diagnosed.
        if order_id is None:
            self.log.append("WARNING", f"Order accepted but no orderId returned for [{account_hash[:8]}] — cannot verify fill.")
            return None, None
        status = None
        order_data = None
        for _ in range(attempts):
            try:
                resp = self.client.get_order(order_id, account_hash)
                resp.raise_for_status()
                order_data = resp.json()
                status = Client.Order.Status(order_data.get("status"))
                if status in TERMINAL_ORDER_STATUSES:
                    return status, order_data
            except Exception as e:
                self.log.append("WARNING", f"Could not check order {order_id} status for [{account_hash[:8]}]: {e}")
            if self._stop_event.wait(delay):
                break
        return status, order_data
