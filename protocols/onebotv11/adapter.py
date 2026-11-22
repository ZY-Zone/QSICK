"""纯 OneBot v11 协议适配器。

实现 :class:`protocols.base.ChatProtocol`：把内部部件转成 v11 消息段，把 v11
段转回内部部件，并暴露一个发送动作的薄封装。

刻意**不**在这里实现动作逻辑：动作的校验、注册与执行都在
``src/qsick/onebot/registry.py`` 与它下面的 ``actions/`` 包里。适配器只在
真正要发出去的那一刻做一次格式转换，然后交给已有的 registry，避免同一个动作
存在两套语义。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from ..base import ChatProtocol, KIND_EMOTION
from . import codec

#: 能力声明的版本。上层可以据此判断字段含义有没有变。
CAPABILITY_VERSION = 1


@runtime_checkable
class ActionDispatcher(Protocol):
    """适配器需要的最小执行入口。

    只要求一个 ``dispatch``：已有的 ``ActionRegistry`` 正好符合这个签名，
    但适配器不依赖它的具体类型，测试里传一个假的也能跑。
    """

    async def dispatch(self, name: str, params: dict[str, Any] | None, ctx: Any) -> dict[str, Any]: ...


class OneBotV11Protocol(ChatProtocol):
    """OneBot v11 适配器。

    ``dispatcher`` 与 ``ctx`` 都是可选的：只做格式转换时不需要它们；只在真的
    要发消息时才传入。这样编码器可以在没有运行时的情况下被测试和复用。
    """

    def __init__(self, dispatcher: ActionDispatcher | None = None, ctx: Any = None) -> None:
        self._dispatcher = dispatcher
        self._ctx = ctx

    @property
    def name(self) -> str:
        # 与 protocols/ 下的目录名一致，便于按名字加载。
        return "onebotv11"

    # ------------------------------------------------------------- 消息编解码

    def encode_message(self, parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return codec.parts_to_segments(parts)

    def decode_message(self, segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return codec.segments_to_parts(segments)

    def encode_cq_string(self, parts: list[dict[str, Any]]) -> str:
        """部件 -> CQ 字符串。v11 允许字符串与数组两种消息形态，两者都要能出。"""
        return codec.render_cq_string(parts)

    def decode_cq_string(self, text: str) -> list[dict[str, Any]]:
        """CQ 字符串 -> 内部部件。"""
        return codec.segments_to_parts(codec.parse_cq_string(text))

    # --------------------------------------------------------------- 动作调用

    def encode_action(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        """内部动作调用 -> v11 请求体。

        ``message`` 参数如果是内部部件，会在这里转成 v11 消息段 —— 调用方不必
        关心自己手里拿的是哪种表示。已经是字符串或段数组的则原样保留，因为
        v11 两种形态都接受。
        """
        name = str(action or "").strip()
        if not name:
            raise ValueError("action name is required")
        body = dict(params or {})
        message = body.get("message")
        if isinstance(message, list) and message and _looks_like_parts(message):
            body["message"] = self.encode_message(message)
        return {"action": name, "params": body}

    async def send_message(self, chat: dict[str, Any], parts: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
        """发送一条消息：转格式，然后交给已有的 registry 执行。

        薄封装的分界线就在这里 —— 只决定调哪个动作、怎么塞参数，剩下的
        校验、序列化、native 调用都交给 ``src/qsick/onebot/``。
        """
        if self._dispatcher is None:
            raise RuntimeError("OneBotV11Protocol needs a dispatcher to send messages")
        chat_type = str(chat.get("type") or "")
        chat_id = chat.get("id")
        if chat_type not in ("group", "private"):
            raise ValueError(f"chat type must be group or private, got {chat_type!r}")
        if chat_id is None:
            raise ValueError("chat id is required")

        action = "send_group_msg" if chat_type == "group" else "send_private_msg"
        params: dict[str, Any] = {
            "group_id" if chat_type == "group" else "user_id": int(chat_id),
            "message": self.encode_message(parts),
        }
        params.update(extra)
        request = self.encode_action(action, params)
        return await self._dispatcher.dispatch(request["action"], request["params"], self._ctx)

    # ---------------------------------------------------------------- 能力声明

    def capabilities(self) -> dict[str, Any]:
        """告诉上层这份协议能表达什么、要降级什么。

        ``emotion`` 报 ``False`` 是关键：纯 v11 只有 face 段，动态表情会被降级，
        上层可以据此选择切到 EXTemotions 协议而不是静默丢信息。
        """
        return {
            "version": CAPABILITY_VERSION,
            "protocol": "onebotv11",
            "message_formats": ["array", "string"],
            "parts": {
                "text": True,
                "emotion": False,  # 只能降级成 face
                "at": True,
                "reply": True,
                "image": True,
                "record": True,
                "video": True,
                "file": True,
            },
            "degraded": {
                KIND_EMOTION: "face",
            },
            "emotion_rich_fields": False,  # 动态表情/贴纸三元组在 v11 无法表达
            "unknown_segments": "passthrough",
        }


def _looks_like_parts(message: list[Any]) -> bool:
    """判断列表是内部部件还是已经成形的 v11 段。

    两者都是 dict 列表，唯一可靠的区分是键名：部件一定有 ``kind``，v11 段一定
    有 ``type``。拿不准时按“不是部件”处理，交给 registry 原有的校验报错，
    比在这里猜错要安全。
    """
    return any(isinstance(item, dict) and "kind" in item for item in message)


#: 按协议名注册的实例工厂，供上层按名字取适配器。
def create(dispatcher: ActionDispatcher | None = None, ctx: Any = None) -> OneBotV11Protocol:
    return OneBotV11Protocol(dispatcher=dispatcher, ctx=ctx)


def from_runtime(runtime: Any) -> OneBotV11Protocol:
    """从现有运行时取适配器：直接复用它的 registry 与动作上下文。

    这是“不重复实现动作逻辑”的落点 —— 发送仍然走 ``src/qsick/onebot/`` 里
    已经注册好的动作，适配器只提供格式转换。用鸭子类型取属性而不是 import
    具体类型：适配器不该反向依赖运行时，否则协议层就无法单独测试或复用。
    """
    dispatcher = getattr(runtime, "registry", None)
    if dispatcher is None or not hasattr(dispatcher, "dispatch"):
        raise TypeError("runtime has no action registry to dispatch through")
    context = getattr(runtime, "context", None)
    return OneBotV11Protocol(dispatcher=dispatcher, ctx=context() if callable(context) else None)
