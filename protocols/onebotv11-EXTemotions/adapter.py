"""``onebotv11-EXTemotions`` 适配器：OneBot v11 承载消息，表情走 EXTemotions。

为什么需要它：OneBot v11 的 ``[CQ:face,id=N]`` 只有一个数字，说不清「这个表情是
不是动态」「文件是什么格式」「属于哪一类」。于是超级表情只能在两端被猜，猜错就
画成别的表情。EXTemotions（``D:\\projects\\EXTemotions``）把表情定义成自包含的
JSON 对象，四个字段直击四个断点：``type``（类别）、``uid``（身份）、``moveable``
（是否动态）、``fileType``（真实格式）。

分工：

* **出入站消息结构**仍是 OneBot v11 的段数组，这样现存的 OneBot 生态、动作接口、
  事件推送都不用动；
* **表情段**换成 EXTemotions 对象。内部统一用 ``kind: "emotion"`` 部件承载，
  ``emotion`` 字段就是规范里的 emotion 对象本身。

与纯 ``onebotv11`` 适配器的区别只有一处：``emotion`` 不再被压扁成 ``face`` 段，
而是原样带到上层，由 ``headless_codec`` 按 ``moveable``/``type`` 选择 QQNT 的
编码通道（``faceType=3`` 的 AniSticke 还是 ``faceType=1`` 的旧式黄脸）。
"""

from __future__ import annotations

from typing import Any

from ..base import (
    KIND_AT,
    KIND_EMOTION,
    KIND_REPLY,
    KIND_TEXT,
    KIND_UNKNOWN,
    MEDIA_KINDS,
    ChatProtocol,
    PartError,
    normalize_part,
)
from ..onebotv11 import OneBotV11Protocol
from ..onebotv11.codec import escape_param, parse_cq_string

#: 协议标识，同时也是 ``protocols/`` 下的目录名。
PROTOCOL_NAME = "onebotv11-EXTemotions"


def emotion_part(emotion: dict[str, Any]) -> dict[str, Any]:
    """构造一个 emotion 部件，顺手把规范要求的字段补到最小可用。"""
    if not isinstance(emotion, dict):
        raise PartError("emotion must be an object")
    payload = dict(emotion)
    # 内部表示里 emotion 可能只带 id（来自 OneBot 客户端或手输标记）。补全成
    # 规范态是上层（face_catalog + extemotions.build）的职责，这里不猜类型：
    # 猜错类型比缺字段更糟，因为它会走错编码通道。
    return {"kind": KIND_EMOTION, "emotion": payload}


class OneBotV11ExtemotionsProtocol(OneBotV11Protocol):
    """OneBot v11 + EXTemotions。

    继承纯 v11 适配器，只覆盖表情相关的三个位置：段 -> 部件、部件 -> 段、
    能力声明。其余（文本、at、reply、媒体、CQ 解析）完全复用，避免两份
    实现慢慢走偏。
    """

    @property
    def name(self) -> str:
        return PROTOCOL_NAME

    # ---------------------------------------------------------------- 出入站

    def decode_message(self, segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """段 -> 部件。表情段产出 ``emotion`` 而不是压扁的 ``face``。"""
        parts: list[dict[str, Any]] = []
        for segment in segments:
            if not isinstance(segment, dict):
                parts.append({"kind": KIND_UNKNOWN, "type": KIND_UNKNOWN, "data": {}})
                continue
            kind = str(segment.get("type") or "")
            data = segment.get("data") if isinstance(segment.get("data"), dict) else {}

            if kind == "face":
                parts.append(self._face_segment_to_part(data))
                continue
            if kind == "mface":
                parts.append(self._mface_segment_to_part(data))
                continue
            # 其余交给纯 v11 的实现：它已经处理好文本/at/reply/媒体/未知段。
            parts.extend(super().decode_message([segment]))
        return parts

    def _face_segment_to_part(self, data: dict[str, Any]) -> dict[str, Any]:
        """``face`` 段 -> ``face`` 类 emotion。

        段里可能已经带了 ``format``/``file_type``（本协议自己的出站段），那就
        照用；只有裸 ``id`` 时按 ``face`` 处理——**不推断成 super**。同一个数字
        ID 在 QQ 里既可能是黄脸也可能是超级表情（有 39 例），靠 ID 区间猜必然
        出错，那正是要修的病根。
        """
        face_id = str(data.get("id") or data.get("face_id") or "").strip()
        if not face_id:
            return {"kind": KIND_UNKNOWN, "type": "face", "data": dict(data)}
        emotion: dict[str, Any] = {"id": face_id}
        declared = str(data.get("format") or "").strip()
        if declared in ("static", "animated"):
            emotion["format"] = declared
            emotion["moveable"] = declared == "animated"
        file_type = str(data.get("file_type") or "").strip()
        if file_type:
            emotion["fileType"] = file_type
        for source, target in (("pack_id", "pack_id"), ("sticker_id", "sticker_id"),
                               ("sticker_type", "sticker_type")):
            value = data.get(source)
            if value not in (None, ""):
                emotion[target] = value
        name = str(data.get("name") or data.get("text") or "").strip()
        if name:
            emotion["name"] = name
        return {"kind": KIND_EMOTION, "emotion": emotion}

    def _mface_segment_to_part(self, data: dict[str, Any]) -> dict[str, Any]:
        """``mface`` 段 -> ``super`` 或 ``special`` 类 emotion。

        判据用的是包 ID 与授权键是否存在（附录 A §2），而不是 ID 区间：
        有 ``emojiPackageId`` 且带 ``key`` 的是账号级授权表情，形同 ``special``；
        其余按 ``super`` 处理。信息不足时按 ``super`` 保底，并保留原始字段。
        """
        emoji_id = str(data.get("emoji_id") or "").strip()
        package = str(data.get("emoji_package_id") or "").strip()
        key = str(data.get("key") or "").strip()
        if not emoji_id and not package:
            return {"kind": KIND_UNKNOWN, "type": "mface", "data": dict(data)}

        emotion_type = "special" if (package and key) else "super"
        emotion: dict[str, Any] = {
            "type": emotion_type,
            "id": emoji_id or package,
            "moveable": True,
            "source": "market" if package else "system",
        }
        qq: dict[str, Any] = {}
        if package:
            qq["emojiPackageId"] = package
        if key:
            qq["marketKey"] = key
        if data.get("tab_id") not in (None, ""):
            qq["tabId"] = str(data["tab_id"])
        if data.get("sub_type") not in (None, ""):
            qq["subType"] = str(data["sub_type"])
        if qq:
            emotion["qq"] = qq
        if data.get("summary"):
            emotion["summary"] = str(data["summary"])
        if data.get("url"):
            emotion["url"] = str(data["url"])
        return {"kind": KIND_EMOTION, "emotion": emotion}

    def encode_message(self, parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """部件 -> 段。``emotion`` 段带上 EXTemotions 的声明字段。"""
        normalized = [normalize_part(part) for part in parts]
        segments: list[dict[str, Any]] = []
        for part in normalized:
            kind = part["kind"]
            if kind == KIND_EMOTION:
                segments.append(self._emotion_to_segment(part["emotion"]))
                continue
            if kind in (KIND_TEXT, KIND_AT, KIND_REPLY, KIND_UNKNOWN) or kind in MEDIA_KINDS:
                # 这些段纯 v11 已经完全处理好了，直接复用而不是重写一遍。
                segments.extend(super().encode_message([part]))
                continue
            raise PartError(f"unsupported part kind {kind!r}")
        return segments

    def _emotion_to_segment(self, emotion: dict[str, Any]) -> dict[str, Any]:
        """emotion -> ``face`` 段，把 EXTemotions 的语义字段一并带上。

        为什么仍用 ``face`` 这一种段类型：下游的发送编码器按 ``format`` 决定
        QQNT 通道（AniSticke / 旧式黄脸），它只需要读这些字段，不需要新的段类型；
        多造一种段反而要让每个消费方都加分支。
        """
        face_id = str(emotion.get("id") or "")
        if not face_id:
            raise PartError("emotion segment needs an 'id'")
        data: dict[str, Any] = {"id": face_id}

        declared = str(emotion.get("format") or "").strip()
        if declared not in ("static", "animated"):
            # 没有显式声明时由 moveable 推导；两者都没有说明调用方没补全，
            # 此时按静态处理——但绝不删掉 id，那是唯一不可丢的信息。
            declared = "animated" if emotion.get("moveable") is True else "static"
        data["format"] = declared
        data["animated"] = declared == "animated"

        file_type = str(emotion.get("fileType") or emotion.get("file_type") or "").strip()
        if file_type:
            data["file_type"] = file_type
        for source, target in (("pack_id", "pack_id"), ("sticker_id", "sticker_id"),
                               ("sticker_type", "sticker_type")):
            value = emotion.get(source)
            if value in (None, ""):
                # 也接受 EXTemotions 规范里的 qq.* / pack.* 写法，避免调用方
                # 必须记住内部扁平字段名。
                qq = emotion.get("qq") or {}
                pack = emotion.get("pack") or {}
                if source == "pack_id":
                    value = pack.get("id") or qq.get("aniStickerPackId")
                elif source == "sticker_id":
                    value = pack.get("index") or qq.get("aniStickerId")
                elif source == "sticker_type":
                    value = qq.get("aniStickerType")
            if value not in (None, ""):
                # sticker_type 是数字语义，保持 int；pack/sticker id 才是字符串。
                data[target] = int(value) if target == "sticker_type" and str(value).isdigit() else str(value)
        name = str(emotion.get("name") or "").strip()
        if name:
            # ``name`` 是本协议的字段；``text`` 是旧 face 段的既有写法。两个都带，
            # 老消费方（按 text 取名字）不用改，新消费方按 name 取。
            data["name"] = name
            data["text"] = name
        return {"type": "face", "data": data}

    # ------------------------------------------------------------------ CQ 串

    def encode_cq_string(self, parts: list[dict[str, Any]]) -> str:
        """部件 -> CQ 字符串。

        表情在纯 CQ 串里只能表达基础黄脸；为了不丢 ``format``/``file_type``，
        带这些声明的表情改用 ``ext_emotion`` 段承载（附录 B §3 的兜底形态）。
        纯 v11 消费方读到不认识的段会跳过，不会把消息整体判废。
        """
        pieces: list[str] = []
        for part in (normalize_part(item) for item in parts):
            if part["kind"] == KIND_EMOTION:
                emotion = part["emotion"]
                declared = str(emotion.get("format") or "");
                if declared in ("static", "animated") or emotion.get("fileType"):
                    import base64
                    import json as _json

                    raw = _json.dumps(emotion, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                    encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
                    pieces.append(f"[CQ:ext_emotion,json={escape_param(encoded)}]")
                else:
                    pieces.append(f"[CQ:face,id={escape_param(str(emotion.get('id') or ''))}]")
                continue
            pieces.append(super().encode_cq_string([part]))
        return "".join(pieces)

    def decode_cq_string(self, text: str) -> list[dict[str, Any]]:
        """CQ 字符串 -> 部件。认得 ``ext_emotion`` 就还原成 emotion 部件。"""
        parts: list[dict[str, Any]] = []
        for segment in parse_cq_string(text):
            data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
            if segment.get("type") == "ext_emotion":
                decoded = self._decode_ext_emotion(data.get("json"))
                if decoded is not None:
                    parts.append({"kind": KIND_EMOTION, "emotion": decoded})
                    continue
                # 解不出来就整段丢弃并留痕，而不是让整条消息解析失败
                # （附录 B §3 明确要求）。
                parts.append({"kind": KIND_UNKNOWN, "type": "ext_emotion", "data": dict(data)})
                continue
            parts.extend(self.decode_message([segment]))
        return parts

    @staticmethod
    def _decode_ext_emotion(value: Any) -> dict[str, Any] | None:
        if not isinstance(value, str) or not value:
            return None
        import base64
        import json as _json

        padded = value + "=" * (-len(value) % 4)
        try:
            raw = base64.urlsafe_b64decode(padded.encode("ascii"))
            payload = _json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    # ------------------------------------------------------------------ 能力

    def capabilities(self) -> dict[str, Any]:
        base = super().capabilities()
        base.update({
            "emotions": "extemotions",
            # 与纯 v11 的差别就在这里：表情的语义字段不再被丢弃。
            "emotion_rich_fields": True,
            "emotion_types": ["emoji", "face", "super", "special"],
            "emotion_moveable": True,
            "emotion_file_types": ["png", "apng", "gif", "webp", "lottie"],
            "degraded": {},
        })
        return base


def create() -> OneBotV11ExtemotionsProtocol:
    """工厂函数，供协议注册表按名字构造实例。"""
    return OneBotV11ExtemotionsProtocol()
