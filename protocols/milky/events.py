"""内部事件（OneBot v11 形状）-> Milky 事件。

## 为什么从 v11 转而不是两套并行

内部事件的规范形状就是 v11 的（`post_type` / `message_type` / `sender` / `message` 段数组）。
再造一套内部的"中立事件"意味着要同时改事件生成、插件钩子和 WebUI，而收益只是让转换看起来
对称。所以 Milky 走"从 v11 转"：共用事件源，只在必要处保留源已有的额外字段。

代价要说清楚：v11 事件里没有的信息，转出来也不会有。包推送和灰条共用这层转换，
但转换成功不等于字段完整或真实客户端验收通过。消息实体的空串/0 是兼容占位，
不代表已获取群名、好友分组等详情；temp 仅在源提供明确场景时可识别。

撤回仅使用 message_seq/msg_seq，不把 message_id 当序号；缺序号时的 0 不能定位消息。
管理员通知未提供的操作者、请求未提供的审批序号仍是源缺口，不能由主体或包信封序号猜测。
好友申请 UID 由 OLPush 保留；群邀请与入群申请使用各自明确提供的序号，不互相替代。
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


def _uin_or_zero(value: Any) -> int:
    """A required int64 field when the value may be absent: 0 is the spec's "nobody"."""
    resolved = _uin(value)
    return resolved if isinstance(resolved, int) else 0


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
    scene = _message_scene(event)
    is_group = scene == "group"
    out = _envelope("message_receive", event)
    out["data"] = {
        "message_scene": scene,
        "peer_id": _uin(event.get("group_id") if is_group else
                        (event.get("peer_id") or event.get("target_id") or event.get("user_id"))),
        "message_seq": int(event.get("message_seq") or 0),
        "sender_id": _uin(event.get("user_id")),
        "time": int(event.get("time") or 0),
        "segments": _segments(event.get("message")),
        # Milky 的 message_id 是 int64。v11 侧超出 JS 安全范围时会退化成字符串
        # （`events.py` 里那段注释），这里照实传，不做数字化。
        "message_id": event.get("message_id"),
        "group": _group(event) if is_group or (scene == "temp" and event.get("group_id")) else None,
        "friend": _friend(event) if scene == "friend" else None,
    }
    if is_group:
        out["data"]["group_member"] = _sender(event)
    return out


def _message_scene(event: dict[str, Any]) -> str:
    explicit = event.get("message_scene")
    if explicit in ("friend", "group", "temp"):
        return explicit
    if event.get("message_type") == "group" or event.get("notice_type") == "group_recall":
        return "group"
    # OneBot private/group means a temporary session, not a group message.
    if event.get("message_type") == "temp" or event.get("sub_type") in ("group", "temp"):
        return "temp"
    return "friend"


def _sender(event: dict[str, Any]) -> dict[str, Any]:
    sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
    member = event.get("group_member") if isinstance(event.get("group_member"), dict) else {}
    source = sender | member
    return {
        "user_id": _uin(source.get("user_id") or event.get("user_id")),
        "nickname": str(source.get("nickname") or ""),
        "sex": source.get("sex") if source.get("sex") in ("male", "female") else "unknown",
        "group_id": _uin(source.get("group_id") or event.get("group_id")),
        "card": str(source.get("card") or ""),
        "title": str(source.get("title") or ""),
        "level": int(source.get("level") or 0),
        "role": str(source.get("role") or "member"),
        "join_time": int(source.get("join_time") or 0),
        "last_sent_time": int(source.get("last_sent_time") or 0),
        "shut_up_end_time": source.get("shut_up_end_time"),
    }


def _group(event: dict[str, Any]) -> dict[str, Any]:
    source = event.get("group") if isinstance(event.get("group"), dict) else {}
    # Empty/zero metadata are compatibility placeholders, not fetched entity details.
    out = {"group_id": _uin(source.get("group_id") or event.get("group_id"))}
    for key in ("group_name", "remark", "description", "question", "announcement"):
        out[key] = str(source.get(key) or (event.get("group_name") if key == "group_name" else "") or "")
    for key in ("member_count", "max_member_count", "created_time"):
        out[key] = int(source.get(key) or 0)
    return out


def _friend(event: dict[str, Any]) -> dict[str, Any]:
    sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
    friend = event.get("friend") if isinstance(event.get("friend"), dict) else {}
    peer = event.get("peer_id") or event.get("target_id") or event.get("user_id")
    # Sent-message sender metadata describes the bot, not the friend entity.
    if _uin(sender.get("user_id") or event.get("user_id")) != _uin(peer):
        sender = {}
    source = sender | friend
    category = source.get("category") if isinstance(source.get("category"), dict) else {}
    return {
        "user_id": _uin(friend.get("user_id") or event.get("peer_id") or event.get("target_id")
                        or source.get("user_id") or event.get("user_id")),
        "nickname": str(source.get("nickname") or ""),
        "sex": source.get("sex") if source.get("sex") in ("male", "female") else "unknown",
        "qid": str(source.get("qid") or ""),
        "remark": str(source.get("remark") or ""),
        "category": {"category_id": int(category.get("category_id") or 0),
                     "category_name": str(category.get("category_name") or "")},
    }


# ------------------------------------------------------------------- 通知


def _notice(event: dict[str, Any]) -> dict[str, Any] | None:
    notice_type = str(event.get("notice_type") or "")

    if notice_type.endswith("recall"):
        out = _envelope("message_recall", event)
        out["data"] = {
            "message_scene": _message_scene(event),
            "peer_id": _uin_or_zero(event.get("group_id") if _message_scene(event) == "group" else
                                    event.get("peer_id", event.get("target_id", event.get("user_id")))),
            # Missing sequence remains partial support: 0 cannot identify the recalled message.
            # A message_id is NOT a sequence, even when it happens to fit in an integer.
            "message_seq": _uin_or_zero(event.get("message_seq") if event.get("message_seq") is not None
                                        else event.get("msg_seq")),
            "sender_id": _uin_or_zero(event.get("sender_id") or event.get("user_id")),
            "operator_id": _uin_or_zero(event.get("operator_id")),
            "display_suffix": str(event.get("display_suffix") or ""),
        }
        return out

    # 戳一戳在 OneBot 里是 `notice.notify` + `sub_type=poke`，Milky 把它分成好友与群两个事件。
    # 分开的依据就是有没有 group_id —— 与 OneBot 的约定一致。
    if notice_type == "notify":
        return _nudge(event)
    if notice_type in ("peer_pin", "peer_pin_change"):
        # 会话置顶不在本程序范围内，没有对应的 Milky 事件可发。返回 None 让分流点跳过这条连接，
        # 而不是编一个字段不全的。
        return None
    if notice_type == "group_disband":
        out = _envelope("group_disband", event)
        out["data"] = {
            "group_id": _uin(event.get("group_id")),
            "operator_id": _uin_or_zero(event.get("operator_id")),
        }
        return out

    builders = {
        "group_increase": _member_increase,
        "group_decrease": _member_decrease,
        "group_admin": _admin_change,
        "group_ban": _member_mute,
        "group_whole_ban": _whole_mute,
        "group_name": _name_change,
        "group_upload": _file_upload,
        "group_title": _title_change,
        "group_essence": _essence_change,
        "group_msg_emoji_like": _reaction,
    }
    build = builders.get(notice_type)
    if build is None:
        # group_card / group_upload 等：Milky 没有对应事件。跳过而不是硬塞进邻近的类型——
        # 一个 catch-all 会把禁言当成入群，客户端只能看见错的东西。
        return None
    return build(event)


#: 字段名一律以 Milky 规范为准（见规范的事件表），不从 OneBot 的名字照搬：
#: OneBot 说 `group_name`，Milky 说 `new_group_name`；OneBot 说 `duration` 的正负，
#: Milky 用显式的 `is_mute`。照搬的结果是客户端读到一个不存在的字段，然后显示空白。


def _member_increase(event: dict[str, Any]) -> dict[str, Any]:
    out = _envelope("group_member_increase", event)
    out["data"] = {
        "group_id": _uin(event.get("group_id")),
        "user_id": _uin(event.get("user_id")),
        "operator_id": _uin_or_zero(event.get("operator_id")),
        # OneBot 的 `group_increase` 不区分邀请人；有就带上，没有就给 0。
        "invitor_id": _uin_or_zero(event.get("invitor_id")),
    }
    return out


def _member_decrease(event: dict[str, Any]) -> dict[str, Any]:
    out = _envelope("group_member_decrease", event)
    out["data"] = {
        "group_id": _uin(event.get("group_id")),
        "user_id": _uin(event.get("user_id")),
        "operator_id": _uin_or_zero(event.get("operator_id")),
    }
    return out


def _admin_change(event: dict[str, Any]) -> dict[str, Any]:
    out = _envelope("group_admin_change", event)
    out["data"] = {
        "group_id": _uin(event.get("group_id")),
        "user_id": _uin(event.get("user_id")),
        "operator_id": _uin_or_zero(event.get("operator_id")),
        "is_set": (event["sub_type"] == "set" if event.get("sub_type") in ("set", "unset")
                   else bool(event.get("is_set", event.get("set")))),
    }
    return out


def _member_mute(event: dict[str, Any]) -> dict[str, Any]:
    out = _envelope("group_mute", event)
    out["data"] = {
        "group_id": _uin(event.get("group_id")),
        "user_id": _uin(event.get("user_id")),
        "operator_id": _uin_or_zero(event.get("operator_id")),
        # 规范里是「秒」，解除禁言就是 0；没有单独的 is_mute 字段。
        "duration": int(event.get("duration") or 0),
    }
    return out


def _whole_mute(event: dict[str, Any]) -> dict[str, Any]:
    out = _envelope("group_whole_mute", event)
    duration = int(event.get("duration") or 0)
    is_mute = event.get("is_mute")
    if is_mute is None:
        # OneBot 用两种方式表达开关：`sub_type` 是 ban/lift_ban，或者 duration 为正。
        # 两者都在时以 sub_type 为准 —— 它是专门表达开关的那个字段。
        sub = str(event.get("sub_type") or "")
        if sub in ("ban", "lift_ban"):
            is_mute = sub == "ban"
        else:
            is_mute = duration > 0
    out["data"] = {
        "group_id": _uin(event.get("group_id")),
        "operator_id": _uin_or_zero(event.get("operator_id")),
        "is_mute": bool(is_mute),
    }
    return out


def _name_change(event: dict[str, Any]) -> dict[str, Any]:
    out = _envelope("group_name_change", event)
    out["data"] = {
        "group_id": _uin(event.get("group_id")),
        "new_group_name": str(event.get("group_name") or ""),
        "operator_id": _uin_or_zero(event.get("operator_id")),
    }
    return out


def _file_upload(event: dict[str, Any]) -> dict[str, Any]:
    """OneBot `notice.group_upload` -> Milky `group_file_upload`。

    OneBot 的文件是一个 `{id, name, size, busid}` 对象；Milky 摊平成四个字段，且**没有**
    busid —— 那是 QQ 自己的业务类型，规范里不表达。
    """
    file = event.get("file") if isinstance(event.get("file"), dict) else {}
    out = _envelope("group_file_upload", event)
    out["data"] = {
        "group_id": _uin(event.get("group_id")),
        "user_id": _uin(event.get("user_id")),
        "file_id": str(file.get("id") or ""),
        "file_name": str(file.get("name") or ""),
        "file_size": int(file.get("size") or 0),
    }
    return out


def _title_change(event: dict[str, Any]) -> dict[str, Any]:
    """群成员专属头衔。Milky 的 `group_member_special_title` 形态是 `{group_id, user_id, title}`。

    空标题是合法值（表示取消），所以不判空。
    """
    out = _envelope("group_member_special_title_change", event)
    out["data"] = {
        "group_id": _uin(event.get("group_id")),
        "user_id": _uin(event.get("user_id")),
        "title": str(event.get("title") or ""),
    }
    return out


def _essence_change(event: dict[str, Any]) -> dict[str, Any]:
    """群精华消息变更。Milky 只有 `message_seq`，没有 OneBot 的 `message_id`。

    OneBot 的精华事件靠 `sub_type=add/delete` 表示方向；Milky 用 `is_set` 布尔。
    """
    out = _envelope("group_essence_message_change", event)
    sub = str(event.get("sub_type") or "add")
    seq = event.get("message_seq")
    out["data"] = {
        "group_id": _uin(event.get("group_id")),
        "message_seq": _uin_or_zero(seq),
        "operator_id": _uin_or_zero(event.get("operator_id")),
        "is_set": sub != "delete",
    }
    return out


def _reaction(event: dict[str, Any]) -> dict[str, Any] | None:
    """表情回应。OneBot 给 `likes: [{emoji_id, count}]`，Milky 只要一个 `face_id` 与 `count`。

    多项时取第一项：Milky 的事件是一条回应，而 OneBot 的 likes 是一组汇总。取第一而不求和，
    因为求和会造出一个"若干人同时回应了同一个表情"的假象。
    """
    likes = event.get("likes") if isinstance(event.get("likes"), list) else []
    first = likes[0] if likes and isinstance(likes[0], dict) else {}
    out = _envelope("group_message_reaction", event)
    out["data"] = {
        "group_id": _uin(event.get("group_id")),
        "user_id": _uin(event.get("user_id")),
        "message_seq": _uin_or_zero(event.get("message_seq")),
        "face_id": str(first.get("emoji_id") or ""),
        # Milky 区分 face 与 emoji；OneBot 不区分，表情回应本身就来自 emoji 接口，报 face。
        "reaction_type": "face",
        "is_add": bool(event.get("is_add", True)),
    }
    return out


def _nudge(event: dict[str, Any]) -> dict[str, Any] | None:
    """OneBot 的 `notice.notify`(poke) -> Milky 的 `friend_nudge` / `group_nudge`。"""
    if str(event.get("sub_type") or "") != "poke":
        return None
    sender = _uin(event.get("user_id"))
    receiver = _uin(event.get("target_id"))
    group = _uin(event.get("group_id"))
    self_id = _uin(event.get("self_id"))
    if group:
        out = _envelope("group_nudge", event)
        out["data"] = {
            "group_id": group,
            "sender_id": sender,
            "receiver_id": receiver,
            # 文案与图片来自包推送的模板参数；灰条路径没有它们，那时是空串。
            "display_action": str(event.get("display_action") or ""),
            "display_suffix": str(event.get("display_suffix") or ""),
            "display_action_img_url": str(event.get("display_action_img_url") or ""),
        }
        return out
    out = _envelope("friend_nudge", event)
    out["data"] = {
        "user_id": sender,
        "is_self_send": bool(self_id and sender == self_id),
        "is_self_receive": bool(self_id and receiver == self_id),
        "display_action": str(event.get("display_action") or ""),
        "display_suffix": str(event.get("display_suffix") or ""),
        "display_action_img_url": str(event.get("display_action_img_url") or ""),
    }
    return out


# ------------------------------------------------------------------- 请求


def _request(event: dict[str, Any]) -> dict[str, Any] | None:
    """OneBot 的 `request.*` -> Milky。

    这里的字段名按 Milky 规范，**不**保留 OneBot 的 `flag`：规范和本仓库的动作层用的是两套
    标识（`notification_seq` / `initiator_uid` 对 `flag`），照搬会把一个不透明字符串塞进
    int64 字段。转换不出的事件返回 ``None``，而不是发一个字段对不上的。
    """
    request_type = str(event.get("request_type") or "")
    if request_type == "friend":
        out = _envelope("friend_request", event)
        initiator = _uin(event.get("user_id"))
        out["data"] = {
            "initiator_id": initiator,
            "initiator_uid": str(event.get("user_id_uid") or ""),
            "comment": str(event.get("comment") or ""),
            "via": str(event.get("via") or ""),
        }
        return out
    if request_type == "group":
        sub = str(event.get("sub_type") or "")
        seq = event.get("notification_seq")
        if sub == "invite":
            # 「他人邀请自身入群」——Milky 的 group_invitation 用自己的序号，不是群通知序号。
            out = _envelope("group_invitation", event)
            out["data"] = {
                "group_id": _uin(event.get("group_id")),
                "invitation_seq": _uin_or_zero(event.get("invitation_seq")),
                "initiator_id": _uin_or_zero(event.get("user_id")),
                "source_group_id": _uin_or_zero(event.get("source_group_id")),
            }
            return out
        out = _envelope("group_join_request", event)
        out["data"] = {
            "group_id": _uin(event.get("group_id")),
            "notification_seq": _uin_or_zero(seq),
            "is_filtered": bool(event.get("is_filtered")),
            "initiator_id": _uin_or_zero(event.get("user_id")),
            "comment": str(event.get("comment") or ""),
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
