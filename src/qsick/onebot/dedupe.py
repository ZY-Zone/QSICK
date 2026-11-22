"""Bounded duplicate suppression for inbound events.

QQ re-delivers messages after reconnects and may echo a message once as a receive
event and once as a sent event. NapCat guards its recall reports with a bounded
"already reported" cache; this is the same idea for the whole event stream.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Hashable


class EventDeduper:
    def __init__(self, ttl: float = 120.0, max_size: int = 4096) -> None:
        self.ttl = max(0.0, float(ttl))
        self.max_size = max(16, int(max_size))
        self.duplicates = 0
        self._seen: OrderedDict[Hashable, float] = OrderedDict()
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.ttl > 0

    def is_duplicate(self, key: Hashable) -> bool:
        if not self.enabled:
            return False
        now = time.monotonic()
        with self._lock:
            while self._seen:
                oldest_key, stamp = next(iter(self._seen.items()))
                if now - stamp < self.ttl:
                    break
                self._seen.popitem(last=False)
            stamp = self._seen.get(key)
            if stamp is not None and now - stamp < self.ttl:
                self._seen.move_to_end(key)
                self.duplicates += 1
                return True
            self._seen[key] = now
            self._seen.move_to_end(key)
            while len(self._seen) > self.max_size:
                self._seen.popitem(last=False)
            return False

    def reset(self) -> None:
        with self._lock:
            self._seen.clear()
            self.duplicates = 0
