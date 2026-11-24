"""Milky 的 65 个 API 名 -> QSICK 已有的内部动作名。

## 这张表是"能直接挪用"的全部内容

Milky 的调用是 `POST /MILKY/api/{api}`，QSICK 的动作是注册表里的 245 个名字。两边做的是同
一件事，名字不同。所以接入一个协议**不需要新写动作逻辑**，只需要一张翻译表加一层参数改名。

## 四类，区别在"失败时说什么"

| 类 | 数量 | 含义 | 调用结果 |
| --- | --- | --- | --- |
| `SUPPORTED` | 静态一对一 | 名字与参数都能对上 | 真正执行 |
| `DYNAMIC` | 目标动作取决于参数 | 比如历史消息要按 `message_scene` 选动作 | 真正执行 |
| `NEEDS_TRANSLATION` | 名字有、参数对不上 | Milky 用 `notification_seq`，内部用 `flag` | **明确失败**并说明差什么 |
| `NO_ACTION` | 连动作名都没有 | QQ 侧没有对应接口 | **明确失败**并说明 |
| `LOCAL` | 适配器自己回答 | 协议端自身信息 | 本地返回 |

把后三类也硬翻过去，客户端会拿到一个成功的响应却什么都没发生——那比"不支持"坏得多。所以
它们**都是失败**，区别只在失败信息说清是哪一层缺。

## 参数改名

两边的参数大体一致，差异集中在这几处：

* `message`：Milky 的段是 `{type, ...}`，内部是 OneBot 段。适配器里转。
* 文件上传：Milky 用 `file_uri` / `file_name` / `parent_folder_id`，内部用 `file` / `name` / `folder`。
* 表情回应：Milky 用 `reaction`，内部用 `emoji_id`。
* 头像：Milky 用 `uri`，内部用 `file`。

## 名字的命名空间

Milky 与内部的 API 名都是扁平的。表用两个 dict 双向存；查不到就抛 `UnknownAction`——
**不猜近似的名字**。猜错的结果是动作作用在别的对象上，比报错危险得多。
"""

from __future__ import annotations

from typing import Any, Callable

from ..base import PartError


class UnknownAction(PartError):
    """Milky 收到了一个这张表不认识（或已知不可用）的调用。"""

    def __init__(self, api: str, reason: str = "") -> None:
        self.api = api
        self.reason = reason
        super().__init__(f"unsupported Milky API {api!r}" + (f": {reason}" if reason else ""))


#: Milky API -> 内部动作名，静态一对一。
SUPPORTED: dict[str, str] = {
    # ---------------------------------------------------------------- 系统（13）
    "get_login_info": "get_login_info",
    "get_user_profile": "get_stranger_info",
    "get_friend_list": "get_friend_list",
    "get_friend_info": "get_stranger_info",
    "get_group_list": "get_group_list",
    "get_group_info": "get_group_info",
    "get_group_member_list": "get_group_member_list",
    "get_group_member_info": "get_group_member_info",
    "set_avatar": "set_qq_avatar",
    "set_nickname": "set_qq_profile",
    "set_bio": "set_qq_profile",
    "get_custom_face_url_list": "fetch_custom_face",
    "get_cookies": "get_cookies",
    "get_csrf_token": "get_csrf_token",
    # ---------------------------------------------------------------- 消息（5）
    "send_private_message": "send_private_msg",
    "send_group_message": "send_group_msg",
    "recall_private_message": "delete_msg",
    "recall_group_message": "delete_msg",
    "get_message": "get_msg",
    "get_forwarded_messages": "get_forward_msg",
    # ---------------------------------------------------------------- 好友（4）
    "send_friend_nudge": "send_poke",
    "send_profile_like": "send_like",
    "delete_friend": "delete_friend",
    # ---------------------------------------------------------------- 群聊（17）
    "set_group_name": "set_group_name",
    "set_group_avatar": "set_group_portrait",
    "set_group_member_card": "set_group_card",
    "set_group_member_special_title": "set_group_special_title",
    "set_group_member_admin": "set_group_admin",
    "set_group_member_mute": "set_group_ban",
    "set_group_whole_mute": "set_group_whole_ban",
    "kick_group_member": "set_group_kick",
    "get_group_announcements": "_get_group_notice",
    "send_group_announcement": "_send_group_notice",
    "delete_group_announcement": "_del_group_notice",
    "get_group_essence_messages": "get_essence_msg_list",
    "set_group_essence_message": "set_essence_msg",
    "quit_group": "set_group_leave",
    "send_group_message_reaction": "set_msg_emoji_like",
    "send_group_nudge": "group_poke",
    # ---------------------------------------------------------------- 文件（9）
    "upload_private_file": "upload_private_file",
    "upload_group_file": "upload_group_file",
    "get_private_file_download_url": "get_private_file_url",
    "get_group_file_download_url": "get_group_file_url",
    "get_group_files": "get_group_root_files",
    "move_group_file": "move_group_file",
    "rename_group_file": "rename_group_file",
    "delete_group_file": "delete_group_file",
    "create_group_folder": "create_group_file_folder",
    "delete_group_folder": "delete_group_folder",
}

#: 内部动作名 -> Milky API。一个内部名可能对应多个 API（`delete_msg` 对应私聊与群聊两条），
#: 取第一条——反过来只用于"这个内部动作在 Milky 里叫什么"的展示，不需要精确。
REVERSE: dict[str, str] = {}
for _api, _name in SUPPORTED.items():
    REVERSE.setdefault(_name, _api)


# --------------------------------------------------------------------- 动态


def _history(params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """`get_history_messages` -> 群历史或私聊历史，取决于 `message_scene`。

    Milky 用一个 API 覆盖三种会话，内部按会话分成两个动作。这是"目标动作取决于参数"的唯一
    一类，所以用一个函数而不是表项。
    """
    scene = str(params.get("message_scene") or "group")
    peer = params.get("peer_id")
    body: dict[str, Any] = {
        # Milky 的 `start_message_seq` 是"从哪条开始"，内部叫 `message_seq`。
        "message_seq": int(params.get("start_message_seq") or 0),
        "count": int(params.get("limit") or 20),
    }
    if scene == "group":
        body["group_id"] = peer
        return "get_group_msg_history", body
    # 私聊与临时会话都落到好友历史：临时会话在 OneBot 里没有独立的动作。
    body["user_id"] = peer
    return "get_friend_msg_history", body


def _mark_read(params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """`mark_message_as_read` -> 已读回执。

    Milky 带 `message_scene` 与 `peer_id`，内部只认 `message_id`（QQ 的已读回执按会话走，
    由动作自己补 peer）。所以这里把能给的给过去，多的丢掉。
    """
    body: dict[str, Any] = {}
    for source, target in (("message_seq", "message_id"), ("message_id", "message_id")):
        if params.get(source) is not None and target not in body:
            body[target] = params[source]
    if params.get("peer_id") is not None:
        scene = str(params.get("message_scene") or "group")
        body["group_id" if scene == "group" else "user_id"] = params["peer_id"]
    return "mark_msg_as_read", body


def _resource_url(params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """`get_resource_temp_url` -> 文件下载链接，按是否有 `group_id` 分左右。"""
    body = {"file_id": params.get("resource_id") or params.get("file_id")}
    if params.get("group_id") is not None:
        body["group_id"] = params["group_id"]
        return "get_group_file_url", body
    if params.get("user_id") is not None:
        body["user_id"] = params["user_id"]
    return "get_private_file_url", body


#: Milky API -> 解析函数。函数返回 `(内部动作名, 内部参数)`。
DYNAMIC: dict[str, Callable[[dict[str, Any]], tuple[str, dict[str, Any]]]] = {
    "get_history_messages": _history,
    "mark_message_as_read": _mark_read,
    "get_resource_temp_url": _resource_url,
}


# ------------------------------------------------------------------ 参数改名

#: 每次调用要做的改名，按 API 分开写。
#:
#: 全局放在一起会串味：`file_name` 在文件上传里要变 `name`，在文件夹里不该动。所以按 API 分组。
RENAMES: dict[str, tuple[tuple[str, str], ...]] = {
    "set_avatar": (("uri", "file"),),
    "set_nickname": (("new_nickname", "nickname"),),
    "set_bio": (("new_bio", "personal_note"),),
    "set_group_avatar": (("uri", "file"),),
    "set_group_member_card": (("card", "card"),),
    "set_group_member_special_title": (("special_title", "special_title"),),
    "set_group_member_mute": (("user_id", "user_id"),),
    "kick_group_member": (("user_id", "user_id"),),
    "set_group_essence_message": (("message_seq", "message_id"),),
    "upload_private_file": (("file_uri", "file"), ("file_name", "name")),
    "upload_group_file": (("file_uri", "file"), ("file_name", "name"),
                          ("parent_folder_id", "folder")),
    "get_group_files": (("parent_folder_id", "folder_id"),),
    "move_group_file": (("parent_folder_id", "folder_id"),
                        ("target_folder_id", "target_folder_id")),
    "create_group_folder": (("folder_name", "name"),),
    "get_group_file_download_url": (("file_id", "file_id"),),
    "get_resource_temp_url": (("resource_id", "file_id"),),
    "send_group_message_reaction": (("reaction", "emoji_id"), ("is_add", "set")),
    "send_friend_nudge": (("user_id", "user_id"), ("target_id", "user_id")),
    "send_group_nudge": (("user_id", "user_id"), ("target_id", "user_id")),
    "send_profile_like": (("user_id", "user_id"), ("target_id", "user_id")),
}

#: 显式 `null` 表示"不设置"。内部动作把 `None` 当成"没给"更安全，所以删掉而不是传下去。
_DROP_WHEN_NONE = ("duration", "reason", "card", "special_title", "folder_id",
                   "target_folder_id", "name", "folder", "personal_note", "nickname")


# ------------------------------------------------------- 名字有、参数对不上

#: 这些 API 有内部动作名，但两边的**参数模型**不是同一个东西，硬翻会让动作作用在错的对象上。
#:
#: 典型是请求处理：Milky 用 `notification_seq` + `initiator_uid`，内部用 OneBot 的 `flag`——
#: 一个不透明的字符串，由平台在推送时生成。两者之间没有换算关系，只能等某一侧的映射表。
NEEDS_TRANSLATION: dict[str, str] = {
    "accept_friend_request": "Milky 用 initiator_uid，内部用 OneBot 的 flag（不透明字符串），没有换算关系",
    "reject_friend_request": "同上：initiator_uid 与 flag 不是同一个东西",
    "accept_group_request": "Milky 用 notification_seq，内部用 flag，没有换算关系",
    "reject_group_request": "同上：notification_seq 与 flag 不是同一个东西",
    "accept_group_invitation": "同上",
    "reject_group_invitation": "同上",
    "get_friend_requests": "返回值形状不同：内部按 flag 组织，Milky 要 initiator_uid",
    "get_group_notifications": "返回值形状不同：内部按 flag 组织，Milky 要 notification_seq",
}


# ------------------------------------------------------- 连动作名都没有的

#: 内部连动作名都没有，因为 QQ 侧没有对应接口。
NO_ACTION: dict[str, str] = {
    "persist_group_file": "QQ 侧没有转存永久文件的接口",
    "rename_group_folder": "宿主只有 group.folder.create / delete，没有 rename",
}

#: 明确**不实现**的一组。
#:
#: 与 `NO_ACTION` 的区别是态度而不是原因：`NO_ACTION` 是"QQ 没有这个能力，也许将来会有"，
#: 这里是"这条能力不在本程序范围内"。分开记的理由是失败信息要能如实回答调用方：
#: 前者的答案是"底层没做"，后者的答案是"本程序没有这个 API"。
UNSUPPORTED_BY_DESIGN: dict[str, str] = {
    "get_peer_pins": "本程序不提供会话置顶能力，没有对应 API",
    "set_peer_pin": "本程序不提供会话置顶能力，没有对应 API",
}


# ------------------------------------------------------------- 适配器自己回答

#: 不需要经过动作层的：协议端自身的元信息。
#:
#: 这类 API 问的是"你是哪个协议端、支持什么"，答案在本进程里，去问 QQ 反而错了。
LOCAL: dict[str, str] = {
    "get_impl_info": "协议端自身信息",
}


def canonical(api: str) -> str:
    """Milky API 名 -> 内部动作名。不认识就抛，不猜。"""
    name = str(api or "").strip()
    if name in SUPPORTED:
        return SUPPORTED[name]
    if name in DYNAMIC:
        return DYNAMIC[name].__name__
    if name in NEEDS_TRANSLATION:
        raise UnknownAction(name, NEEDS_TRANSLATION[name])
    if name in NO_ACTION:
        raise UnknownAction(name, NO_ACTION[name])
    if name in UNSUPPORTED_BY_DESIGN:
        raise UnknownAction(name, UNSUPPORTED_BY_DESIGN[name])
    if name in LOCAL:
        raise UnknownAction(name, "由适配器本地回答，不经过动作层")
    raise UnknownAction(name, "不是 Milky 规范里的 API，或本适配器未收录")


def api_for(action: str) -> str:
    """内部动作名 -> Milky API 名。用于应答本来要带调用名的地方。"""
    return REVERSE.get(str(action or "").strip(), "")


def resolve(api: str, params: dict | None) -> tuple[str, dict[str, Any]]:
    """Milky 调用 -> `(内部动作名, 内部参数)`。

    这是适配器 `decode_action` 的唯一实现，四类都在这里分流：静态表、动态解析、明确拒绝。
    拒绝时抛 :class:`UnknownAction`，失败信息说清是哪一层缺。
    """
    name = str(api or "").strip()
    body = dict(params or {})
    body.pop("access_token", None)
    for key in _DROP_WHEN_NONE:
        if key in body and body[key] is None:
            del body[key]

    if name in DYNAMIC:
        return DYNAMIC[name](body)
    if name not in SUPPORTED:
        # 复用 canonical 的拒绝逻辑，错误信息只写一处。
        canonical(name)
        raise UnknownAction(name, "不是 Milky 规范里的 API")

    for source, target in RENAMES.get(name, ()):
        if source != target and source in body and target not in body:
            body[target] = body.pop(source)
    return SUPPORTED[name], body


def encode_params(action: str, params: dict | None) -> dict:
    """内部参数 -> Milky 请求体（反向，给 WebUI 的动作测试器用）。"""
    body = dict(params or {})
    api = api_for(action)
    for source, target in RENAMES.get(api, ()):
        if target != source and target in body and source not in body:
            body[source] = body.pop(target)
    return body
