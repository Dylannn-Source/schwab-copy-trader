import collections
import logging
import threading
from datetime import datetime, timedelta, timezone

from schwab.client import Client
from schwab.orders import options as opt

log = logging.getLogger(__name__)

OPTION_INSTRUCTIONS = {
    "BUY_TO_OPEN": opt.option_buy_to_open_market,
    "SELL_TO_CLOSE": opt.option_sell_to_close_market,
    "BUY_TO_CLOSE": opt.option_buy_to_close_market,
    "SELL_TO_OPEN": opt.option_sell_to_open_market,
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
        self.follower = config["follower_account_hash"]
        self.multiplier = float(config.get("size_multiplier", 1.0))
        self.poll_interval = int(config.get("poll_interval_seconds", 5))
        self.lookback_hours = int(config.get("lookback_hours", 2))
        self.seen_order_ids: set[str] = set()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

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
            for order in self._filled_orders():
                self.seen_order_ids.add(str(order["orderId"]))
            self.log.append("INFO", f"Seeded {len(self.seen_order_ids)} existing fills.")
        except Exception as e:
            self.log.append("ERROR", f"Failed to seed orders: {e}")

        while not self._stop_event.is_set():
            try:
                self._poll()
            except Exception as e:
                self.log.append("ERROR", f"Poll error: {e}")
            self._stop_event.wait(self.poll_interval)

    def _poll(self):
        for order in self._filled_orders():
            oid = str(order["orderId"])
            if oid not in self.seen_order_ids:
                self.seen_order_ids.add(oid)
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
            return
        symbol = inst["symbol"]
        instruction = leg["instruction"]
        leader_qty = leg["quantity"]
        follower_qty = self._follower_qty(leader_qty)

        builder = OPTION_INSTRUCTIONS.get(instruction)
        if builder is None:
            self.log.append("WARNING", f"Unknown instruction {instruction} — skipped")
            return

        resp = self.client.place_order(self.follower, builder(symbol, follower_qty))
        if resp.status_code in (200, 201):
            self.log.append(
                "INFO",
                f"REPLICATED  {instruction} {symbol}  "
                f"leader={int(leader_qty)}  follower={follower_qty}  ({self.multiplier}x)",
            )
        else:
            self.log.append(
                "ERROR",
                f"ORDER FAILED  {instruction} {symbol}  "
                f"HTTP {resp.status_code}: {resp.text[:300]}",
            )
