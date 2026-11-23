"""把 maxStore 接到 ``MessageStore`` 上。

为什么需要这一层：``protocols/maxStore`` 是"证据库"——它按原始 JSON 落盘，接口是
``append/get/query``，刻意不关心消息该怎么用。而 WebUI、动作层、插件都按
``MessageStore``（``put/recent/search/set_media_path``）读消息。两者之间的翻译就
在这里，只有这一处。

存储格式的关键取舍：**入库存原始 payload，出库时再解析成 ``Message``**。这样
maxStore 保留的"一切字段"是真的——将来某个协议需要今天没人读的字段，它还在库里，
不需要为了兼容性而重写一遍历史数据。这与 ``qsick.storage.SqliteStore`` 相反，后者
存的是"够用的规范化副本"，字段一旦没被解析就等于丢了。
"""

from __future__ import annotations

import json
from collections import OrderedDict
from pathlib import Path
from typing import Any, Iterable

from .protocol.message import Message
from .protocol_bridge import ensure_importable


class RawMessageStore:
    """``MessageStore`` 的 maxStore 实现（每个实例一份）。

    ``path`` 是该实例自己的数据库文件：``data/instances/C1/messages.db``。实例之间
    不共享文件，这是"每个消息处理器各自处理消息接收"的物理前提——共用一个库时，
    两个实例的 message_id 可能撞车，撞了就只能丢一条。
    """

    backend = "maxstore"

    def __init__(self, path: Path | str, *, max_messages: int = 0, max_days: int = 0,
                 protocol: str = "") -> None:
        ensure_importable()
        from protocols.maxStore.maxstore import SqliteMaxStore

        self.path = Path(path)
        self.max_messages = max(0, max_messages)
        self.max_days = max(0, max_days)
        self._store = SqliteMaxStore(self.path, protocol=protocol)

    # ---------------------------------------------------------------- 写入

    def put(self, message: Message) -> None:
        """落盘一条消息。

        写的是"原始 payload + 解析后补上的字段"的合并结果。

        只用 ``message.raw`` 是不够的：那是宿主给的**解析前**快照，而解析会往
        ``element.data`` 里补 ``local_path``（下载落盘位置）、往 ``account`` 上
        补归属账号。只用原样 raw 就等于把这些结果丢掉，读回来 account 是空的、
        图片路径也没有——表现正是"自动下载看起来没生效，手动点击却正常"。

        合并而不是直接写 ``to_dict()``：raw 里还有今天没被解析的字段，那些是
        maxStore 要保留的东西，不能被一次规范化裁掉。
        """
        payload = self._merge_payload(message)
        self._append(payload)

    @staticmethod
    def _merge_payload(message: Message) -> dict[str, Any]:
        """原始 payload 为底，用解析后的字段覆盖其上。

        ``elements`` 单独处理、按下标逐个合并：整体覆盖会把 raw 里元素内部
        尚未识别的字段抹掉，而"看不懂也要留着"正是这个库的职责。
        """
        parsed = message.to_dict()
        raw = message.raw if isinstance(message.raw, dict) else {}
        if not raw:
            return parsed
        merged = dict(raw)
        for key, value in parsed.items():
            if key in ("raw", "elements"):
                continue
            if value not in (None, "", [], {}):
                merged[key] = value
        merged["elements"] = _merge_elements(raw.get("elements"), parsed.get("elements"))
        return merged

    def _append(self, payload: dict[str, Any]) -> None:
        self._store.append(payload)

    # ---------------------------------------------------------------- 读取

    def get(self, message_id: int) -> Message | None:
        record = self._store.get(message_id)
        return _to_message(record)

    def page(self, chat_type: str, chat_id: Any, *, limit: int = 80,
             before: Any = None, account: str = "") -> list[Message]:
        """按会话取一页历史（时间正序）。

        这是聊天栏的主路径：有 ``(chat_type, chat_id, time)`` 索引，翻页不必
        扫全库。``before`` 是排他游标，由 maxStore 的 ``query`` 负责比对。
        """
        records = self._store.query(chat_type, chat_id, max(1, limit), before)
        messages = [message for message in (_to_message(record) for record in records) if message]
        if account:
            messages = [message for message in messages if str(message.account) == str(account)]
        return messages

    def page_after(self, chat_type: str, chat_id: Any, *, after_id: int, limit: int = 80,
                   account: str = "") -> list[Message]:
        """取比 ``after_id`` 更新的消息（时间正序），供聊天栏增量轮询。

        这是"收到消息延迟高"的一半解法：轮询不再整页重拉 80 条，只读新增的
        那几条，通常一条都没有。
        """
        store = self._store
        clauses = ["chat_type = ?", "chat_id = ?", "message_id > ?"]
        params: list[Any] = [chat_type, chat_id, int(after_id)]
        if account:
            clauses.append("account = ?")
            params.append(str(account))
        sql = ("SELECT * FROM messages WHERE " + " AND ".join(clauses)
               + " ORDER BY time ASC, message_key ASC LIMIT ?")
        params.append(max(1, int(limit)))
        with store._lock:  # noqa: SLF001 - 同包内受控访问
            store._require_open()
            rows = store._db.execute(sql, tuple(params)).fetchall()
        from protocols.maxStore.maxstore.store import _row_to_dict

        return [message for message in (_to_message(_row_to_dict(row)) for row in rows) if message]

    def conversation_summaries(self, account: str = "") -> list[dict[str, Any]]:
        """每个会话的最新一条消息（会话列表用）。

        用 SQL 的窗口函数取"每组最新一行"，而不是把全库读出来在 Python 里覆盖
        字典——后者在库大时会把每次下拉刷新都变成一次全表反序列化。
        """
        store = self._store
        with store._lock:  # noqa: SLF001 - 同包内受控访问
            store._require_open()
            rows = store._db.execute(
                """
                SELECT * FROM (
                    SELECT messages.*, ROW_NUMBER() OVER (
                        PARTITION BY chat_type, chat_id ORDER BY time DESC, message_key DESC
                    ) AS rn
                    FROM messages
                    WHERE chat_type IN ('group', 'private') AND chat_id IS NOT NULL
                ) WHERE rn = 1
                ORDER BY time DESC, message_key DESC
                """
            ).fetchall()
        from protocols.maxStore.maxstore.store import _row_to_dict

        out: list[dict[str, Any]] = []
        for row in rows:
            message = _to_message(_row_to_dict(row))
            if message is None:
                continue
            if account and str(message.account) not in ("", account):
                continue
            out.append({"message": message})
        return out

    def recent(self, limit: int = 50) -> list[Message]:
        records = self._records(limit=max(1, limit))
        return [message for message in (_to_message(record) for record in reversed(records)) if message]

    def search(self, query: str = "", offset: int = 0, limit: int = 50) -> list[Message]:
        needle = query.strip().lower()
        if not needle:
            return list(reversed(self.recent(max(1, limit) + max(0, offset))))[max(0, offset):]

        # 走 SQL 先粗筛，再在 Python 里用与内存实现相同的规则精筛：
        # payload 里任何位置出现该字符串，或 chat_id 命中。
        rows = self._raw_rows(needle)
        matched: list[Message] = []
        for record in rows:
            message = _to_message(record)
            if message is None:
                continue
            if needle in (message.text or "").lower() or needle in str(message.chat_id):
                matched.append(message)
        matched.sort(key=lambda item: (item.time, item.message_id))
        return matched[offset: offset + max(1, limit)]

    def count_matching(self, query: str = "") -> int:
        needle = query.strip().lower()
        if not needle:
            return len(self)
        return len(self.search(query, 0, 1 << 30))

    def count(self) -> int:
        return self._store.count()

    # ------------------------------------------------------------ 媒体路径

    def set_media_path(self, message_id: int, element_id: str, path: str) -> bool:
        """把下载好的本地路径写回消息。

        maxStore 是 append-only 的原文库，但它用 ``ON CONFLICT DO UPDATE`` 覆盖
        同一条记录，所以"读出来、改一处、写回去"是安全的：整包原文仍然完整，
        只多了一个 ``local_path``。
        """
        record = self._store.get(message_id)
        if record is None:
            return False
        payload = record.get("raw")
        if not isinstance(payload, dict):
            return False
        changed = False
        for element in payload.get("elements") or []:
            if not isinstance(element, dict):
                continue
            data = element.get("data")
            if isinstance(data, dict) and str(data.get("element_id", "")) == str(element_id):
                data["local_path"] = path
                changed = True
        if not changed:
            return False
        self._append(payload)
        return True

    # ---------------------------------------------------------------- 删除

    def delete(self, message_id: int) -> bool:
        return bool(self._store.delete(message_id))

    def delete_chat(self, chat_type: str, chat_id: int) -> int:
        records = self._records(chat_type=chat_type, chat_id=chat_id, limit=1 << 30)
        removed = 0
        for record in records:
            message_id = record.get("message_id")
            if message_id and self._store.delete(message_id):
                removed += 1
        return removed

    def close(self) -> None:
        self._store.close()

    def __len__(self) -> int:
        return self._store.count()

    # ------------------------------------------------------------ 内部实现

    def _records(self, *, chat_type: str | None = None, chat_id: Any = None,
                 limit: int = 50, before: Any = None) -> list[dict[str, Any]]:
        return self._store.query(chat_type, chat_id, limit, before)

    def _raw_rows(self, needle: str) -> list[dict[str, Any]]:
        """按原文粗筛，时间正序。

        用参数化 LIKE 而不是拼接：needle 来自 HTTP 查询参数，拼进 SQL 就是注入。
        """
        store = self._store
        pattern = f"%{needle}%"
        with store._lock:  # noqa: SLF001 - 同包内的受控访问，避免再开一份连接
            store._require_open()
            rows = store._db.execute(
                "SELECT * FROM messages WHERE lower(raw_json) LIKE ? OR cast(chat_id AS TEXT) LIKE ?"
                " ORDER BY time DESC, message_key DESC",
                (pattern, pattern),
            ).fetchall()
        from protocols.maxStore.maxstore.store import _row_to_dict

        return [_row_to_dict(row) for row in rows]

    def prune(self) -> int:
        """按配置裁剪历史，只删最旧的。

        由 ``Runtime.start`` 在启动时调用一次，而不是每次落盘都调：逐条裁剪会把保留上限变成硬顶，
        写进来的历史边写边被吃掉。调成启动时一趟，语义就是"启动时清理超额消息"。
        """
        return self._prune()

    def _prune(self) -> int:
        """按配置裁剪历史，只删最旧的。

        以前这里有个反了的假设：``_records`` 的时间序是**正序**（最旧在前），
        而代码把它当成了倒序，于是"删掉超出的条数"变成"删掉最旧的那几条"之外
        的结果——实测 max_messages=3 插入 10..50 之后留下的是 [10,20,30]，
        新消息一落盘就被自己裁掉，库永远冻结在最初 N 条。

        现在显式按 (time, message_id) 排序后取最旧的一批，语义不依赖
        ``_records`` 的返回顺序。
        """
        if not self.max_messages and not self.max_days:
            return 0
        drop: list[Any] = []
        records = self._records(limit=1 << 30)
        if self.max_days:
            import time as _time

            cutoff = int(_time.time()) - self.max_days * 86400
            for record in records:
                if 0 < int(record.get("time") or 0) < cutoff:
                    drop.append(record.get("message_id"))
        if self.max_messages:
            remaining = [record for record in records if record.get("message_id") not in drop]
            if len(remaining) > self.max_messages:
                remaining.sort(key=lambda item: (int(item.get("time") or 0),
                                                 int(item.get("message_id") or 0)))
                extra = len(remaining) - self.max_messages
                drop.extend(record.get("message_id") for record in remaining[:extra])
        removed = 0
        for message_id in drop:
            if message_id:
                self._store.delete(message_id)
                removed += 1
        return removed


class MemoryRawMessageStore:
    """内存版实例库（``storage.backend == "memory"``）。

    语义与 :class:`RawMessageStore` 一致，只是不落盘：存原始 payload，
    读时再解析成 ``Message``。测试与轻量运行用它，避免为每个实例建库文件。
    """

    backend = "memory"

    def __init__(self, *, max_messages: int = 0, max_days: int = 0, protocol: str = "") -> None:
        self.max_messages = max(0, max_messages)
        self.max_days = max(0, max_days)
        self._records: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
        self.path = None

    # ---------------------------------------------------------------- 写入

    def put(self, message: Message) -> None:
        payload = message.raw if isinstance(message.raw, dict) and message.raw else message.to_dict()
        key = str(message.message_id)
        self._records[key] = payload
        self._records.move_to_end(key)

    # ---------------------------------------------------------------- 读取

    def get(self, message_id: int) -> Message | None:
        payload = self._records.get(str(message_id))
        return Message.from_payload(payload) if isinstance(payload, dict) else None

    def page(self, chat_type: str, chat_id: Any, *, limit: int = 80,
             before: Any = None, account: str = "") -> list[Message]:
        """按会话取一页（时间正序），语义与 :meth:`RawMessageStore.page` 一致。"""
        rows = [message for message in self._iter(chat_type, chat_id, account) if message is not None]
        rows.sort(key=lambda message: (message.time, message.message_id))
        if before is not None:
            wanted = int(before) if str(before).isdigit() else None
            if wanted is not None:
                # 游标是排他的：只取严格早于它的那些。
                pivot = next((message for message in rows if message.message_id == wanted), None)
                if pivot is not None:
                    rows = [message for message in rows
                            if (message.time, message.message_id) < (pivot.time, pivot.message_id)]
        return rows[-max(1, limit):]

    def recent(self, limit: int = 50) -> list[Message]:
        rows = [message for message in self._iter() if message is not None]
        rows.sort(key=lambda message: (message.time, message.message_id))
        return rows[-max(1, limit):]

    def search(self, query: str = "", offset: int = 0, limit: int = 50) -> list[Message]:
        needle = query.strip().lower()
        rows = [message for message in self._iter() if message is not None]
        if needle:
            rows = [message for message in rows
                    if needle in (message.text or "").lower() or needle in str(message.chat_id)]
        rows.sort(key=lambda message: (message.time, message.message_id))
        return rows[offset: offset + max(1, limit)]

    def count_matching(self, query: str = "") -> int:
        return len(self.search(query, 0, 1 << 30))

    def count(self) -> int:
        return len(self._records)

    def page_after(self, chat_type: str, chat_id: Any, *, after_id: int, limit: int = 80,
                   account: str = "") -> list[Message]:
        """取比 ``after_id`` 更新的消息（时间正序）。"""
        rows = [message for message in self._iter(chat_type, chat_id, account)
                if message is not None and int(message.message_id) > int(after_id)]
        rows.sort(key=lambda message: (message.time, message.message_id))
        return rows[: max(1, int(limit))]

    def conversation_summaries(self, account: str = "") -> list[dict[str, Any]]:
        latest: dict[tuple[str, int], Message] = {}
        for message in self._iter():
            if message is None:
                continue
            if message.chat_type not in ("group", "private") or message.chat_id <= 0:
                continue
            latest[(message.chat_type, message.chat_id)] = message
        out = [{"message": message} for message in latest.values()]
        out.sort(key=lambda item: (item["message"].time, item["message"].message_id), reverse=True)
        return out

    def set_media_path(self, message_id: int, element_id: str, path: str) -> bool:
        payload = self._records.get(str(message_id))
        if not isinstance(payload, dict):
            return False
        changed = False
        for element in payload.get("elements") or []:
            if not isinstance(element, dict):
                continue
            data = element.get("data")
            if isinstance(data, dict) and str(data.get("element_id", "")) == str(element_id):
                data["local_path"] = path
                changed = True
        return changed

    def delete(self, message_id: int) -> bool:
        return self._records.pop(str(message_id), None) is not None

    def delete_chat(self, chat_type: str, chat_id: int) -> int:
        doomed = [key for key, payload in self._records.items()
                  if str(payload.get("chat", {}).get("type") or payload.get("chat_type")) == chat_type
                  and str(payload.get("chat", {}).get("id") or payload.get("chat_id")) == str(chat_id)]
        for key in doomed:
            self._records.pop(key, None)
        return len(doomed)

    def close(self) -> None:
        self._records.clear()

    def __len__(self) -> int:
        return len(self._records)

    # ------------------------------------------------------------ 内部实现

    def _iter(self, chat_type: str | None = None, chat_id: Any = None,
              account: str = "") -> Iterable[Message | None]:
        for payload in self._records.values():
            message = Message.from_payload(payload) if isinstance(payload, dict) else None
            if message is None:
                yield None
                continue
            if chat_type is not None and message.chat_type != chat_type:
                continue
            if chat_id is not None and str(message.chat_id) != str(chat_id):
                continue
            if account and str(message.account) != str(account):
                continue
            yield message

    def prune(self) -> int:
        """启动时裁剪一趟；语义与 :meth:`RawMessageStore.prune` 一致。"""
        before = len(self._records)
        self._prune()
        return before - len(self._records)

    def _prune(self) -> None:
        if self.max_messages and len(self._records) > self.max_messages:
            extra = len(self._records) - self.max_messages
            for _ in range(extra):
                if not self._records:
                    break
                self._records.popitem(last=False)


def _merge_elements(raw_elements: Any, parsed_elements: Any) -> Any:
    """按下标合并元素：raw 的元素为底，解析后补上的字段覆盖其上。

    这样 ``local_path``（下载落盘位置）这类**运行时才知道**的字段能存下来，
    而 raw 里元素内部尚未识别的字段也不会因为一次规范化而消失。
    """
    if not isinstance(parsed_elements, list):
        return raw_elements if isinstance(raw_elements, list) else []
    if not isinstance(raw_elements, list):
        return parsed_elements
    merged: list[Any] = []
    for index, parsed in enumerate(parsed_elements):
        base = raw_elements[index] if index < len(raw_elements) else None
        if not isinstance(base, dict) or not isinstance(parsed, dict):
            merged.append(parsed)
            continue
        combined = dict(base)
        for key, value in parsed.items():
            if key == "data":
                continue
            combined[key] = value
        base_data = base.get("data") if isinstance(base.get("data"), dict) else {}
        parsed_data = parsed.get("data") if isinstance(parsed.get("data"), dict) else {}
        combined["data"] = {**base_data, **parsed_data}
        merged.append(combined)
    return merged


def _to_message(record: dict[str, Any] | None) -> Message | None:
    """库记录 -> ``Message``。

    优先用 ``raw``（原始 payload），它是唯一无损的来源；`raw` 缺失时才退回
    各索引列拼装，那种情况下消息仍然可读，只是丢了协议特有的字段。
    """
    if not isinstance(record, dict):
        return None
    raw = record.get("raw")
    if isinstance(raw, dict) and raw:
        return Message.from_payload(raw)
    try:
        elements = json.loads(record.get("elements_json") or "[]")
    except (TypeError, json.JSONDecodeError):
        elements = []
    return Message.from_payload({
        "message_id": record.get("message_id"),
        "seq": record.get("seq"),
        "time": record.get("time"),
        "chat": {"type": record.get("chat_type"), "id": record.get("chat_id")},
        "sender": {"uin": record.get("sender_uin"), "uid": record.get("sender_uid"),
                   "nickname": record.get("sender_name"), "card": record.get("card"),
                   "role": record.get("role")},
        "account": record.get("account"),
        "elements": elements if isinstance(elements, list) else [],
    })


def create_instance_store(config: Any, instance: str, uin: str = "") -> Any:
    """为一个账号建库。

    ``storage.backend == "memory"`` 时用内存实现：这是测试与轻量运行的默认，
    忽略它会让每个实例都去建 SQLite 文件——每次都落盘，整个套件因此慢了
    一倍多，而且 ``tmp_path`` 里会留下一堆库文件。

    目录按 QQ 号（``uin``）命名，而不是按实例名。理由是实例名会变：pipe session
    用临时 id，每次启动都不一样，按它分库等于每次启动都开一个新库，历史碎成一堆
    目录。QQ 号是账号的稳定标识，换 session、换实例都不影响它——同一个号的历史
    始终在同一个库里。

    ``uin`` 为空时退回实例名：还没登录就建库是正常路径（实例先创建），此时没有
    号可用，只能先按实例名落一份，登录后再由调用方切到号对应的库。
    """
    backend = str(getattr(getattr(config, "storage", None), "backend", "") or "").lower()
    if backend == "memory":
        return MemoryRawMessageStore()
    # 号必须是纯数字，且不能长得像路径：它来自 QQ，但落盘前不能假设它一定干净。
    key = str(uin or "").strip()
    if not (key.isdigit() and len(key) <= 20):
        key = str(instance or "").strip()
    if not key or "/" in key or "\\" in key or key in (".", ".."):
        key = "unknown"
    base = Path(config.path("instances")) / key
    base.mkdir(parents=True, exist_ok=True)
    return RawMessageStore(
        base / "messages.db",
        max_messages=config.rotation.max_messages,
        max_days=config.rotation.max_days,
        protocol=config.storage.protocol if hasattr(config.storage, "protocol") else "",
    )


def raw_fields(store: RawMessageStore, message_id: int) -> list[str]:
    """该消息在库里能查到的所有原始字段路径（调试/将来的协议兼容用）。"""
    return store._store.raw_fields(message_id)  # noqa: SLF001


def raw_field(store: RawMessageStore, message_id: int, path: str) -> Any:
    """按 JSON 指针取任意原始字段。"""
    return store._store.raw_field(message_id, path)  # noqa: SLF001


def iter_records(store: RawMessageStore, limit: int = 50) -> Iterable[dict[str, Any]]:
    """原始记录流（不做 ``Message`` 转换），供需要完整字段的分发方使用。"""
    return store._records(limit=limit)
