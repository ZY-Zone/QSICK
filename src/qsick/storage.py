"""Message persistence: memory for tests, SQLite for real deployments.

The SQLite backend is the default: message history is a durable record, not a cache.
WAL keeps concurrent reads cheap while the event loop writes one row per message.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Protocol

from .config import RuntimeConfig
from .protocol.message import Message

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    message_id   INTEGER PRIMARY KEY,
    seq          INTEGER NOT NULL DEFAULT 0,
    time         INTEGER NOT NULL DEFAULT 0,
    chat_type    TEXT    NOT NULL,
    chat_id      INTEGER NOT NULL,
    sender_uin   INTEGER NOT NULL DEFAULT 0,
    sender_uid   TEXT    NOT NULL DEFAULT '',
    sender_name  TEXT    NOT NULL DEFAULT '',
    card         TEXT    NOT NULL DEFAULT '',
    role         TEXT    NOT NULL DEFAULT 'member',
    elements     TEXT    NOT NULL DEFAULT '[]',
    raw          TEXT    NOT NULL DEFAULT '{}',
    stored_at    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_type, chat_id, time);
CREATE INDEX IF NOT EXISTS idx_messages_time ON messages(time);
CREATE INDEX IF NOT EXISTS idx_messages_stored ON messages(stored_at);
"""


class MessageStore(Protocol):
    backend: str

    def put(self, message: Message) -> None: ...
    def get(self, message_id: int) -> Message | None: ...
    def recent(self, limit: int = 50) -> list[Message]: ...
    def search(self, query: str = "", offset: int = 0, limit: int = 50) -> list[Message]: ...
    def count_matching(self, query: str = "") -> int: ...
    def count(self) -> int: ...
    def counts_by_bucket(self, start: int, end: int, bucket_seconds: int) -> dict[int, int]: ...
    def counts_by_bucket_and_account(self, start: int, end: int, bucket_seconds: int
                                     ) -> dict[int, dict[str, int]]: ...
    def prune(self) -> Any: ...
    def set_media_path(self, message_id: int, element_id: str, path: str) -> bool: ...
    def delete(self, message_id: int) -> bool: ...
    def delete_chat(self, chat_type: str, chat_id: int) -> int: ...
    def clear(self) -> int: ...
    def close(self) -> None: ...

    def __len__(self) -> int: ...


class MemoryStore:
    backend = "memory"

    def __init__(self, max_messages: int = 0, max_days: int = 0) -> None:
        self.max_messages = max(0, max_messages)
        self.max_days = max(0, max_days)
        self._messages: OrderedDict[int, Message] = OrderedDict()

    def put(self, message: Message) -> None:
        self._messages[message.message_id] = message
        self._messages.move_to_end(message.message_id)

    def get(self, message_id: int) -> Message | None:
        return self._messages.get(message_id)

    def recent(self, limit: int = 50) -> list[Message]:
        return list(self._messages.values())[-limit:]

    def search(self, query: str = "", offset: int = 0, limit: int = 50) -> list[Message]:
        needle = query.strip().lower()
        rows = [
            message for message in self._messages.values()
            if not needle or needle in (message.text or "").lower() or needle in str(message.chat_id)
        ]
        rows.sort(key=lambda message: (message.time, message.message_id))
        return rows[offset : offset + max(1, limit)]

    def count_matching(self, query: str = "") -> int:
        needle = query.strip().lower()
        if not needle:
            return len(self._messages)
        return sum(
            1 for message in self._messages.values()
            if needle in (message.text or "").lower() or needle in str(message.chat_id)
        )

    def counts_by_bucket(self, start: int, end: int, bucket_seconds: int) -> dict[int, int]:
        """See :meth:`SqliteStore.counts_by_bucket`; the in-memory store answers the same question."""
        width = max(1, int(bucket_seconds))
        found: dict[int, int] = {}
        for message in self._messages.values():
            stamp = int(getattr(message, "time", 0) or 0)
            if not stamp or stamp < start or stamp >= end:
                continue
            slot = int(start) + ((stamp - int(start)) // width) * width
            found[slot] = found.get(slot, 0) + 1
        return found

    def counts_by_bucket_and_account(self, start: int, end: int, bucket_seconds: int
                                     ) -> dict[int, dict[str, int]]:
        """See :meth:`SqliteStore.counts_by_bucket_and_account`."""
        width = max(1, int(bucket_seconds))
        out: dict[int, dict[str, int]] = {}
        for message in self._messages.values():
            stamp = int(getattr(message, "time", 0) or 0)
            if not stamp or stamp < start or stamp >= end:
                continue
            bucket = int(start) + ((stamp - int(start)) // width) * width
            account = str(getattr(message, "account", "") or "") or "unknown"
            slot = out.setdefault(bucket, {})
            slot[account] = slot.get(account, 0) + 1
        return out

    def clear(self) -> int:
        """Drop every message. Returns how many were removed."""
        removed = len(self._messages)
        self._messages.clear()
        return removed

    def count(self) -> int:
        return len(self._messages)

    def set_media_path(self, message_id: int, element_id: str, path: str) -> bool:
        message = self._messages.get(message_id)
        if message is None:
            return False
        for element in message.elements:
            if str(element.data.get("element_id", "")) == str(element_id):
                element.data["local_path"] = path
                return True
        return False

    def delete(self, message_id: int) -> bool:
        return self._messages.pop(message_id, None) is not None

    def delete_chat(self, chat_type: str, chat_id: int) -> int:
        doomed = [key for key, message in self._messages.items()
                  if message.chat_type == chat_type and message.chat_id == chat_id]
        for key in doomed:
            self._messages.pop(key, None)
        return len(doomed)

    def prune(self) -> None:
        if self.max_messages and len(self._messages) > self.max_messages:
            for _ in range(len(self._messages) - self.max_messages):
                self._messages.popitem(last=False)
        if self.max_days:
            cutoff = int(time.time()) - self.max_days * 86400
            for message_id in [mid for mid, msg in self._messages.items() if msg.time and msg.time < cutoff]:
                self._messages.pop(message_id, None)
    def close(self) -> None:
        self._messages.clear()

    def __len__(self) -> int:
        return len(self._messages)


class SqliteStore:
    backend = "sqlite"

    def __init__(self, path: Path, max_messages: int = 0, max_days: int = 0) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_messages = max(0, max_messages)
        self.max_days = max(0, max_days)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.commit()

    def put(self, message: Message) -> None:
        payload = message.to_dict()
        with self._lock:
            self._db.execute(
                """
                INSERT INTO messages (
                    message_id, seq, time, chat_type, chat_id, sender_uin, sender_uid,
                    sender_name, card, role, elements, raw, stored_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(message_id) DO UPDATE SET
                    seq=excluded.seq, time=excluded.time, chat_type=excluded.chat_type,
                    chat_id=excluded.chat_id, sender_uin=excluded.sender_uin,
                    sender_uid=excluded.sender_uid, sender_name=excluded.sender_name,
                    card=excluded.card, role=excluded.role, elements=excluded.elements,
                    raw=excluded.raw, stored_at=excluded.stored_at
                """,
                (
                    _sqlite_message_key(message.message_id),
                    message.seq if -(1 << 63) <= message.seq < (1 << 63) else str(message.seq),
                    message.time,
                    message.chat_type,
                    message.chat_id,
                    message.sender_uin,
                    message.sender_uid,
                    message.sender_name,
                    message.card,
                    message.role,
                    json.dumps(payload["elements"], ensure_ascii=False),
                    json.dumps(payload, ensure_ascii=False),
                    int(time.time()),
                ),
            )
            self._db.commit()

    def get(self, message_id: int) -> Message | None:
        with self._lock:
            row = self._db.execute("SELECT raw FROM messages WHERE message_id = ?", (_sqlite_message_key(message_id),)).fetchone()
        return _message_from_raw(row["raw"]) if row else None

    def recent(self, limit: int = 50) -> list[Message]:
        with self._lock:
            rows = self._db.execute(
                "SELECT raw FROM messages ORDER BY stored_at DESC, message_id DESC LIMIT ?", (max(1, limit),)
            ).fetchall()
        return [message for message in (_message_from_raw(row["raw"]) for row in reversed(rows)) if message]

    def page_after(self, chat_type: str, chat_id: Any, *, after_id: int, limit: int = 80,
                   account: str = "") -> list[Message]:
        """取比 ``after_id`` 更新的消息（时间正序），供聊天栏增量轮询。"""
        params: list[Any] = [chat_type, int(chat_id), _sqlite_message_key(int(after_id))]
        clauses = " AND json_extract(raw, '$.account') = ?" if account else ""
        if account:
            params.append(str(account))
        sql = ("SELECT raw FROM messages WHERE chat_type = ? AND chat_id = ? AND message_id > ?"
               + clauses + " ORDER BY time ASC, message_id ASC LIMIT ?")
        params.append(max(1, limit))
        with self._lock:
            rows = self._db.execute(sql, tuple(params)).fetchall()
        return [message for message in (_message_from_raw(row["raw"]) for row in rows) if message]

    def page(self, chat_type: str, chat_id: Any, *, limit: int = 80,
             before: Any = None, account: str = "") -> list[Message]:
        """按会话取一页历史（时间正序），走 ``(chat_type, chat_id, time)`` 索引。

        ``before`` 是排他游标（message_id）。没有它时取最新的一页；有它时取它
        之前的一页。``account`` 非空时只取该账号的消息——升级前的主库里混着
        多个账号，不在 SQL 里过滤就只能靠"多取几页再筛"，窗口会被别人的消息
        占满。
        """
        params: list[Any] = [chat_type, int(chat_id)]
        clauses = ""
        if account:
            # account 不在索引列里（它在 raw JSON 内），用 json_extract 过滤。
            # 这仍然比"取一页再在 Python 里筛"可靠：窗口不会被别的账号占满。
            clauses += " AND json_extract(raw, '$.account') = ?"
            params.append(str(account))
        if before is not None:
            key = _sqlite_message_key(int(before))
            # 用 (time, message_id) 一起比较：同一秒内的多条消息只比时间会漏行。
            clauses += (" AND (time < (SELECT time FROM messages WHERE message_id = ?)"
                        " OR (time = (SELECT time FROM messages WHERE message_id = ?)"
                        " AND message_id < ?))")
            params.extend([key, key, key])
        sql = ("SELECT raw FROM messages WHERE chat_type = ? AND chat_id = ?" + clauses
               + " ORDER BY time DESC, message_id DESC LIMIT ?")
        params.append(max(1, limit))
        with self._lock:
            rows = self._db.execute(sql, tuple(params)).fetchall()
        return [message for message in (_message_from_raw(row["raw"]) for row in reversed(rows)) if message]

    def search(self, query: str = "", offset: int = 0, limit: int = 50) -> list[Message]:
        needle = f"%{query.strip()}%"
        with self._lock:
            rows = self._db.execute(
                """
                SELECT raw FROM messages
                WHERE (? = '%%') OR raw LIKE ? OR CAST(chat_id AS TEXT) LIKE ?
                ORDER BY stored_at DESC, message_id DESC LIMIT ? OFFSET ?
                """,
                (needle, needle, needle, max(1, limit), max(0, offset)),
            ).fetchall()
        return [message for message in (_message_from_raw(row["raw"]) for row in rows) if message]

    def count_matching(self, query: str = "") -> int:
        needle = f"%{query.strip()}%"
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) AS total FROM messages WHERE (? = '%%') OR raw LIKE ? OR CAST(chat_id AS TEXT) LIKE ?",
                (needle, needle, needle),
            ).fetchone()
        return int(row["total"]) if row else 0

    def count(self) -> int:
        with self._lock:
            row = self._db.execute("SELECT COUNT(*) AS total FROM messages").fetchone()
        return int(row["total"]) if row else 0

    def counts_by_bucket(self, start: int, end: int, bucket_seconds: int) -> dict[int, int]:
        """Message counts per `bucket_seconds` window between `start` and `end`, keyed by bucket start.

        Buckets are aligned to `start` so they do not shift between renders. Rows with no timestamp (`time = 0`)
        are left out: they cannot be placed on a time axis, and inventing a position for them would draw a spike
        that never happened. The grouping is integer arithmetic rather than a date function so it behaves the same
        whatever SQLite build is bundled.
        """
        width = max(1, int(bucket_seconds))
        with self._lock:
            rows = self._db.execute(
                """
                SELECT CAST((time - ?) / ? AS INTEGER) AS slot, COUNT(*) AS total
                FROM messages
                WHERE time >= ? AND time < ?
                GROUP BY slot
                """,
                (int(start), width, int(start), int(end)),
            ).fetchall()
        return {int(start) + int(row["slot"]) * width: int(row["total"]) for row in rows}

    def counts_by_bucket_and_account(self, start: int, end: int, bucket_seconds: int
                                     ) -> dict[int, dict[str, int]]:
        """Same buckets as :meth:`counts_by_bucket`, split one level further by account.

        The account lives in the raw payload's `account` field -- that is where the transport puts the uin the
        message belongs to, and there is no column for it. `json_extract` is used rather than a LIKE: matching JSON
        with a substring would also match an account number that merely appeared inside a message body.
        """
        width = max(1, int(bucket_seconds))
        with self._lock:
            rows = self._db.execute(
                """
                SELECT CAST((time - ?) / ? AS INTEGER) AS slot,
                       COALESCE(json_extract(raw, '$.account'), '') AS account,
                       COUNT(*) AS total
                FROM messages
                WHERE time >= ? AND time < ?
                GROUP BY slot, account
                """,
                (int(start), width, int(start), int(end)),
            ).fetchall()
        out: dict[int, dict[str, int]] = {}
        for row in rows:
            bucket = int(start) + int(row["slot"]) * width
            account = str(row["account"] or "") or "unknown"
            out.setdefault(bucket, {})[account] = int(row["total"])
        return out

    def _prune_locked(self) -> int:
        """Drop whatever the retention limits say is over, and report how many rows went.

        Called once at startup rather than after every insert. Pruning inside `put` made the limit a hard ceiling
        that quietly ate history as it was written; as a startup pass it means "the cache is trimmed when QSICK
        starts", and a session that runs for a week keeps what it recorded until the next restart.
        """
        removed = 0
        if self.max_messages:
            cursor = self._db.execute(
                """
                DELETE FROM messages WHERE message_id IN (
                    SELECT message_id FROM messages ORDER BY stored_at ASC, message_id ASC
                    LIMIT (SELECT MAX(COUNT(*) - ?, 0) FROM messages)
                )
                """,
                (self.max_messages,),
            )
            removed += max(0, cursor.rowcount or 0)
        if self.max_days:
            cutoff = int(time.time()) - self.max_days * 86400
            cursor = self._db.execute("DELETE FROM messages WHERE time > 0 AND time < ?", (cutoff,))
            removed += max(0, cursor.rowcount or 0)
        self._db.commit()
        return removed

    def prune(self) -> int:
        with self._lock:
            return self._prune_locked()

    def set_media_path(self, message_id: int, element_id: str, path: str) -> bool:
        with self._lock:
            row = self._db.execute("SELECT raw FROM messages WHERE message_id = ?", (_sqlite_message_key(message_id),)).fetchone()
            if row is None:
                return False
            try:
                payload = json.loads(row["raw"])
            except (TypeError, ValueError):
                return False
            changed = False
            for element in payload.get("elements") or []:
                if str((element.get("data") or {}).get("element_id", "")) == str(element_id):
                    element["data"]["local_path"] = path
                    changed = True
            if not changed:
                return False
            self._db.execute("UPDATE messages SET raw = ? WHERE message_id = ?",
                             (json.dumps(payload, ensure_ascii=False), _sqlite_message_key(message_id)))
            self._db.commit()
            return True

    def delete(self, message_id: int) -> bool:
        with self._lock:
            cursor = self._db.execute("DELETE FROM messages WHERE message_id = ?", (_sqlite_message_key(message_id),))
            self._db.commit()
            return cursor.rowcount > 0

    def delete_chat(self, chat_type: str, chat_id: int) -> int:
        with self._lock:
            cursor = self._db.execute(
                "DELETE FROM messages WHERE chat_type = ? AND chat_id = ?", (chat_type, int(chat_id))
            )
            self._db.commit()
            return cursor.rowcount

    def clear(self) -> int:
        """Drop every message and give the space back.

        VACUUM is what actually shrinks the file -- a DELETE alone leaves the pages allocated, so the cache would
        still report its old size, which reads as the clear having failed. It needs an exclusive lock, so a failure
        is tolerated: the rows are gone either way and the next clear will reclaim the file.
        """
        with self._lock:
            cursor = self._db.execute("DELETE FROM messages")
            self._db.commit()
            removed = cursor.rowcount
            try:
                self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                self._db.execute("VACUUM")
                self._db.commit()
            except Exception:  # noqa: BLE001
                pass
            return removed

    def close(self) -> None:
        with self._lock:
            try:
                self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            self._db.close()

    def __len__(self) -> int:
        return self.count()


def _sqlite_message_key(value: int) -> int:
    """Bijective uint64-to-signed key; public ID stays exact in the JSON envelope."""
    value = int(value)
    if value < 0 or value >= 1 << 64:
        raise ValueError("message ID is outside uint64 range")
    return value if value < 1 << 63 else value - (1 << 64)


def _message_from_raw(raw: str) -> Message | None:
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return Message.from_payload(payload)


def create_store(config: RuntimeConfig) -> MessageStore:
    limits = (config.rotation.max_messages, config.rotation.max_days)
    if config.storage.backend.lower() == "memory":
        return MemoryStore(*limits)
    path = Path(config.storage.path).expanduser() if config.storage.path else config.path("messages.db")
    return SqliteStore(path, *limits)


def store_info(store: MessageStore) -> dict[str, Any]:
    info: dict[str, Any] = {"backend": store.backend, "messages": len(store)}
    path = getattr(store, "path", None)
    if path is not None:
        info["path"] = str(path)
    return info
