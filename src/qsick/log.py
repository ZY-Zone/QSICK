"""Logging with a TRACE level, category names and an in-memory ring buffer.

The WebUI tails the buffer over SSE; plugins and transport threads share the same
sink, so a payload disconnect and a blocked message show up in one timeline.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

TRACE = 5
DEBUG = 10
INFO = 20
WARN = 30
ERROR = 40

LEVEL_NAMES = {TRACE: "TRACE", DEBUG: "DEBUG", INFO: "INFO", WARN: "WARN", ERROR: "ERROR"}
NAME_LEVELS = {name: level for level, name in LEVEL_NAMES.items()}

_LOCK = threading.RLock()

#: Every category this build can emit, so the WebUI's category filter can offer the whole list up front.
#:
#: The filter used to be filled only from records it had already seen. That reads as "the list is always correct",
#: but it means the categories that matter exactly when they are empty were the ones you could not select -- the
#: plugin loader before any plugin logs, the WebSocket category before the first client connects. A filter nobody
#: can pick is not a filter.
KNOWN_CATEGORIES: dict[str, dict[str, str]] = {}


def register_category(category: str, *, source: str = "core", description: str = "") -> None:
    """Declare a category so the filter offers it even before anything writes to it."""
    name = str(category or "").strip()
    if not name:
        return
    with _LOCK:
        KNOWN_CATEGORIES[name] = {"category": name, "source": source, "description": description}


def known_categories() -> list[dict[str, str]]:
    """Declared categories: core first, then plugin-declared ones, each group alphabetical."""
    with _LOCK:
        items = [dict(value) for value in KNOWN_CATEGORIES.values()]
    rank = {"core": 0, "plugin": 1}
    return sorted(items, key=lambda item: (rank.get(item.get("source", ""), 2), item["category"]))


def unregister_category(category: str) -> None:
    """Drop a declared category -- used when the plugin that declared it is unloaded.

    Core categories are not removable: they are part of the build, and a filter that loses entries depending on
    runtime state is worse than one that offers a slightly long list.
    """
    name = str(category or "").strip()
    with _LOCK:
        entry = KNOWN_CATEGORIES.get(name)
        if entry and entry.get("source") == "core":
            return
        KNOWN_CATEGORIES.pop(name, None)


for _name, _description in (
    ("Runtime", "运行时的启停与实例状态"),
    ("Plugins", "插件的加载、卸载与热重载"),
    ("OneBot.WS", "OneBot WebSocket 连接：接入、断开、拒绝"),
    ("OneBot.HTTP", "OneBot HTTP 端点：监听、鉴权拒绝、动作错误"),
    ("OneBot.Reverse", "反向 WebSocket 客户端"),
    ("OneBot.Events", "OneBot 事件映射与分发"),
    ("OneBot.Actions", "动作注册与执行"),
    ("MessageCache", "消息缓存写入"),
    ("Transport.Pipe", "pipe 载荷与 DLL 通道"),
    ("Transport.Headless", "无头实例进程"),
    ("SessionLink", "会话与载荷的连接状态"),
    ("Protocol.Media", "媒体地址与 rkey"),
    ("Protocol.RKey", "rkey 提供方"),
    ("WebUI", "WebUI 服务"),
    ("Market", "插件市场"),
):
    register_category(_name, description=_description)


@dataclass(slots=True)
class Record:
    seq: int
    ts: float
    level: int
    category: str
    message: str
    fields: dict[str, Any] = field(default_factory=dict)

    @property
    def level_name(self) -> str:
        return LEVEL_NAMES.get(self.level, str(self.level))

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "time": time.strftime("%H:%M:%S", time.localtime(self.ts)) + f".{int(self.ts % 1 * 1000):03d}",
            "level": self.level_name,
            "category": self.category,
            "message": self.message,
            "fields": self.fields,
        }


class LogBuffer:
    def __init__(self, capacity: int = 2000) -> None:
        self._records: deque[Record] = deque(maxlen=max(64, capacity))
        self._seq = 0
        self._listeners: list[Callable[[Record], None]] = []

    def append(self, record: Record) -> None:
        with _LOCK:
            self._records.append(record)
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(record)
            except Exception:  # a bad listener must not break logging
                pass

    def next_seq(self) -> int:
        with _LOCK:
            self._seq += 1
            return self._seq

    def since(self, cursor: int) -> list[Record]:
        with _LOCK:
            return [r for r in self._records if r.seq > cursor]

    def clear(self) -> None:
        with _LOCK:
            self._records.clear()

    def tail(self, limit: int = 200) -> list[Record]:
        with _LOCK:
            return list(self._records)[-limit:]

    def subscribe(self, listener: Callable[[Record], None]) -> Callable[[], None]:
        with _LOCK:
            self._listeners.append(listener)

        def unsubscribe() -> None:
            with _LOCK:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return unsubscribe


BUFFER = LogBuffer()


class Logger:
    __slots__ = ("category", "context")

    def __init__(self, category: str) -> None:
        self.category = category
        self.context: dict[str, Any] = {}

    def bind(self, **fields: Any) -> "Logger":
        child = Logger(self.category)
        child.context = {**self.context, **fields}
        return child

    def _emit(self, level: int, message: str, fields: dict[str, Any]) -> None:
        if level < _MIN_LEVEL[0]:
            return
        record = Record(
            seq=BUFFER.next_seq(),
            ts=time.time(),
            level=level,
            category=self.category,
            message=message,
            fields={**self.context, **fields},
        )
        BUFFER.append(record)

        if _PRINT_TO_STDERR[0] and level >= max(_MIN_LEVEL[0], INFO):
            line = f"[{record.to_dict()['time']}] {record.level_name:<5} {self.category:<12} {message}"
            if record.fields:
                line += " " + " ".join(f"{k}={v!r}" for k, v in record.fields.items())
            print(line, file=sys.stderr, flush=True)

    def trace(self, message: str, **fields: Any) -> None:
        self._emit(TRACE, message, fields)

    def debug(self, message: str, **fields: Any) -> None:
        self._emit(DEBUG, message, fields)

    def info(self, message: str, **fields: Any) -> None:
        self._emit(INFO, message, fields)

    def warn(self, message: str, **fields: Any) -> None:
        self._emit(WARN, message, fields)

    def error(self, message: str, **fields: Any) -> None:
        self._emit(ERROR, message, fields)

    # ``warning`` is what most people type; keep both.
    warning = warn


_MIN_LEVEL = [INFO]
_PRINT_TO_STDERR = [True]
_FILE: Path | None = None
_FILE_JSON = [False]


def current_level() -> str:
    return LEVEL_NAMES.get(_MIN_LEVEL[0], "INFO")


def configure(
    level: str | int = "INFO",
    *,
    capacity: int = 2000,
    file: str | Path | None = None,
    json_lines: bool = False,
    echo: bool = True,
) -> None:
    global _FILE
    level_no = NAME_LEVELS.get(str(level).upper(), INFO) if isinstance(level, str) else int(level)
    with _LOCK:
        _MIN_LEVEL[0] = level_no
        _PRINT_TO_STDERR[0] = echo
        BUFFER._records = deque(BUFFER._records, maxlen=max(64, capacity))
        _FILE = Path(file) if file else None
        _FILE_JSON[0] = json_lines
    if _FILE:
        _FILE.parent.mkdir(parents=True, exist_ok=True)


def _write_file(record: Record) -> None:
    if _FILE is None:
        return
    try:
        with _FILE.open("a", encoding="utf-8") as fh:
            if _FILE_JSON[0]:
                fh.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")
            else:
                fields = " ".join(f"{k}={v!r}" for k, v in record.fields.items())
                fh.write(
                    f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(record.ts))} "
                    f"{record.level_name:<5} {record.category:<12} {record.message} {fields}\n"
                )
    except OSError:
        pass


BUFFER.subscribe(_write_file)

_LOG_LEVEL = _MIN_LEVEL  # re-exported for tests


def get_logger(category: str) -> Logger:
    return Logger(category)
