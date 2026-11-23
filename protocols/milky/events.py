"""内部事件（OneBot v11 形状）-> Milky 事件。

## 为什么从 v11 转而不是两套并行

内部事件的规范形状就是 v11 的（`post_type` / `message_type` / `sender` / `message` 段数组）。
再造一套内部的"中立事件"意味着要同时改事件生成、插件钩子和 WebUI，而收益只是让转换看起来
对称。所以 Milky 走"从 v11 转"：现有事件源一行不改，转换只在这一处。

代价要说清楚：v11 事件里没有的信息，转出来也不会有。比如 Milky 的 `group_member_increase`
要 `operator_id`，而 v11 的 `notice.group_increase` 今天是有的（`payload-abi.md` 定义了），
但宿主并不发它——所以这里只能转出**宿主真正产生**的事件。

## 事件覆盖

| Milky 事件 | 来源 | 状态 |
| --- | --- | --- |
| `message_receive` | `post_type=message` | ✅ |
| `message_recall` | `notice.group_recall` / `notice.friend_recall` | ✅ |
| `bot_offline` | `meta_event` 里 `status.online=false` 或 lifecycle 断开 | ✅ |
| `group_admin_change` / `group_member_increase` / `group_member_decrease` / `group_mute` | 宿主有槽位（`notice.group_*`），今天不产生 | ⏳ 转换已写好，来事件就能转 |
| `friend_request` / `group_join_request` / `group_invitation` | `request.*`，宿主不发 | ⏳ 同上 |
| 其余 11 个 | 内部连槽位都没有 | ❌ 需要先让宿主产生事件 |

"转换已写好但没事件可转"是诚实的中间状态：一旦宿主开始产生通知，它们立刻生效，不需要再改
这里。这也是为什么这一层要按 tag 分派而不是按"现在有什么"硬编码。
"""

from __future__ import annotations

from typing import Any

from . import codec

#: Milky 的事件信封字段名。v11 用 `self_id`，Milky 用 `self_id`；时间两边都是 `time`（秒）。
_ENVELOPE_KEYS = ("event_type", "time", "self_id")


def _envelope(event_type: str, event: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_type": event_type,
        "time": int(event.get("time") or 0),
        "self_id": _uin(event.get("self_id")),
    }


def _uin(value: Any) -> int | str:
    text = str(value if value is not None else "").strip()
    if text.isdigit():
        number = int(text)
        # uin 是 64 位，但 QQ 号实际不超过 10 位；超出安全整数范围说明这不是号，原样给字符串。
        return number if number < 2 ** 53 else text
    return text


def _segments(message: Any) -> list[dict[str, Any]]:
    """v11 的段数组 -> Milky 段数组。

    消息可能是数组也可能的 CQ 字符串（v11 两种都允许），但内部事件永远是数组
    （`events.py` 里 `message_format: "array"`），所以这里只处理数组；万一拿到别的形状，
    当成空数组而不是抛异常——一条消息转不出来不该让整条连接断掉。
    """
    if not isinstance(message, list):
        return []
    parts: list[dict[str, Any]] = []
    for segment in message:
        if not isinstance(segment, dict):
            continue
        kind = str(segment.get("type") or "")
        data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
        parts.append({"kind": kind, "type": kind, "data": data})
    return codec.parts_to_segments(_to_parts(message))


def _to_parts(message: list[Any]) -> list[dict[str, Any]]:
    """v11 段 -> 内部部件（借道 v11 的 codec，不重写一遍解析）。"""
    from protocols.onebotv11 import codec as v11codec

    return v11codec.segments_to_parts(message)


def convert(event: dict[str, Any]) -> dict[str, Any] | None:
    """一个内部事件 -> 一个 Milky 事件；表达不了时返回 ``None``（调用方跳过这条连接）。"""
    kind = str(event.get("post_type") or "")

    if kind in ("message", "message_sent"):
        return _message(event)
    if kind == "notice":
        return _notice(event)
    if kind == "request":
        return _request(event)
    if kind == "meta_event":
        return _meta(event)
    # 未知 post_type：v11 里就到这里为止了，转不出 Milky 的形状。
    return None


# --------------------------------------------------------------------- 消息


def _message(event: dict[str, Any]) -> dict[str, Any] | None:
    is_group = str(event.get("message_type") or "") == "group"
    out = _envelope("message_receive", event)
    out["data"] = {
        "message_scene": "group" if is_group else "friend",
        "peer_id": _uin(event.get("group_id") if is_group else event.get("target_id")),
        "message_seq": int(event.get("message_seq") or 0),
        "sender_id": _uin(event.get("user_id")),
        "time": int(event.get("time") or 0),
        "segments": _segments(event.get("message")),
        # Milky 的 message_id 是 int64。v11 侧超出 JS 安全范围时会退化成字符串
        # （`events.py` 里那段注释），这里照实传，不做数字化。
        "message_id": event.get("message_id"),
        "group": _group(event) if is_group else None,
        "friend": None if is_group else _friend(event),
    }
    return out


def _sender(event: dict[str, Any]) -> dict[str, Any]:
    sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
    return {
        "user_id": _uin(sender.get("user_id") or event.get("user_id")),
        "nickname": str(sender.get("nickname") or ""),
        "card": str(sender.get("card") or ""),
        "role": str(sender.get("role") or ""),
    }


def _group(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "group_id": _uin(event.get("group_id")),
        # v11 事件里没有群名（见 events.py 的注释），这里不编一个。
        "group_name": "",
        "member": _sender(event),
    }


def _friend(event: dict[str, Any]) -> dict[str, Any]:
    sender = _sender(event)
    return {"user_id": sender["user_id"], "nickname": sender["nickname"], "remark": ""}


# ------------------------------------------------------------------- 通知


def _notice(event: dict[str, Any]) -> dict[str, Any] | None:
    notice_type = str(event.get("notice_type") or "")

    if notice_type.endswith("recall"):
        out = _envelope("message_recall", event)
        out["data"] = {
            "message_scene": "group" if notice_type.startswith("group") else "friend",
            "peer_id": _uin(event.get("group_id") or event.get("target_id")),
            "message_id": event.get("message_id"),
            "user_id": _uin(event.get("user_id") or event.get("operator_id")),
        }
        return out

    # 下面几个是"转换已写好、宿主今天不产生"的那批。留着不是死代码：
    # 宿主一旦开始发 notice.group_increase 之类，它们立刻生效。
    mapping = {
        "group_increase": "group_member_increase",
        "group_decrease": "group_member_decrease",
        "group_admin": "group_admin_change",
        "group_ban": "group_mute",
        "group_whole_ban": "group_whole_mute",
        "group_name": "group_name_change",
    }
    event_type = mapping.get(notice_type)
    if not event_type:
        # group_card / group_upload / notify(poke) 等：Milky 没有对应事件，
        # 或者要为它另造一个结构体。跳过而不是硬塞进 message_receive。
        return None

    out = _envelope(event_type, event)
    base: dict[str, Any] = {"group_id": _uin(event.get("group_id"))}
    if notice_type == "group_increase":
        base["user_id"] = _uin(event.get("user_id"))
        base["operator_id"] = _uin(event.get("operator_id"))
    elif notice_type == "group_decrease":
        base["user_id"] = _uin(event.get("user_id"))
        base["operator_id"] = _uin(event.get("operator_id"))
    elif notice_type == "group_admin":
        base["user_id"] = _uin(event.get("user_id"))
        base["is_set"] = bool(event.get("is_set", event.get("set")))
        base["operator_id"] = _uin(event.get("operator_id"))
    elif notice_type in ("group_ban", "group_whole_ban"):
        base["operator_id"] = _uin(event.get("operator_id"))
        base["is_ban"] = int(event.get("duration") or 0) > 0
        base["duration"] = int(event.get("duration") or 0)
        if notice_type == "group_ban":
            base["user_id"] = _uin(event.get("user_id"))
    elif notice_type == "group_name":
        base["group_name"] = str(event.get("group_name") or "")
        base["operator_id"] = _uin(event.get("operator_id"))
    out["data"] = base
    return out


# ------------------------------------------------------------------- 请求


def _request(event: dict[str, Any]) -> dict[str, Any] | None:
    request_type = str(event.get("request_type") or "")
    if request_type == "friend":
        out = _envelope("friend_request", event)
        out["data"] = {
            "initiator_id": _uin(event.get("user_id")),
            "comment": str(event.get("comment") or ""),
            "via": str(event.get("via") or ""),
            "flag": str(event.get("flag") or ""),
        }
        return out
    if request_type == "group":
        sub = str(event.get("sub_type") or "")
        # Milky 把"申请入群"和"被邀请入群"分成两个事件。
        if sub == "invite":
            out = _envelope("group_invitation", event)
            out["data"] = {
                "group_id": _uin(event.get("group_id")),
                "initiator_id": _uin(event.get("user_id")),
                "comment": str(event.get("comment") or ""),
                "flag": str(event.get("flag") or ""),
            }
        else:
            out = _envelope("group_join_request", event)
            out["data"] = {
                "group_id": _uin(event.get("group_id")),
                "initiator_id": _uin(event.get("user_id")),
                "comment": str(event.get("comment") or ""),
                "is_filtered": bool(event.get("is_filtered")),
                "flag": str(event.get("flag") or ""),
            }
        return out
    return None


# ------------------------------------------------------------------- 元事件


def _meta(event: dict[str, Any]) -> dict[str, Any] | None:
    meta_type = str(event.get("meta_event_type") or "")
    if meta_type == "lifecycle":
        sub = str(event.get("sub_type") or "")
        if sub in ("connect", "enable"):
            # Milky 没有"上线"事件（连接本身就是），只报 `bot_offline`。
            return None
        return _envelope("bot_offline", event) | {"data": {"reason": sub or "disconnect"}}
    if meta_type == "heartbeat":
        status = event.get("status") if isinstance(event.get("status"), dict) else {}
        if status.get("online") is False:
            return _envelope("bot_offline", event) | {"data": {"reason": "heartbeat"}}
        return None
    return None
