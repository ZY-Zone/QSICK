"""Milky 协议适配器。

Milky 是「协议端开 HTTP 服务器 + 应用端当客户端」的强类型 JSON 协议：调用统一为
`POST /api/{api}`，返回 `{status,retcode,message,data}` 信封；推送走 `GET /event`（SSE / WS）
或 WebHook。业务失败也是 HTTP 200，要不要重试得看 `retcode`。

## 这份适配器复用了什么

**动作逻辑一行没写。** 65 个 API 里今天能执行的 29 个，全部翻译成 QSICK 已有的内部动作名
（见 `actions.py`），执行走 `runtime.invoke` —— 插件过滤、账号级出入限制、只读缓存、发送
串行化都在那里，绕开它的协议接入等于把内核权限系统关掉。

**消息段解析借道 v11。** 内部事件的 `message` 是 v11 段数组，转 Milky 段时先用
`protocols/onebotv11/codec.py` 还原成内部部件，再用本目录的 `codec.py` 编码。这样两条路
只有一份 v11 解析，改一处不会只改对一边。

## 出口与入口

* `encode_event`：内部事件（v11 形状）-> Milky 事件。表达不了时抛异常，分流点会只跳过走
  Milky 的连接。
* `decode_action`：`(api 名, Milky 请求体)` -> `(内部动作名, 内部参数)`。
* `format_reply`：内部结果 -> Milky 信封。

HTTP 路由（`/api/{api}`、`/event`）不在这里：适配器只管格式，路由在
`src/qsick/onebot/milky_server.py`，与 OneBot v11 的服务端并列。这跟 v11 适配器不实现动作
逻辑是同一个分界。
"""

from __future__ import annotations

from typing import Any

from ..base import ChatProtocol, PartError
from . import actions, codec, events

CAPABILITY_VERSION = 1

#: Milky 的 retcode。规范只固定了三个，其余由协议端定义；这里用规范建议值。
RETCODE_OK = 0
RETCODE_BAD_REQUEST = -400
RETCODE_NOT_LOGGED_IN = -403


class MilkyProtocol(ChatProtocol):
    """Milky 适配器。"""

    @property
    def name(self) -> str:
        return "milky"

    # ------------------------------------------------------------- 消息编解码

    def encode_message(self, parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return codec.parts_to_segments(parts)

    def decode_message(self, segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return codec.segments_to_parts(segments)

    # ------------------------------------------------------------- 动作调用

    def decode_action(self, name: str, params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """Milky 的 API 名与请求体 -> 内部动作名与参数。

        四类分流全在 `actions.resolve` 里：静态表、按参数决定目标动作、明确拒绝。未收录的 API
        抛 :class:`actions.UnknownAction`，调用方回一个明确的失败——把名字原样传下去会得到
        `unknown action 'send_group_message'`，看上去像内部坏了。
        """
        canonical, body = actions.resolve(name, params)
        # `message` 若是 Milky 段数组，转成内部部件（内部动作层接受部件）。
        message = body.get("message")
        if isinstance(message, list) and message:
            body["message"] = self._to_internal_segments(message)
        return canonical, body

    def local_reply(self, api: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """回答不需要经过动作层的 API（`LOCAL`）。不认领时返回 ``None``。

        这类问的是"你是哪个协议端、支持什么"，答案在本进程里。去问 QQ 反而错了——它不知道自己
        被谁包着。
        """
        name = str(api or "").strip()
        if name != "get_impl_info":
            return None
        caps = self.capabilities()
        return {
            "status": "ok",
            "retcode": 0,
            "message": "",
            "data": {
                "impl_name": "QSICK",
                "impl_version": _version(),
                "protocol_name": "Milky",
                "protocol_version": "1.3",
                "apis_supported": caps["apis_supported"],
                "apis_needs_translation": caps["apis_needs_translation"],
                "apis_no_action": caps["apis_no_action"],
                "events_supported": caps["events_supported"],
            },
        }

    def encode_action(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        """内部动作 -> Milky 调用。给 WebUI 的动作测试器与插件用。"""
        api = actions.api_for(action)
        body = actions.encode_params(action, params)
        return {"action": api or action, "params": body}

    def _to_internal_segments(self, segments: list[Any]) -> list[Any]:
        """Milky 段 -> 内部部件；已经是 OneBot 段的原样放过。

        内部动作层同时接受"内部部件"和"已成形段"（见 `onebotv11/adapter.py` 的
        `_looks_like_parts`）。Milky 的段是 `{type, ...}` 平铺，不带 `data`，所以必须转；
        已经是 `{type, data}` 的（比如内部调用方直接给的）不转。
        """
        parts: list[Any] = []
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            if "kind" in segment:
                parts.append(segment)
                continue
            if "data" in segment and "type" in segment:
                parts.append(segment)
                continue
            parts.append(codec.segment_to_part(segment))
        return parts

    # ------------------------------------------------------------- 事件推送

    def encode_event(self, event: dict[str, Any]) -> dict[str, Any]:
        converted = events.convert(event)
        if converted is None:
            raise PartError(
                f"Milky cannot express this event: post_type={event.get('post_type')!r}"
                + (f" notice_type={event.get('notice_type')!r}" if event.get("notice_type") else "")
            )
        return converted

    # ------------------------------------------------------------- 应答信封

    def format_reply(self, result: dict[str, Any], echo: Any = None) -> dict[str, Any]:
        """内部结果 -> `{status, retcode, message, data}`。

        内部结果的形状是 v11 的 `{status: ok|failed, retcode, data, message}`，与 Milky 的差别
        只在细节：Milky 的成功 `retcode` 必须是 `0`，而 v11 的内部成功是 `0`、失败是别的值。
        所以这里做一次映射，不是简单的重命名。
        """
        payload = dict(result or {})
        status = str(payload.get("status") or "").lower()
        # 只看 status。曾经额外要求 `data` 非 None，结果"无输出的成功"（比如改群名片）被判成失败——
        # v11 里那类动作就是 `{"status":"ok","retcode":0,"data":null}`，那是成功。
        ok = status == "ok"
        data = payload.get("data")
        if ok and data is None:
            # Milky 要求无输出的成功给 `{}`，而 v11 给的是 `null`。
            data = {}
        envelope: dict[str, Any] = {
            "status": "ok" if ok else "failed",
            "retcode": RETCODE_OK if ok else _retcode_of(payload),
            "data": data if ok else None,
        }
        if not ok:
            envelope["message"] = str(payload.get("message") or payload.get("wording") or "failed")
        else:
            envelope["message"] = ""
        return envelope

    # ---------------------------------------------------------------- 能力声明

    def capabilities(self) -> dict[str, Any]:
        return {
            "version": CAPABILITY_VERSION,
            "protocol": "milky",
            "message_formats": ["array"],
            "parts": {
                "text": True,
                "emotion": False,   # face 段只有 face_id
                "at": True,
                "reply": True,
                "image": True,
                "record": True,
                "video": True,
                "file": True,
                "forward": True,
            },
            "degraded": {"emotion": "face"},
            "emotion_rich_fields": False,
            "unknown_segments": "passthrough",
            # 实际能翻译的 API 数量，供状态页与文档核对。三类拒绝分开计数：
            # "参数对不上"与"根本没有接口"是两种不同的缺口，混在一起就判断不出下一步该做什么。
            "apis_supported": len(actions.SUPPORTED) + len(actions.DYNAMIC),
            "apis_needs_translation": len(actions.NEEDS_TRANSLATION),
            "apis_no_action": len(actions.NO_ACTION),
            # 按设计不支持的那一组单独计数：它与"底层没做"是两种缺口。
            "apis_unsupported_by_design": len(actions.UNSUPPORTED_BY_DESIGN),
            "apis_local": len(actions.LOCAL),
            "events_supported": 3,
            "events_pending_host": 7,
        }


def _version() -> str:
    """本程序的版本号。取不到时给空串，不猜。"""
    try:
        from importlib.metadata import version

        return version("qsick")
    except Exception:  # noqa: BLE001 - 未安装或元数据缺失时没有版本可报
        return ""


def _retcode_of(payload: dict[str, Any]) -> int:
    """内部失败 -> Milky retcode。

    Milky 建议值只有 `-400`（参数错）与 `-403`（未登录），其余由协议端定义。内部 retcode
    是 OneBot 的取值范围（1400/1404/1500……），直接给出去对 Milky 客户端毫无意义，所以
    归到规范建议的那两个上，其余的用 `-500` 表示"协议端内部错误"。
    """
    raw = payload.get("retcode")
    try:
        code = int(raw)
    except (TypeError, ValueError):
        return -500
    if code == 1400:
        return RETCODE_BAD_REQUEST
    if code == 1403:
        return RETCODE_NOT_LOGGED_IN
    return -500


def create(*_args: Any, **_kwargs: Any) -> MilkyProtocol:
    """注册表按名字取适配器用。Milky 不需要 dispatcher：执行在分流点之后。"""
    return MilkyProtocol()


def from_runtime(_runtime: Any) -> MilkyProtocol:
    """与 v11 适配器同样的入口，便于上层统一构造。"""
    return MilkyProtocol()
