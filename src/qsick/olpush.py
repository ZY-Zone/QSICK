"""OLPush 包推送解码：`contentHead.type` 分派 → OneBot v11 事件。

## 这条通道是什么

QQ 的**另一条**推送路径。灰条走的是"收到一条消息"，包推送走的是
`trpc.msg.olpush.OlPushService.MsgPush`——一个 protobuf 帧，内层再按 `contentHead.type` 分派。
被踢出群、别人入群、管理员任免、好友申请、入群申请只在这里出现，灰条里没有。

## 字段定义从哪来

不是猜的。三份独立实现互相印证到字段号：

* NapCat `packages/napcat-core/packet/transformer/proto/message/message.ts`——`PushMsgBody`、
  `ContentHead`、`GroupChange`、`GroupChangeInfo`、`GroupAdmin`、`GroupInvite` 的字段号与类型；
* Lagrange `Internal/Packets/Notify/*.cs`——同一批结构的 `[ProtoMember(n)]` 标号，以及
  `FriendRequest` / `GroupJoin` / `Event0x20D` / `GroupReaction` 的定义；
* Lagrange `Internal/Logic/PushLogic.cs` 的 `MsgType` 枚举，给出 type 与事件的对应关系。

三份实现对 `GroupChange` 的字段号完全一致（1 groupUin, 2 flag, 3 memberUid, 4 decreaseType,
5 operatorInfo, 6 increaseType, 7 其它），这是可以照着实现的依据。

## 当前状态

**解码器已完整、已测；还没有接线。** 喂它一个帧就能得到事件，但把帧拿进来需要 QQ 侧的一个
包钩子，那条路（`native/qsick-payload`）今天还是脚手架。见 `docs/headless-events.md`。

33/34/44/84/85/87 已用 QQ 9.9.33-52230 的实抓帧核对（2026-11-24，23:37:30–23:42:14，39 帧）。
实抓改写了三处认知：

* 34 的 `operatorInfo` **全部**是内嵌 protobuf（包括 131 他人被踢）；33 的是纯 uid 字符串；
* 群主解散群时，解散者自己收到的是 **34 / decreaseType 1**（参考实现只知道 129）；
* 本号经管理员批准 / 接受邀请入群时发的是 **85（SelfJoinInGroup）**，33 不发。

decreaseType 2（本号自己退群）来自上一轮抓包，但那份文件已丢失、本轮 39 帧里没有复现，暂按
待复核处理（映射保留，见 :data:`CHANGE_TYPE_SELF_LEFT`）。

## 为什么解码不放在 DLL 里

protobuf 解码是纯计算，放在 Python 里可以直接用合成帧测试；放进 C++ 就只能靠真实 QQ 验证，
而那段代码现在没法验证。同理，事件映射（type → cmd → 字段改名）也在这一侧，因为改一版协议
不该需要重新编译 DLL。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: `contentHead.type`。名字与取值来自 Lagrange 的 `MsgType`，与 NapCat 的分派一致。
TYPE_GROUP_MEMBER_INCREASE = 33
TYPE_GROUP_MEMBER_DECREASE = 34
TYPE_GROUP_ADMIN = 44
TYPE_GROUP_MESSAGE = 82
TYPE_GROUP_SELF_JOINED = 85
TYPE_GROUP_JOIN_NOTIFICATION = 84
TYPE_GROUP_INVITE = 87
TYPE_EVENT_0X20D = 525
TYPE_EVENT_0X210 = 528
TYPE_EVENT_0X2DC = 732

#: `GroupChange` 的 4 号字段。**同一个字段号在 33 与 34 上语义相反**，这是最容易写错的地方：
#:
#:     33 (成员增加)  increaseType: 130 = 管理员批准, 131 = 被邀请
#:     34 (成员减少)  decreaseType: 1 = 群主解散（实抓）, 2 = 本号主动退群（上一轮抓包，待复核）,
#:                                  3 = 本号被踢, 129 = 群解散（参考实现）, 130 = 他人退群,
#:                                  131 = 他人被踢
#:
#: 3/130/131 三份参考实现一致（Lagrange 的 `DecreaseType`、NapCat 的
#: `groupChangDecreseType2String`、SnowLuma 的 leave decoder），129 来自 NapCat 与 SnowLuma。
#: 1 是 2026-11-24 在 9.9.33-52230 上实抓到的：本号作为群主解散群时 memberUid 与 operatorInfo
#: 里都是本号（member == operator == self），参考实现都没有这个值，会落进默认分支当成 kick。
#: 2 来自 2026-11-22 上一轮抓包（本号自己退群时 operatorInfo 是空的 `{2: {1: 0}}`），但那份抓包
#: 文件已丢失、2026-11-24 的 39 帧里没有复现，所以先保留映射、结论待复核。
CHANGE_TYPE_DISSOLVED_BY_OWNER = 1
CHANGE_TYPE_SELF_LEFT = 2
CHANGE_TYPE_SELF_REMOVED = 3
CHANGE_TYPE_DISBANDED = 129
CHANGE_TYPE_LEFT = 130
CHANGE_TYPE_INVITED = 131

#: 本号入群后多少秒内，同群的 87 视为"入群的余波"而不是待审批的邀请。2026-11-24 实抓：33 到
#: 同群 87 相隔 429 ms；取 30 秒留出推送乱序与延迟的余量。33（群主直接拉人）与 85（批准 /
#: 接受邀请）都算"本号刚入群"。
SELF_JOIN_INVITE_WINDOW = 30.0

#: `Event0x210` 的 `subType`。
SUB_FRIEND_RECALL = 138
SUB_FRIEND_REQUEST = 35

#: `Event0x2DC` 的 `subType`。
SUB_GROUP_REACTION = 16
SUB_GROUP_RECALL = 17
SUB_GROUP_NUDGE = 20

#: 732 内层 NotifyMessageBody.sub_type。与外层 contentHead.subType 不是一回事：外层只是
#: 群提示这个分类，真正的类型在内层。Lagrange 的 GroupReactionProcessor 判 35、
#: GroupNudgeProcessor 判 19。
NOTIFY_SUB_GROUP_NUDGE = 19
NOTIFY_SUB_GROUP_REACTION = 35

#: GeneralGrayTip.busiType。12 = 戳一戳（Lagrange GroupNudgeProcessor）。
BUSI_POKE_GRAYTIP = 12


class DecodeError(ValueError):
    """帧不是合法的 protobuf，或缺少必需字段。"""


# --------------------------------------------------------------------- protobuf
#
# 一个最小的 decoder：只支持这里用到的两种 wire type —— varint（0）与 length-delimited（2）。
# 不引入依赖的理由是这个文件只需要这两样，而多一个库就多一个版本约束。

def _varint(data: bytes, offset: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if offset >= len(data):
            raise DecodeError("truncated varint")
        byte = data[offset]
        offset += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, offset
        shift += 7
        if shift > 63:
            raise DecodeError("varint too long")


def decode_fields(data: bytes) -> dict[int, list[Any]]:
    """`{field_number: [values]}`。varint 给 int，length-delimited 给 bytes。"""
    fields: dict[int, list[Any]] = {}
    offset = 0
    size = len(data)
    while offset < size:
        key, offset = _varint(data, offset)
        number = key >> 3
        wire = key & 0x07
        if wire == 0:
            value, offset = _varint(data, offset)
        elif wire == 2:
            length, offset = _varint(data, offset)
            if offset + length > size:
                raise DecodeError("truncated length-delimited field")
            value = data[offset:offset + length]
            offset += length
        elif wire == 5:
            if offset + 4 > size:
                raise DecodeError("truncated fixed32")
            value = data[offset:offset + 4]
            offset += 4
        elif wire == 1:
            if offset + 8 > size:
                raise DecodeError("truncated fixed64")
            value = data[offset:offset + 8]
            offset += 8
        else:
            raise DecodeError(f"unsupported wire type {wire}")
        fields.setdefault(number, []).append(value)
    return fields


def _int(fields: dict[int, list[Any]], number: int, default: int = 0) -> int:
    values = fields.get(number)
    if not values:
        return default
    value = values[0]
    return value if isinstance(value, int) else default


def _bytes(fields: dict[int, list[Any]], number: int) -> bytes:
    values = fields.get(number)
    if not values:
        return b""
    value = values[0]
    return value if isinstance(value, bytes) else b""


def _text(fields: dict[int, list[Any]], number: int) -> str:
    return _bytes(fields, number).decode("utf-8", "replace")


# ---------------------------------------------------------------- 内层结构


@dataclass(slots=True)
class ContentHead:
    """`ContentHead`（NapCat `message.ts` / Lagrange `ContentHead`）。"""

    type: int = 0
    sub_type: int = 0
    c2c_cmd: int = 0
    sequence: int = 0


@dataclass(slots=True)
class GroupChange:
    """`GroupChange`：33/34 的内层。字段号两份实现一致。"""

    group_uin: int = 0
    flag: int = 0
    member_uid: str = ""
    #: 33 上它是 `increaseType`，34 上它是 `decreaseType`；同一个字段号，语义由外层 type 决定。
    change_type: int = 0
    #: `operatorInfo`。**不是**总是字符串：被踢时它是内嵌的 `GroupChangeInfo` protobuf。
    operator_raw: bytes = b""


@dataclass(slots=True)
class GroupAdmin:
    """`GroupAdmin`：44 的内层。`body.extraEnable` / `extraDisable` 二选一。"""

    group_uin: int = 0
    admin_uid: str = ""
    is_set: bool = False


@dataclass(slots=True)
class GroupInvite:
    """`GroupInvite`：`Event0x20D` 的内层。"""

    group_uin: int = 0
    inviter_uid: str = ""
    target_uid: str = ""


@dataclass(slots=True)
class GroupJoin:
    """`GroupJoin`：84 的内层。"""

    group_uin: int = 0
    join_type: int = 0
    target_uid: str = ""
    comment: str = ""


@dataclass(slots=True)
class FriendRequest:
    """`FriendRequest`：`Event0x210` subType 35 的内层。"""

    source_uid: str = ""
    target_uid: str = ""
    message: str = ""
    source: str = ""


@dataclass(slots=True)
class GroupNudge:
    """`GroupRecallNudge`：`Event0x2DC` subType 20 的内层。"""

    operator_uid: str = ""
    group_uin: int = 0
    busi_id: int = 0


@dataclass(slots=True)
class GroupReaction:
    """`GroupReaction`：`Event0x2DC` subType 16 的内层。"""

    group_uin: int = 0
    sequence: int = 0
    code: str = ""
    operator_uid: str = ""
    #: 1 = 添加，2 = 移除。
    reaction_type: int = 0
    count: int = 0
    is_add: bool = True


@dataclass(slots=True)
class NotifyMessageBody:
    """732 的内层。字段号来自 Lagrange 的 `NotifyMessageBody`。

    732 的 `msgContent` **不是**直接一个 protobuf：它是「4 字节群号（大端）+ 1 字节未知 +
    2 字节大端长度 + 长度那么多字节的 protobuf」。NapCat 的做法是 `msgContent.slice(7)` 直接解
    剩下的部分，Lagrange 是 skip(5) 再读 Int16 长度——两者对"正文从第 7 字节开始"这一点一致。
    这里用后者：带长度校验，截断的帧会明确失败而不是解出半个事件。
    """

    sub_type: int = 0
    group_uin: int = 0
    operator_uid: str = ""
    msg_sequence: int = 0
    tips_seq_id: int = 0
    reaction: "GroupReaction | None" = None
    nudge_params: dict[str, str] = field(default_factory=dict)
    nudge_busi_type: int = 0


def unwrap_0x2dc(data: bytes) -> bytes:
    """732 的 `msgContent` -> 内层 protobuf 字节。"""
    if len(data) < 7:
        raise DecodeError("0x2DC payload is shorter than its framing")
    length = int.from_bytes(data[5:7], "big")
    body = data[7:7 + length]
    if len(body) != length:
        raise DecodeError("0x2DC inner length does not match the payload")
    return body


def decode_notify_message_body(data: bytes) -> NotifyMessageBody:
    fields = decode_fields(data)
    body = NotifyMessageBody(
        sub_type=_int(fields, 13),
        group_uin=_int(fields, 4),
        operator_uid=_text(fields, 21),
        msg_sequence=_int(fields, 37),
        tips_seq_id=_int(fields, 50),
    )
    reaction_bytes = _bytes(fields, 44)
    if reaction_bytes:
        body.reaction = decode_group_reaction(reaction_bytes)
    gray_tip = decode_fields(_bytes(fields, 26))
    if gray_tip:
        body.nudge_busi_type = _int(gray_tip, 1)
        params: dict[str, str] = {}
        for entry in gray_tip.get(7, []) or []:
            if not isinstance(entry, bytes):
                continue
            pair = decode_fields(entry)
            name = _text(pair, 1)
            if name:
                params[name] = _text(pair, 2)
        body.nudge_params = params
    return body


@dataclass(slots=True)
class GroupRecall:
    """`GroupRecall`：`Event0x2DC` subType 17 的内层。"""

    group_uin: int = 0
    sequence: int = 0
    operator_uid: str = ""


@dataclass(slots=True)
class Frame:
    """解出来的一帧。`inner` 是按 type 选的那个结构，`body` 是未解析的内层字节。"""

    type: int
    sub_type: int
    sequence: int
    c2c_cmd: int = 0
    body: bytes = b""
    inner: Any = None
    kind: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


# ------------------------------------------------------------------ 解码入口


def decode_content_head(data: bytes) -> ContentHead:
    fields = decode_fields(data)
    return ContentHead(
        type=_int(fields, 1),
        sub_type=_int(fields, 2),
        c2c_cmd=_int(fields, 3),
        sequence=_int(fields, 5),
    )


def decode_group_change(data: bytes) -> GroupChange:
    fields = decode_fields(data)
    return GroupChange(
        group_uin=_int(fields, 1),
        flag=_int(fields, 2),
        member_uid=_text(fields, 3),
        change_type=_int(fields, 4),
        operator_raw=_bytes(fields, 5),
    )


def decode_group_admin(data: bytes) -> GroupAdmin:
    fields = decode_fields(data)
    body = decode_fields(_bytes(fields, 4))
    # `GroupAdminBody`: 1 = extraDisable, 2 = extraEnable. Whichever is present decides the direction.
    enabled = _bytes(body, 2)
    disabled = _bytes(body, 1)
    extra = decode_fields(enabled) if enabled else decode_fields(disabled)
    return GroupAdmin(
        group_uin=_int(fields, 1),
        admin_uid=_text(extra, 1),
        is_set=bool(enabled),
    )


def decode_group_invite(data: bytes) -> GroupInvite:
    """84/87 共用：NapCat 的 `GroupInvite` 是扁平的，Lagrange 的包了一层 `body`。

    两种形状都认：先按扁平读，读不到 group_uin 再按包一层读。规范没统一，实现之间也不一致，
    两种都试比选一个更安全。
    """
    fields = decode_fields(data)
    flat = GroupInvite(
        group_uin=_int(fields, 1),
        inviter_uid=_text(fields, 5),
        target_uid=_text(fields, 6),
    )
    if flat.group_uin:
        return flat
    inner = decode_fields(_bytes(fields, 1))
    return GroupInvite(
        group_uin=_int(inner, 1),
        inviter_uid=_text(inner, 6),
        target_uid=_text(inner, 5),
    )


def decode_group_join(data: bytes) -> GroupJoin:
    """`GroupJoin`（84）：2026-11-24 实抓帧是
    `{1 群, 2=1（Lagrange 把它叫 Type）, 3 申请人 uid, 4=30（参考实现都没有命名）, 5 附言, 6, 8}`。

    这里按 Lagrange 的字段表读 2 号作 `join_type`；4 号的值（30）目前没有可靠语义，先不解 ——
    事件映射只用到群号、申请人与附言。
    """
    fields = decode_fields(data)
    return GroupJoin(
        group_uin=_int(fields, 1),
        join_type=_int(fields, 2),
        target_uid=_text(fields, 3),
        comment=_text(fields, 5),
    )


def decode_friend_request(data: bytes) -> FriendRequest:
    fields = decode_fields(data)
    info = decode_fields(_bytes(fields, 1))
    return FriendRequest(
        source_uid=_text(info, 2),
        target_uid=_text(info, 1),
        message=_text(info, 10),
        source=_text(info, 11),
    )


def decode_group_nudge(data: bytes) -> GroupNudge:
    fields = decode_fields(data)
    return GroupNudge(
        operator_uid=_text(fields, 1),
        group_uin=_int(fields, 3),
        busi_id=_int(fields, 4),
    )


def decode_group_reaction(data: bytes) -> GroupReaction:
    """`GroupReactionData` 的嵌套是 1-1-2/3。

    形状（Lagrange 的 `GroupReactionData0/1/2` 与 NapCat 的 `GroupReactionData` 一致）：
    `{1: {1: {2: {1: seq}, 3: {1: code, 3: count, 4: operatorUid, 5: type}}}}`
    """
    outer = decode_fields(data)
    level1 = decode_fields(_bytes(outer, 1))
    level2 = decode_fields(_bytes(level1, 1))
    target = decode_fields(_bytes(level2, 2))
    content = decode_fields(_bytes(level2, 3))
    reaction_type = _int(content, 5)
    return GroupReaction(
        sequence=_int(target, 1),
        code=_text(content, 1),
        count=_int(content, 3),
        operator_uid=_text(content, 4),
        reaction_type=reaction_type,
        # 1 = add, 2 = remove (Lagrange's comment). Absent means add: the field showed up later.
        is_add=reaction_type != 2,
    )


def decode_group_recall(data: bytes) -> GroupRecall:
    """`GroupRecall`：`{3: {1: seq, ...}, 4: {1: uin}, ...}`（NapCat `GroupRecallMsg`）。

    这里只需要序列号与群号，其余字段留着不解——它们是撤回侧才用得到的。
    """
    fields = decode_fields(data)
    group_uin = _int(fields, 2)
    seq_fields = decode_fields(_bytes(fields, 3))
    return GroupRecall(group_uin=group_uin, sequence=_int(seq_fields, 1))


def decode_event_0x20d(data: bytes) -> tuple[int, bytes]:
    """`Event0x20D`：`{1: subType, 2: body}`。`subType == 87` 才是群邀请。"""
    fields = decode_fields(data)
    return _int(fields, 1), _bytes(fields, 2)


def decode_push(data: bytes) -> Frame:
    """一帧 `PushMsg` -> :class:`Frame`。

    `PushMsg` 是 `{1: PushMsgBody}`，`PushMsgBody` 是 `{1: ResponseHead, 2: ContentHead, 3: MessageBody}`，
    `MessageBody` 是 `{1: RichText, 2: msgContent, 3: msgEncryptContent}`。这里只取 `contentHead` 与
    `msgContent`：前者决定分派，后者是内层结构。
    """
    push = decode_fields(data)
    body = decode_fields(_bytes(push, 1))
    head = decode_content_head(_bytes(body, 2))
    message_body = decode_fields(_bytes(body, 3))
    content = _bytes(message_body, 2)
    frame = Frame(type=head.type, sub_type=head.sub_type, sequence=head.sequence,
                  c2c_cmd=head.c2c_cmd, body=content)
    return frame


def frame_identity(data: bytes) -> dict[str, str]:
    """从一帧 `PushMsg` 的 `ResponseHead` 里读身份字段。

    实抓（9.9.33-52230）里 `ResponseHead` 是
    `{1 fromUin, 2 fromUid, 3 type, 4 sigMap, 5 toUin, 6 toUid, ...}`：字段 5/6 是**本号**
    （收到这一帧的账号），字段 1/2 是这条推送的主体——普通消息里是发送者，群事件里是群号。

    本号身份在每一帧里都带着，所以即使还没收到过本号的消息、uid→uin 表还是冷的，也能在解码
    之前先把这一条补进去。否则"主体是本号"的事件（33 本号被拉入、85 本号进群、44 给本号设
    管理员、3 本号被踢）会因解析不出 uin 被丢弃，而紧随 33/85 之后的 87 余波还会被误报成一条
    待审批的入群邀请。

    返回的键可能缺省（空串）：`self_uin` / `self_uid` / `from_uin` / `from_uid`。群事件的
    `fromUid` 是群号字符串（不是 `u_` 开头的 uid），此时 `from_uid` 为空——那是群号，不能当
    用户 uid 记进映射表。
    """
    try:
        push = decode_fields(data)
        body = decode_fields(_bytes(push, 1))
        head = decode_fields(_bytes(body, 1))
    except (DecodeError, ValueError):
        return {}
    self_uin = _int(head, 5)
    self_uid = _text(head, 6)
    from_uin = _int(head, 1)
    from_uid = _text(head, 2)
    return {
        "self_uin": str(self_uin) if self_uin else "",
        "self_uid": self_uid if self_uid.startswith("u_") else "",
        "from_uin": str(from_uin) if from_uin else "",
        "from_uid": from_uid if from_uid.startswith("u_") else "",
    }


# ---------------------------------------------------------------- 事件映射


def decode_operator(operator_raw: bytes) -> str:
    """`GroupChange.operatorInfo` -> 操作人 uid。

    这个字段**不是**稳定的字符串。2026-11-24 的实抓里：33 的操作人是纯 UTF-8 的 `u_...`；
    **34 的操作人全部是内嵌的 protobuf** `GroupChangeInfo {1: {1: operatorUid, 2: 6, 3: <bytes>,
    4: 0}}`——不只是"自己被踢"（decreaseType 3），131 他人被踢也一样。NapCat 靠「以 0a 开头就当
    protobuf」来猜，这里改成按结构判断：能解出合法 uid 就用，否则按字符串读。

    猜错的后果是操作人变成一串乱码，然后被当成 uid 解析成 0 —— 一个静默的错账号。所以两条路
    都要验：protobuf 解出来的 uid 必须以 `u_` 开头，字符串分支必须全是可打印字符。
    """
    if not operator_raw:
        return ""
    try:
        outer = decode_fields(operator_raw)
        inner = decode_fields(_bytes(outer, 1))
        candidate = _text(inner, 1)
        if candidate.startswith("u_"):
            return candidate
    except (DecodeError, ValueError):
        pass
    try:
        text = operator_raw.decode("utf-8")
    except UnicodeDecodeError:
        return ""
    if text and all(32 <= ord(char) <= 126 for char in text):
        return text
    return ""


class PushEventMapper:
    """一帧 OLPush -> 0 个或多个 OneBot v11 事件。

    形状与 `onebot/events.py` 保持一致（`post_type` / `notice_type` / `request_type`），所以两种
    协议层不需要知道事件是从灰条还是从包里来的 —— 它们只看 `cmd` 与字段。

    `resolve_uin(uid)` 把 QQ 的 uid 映射成号。**解析不出来时返回空事件**，而不是写 0：OneBot 的
    `user_id` 是 QQ 号，0 不是一个用户。
    """

    def __init__(self, resolve_uin, *, self_uin: str = "", now=None,
                 self_joins: dict[int, float] | None = None) -> None:
        self._resolve = resolve_uin
        self._self_uin = str(self_uin or "")
        self._now = now
        #: 群号 -> 本号经 33 入群的时刻。跨帧的状态：调用方每帧新建 mapper 时要传同一个 dict
        #: 进来，否则 87 的去重不生效（见 `_invite`）。
        self._self_joins = self_joins if self_joins is not None else {}

    def _uin(self, uid: str) -> int:
        if not uid:
            return 0
        resolved = str(self._resolve(uid) or "")
        if resolved.isdigit():
            return int(resolved)
        # Some payloads put the uin where a uid would be.
        return int(uid) if uid.isdigit() else 0

    def _time(self) -> int:
        if self._now is not None:
            return int(self._now())
        import time as _time

        return int(_time.time())

    def _envelope(self, post_type: str, kind_field: str, kind: str) -> dict[str, Any]:
        return {
            "post_type": post_type,
            kind_field: kind,
            "time": self._time(),
            "self_id": int(self._self_uin) if self._self_uin.isdigit() else 0,
        }

    def _notice(self, notice_type: str) -> dict[str, Any]:
        return self._envelope("notice", "notice_type", notice_type)

    def _request(self, request_type: str, sub_type: str) -> dict[str, Any]:
        out = self._envelope("request", "request_type", request_type)
        out["sub_type"] = sub_type
        return out

    # ------------------------------------------------------------------ 分派

    def map_bytes(self, data: bytes) -> list[dict[str, Any]]:
        """原始帧 -> 事件。解不出来时返回空表而不是抛：一帧坏包不该断掉整条通道。"""
        try:
            return self.map(decode_push(data))
        except (DecodeError, ValueError):
            return []

    def map(self, frame: Frame) -> list[dict[str, Any]]:
        handler = {
            TYPE_GROUP_MEMBER_INCREASE: self._member_increase,
            TYPE_GROUP_MEMBER_DECREASE: self._member_decrease,
            TYPE_GROUP_SELF_JOINED: self._self_joined,
            TYPE_GROUP_ADMIN: self._admin,
            TYPE_GROUP_JOIN_NOTIFICATION: self._group_join,
            TYPE_GROUP_INVITE: self._invite,
            TYPE_EVENT_0X20D: self._event_0x20d,
            TYPE_EVENT_0X210: self._event_0x210,
            TYPE_EVENT_0X2DC: self._event_0x2dc,
        }.get(frame.type)
        if handler is None:
            return []
        event = handler(frame)
        if event is None:
            return []
        return event if isinstance(event, list) else [event]

    # ------------------------------------------------------------------ 33/34

    def _member_increase(self, frame: Frame) -> dict[str, Any] | None:
        change = decode_group_change(frame.body)
        member = self._uin(change.member_uid)
        if not change.group_uin or not member:
            return None
        if self._self_uin and str(member) == self._self_uin:
            # 记下来给紧随其后的 87 余波去重（见 `_invite`）；33 与 85 都会走到这个状态。
            self._self_joins[change.group_uin] = float(self._time())
        event = self._notice("group_increase")
        event.update({
            "group_id": change.group_uin,
            "user_id": member,
            "operator_id": self._uin(decode_operator(change.operator_raw)),
            # 131 是"被邀请"，其余按"申请通过"。两个值来自 Lagrange 的 Processor 与 NapCat 的
            # groupChangDecreseType2String —— 两份实现给的是同一个数。2026-11-24 实抓两种都出现
            # （131 群主直接拉人、130 批准入群申请）。
            "sub_type": "invite" if change.change_type == CHANGE_TYPE_INVITED else "approve",
        })
        return event

    def _self_joined(self, frame: Frame) -> dict[str, Any] | None:
        """85 = 本号进群（`SelfJoinInGroup`）。

        2026-11-24 实抓：`{1 群, 2=1, 3 审批人 uid, 4=0, 5 bytes, 6=0}`，发生在管理员批准
        入群申请 / 接受邀请之后 —— 这条路径 **33 不发**（SnowLuma 的
        `PkgType.GroupSelfJoinedNotice` 注释同样这么写；Lagrange 的 `SelfJoinInGroup` 字段表
        `{1 groupUin, 2, 3 operatorUid, 4, 6, 7}` 与实抓一致）。不接它，客户端要等到本号收到
        第一条群消息才知道自己已经进群。

        `sub_type` 用 `approve`：85 没有 33 那种 130/131 的区分，参考实现也都落到"批准"。
        同时记一条 self_join 供紧随其后的 87 余波去重（实抓里 85 之后没有 87，但 33/85 共用
        同一套"本号刚入群"状态）。
        """
        fields = decode_fields(frame.body)
        group_uin = _int(fields, 1)
        if not group_uin or not self._self_uin.isdigit():
            return None
        self._self_joins[group_uin] = float(self._time())
        event = self._notice("group_increase")
        event.update({
            "group_id": group_uin,
            "user_id": int(self._self_uin),
            "operator_id": self._uin(_text(fields, 3)),
            "sub_type": "approve",
        })
        return event

    def _member_decrease(self, frame: Frame) -> dict[str, Any] | None:
        change = decode_group_change(frame.body)
        member = self._uin(change.member_uid)
        if not change.group_uin or not member:
            return None
        # 取值表见 CHANGE_TYPE_* 的注释。退群（2/130）没有独立的操作人：operatorInfo 里放的是
        # 结构化的元数据而不是 uid，OneBot 的惯例是把退群者本人当操作人（SnowLuma 同样处理）。
        kind = change.change_type
        if member and str(member) == self._self_uin:
            self._self_joins.pop(change.group_uin, None)
        if kind in (CHANGE_TYPE_DISSOLVED_BY_OWNER, CHANGE_TYPE_DISBANDED):
            # 1 = 群主自己解散（2026-11-24 实抓：member 与 operator 都是本号）；129 = 参考实现里
            # 的"群解散"，应是非群主成员侧收到的值。两者都发 `group_disband`，与灰条通道
            # （headless_notices.cjs 的 DISBANDED）一致，Milky 侧才会都落到 group_disband。
            event = self._notice("group_disband")
            event.update({
                "group_id": change.group_uin,
                "operator_id": self._uin(decode_operator(change.operator_raw)),
            })
            return event
        if kind in (CHANGE_TYPE_LEFT, CHANGE_TYPE_SELF_LEFT):
            sub_type = "leave"
            operator = member
        else:
            sub_type = "kick_me" if kind == CHANGE_TYPE_SELF_REMOVED else "kick"
            operator = self._uin(decode_operator(change.operator_raw))
        event = self._notice("group_decrease")
        event.update({
            "group_id": change.group_uin,
            "user_id": member,
            "operator_id": operator,
            "sub_type": sub_type,
        })
        return event

    # --------------------------------------------------------------------- 44

    def _admin(self, frame: Frame) -> dict[str, Any] | None:
        admin = decode_group_admin(frame.body)
        member = self._uin(admin.admin_uid)
        if not admin.group_uin or not member:
            return None
        event = self._notice("group_admin")
        event.update({
            "group_id": admin.group_uin,
            "user_id": member,
            "sub_type": "set" if admin.is_set else "unset",
        })
        return event

    # ------------------------------------------------------------------ 84/87

    def _group_join(self, frame: Frame) -> dict[str, Any] | None:
        join = decode_group_join(frame.body)
        if not join.group_uin:
            return None
        return self._group_request(join.group_uin, join.target_uid, join.comment)

    def _invite(self, frame: Frame) -> dict[str, Any] | None:
        invite = decode_group_invite(frame.body)
        if not invite.group_uin:
            return None
        # 87 不只在"需要本号同意的邀请"时出现：实抓里群主直接把本号拉进群，先到 33（本号入群），
        # 429 ms 后再到一条同群的 87。那时已经在群里了，再发一条 invite 请求，客户端去审批只会
        # 失败。NapCat 靠"1 秒内收到邀请人发来的 ark 卡片"确认是真邀请，这里没有那条消息，就反过
        # 来判断：本号刚经 33/85 进了这个群，这条 87 就是余波。
        joined_at = self._self_joins.get(invite.group_uin)
        if joined_at is not None and self._time() - joined_at <= SELF_JOIN_INVITE_WINDOW:
            return None
        # NapCat 把 87 当作"他人邀请自身入群"，sub_type 是 invite —— 与 84 的"申请入群"相对。
        return self._group_request(invite.group_uin, invite.inviter_uid, "", sub_type="invite")

    def _group_request(self, group_id: int, uid: str, comment: str,
                       *, sub_type: str = "add") -> dict[str, Any] | None:
        initiator = self._uin(uid)
        event = self._request("group", sub_type)
        event.update({
            "group_id": group_id,
            # OneBot 的 `flag` 是审批时必须回传的不透明字符串。这里没有它——找出它需要另一次
            # 通知列表查询，而那属于动作层。没有就不写，比编一个安全。
            "user_id": initiator,
            "comment": comment,
        })
        if sub_type == "invite":
            event["sub_type"] = "invite"
        return event

    def _event_0x20d(self, frame: Frame) -> dict[str, Any] | None:
        sub_type, body = decode_event_0x20d(frame.body)
        if sub_type != TYPE_GROUP_INVITE:
            return None
        invite = decode_group_invite(body)
        if not invite.group_uin:
            return None
        return self._group_request(invite.group_uin, invite.inviter_uid, "", sub_type="invite")

    # -------------------------------------------------------------------- 528

    def _event_0x210(self, frame: Frame) -> dict[str, Any] | None:
        if frame.sub_type == SUB_FRIEND_REQUEST:
            request = decode_friend_request(frame.body)
            initiator = self._uin(request.source_uid)
            if not initiator:
                return None
            event = self._request("friend", "")
            event.pop("sub_type", None)
            event.update({
                "user_id": initiator,
                "comment": request.message,
                "via": request.source,
            })
            return event
        if frame.sub_type == SUB_FRIEND_RECALL:
            # 好友撤回。NapCat 从这条推送里拿序列号，而灰条路径已经在发同一件事；两条都发会让
            # 客户端收到两次。灰条那条已经覆盖，这里跳过。
            return None
        return None

    # -------------------------------------------------------------------- 732

    def _event_0x2dc(self, frame: Frame) -> dict[str, Any] | None:
        """732：群相关的提示（表情回应、戳一戳、撤回）。

        正文有额外的一层装载（见 :func:`unwrap_0x2dc`），解出来的 `NotifyMessageBody.sub_type`
        才是真正的类型。外面那层的 `frame.sub_type` 只是"群提示"这个大分类，两者不是一回事：
        NapCat 与 Lagrange 都是先看外层 732，再看内层的 13 号字段。
        """
        try:
            body = decode_notify_message_body(unwrap_0x2dc(frame.body))
        except DecodeError:
            return None

        if body.sub_type == NOTIFY_SUB_GROUP_NUDGE and body.nudge_busi_type == BUSI_POKE_GRAYTIP:
            return self._nudge(body)
        if body.sub_type == NOTIFY_SUB_GROUP_REACTION and body.reaction is not None:
            return self._reaction(body)
        # 撤回（内层 17）已经由灰条路径发出，两条都发会让客户端收到两次。
        return None

    def _nudge(self, body: NotifyMessageBody) -> dict[str, Any] | None:
        """群戳一戳。被戳的人与文案在模板参数里，参数名是 Lagrange 用到的那些。"""
        params = body.nudge_params
        sender = params.get("uin_str1", "")
        receiver = params.get("uin_str2", "")
        from_uin = int(sender) if sender.isdigit() else 0
        to_uin = int(receiver) if receiver.isdigit() else 0
        if not body.group_uin or not from_uin:
            return None
        event = self._notice("notify")
        event.update({
            "sub_type": "poke",
            "group_id": body.group_uin,
            "user_id": from_uin,
            # Milky 要显示的三个字段就在模板参数里；OneBot 的 poke 事件不带它们，多给的字段
            # 会被标准客户端忽略，但省掉它们就得让 Milky 侧留空。
            "target_id": to_uin or from_uin,
            "display_action": params.get("action_str", ""),
            "display_suffix": params.get("suffix_str", ""),
            "display_action_img_url": params.get("action_img_url", ""),
        })
        return event

    def _reaction(self, body: NotifyMessageBody) -> dict[str, Any] | None:
        reaction = body.reaction
        if reaction is None or not body.group_uin or not reaction.sequence:
            return None
        event = self._notice("group_msg_emoji_like")
        event.update({
            "group_id": body.group_uin,
            "user_id": self._uin(reaction.operator_uid),
            "message_seq": reaction.sequence,
            "is_add": reaction.is_add,
            "likes": ([{"emoji_id": reaction.code, "count": reaction.count or 1}]
                      if reaction.code else []),
        })
        return event
