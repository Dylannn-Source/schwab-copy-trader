import collections
import logging
import threading
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)
PACIFIC = ZoneInfo("America/Los_Angeles")


class ActivityLog:
    def __init__(self, maxlen=500):
        self._entries = collections.deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def append(self, level: str, message: str):
        entry = {
            "time": datetime.now(timezone.utc).astimezone(PACIFIC).strftime("%H:%M:%S"),
            "level": level,
            "message": message,
        }
        with self._lock:
            self._entries.append(entry)
        log.log(getattr(logging, level, logging.INFO), message)

    def entries(self) -> list[dict]:
        with self._lock:
            return list(self._entries)
