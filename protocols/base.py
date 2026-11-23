"""协议适配层的公共契约：内部部件表示 + :class:`ChatProtocol` 抽象基类。

为什么要有这一层：``protocols/`` 下可以有**多份**实现（当前是纯 OneBot v11
与 maxStore 存储）。如果每份实现各自定义消息结构，上层每接一个协议就要加一个
分支，抽象就等于没做。所以这里固定一套**与协议无关**的“部件”（part）表示，
各适配器只负责把它翻译成自己协议的消息段。

边界很明确：这一层只做格式转换。动作的真正执行仍在 ``src/qsick/onebot/``，
适配器最多做一层薄封装，绝不复制动作逻辑。

内部部件表示（全部是普通 dict：可跨协议、可 JSON 序列化、无需引入模型类）：

    {"kind": "text",    "text": "你好"}
    {"kind": "emotion", "emotion": {"id": "14", "format": "static", "animated": False}}
    {"kind": "image",   "file": "...", "url": "https://...", "name": "a.png"}
    {"kind": "record" | "video" | "file", ...媒体字段同 image...}
    {"kind": "at",      "qq": "12345"}        # "all" 表示 @全体成员
    {"kind": "reply",   "id": "9001"}
    {"kind": "unknown", "type": "poke", "data": {...}}   # 未建模段：透传，不丢弃

几条约定：

* ``emotion`` 是“表情”的统一表示（``id`` / ``format`` / ``file_type`` /
  ``pack_id`` / ``sticker_id`` / ``sticker_type`` / ``animated`` / ``text``）。
  纯 OneBot v11 的 face 段只装得下 ``id``，所以它把 emotion 降级成 face 段，
  其余字段在编码时丢掉。字段在这里仍然保留完整形状：将来接一个能表达动态性的
  协议时，不需要回头重解析历史。
* ``face`` 可以作为 emotion 的别名传入，照顾 OneBot 客户端的既有习惯。
* ``unknown`` 段永远保留原始 ``type`` 与 ``data``：任何协议都不许静默丢弃
  自己不认识的段，否则上层永远看不到“有东西来了但我不认”。
"""

from __future__ import annotations

import abc
from typing import Any

KIND_TEXT = "text"
KIND_EMOTION = "emotion"
KIND_AT = "at"
KIND_REPLY = "reply"
KIND_UNKNOWN = "unknown"
KIND_IMAGE = "image"
KIND_RECORD = "record"
KIND_VIDEO = "video"
KIND_FILE = "file"

#: 媒体类部件：字段集合一致，只是段类型不同。
MEDIA_KINDS = (KIND_IMAGE, KIND_RECORD, KIND_VIDEO, KIND_FILE)

#: 全部内建 kind；“unknown” 之外的 kind 都可能被适配器降级或透传。
PART_KINDS = frozenset({KIND_TEXT, KIND_EMOTION, KIND_AT, KIND_REPLY, KIND_UNKNOWN, *MEDIA_KINDS})

#: OneBot 客户端习惯用 ``face`` 表示表情，内部统一收敛到 ``emotion``，避免两套同义结构。
_KIND_ALIASES = {"face": KIND_EMOTION}


class PartError(ValueError):
    """部件结构不合法。调用方应拒绝整条消息，而不是丢掉这一段。"""


class ChatProtocol(abc.ABC):
    """一份协议适配器必须实现的最小接口。

    编码方向（内部 -> 协议）叫 ``encode``，解码方向（协议 -> 内部）叫 ``decode``；
    两个方向都不允许抛掉数据：不认识的段一律进 ``unknown`` 部件。
    """

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """协议名，同时是 ``protocols/`` 下的目录名（如 ``onebotv11``）。"""

    @abc.abstractmethod
    def encode_message(self, parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """内部部件 -> 该协议的消息段数组。"""

    @abc.abstractmethod
    def decode_message(self, segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """该协议的消息段数组 -> 内部部件。"""

    @abc.abstractmethod
    def encode_action(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        """内部动作调用 -> 该协议的请求体（动作执行本身不在这里）。"""

    @abc.abstractmethod
    def capabilities(self) -> dict[str, Any]:
        """能力声明：上层据此决定要不要降级（例如表情是否走富协议）。"""

    # -------------------------------------------------------------- 事件与调用
    #
    # 下面三个方法有默认实现，默认实现就是"OneBot v11 原样"，所以老适配器不需要
    # 改任何一行。它们存在的原因是：协议不只是消息段的形状，还包括**事件推送的
    # 形状**和**动作调用的入口形状**。分流点只看这三个方法，不认识具体协议。

    def encode_event(self, event: dict[str, Any]) -> dict[str, Any]:
        """内部事件（已是 OneBot v11 形状）-> 本协议推给客户端的事件。

        默认原样返回。转换失败时应当抛异常而不是返回半个事件——调用方会捕获它并
        只跳过这一条连接，而不是把坏数据喂给对端。
        """
        return event

    def decode_action(self, name: str, params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """本协议的调用名与参数 -> 内部动作名与参数。

        默认原样返回（v11 的 ``action`` 名字就是内部动作名）。名字未知时应当抛
        :class:`PartError`，让调用方回一个"不认识这个动作"，而不是猜一个近似的。
        """
        return str(name or ""), dict(params or {})

    def format_reply(self, result: dict[str, Any], echo: Any = None) -> dict[str, Any]:
        """内部动作结果 -> 本协议的应答信封。默认是 v11 的形状（原样）。

        ``echo`` 只作为提示传进来：内部动作层已经把它放进了结果信封（``registry``
        从参数里取，失败路径也用 ``models.fail(..., echo=echo)``），所以默认实现不再
        补一次。需要的协议（Milky 的 ``{status,retcode,message,data}``）自己重建。
        """
        return dict(result or {})


def normalize_part(part: Any) -> dict[str, Any]:
    """把调用方给的部件收敛成规范形状，顺便挡住拼错的字段。

    适配器拿到的部件可能来自聊天栏、插件或另一个协议，形状未必严格，所以
    统一在入口做一次校验与补全，后面的编码逻辑就能只处理规范形状。
    """
    if not isinstance(part, dict):
        raise PartError(f"part must be an object, got {type(part).__name__}")

    kind = str(part.get("kind") or "").strip()
    if not kind:
        # 已经是协议消息段（``{"type": ..., "data": ...}``）时按透传处理，
        # 这样调用方可以把部件和原始段混在同一个列表里。
        segment_type = part.get("type")
        if isinstance(segment_type, str) and segment_type:
            return _passthrough(segment_type, part.get("data"))
        raise PartError("part needs a 'kind' field")

    kind = _KIND_ALIASES.get(kind, kind)

    if kind == KIND_TEXT:
        text = part.get("text")
        if not isinstance(text, str):
            raise PartError("text part needs a string 'text' field")
        return {"kind": KIND_TEXT, "text": text}

    if kind == KIND_EMOTION:
        emotion = part.get("emotion")
        if not isinstance(emotion, dict):
            # 允许扁平写法 ``{"kind": "face", "id": "14"}``。
            emotion = {key: value for key, value in part.items() if key != "kind"}
        cleaned = dict(emotion)
        if cleaned.get("id") is not None:
            # id 统一成字符串：QQ 的 emoji 字形 id 本来就不是数字，统一类型后
            # 降级判断只需要看“是不是纯数字”。
            cleaned["id"] = str(cleaned["id"])
        if not cleaned:
            raise PartError("emotion part needs at least an 'id'")
        return {"kind": KIND_EMOTION, "emotion": cleaned}

    if kind == KIND_AT:
        qq = part.get("qq", part.get("user_id"))
        if qq is None or str(qq) == "":
            raise PartError("at part needs 'qq' (use 'all' to mention everyone)")
        return {"kind": KIND_AT, "qq": str(qq)}

    if kind == KIND_REPLY:
        ident = part.get("id", part.get("message_id"))
        if ident is None or str(ident) == "":
            raise PartError("reply part needs 'id'")
        return {"kind": KIND_REPLY, "id": str(ident)}

    if kind in MEDIA_KINDS:
        # 媒体段原样带过去：各协议字段名不同（file/url/path），由适配器自己映射。
        payload = {key: value for key, value in part.items() if key != "kind"}
        return {"kind": kind, **payload}

    if kind == KIND_UNKNOWN:
        return _passthrough(part.get("type") or KIND_UNKNOWN, part.get("data"))

    # 未建模的类型：带 data 就按透传段处理，不带就报错，避免拼错的 kind 混过去。
    if isinstance(part.get("data"), dict):
        return _passthrough(kind, part["data"])
    raise PartError(f"unknown part kind {kind!r}")


def _passthrough(segment_type: Any, data: Any) -> dict[str, Any]:
    """构造一个透传部件：保留原始段类型与数据，不做任何解释。"""
    if not isinstance(data, dict):
        data = {}
    return {"kind": KIND_UNKNOWN, "type": str(segment_type), "data": dict(data)}
