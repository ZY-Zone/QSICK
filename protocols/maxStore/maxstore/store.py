"""maxStore 的 SQLite 实现。

设计取向和 ``qsick.storage`` 不同：那边存的是"够用的消息历史"，这里存的是"证据"。
所以每一条收到的 payload 都按两个粒度落盘——一个整包 JSON（``raw_json``）加一份按
JSON 指针摊平的字段表（``raw_frames``）。冗余是刻意的：整包保证能原样还回去，摊平表
保证将来字段改名、嵌套加深、平台偷偷加字段之后，旧数据仍然能被按路径取出来。

写路径上一次事务写完两边，读路径上默认只碰 ``messages``；只有 ``raw_field`` 才走
``raw_frames``，所以日常查询不会因为"为了保留一切"而变慢。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from . import paths as jsonpath
from .schema import SCHEMA, SCHEMA_DROP_FRAMES, SCHEMA_VERSION

__all__ = [
    "RawStore",
    "SqliteMaxStore",
    "MemoryMaxStore",
    "open_store",
    "capture",
    "SCHEMA_VERSION",
]

UINT64 = 1 << 64
INT64_HALF = 1 << 63


class RawStore(Protocol):
    """存储协议接口。

    ``append`` 的入参刻意宽松（原始 dict 或任何带 ``to_dict`` 的消息对象），因为
    "收到的原始信息"不该要求调用方先翻译成我们的类型——翻译本身就是一次字段裁剪。
    """

    backend: str

    def append(self, message: Mapping[str, Any] | Any) -> None: ...
    def get(self, message_id: Any) -> dict[str, Any] | None: ...
    def query(
        self,
        chat_type: str | None,
        chat_id: Any,
        limit: int = 50,
        before: Any = None,
    ) -> list[dict[str, Any]]: ...
    def raw_field(self, message_id: Any, json_path: Any) -> Any: ...
    def raw_fields(self, message_id: Any) -> list[str]: ...
    def delete(self, message_id: Any) -> bool: ...
    def count(self, chat_type: str | None = None, chat_id: Any = None) -> int: ...
    def close(self) -> None: ...

    def __len__(self) -> int: ...


# --------------------------------------------------------------------- capture


def capture(
    message: Mapping[str, Any] | Any,
    *,
    protocol: str = "",
    received_at: float | None = None,
) -> dict[str, Any]:
    """把任意输入收敛成一个"待落盘记录"。

    这里只做一次键名对齐，不做字段过滤：所有没被识别的键都留在 ``raw`` 里，而
    ``raw`` 就是入参原样（若入参是消息对象，则取其原始 payload，缺失时退回
    ``to_dict()``）。因此"看不懂"永远不等于"丢掉"。
    """
    payload = _coerce_payload(message)
    anomalies: list[str] = []

    elements_in = payload.get("elements")
    if elements_in is None:
        elements_in = payload.get("message")
        if isinstance(elements_in, list) and elements_in and isinstance(elements_in[0], Mapping) \
                and "type" in elements_in[0]:
            anomalies.append("elements_taken_from_message_key")
        else:
            elements_in = None
    if elements_in is not None and not isinstance(elements_in, Sequence):
        anomalies.append("elements_not_a_sequence")
        elements_in = None
    if isinstance(elements_in, str):
        # 少数事件把 CQ 码字符串直接放在这里；不解析，只留档。
        anomalies.append("elements_is_string")
        elements_in = None

    chat = payload.get("chat") if isinstance(payload.get("chat"), Mapping) else {}
    sender = payload.get("sender") if isinstance(payload.get("sender"), Mapping) else {}

    chat_type = _text(
        chat.get("type")
        or payload.get("chat_type")
        or payload.get("message_type")
        or payload.get("group_type")
    )
    chat_id = _first_int(
        chat.get("id"), chat.get("chat_id"),
        payload.get("group_id"), payload.get("chat_id"), payload.get("discuss_id"),
        payload.get("target_id"),
    )
    sender_uin = _first_int(
        sender.get("uin"), sender.get("user_id"), payload.get("user_id"),
        payload.get("sender_uin"), payload.get("from_uin"),
    )
    message_id = _first_int(payload.get("message_id"), payload.get("msg_id"), payload.get("id"))
    seq = _first_int(payload.get("seq"), payload.get("message_seq"), payload.get("real_seq"))
    event_time = _first_int(payload.get("time"), payload.get("timestamp"), payload.get("event_time"))

    if message_id == 0:
        anomalies.append("missing_message_id")
    if not chat_type:
        anomalies.append("missing_chat_type")

    normalized = [_normalize_element(item) for item in (elements_in or [])]
    unknown_types = sorted({
        str(element["data"].get("original_type") or "unknown")
        for element in normalized
        if element["type"] == "unknown"
    })

    meta = {
        "schema_version": SCHEMA_VERSION,
        "protocol": protocol or _text(payload.get("protocol") or payload.get("post_type")),
        "received_at": float(received_at if received_at is not None else time.time()),
        "element_count": len(normalized),
        "unknown_element_types": unknown_types,
        "top_level_keys": sorted(str(key) for key in payload.keys()),
        "anomalies": anomalies,
    }
    return {
        "message_id": message_id,
        "seq": seq,
        "time": event_time,
        "chat_type": chat_type,
        "chat_id": chat_id,
        "sender_uin": sender_uin,
        "sender_uid": _text(sender.get("uid") or payload.get("sender_uid") or ""),
        "sender_name": _text(sender.get("nickname") or sender.get("name") or sender.get("card") or ""),
        "card": _text(sender.get("card") or ""),
        "role": _text(sender.get("role") or ""),
        "account": _text(payload.get("account") or payload.get("self_id") or ""),
        "elements": normalized,
        "raw": payload,
        "meta": meta,
    }


def _coerce_payload(message: Any) -> dict[str, Any]:
    if isinstance(message, Mapping):
        return dict(message)
    if message is None:
        raise TypeError("maxStore.append() 不接受 None：没有载荷可存")
    raw = getattr(message, "raw", None)
    if isinstance(raw, Mapping) and raw:
        return dict(raw)
    to_dict = getattr(message, "to_dict", None)
    if callable(to_dict):
        dumped = to_dict()
        if isinstance(dumped, Mapping):
            # 消息对象没留原始 payload 时，``to_dict()`` 就是能拿到的最原始形态。
            return dict(dumped)
    raise TypeError(f"maxStore.append() 需要 mapping 或带 to_dict() 的消息对象，收到 {type(message).__name__}")


def _normalize_element(item: Any) -> dict[str, Any]:
    """与 ``qsick.protocol.elements.normalize`` 同构的最小实现。

    不直接 import 是为了让存储协议能独立加载（协议目录不该依赖核心包的内部布局），
    但产出的结构保持一致，两边数据可以互相喂。
    """
    if not isinstance(item, Mapping):
        return {"type": "unknown", "data": {"original_type": "unknown", "value": repr(item)},
                "raw": item if isinstance(item, (str, int, float, bool)) else None}
    etype = _text(item.get("type")) or "unknown"
    data = item.get("data") if isinstance(item.get("data"), Mapping) else {}
    raw = item.get("raw") if isinstance(item.get("raw"), Mapping) else None
    if etype in _KNOWN_ELEMENT_TYPES or etype == "unknown":
        out: dict[str, Any] = {"type": etype, "data": dict(data)}
        if raw is not None:
            out["raw"] = raw
        return out
    # 未识别类型：保留原类型名 + 原始 JSON，而不是塞进一个 text 元素里假装看得懂。
    out = {"type": "unknown", "data": {"original_type": etype, "data": dict(data)}, "raw": dict(item)}
    return out


_KNOWN_ELEMENT_TYPES = {
    "text", "at", "face", "reply", "image", "record", "video", "file", "json", "xml",
    "forward", "mface", "poke", "markdown", "dice", "rps", "contact", "location",
    "music", "miniapp", "flash_file", "onlinefile", "inline_keyboard", "share",
    "node", "shake", "anonymous",
}


# ----------------------------------------------------------------------- store


class SqliteMaxStore:
    backend = "sqlite"

    def __init__(self, path: Path | str, *, protocol: str = "") -> None:
        self.path = Path(path)
        self.protocol = protocol
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._closed = False
        # isolation_level=None：自己用 BEGIN IMMEDIATE 管事务。默认的隐式事务会在
        # 主行与摊平行之间留下半提交状态，那正是"索引说有、原文查不到"的来源。
        self._db = sqlite3.connect(
            str(self.path), check_same_thread=False, timeout=30.0, isolation_level=None
        )
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(SCHEMA)
            # WAL 让"边写边查"不必互相等；NORMAL 在 WAL 下已经不会丢已提交事务。
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.execute("PRAGMA busy_timeout=30000")
            self._db.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            self._db.commit()

    # -- write ------------------------------------------------------------

    def append(self, message: Mapping[str, Any] | Any) -> None:
        record = capture(message, protocol=self.protocol)
        self.append_record(record)

    def append_record(self, record: Mapping[str, Any]) -> None:
        """写入一条已通过 :func:`capture` 的记录（批量导入时可省去重复解析）。"""
        raw = record.get("raw")
        if not isinstance(raw, Mapping):
            raw = {}
        # 先截断超深/带环的子树再序列化：json.dumps 是递归实现，直接喂深度炸弹会抛
        # RecursionError，那样这条消息连留档的机会都没有。
        raw_json = jsonpath.dump(jsonpath.sanitize(raw))
        meta = dict(record.get("meta") or {})
        meta["schema_version"] = SCHEMA_VERSION
        message_key = _record_key(record, raw_json)
        frames = jsonpath.flatten(raw)
        now = int(time.time())
        row = (
            message_key,
            _int_key(record.get("message_id")),
            int(record.get("seq") or 0),
            int(record.get("time") or 0),
            _text(record.get("chat_type")),
            _nullable_int(record.get("chat_id")),
            _text(record.get("chat_id")),
            _nullable_int(record.get("sender_uin")),
            _text(record.get("sender_uid")),
            _text(record.get("sender_name")),
            _text(record.get("card")),
            _text(record.get("role")),
            _text(record.get("account")),
            _text(meta.get("protocol")),
            jsonpath.dump(jsonpath.sanitize(record.get("elements") or [])),
            raw_json,
            jsonpath.dump(meta),
            len(record.get("elements") or []),
            len(frames),
            now,
        )
        with self._lock:
            self._require_open()
            try:
                # 一条消息的主行和它的摊平行必须同生共死：中途失败会留下
                # "索引说有、原文查不到"的谎话，所以显式开事务。
                self._db.execute("BEGIN IMMEDIATE")
                self._db.execute(
                    """
                    INSERT INTO messages (
                        message_key, message_id, seq, time, chat_type, chat_id, chat_id_text,
                        sender_uin, sender_uid, sender_name, card, role, account, protocol,
                        elements_json, raw_json, meta_json, element_count, frame_count, stored_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(message_key) DO UPDATE SET
                        message_id=excluded.message_id, seq=excluded.seq, time=excluded.time,
                        chat_type=excluded.chat_type, chat_id=excluded.chat_id,
                        chat_id_text=excluded.chat_id_text, sender_uin=excluded.sender_uin,
                        sender_uid=excluded.sender_uid, sender_name=excluded.sender_name,
                        card=excluded.card, role=excluded.role, account=excluded.account,
                        protocol=excluded.protocol, elements_json=excluded.elements_json,
                        raw_json=excluded.raw_json, meta_json=excluded.meta_json,
                        element_count=excluded.element_count, frame_count=excluded.frame_count,
                        stored_at=excluded.stored_at
                    """,
                    row,
                )
                # 覆盖写时旧摊平行必须清掉，否则已删除的字段还能被 raw_field 查出来。
                self._db.execute(SCHEMA_DROP_FRAMES, (message_key,))
                self._db.executemany(
                    "INSERT INTO raw_frames (message_key, path, value_type, value_json, depth)"
                    " VALUES (?, ?, ?, ?, ?)",
                    [
                        (message_key, path, kind, text, jsonpath.depth_of(path))
                        for path, kind, text in frames
                    ],
                )
                self._db.commit()
            except sqlite3.Error:
                self._db.rollback()
                raise

    def delete(self, message_id: Any) -> bool:
        key = _id_key(message_id)
        with self._lock:
            self._require_open()
            try:
                # 主行和摊平行一起删；留在两个事务里会留下查得到路径却取不到消息的孤儿。
                self._db.execute("BEGIN IMMEDIATE")
                cursor = self._db.execute("DELETE FROM messages WHERE message_key = ?", (key,))
                self._db.execute(SCHEMA_DROP_FRAMES, (key,))
                self._db.commit()
            except sqlite3.Error:
                self._db.rollback()
                raise
            return cursor.rowcount > 0

    # -- read -------------------------------------------------------------

    def get(self, message_id: Any) -> dict[str, Any] | None:
        with self._lock:
            self._require_open()
            row = self._db.execute(
                "SELECT * FROM messages WHERE message_key = ?", (_id_key(message_id),)
            ).fetchone()
        return _row_to_dict(row) if row is not None else None

    def query(
        self,
        chat_type: str | None = None,
        chat_id: Any = None,
        limit: int = 50,
        before: Any = None,
    ) -> list[dict[str, Any]]:
        """按会话取历史，时间正序返回（和聊天窗口的读法一致）。

        ``before`` 是排他游标，接受 message_id / message_key；找不到游标时返回空列表，
        因为"往后翻"和"从头再来"是两种结果，悄悄返回最新一页会误导调用方。
        """
        limit = max(1, int(limit or 1))
        clauses: list[str] = []
        params: list[Any] = []
        if chat_type:
            clauses.append("chat_type = ?")
            params.append(_text(chat_type))
        if chat_id is not None and _text(chat_id) != "":
            clauses.append("(chat_id_text = ? OR chat_id = ?)")
            params.extend([_text(chat_id), _nullable_int(chat_id)])
        with self._lock:
            self._require_open()
            if before is not None:
                cursor = self._cursor_row(before)
                if cursor is None:
                    return []
                # 用 (time, message_key) 做游标而不是单列 time：同一秒内的多条消息只比
                # 时间会漏行或重行。这里依赖的是"游标比较式与 ORDER BY 完全一致"，而不是
                # message_key 的字典序等于数值序——前者保证翻页不丢不重，后者做不到。
                clauses.append("(time < ? OR (time = ? AND message_key < ?))")
                params.extend([cursor["time"], cursor["time"], cursor["message_key"]])
            where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
            rows = self._db.execute(
                f"SELECT * FROM messages{where} ORDER BY time DESC, message_key DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
        return [_row_to_dict(row) for row in reversed(rows)]

    def raw_field(self, message_id: Any, json_path: Any) -> Any:
        """按路径取原始字段，路径可以指向叶子，也可以指向任意子树。"""
        key = _id_key(message_id)
        tokens = jsonpath.parse_path(json_path)
        pointer = jsonpath.to_pointer(tokens)
        with self._lock:
            self._require_open()
            if not pointer:
                # 空路径等价于要整包：走原文比逐行重建更准（能保留键序）。
                stored = self._db.execute(
                    "SELECT raw_json FROM messages WHERE message_key = ?", (key,)
                ).fetchone()
                return json.loads(stored["raw_json"]) if stored is not None else None
            row = self._db.execute(
                "SELECT value_type, value_json FROM raw_frames WHERE message_key = ? AND path = ?",
                (key, pointer),
            ).fetchone()
            if row is not None and row["value_type"] not in jsonpath.CONTAINER_TYPES:
                return jsonpath.decode_value(row["value_type"], row["value_json"])
            # 容器行的文本列是 NULL，真正的值在子行里，必须往下取；只看当天那行会把
            # /sender 这类子树查成 {}，那是静默给出错答案。
            prefix = pointer + "/"
            rows = self._db.execute(
                "SELECT path, value_type, value_json FROM raw_frames"
                " WHERE message_key = ? AND substr(path, 1, ?) = ?",
                (key, len(prefix), prefix),
            ).fetchall()
        if rows:
            return jsonpath.rebuild(
                (child["path"][len(prefix):], child["value_type"], child["value_json"]) for child in rows
            )
        # 行存在但没有任何子行：空容器，按它自己的类型还原。
        return jsonpath.decode_value(row["value_type"], row["value_json"]) if row is not None else None

    def raw_fields(self, message_id: Any) -> list[str]:
        """列出这条消息所有可查的原始路径，便于排查"到底存到了哪一层"。"""
        with self._lock:
            self._require_open()
            rows = self._db.execute(
                "SELECT path FROM raw_frames WHERE message_key = ? ORDER BY path",
                (_id_key(message_id),),
            ).fetchall()
        return [row["path"] for row in rows]

    def iter_frames(self, message_id: Any) -> Iterator[tuple[str, str, Any]]:
        with self._lock:
            self._require_open()
            rows = self._db.execute(
                "SELECT path, value_type, value_json FROM raw_frames"
                " WHERE message_key = ? ORDER BY path",
                (_id_key(message_id),),
            ).fetchall()
        for row in rows:
            yield row["path"], row["value_type"], jsonpath.decode_value(row["value_type"], row["value_json"])

    def count(self, chat_type: str | None = None, chat_id: Any = None) -> int:
        clauses: list[str] = []
        params: list[Any] = []
        if chat_type:
            clauses.append("chat_type = ?")
            params.append(_text(chat_type))
        if chat_id is not None and _text(chat_id) != "":
            clauses.append("(chat_id_text = ? OR chat_id = ?)")
            params.extend([_text(chat_id), _nullable_int(chat_id)])
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._lock:
            self._require_open()
            row = self._db.execute(f"SELECT COUNT(*) AS total FROM messages{where}", tuple(params)).fetchone()
        return int(row["total"]) if row is not None else 0

    def frame_count(self, message_id: Any) -> int:
        with self._lock:
            self._require_open()
            row = self._db.execute(
                "SELECT COUNT(*) AS total FROM raw_frames WHERE message_key = ?", (_id_key(message_id),)
            ).fetchone()
        return int(row["total"]) if row is not None else 0

    def stats(self) -> dict[str, Any]:
        with self._lock:
            self._require_open()
            row = self._db.execute(
                "SELECT COUNT(*) AS messages, COALESCE(SUM(frame_count), 0) AS frames,"
                " COALESCE(SUM(length(raw_json)), 0) AS bytes FROM messages"
            ).fetchone()
        return {
            "backend": self.backend,
            "path": str(self.path),
            "schema_version": SCHEMA_VERSION,
            "messages": int(row["messages"]) if row else 0,
            "frames": int(row["frames"]) if row else 0,
            "bytes": int(row["bytes"]) if row else 0,
        }

    # -- lifecycle --------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            self._db.close()

    def __enter__(self) -> "SqliteMaxStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __len__(self) -> int:
        return self.count()

    def _cursor_row(self, before: Any) -> sqlite3.Row | None:
        key = _id_key(before)
        row = self._db.execute(
            "SELECT time, message_key FROM messages WHERE message_key = ?", (key,)
        ).fetchone()
        if row is not None:
            return row
        # 也允许用 seq 当游标：某些上游只回 seq。
        return self._db.execute(
            "SELECT time, message_key FROM messages WHERE seq = ? ORDER BY stored_at DESC LIMIT 1",
            (_nullable_int(before),),
        ).fetchone()

    def _require_open(self) -> None:
        if self._closed:
            raise sqlite3.ProgrammingError("maxStore 已关闭")


class MemoryMaxStore:
    """测试与"先跑通再接库"用的内存后端，语义与 SQLite 版一致。"""

    backend = "memory"

    def __init__(self, *, protocol: str = "") -> None:
        self.protocol = protocol
        self._rows: dict[str, dict[str, Any]] = {}
        self._order: list[str] = []
        self._lock = threading.RLock()

    def append(self, message: Mapping[str, Any] | Any) -> None:
        record = capture(message, protocol=self.protocol)
        record["message_key"] = _record_key(record, jsonpath.dump(record.get("raw") or {}))
        with self._lock:
            if record["message_key"] not in self._rows:
                self._order.append(record["message_key"])
            self._rows[record["message_key"]] = record

    def get(self, message_id: Any) -> dict[str, Any] | None:
        with self._lock:
            record = self._rows.get(_id_key(message_id))
        if record is None:
            return None
        return _record_to_dict(record)

    def query(self, chat_type: str | None = None, chat_id: Any = None, limit: int = 50,
              before: Any = None) -> list[dict[str, Any]]:
        rows = [self._rows[key] for key in self._order if key in self._rows]
        rows.sort(key=lambda record: (int(record.get("time") or 0), record["message_key"]))
        if chat_type:
            rows = [record for record in rows if record.get("chat_type") == _text(chat_type)]
        if chat_id is not None and _text(chat_id) != "":
            rows = [record for record in rows if _text(record.get("chat_id")) == _text(chat_id)]
        if before is not None:
            key = _id_key(before)
            index = next((i for i, record in enumerate(rows) if record["message_key"] == key), None)
            if index is None:
                return []
            rows = rows[:index]
        return [_record_to_dict(record) for record in rows[-max(1, int(limit or 1)):]]

    def raw_field(self, message_id: Any, json_path: Any) -> Any:
        record = self._rows.get(_id_key(message_id))
        if record is None:
            return None
        tokens = jsonpath.parse_path(json_path)
        if not tokens:
            return dict(record.get("raw") or {})
        target: Any = record.get("raw") or {}
        for token in tokens:
            if isinstance(target, list):
                if not token.isdigit() or int(token) >= len(target):
                    return None
                target = target[int(token)]
            elif isinstance(target, Mapping):
                if token not in target:
                    return None
                target = target[token]
            else:
                return None
        return target

    def raw_fields(self, message_id: Any) -> list[str]:
        record = self._rows.get(_id_key(message_id))
        if record is None:
            return []
        return [path for path, _kind, _text_value in jsonpath.flatten(record.get("raw") or {})]

    def delete(self, message_id: Any) -> bool:
        key = _id_key(message_id)
        with self._lock:
            existed = self._rows.pop(key, None) is not None
            if existed and key in self._order:
                self._order.remove(key)
        return existed

    def count(self, chat_type: str | None = None, chat_id: Any = None) -> int:
        wanted_chat = _text(chat_type) if chat_type else ""
        wanted_id = _text(chat_id) if chat_id is not None else ""
        with self._lock:
            matched = 0
            for record in self._rows.values():
                if wanted_chat and record.get("chat_type") != wanted_chat:
                    continue
                if wanted_id and _text(record.get("chat_id")) != wanted_id:
                    continue
                matched += 1
        return matched

    def close(self) -> None:
        with self._lock:
            self._rows.clear()
            self._order.clear()

    def __len__(self) -> int:
        return len(self._rows)


def open_store(path: Path | str = ":memory:", *, protocol: str = "", backend: str = "sqlite") -> RawStore:
    if str(path) == ":memory:" or backend.lower() == "memory":
        return MemoryMaxStore(protocol=protocol)
    return SqliteMaxStore(path, protocol=protocol)


# -------------------------------------------------------------------- helpers


def _record_key(record: Mapping[str, Any], raw_json: str) -> str:
    """消息主键。

    优先用 message_id（同一 ID 重复上报时应当覆盖而不是叠加）；没有 ID 的畸形包用
    内容哈希，保证同一个包重复写入仍然收敛到一行——否则重放会把表撑爆。
    """
    message_id = record.get("message_id")
    if isinstance(message_id, int) and message_id > 0:
        return str(message_id)
    if isinstance(message_id, str) and message_id.strip():
        return message_id.strip()
    digest = hashlib.blake2b(raw_json.encode("utf-8"), digest_size=12).hexdigest()
    return f"raw:{digest}"


def _id_key(value: Any) -> str:
    if isinstance(value, bool):
        return str(int(value))
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    text = _text(value)
    if text.endswith(".0") and text[:-2].lstrip("-").isdigit():
        text = text[:-2]
    return text


def _int_key(value: Any) -> int | None:
    """uint64 -> 有符号 INTEGER 的双射（同 ``qsick.storage``）。

    QQ 的 message_id 是 uint64，超过 2^63 的值直接插会溢出。但列只是查询用的镜像，
    真值始终在 ``raw_json`` 与 TEXT 主键里，所以这里做无损映射而不是截断。
    """
    number = _parse_int(value)
    if number is None:
        return None
    if 0 <= number < UINT64:
        return number if number < INT64_HALF else number - UINT64
    return number if -INT64_HALF <= number < INT64_HALF else None


def _parse_int(value: Any) -> int | None:
    """解析整数但不设范围限制——ID 的精度是这份存储协议的立身之本。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _nullable_int(value: Any) -> int | None:
    """给普通 INTEGER 列用：放不下就存 NULL，别让 ids 溢出把整条消息写废。"""
    number = _parse_int(value)
    return number if number is not None and -INT64_HALF <= number < INT64_HALF else None


def _first_int(*values: Any) -> int:
    for value in values:
        number = _parse_int(value)
        if number:
            return number
    return 0


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return str(int(value))
    return str(value)


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return _compose(
        message_key=row["message_key"],
        raw_json=row["raw_json"],
        elements_json=row["elements_json"],
        meta_json=row["meta_json"],
        message_id=row["message_id"],
        seq=row["seq"],
        time=row["time"],
        chat_type=row["chat_type"],
        chat_id=row["chat_id_text"],
        sender_uin=row["sender_uin"],
        sender_uid=row["sender_uid"],
        sender_name=row["sender_name"],
        card=row["card"],
        role=row["role"],
        account=row["account"],
        protocol=row["protocol"],
        element_count=row["element_count"],
        frame_count=row["frame_count"],
        stored_at=row["stored_at"],
    )


def _record_to_dict(record: Mapping[str, Any]) -> dict[str, Any]:
    return _compose(
        message_key=_record_key(record, jsonpath.dump(jsonpath.sanitize(record.get("raw") or {}))),
        raw_json=jsonpath.dump(jsonpath.sanitize(record.get("raw") or {})),
        elements_json=jsonpath.dump(jsonpath.sanitize(record.get("elements") or [])),
        meta_json=jsonpath.dump(record.get("meta") or {}),
        message_id=record.get("message_id"),
        seq=record.get("seq") or 0,
        time=record.get("time") or 0,
        chat_type=record.get("chat_type") or "",
        chat_id=_text(record.get("chat_id")),
        sender_uin=record.get("sender_uin") or 0,
        sender_uid=record.get("sender_uid") or "",
        sender_name=record.get("sender_name") or "",
        card=record.get("card") or "",
        role=record.get("role") or "",
        account=record.get("account") or "",
        protocol=(record.get("meta") or {}).get("protocol") or "",
        element_count=len(record.get("elements") or []),
        frame_count=len(jsonpath.flatten(record.get("raw") or {})),
        stored_at=(record.get("meta") or {}).get("received_at") or 0,
    )


def _compose(**fields: Any) -> dict[str, Any]:
    """统一的对外形态。

    形状刻意贴近 ``Message.to_dict()``（``chat`` / ``sender`` / ``elements``），这样一个
    熟悉的调用方不必学第二套；而 ``raw`` 里是未经加工的原包，想自己解析就自己解析。

    ``message_id`` 用原文里的值还原，而不是 INTEGER 列：列是镜像，可能为 NULL，
    真值永远在 ``raw`` 里。
    """
    try:
        raw = json.loads(fields["raw_json"]) if fields["raw_json"] else {}
    except (TypeError, ValueError):
        raw = {}
    try:
        elements = json.loads(fields["elements_json"]) if fields["elements_json"] else []
    except (TypeError, ValueError):
        elements = []
    try:
        meta = json.loads(fields["meta_json"]) if fields["meta_json"] else {}
    except (TypeError, ValueError):
        meta = {}
    message_id = _first_int(raw.get("message_id"), raw.get("msg_id"), raw.get("id")) or fields["message_id"] or 0
    out = {
        "message_id": message_id,
        "message_key": fields["message_key"],
        "seq": fields["seq"],
        "time": fields["time"],
        "chat": {"type": fields["chat_type"], "id": _id_value(fields["chat_id"])},
        "sender": {
            "uin": fields["sender_uin"] or 0,
            "uid": fields["sender_uid"],
            "nickname": fields["sender_name"],
            "card": fields["card"],
            "role": fields["role"],
        },
        "account": fields["account"],
        "protocol": fields["protocol"],
        "elements": elements,
        "raw": raw,
        "meta": {
            **meta,
            "element_count": fields["element_count"],
            "frame_count": fields["frame_count"],
            "stored_at": fields["stored_at"],
        },
    }
    return out


def _id_value(text: str) -> int | str:
    """数字就还原成 int，否则原样返回：ID 可能是字符串型（某些协议如此）。"""
    text = _text(text)
    if text.lstrip("-").isdigit():
        try:
            return int(text)
        except ValueError:
            return text
    return text
