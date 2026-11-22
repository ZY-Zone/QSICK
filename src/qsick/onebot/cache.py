"""Small TTL cache for read-only payload queries.

NapCat keeps group/friend caches so a status bot polling `get_group_list` does not
hit QQ every time. This is the same idea, deliberately narrow: only queries that
are expensive and tolerant of a few seconds of staleness opt in.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any


class TTLCache:
    def __init__(self, max_size: int = 256) -> None:
        self.max_size = max(8, int(max_size))
        self.hits = 0
        self.misses = 0
        self._values: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> Any | None:
        now = time.monotonic()
        with self._lock:
            entry = self._values.get(key)
            if entry is None:
                self.misses += 1
                return None
            expires, value = entry
            if expires <= now:
                self._values.pop(key, None)
                self.misses += 1
                return None
            self._values.move_to_end(key)
            self.hits += 1
            return value

    def set(self, key: str, value: Any, ttl: float) -> None:
        if ttl <= 0:
            return
        with self._lock:
            self._values[key] = (time.monotonic() + ttl, value)
            self._values.move_to_end(key)
            while len(self._values) > self.max_size:
                self._values.popitem(last=False)

    def invalidate(self, prefix: str = "") -> None:
        with self._lock:
            if not prefix:
                self._values.clear()
                return
            for key in [key for key in self._values if key.startswith(prefix)]:
                self._values.pop(key, None)

    def stats(self) -> dict[str, int]:
        return {"entries": len(self._values), "hits": self.hits, "misses": self.misses}
