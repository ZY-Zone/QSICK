r"""EXTemotions 1.0 的 emotion 对象实现。

协议规范见 ``docs/PROTOCOLS.md`` 与 EXTemotions 上游规范（v1.0）。
这里只做协议本身的事：构造、校验、嗅探、降级标记。**不碰传输**——怎么把对象送
出去由 ``protocols/onebotv11-EXTemotions`` 决定。
"""

from __future__ import annotations

SCHEMA = "EXTemotions"
VERSION = "1.0"

TYPES = ("emoji", "face", "super", "special")
FILE_TYPES = ("png", "apng", "gif", "webp", "jpg", "bmp", "svg", "lottie", "avif", "heic", "none", "other")

# INV-08/09/15：这些格式自身就决定了 moveable，不能由调用方随便填。
ALWAYS_MOVEABLE = ("apng", "lottie")
ALWAYS_STATIC = ("png", "jpg", "bmp", "svg")
# INV-13：这两个格式动静态无法由格式名自证，必须给 frameCount。
NEEDS_FRAME_COUNT = ("gif", "webp")


class EmotionError(ValueError):
    """违反协议不变量。``code`` 用规范里的稳定错误码，不自造码值。"""

    def __init__(self, code: str, message: str, **details: object) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


def _bad(field: str, expected: object, actual: object) -> EmotionError:
    return EmotionError("EXT_1001_SCHEMA_INVALID", f"{field} 不符合协议要求", field=field,
                        expected=expected, actual=actual)


def codepoints(text: str) -> list[int]:
    """字符的十进制码点，去掉 VS15/VS16。

    变体选择符只影响渲染样式，不改变表情身份（docs/01 §3.1），所以既不能算进
    ``uid``，也不该算进 ``codepoints``——否则 ``❤`` 与 ``❤️`` 会变成两个表情。
    """
    return [ord(char) for char in text if char not in ("\ufe0e", "\ufe0f")]


def emoji_uid(text: str) -> str:
    points = codepoints(text)
    return "emoji:" + "-".join(f"U+{point:04X}" for point in points)


def build_uid(emotion_type: str, identifier: str) -> str:
    """按 docs/01 §3 生成 ``uid``：space 必须等于 type。"""
    if emotion_type not in TYPES:
        raise EmotionError("EXT_1002_TYPE_UNSUPPORTED", f"未知表情类型 {emotion_type!r}")
    return f"{emotion_type}:{identifier}"


def detect_file_type(head: bytes) -> tuple[str, dict[str, object]]:
    """读文件头判定真实格式，返回 ``(fileType, 附加信息)``。

    规范强制按魔数嗅探：语料里 ``apng/424.png`` 扩展名是 ``.png``，内容却是
    APNG。按扩展名判会把动态表情全部判成静态图。
    """
    info: dict[str, object] = {}
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        # acTL 必须出现在第一个 IDAT 之前才算 APNG；直接找 acTL 可能命中尾部
        # 追加数据，所以先定位 IDAT。
        actl = head.find(b"acTL")
        idat = head.find(b"IDAT")
        if actl >= 0 and (idat < 0 or actl < idat):
            frames = int.from_bytes(head[actl + 4:actl + 8], "big") if actl + 8 <= len(head) else 0
            plays = int.from_bytes(head[actl + 8:actl + 12], "big") if actl + 12 <= len(head) else 0
            info["frameCount"] = frames
            info["loop"] = plays == 0
            return "apng", info
        return "png", info
    if head[:6] in (b"GIF87a", b"GIF89a"):
        info["frameCount"] = head.count(b"\x21\xf9\x04")
        return "gif", info
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp", info
    if head[:3] == b"\xff\xd8\xff":
        return "jpg", info
    if head[:2] == b"BM":
        return "bmp", info
    if head[:4] == b"\x00\x00\x00" and head[4:8] in (b"ftyp",):
        return "avif", info
    if head.lstrip()[:5] in (b"<?xml", b"<svg "):
        return "svg", info
    if head.lstrip()[:1] == b"{":
        # Lottie 是 JSON，且必须同时有 v / fr / layers 三个键。
        try:
            import json

            payload = json.loads(head.decode("utf-8", "ignore"))
        except (ValueError, UnicodeDecodeError):
            return "other", info
        if isinstance(payload, dict) and {"v", "fr", "layers"} <= set(payload):
            info["frameCount"] = int(payload.get("fr") or 0)
            return "lottie", info
    return "other", info


def png_size(head: bytes) -> tuple[int, int] | None:
    """PNG/APNG 的宽高，取自 IHDR。"""
    if head[:8] != b"\x89PNG\r\n\x1a\n" or len(head) < 24:
        return None
    return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")


def moves(file_type: str, frame_count: int | None = None) -> bool:
    """由格式与帧数推导 ``moveable``（规范 §6 的第 2、3 步）。"""
    if file_type in ALWAYS_MOVEABLE:
        return True
    if file_type in ALWAYS_STATIC:
        return False
    if frame_count is not None:
        return frame_count > 1
    return False


def validate(emotion: dict) -> None:
    """检查 INV-01 ~ INV-17 中能离线判定的部分。

    发送前与接收后都要调用：收到的东西也不能是自相矛盾的。
    """
    if not isinstance(emotion, dict):
        raise _bad("emotion", "object", type(emotion).__name__)
    if emotion.get("schema") != SCHEMA:
        raise _bad("schema", SCHEMA, emotion.get("schema"))
    version = str(emotion.get("version") or "")
    if not version or version.count(".") != 1 or not all(part.isdigit() for part in version.split(".")):
        raise _bad("version", "MAJOR.MINOR", version)

    emotion_type = str(emotion.get("type") or "")
    if emotion_type not in TYPES:
        raise EmotionError("EXT_1002_TYPE_UNSUPPORTED", f"未知表情类型 {emotion_type!r}")
    uid = str(emotion.get("uid") or "")
    if not uid.startswith(emotion_type + ":"):
        raise _bad("uid", f"{emotion_type}:...", uid)

    if "moveable" not in emotion or not isinstance(emotion["moveable"], bool):
        raise _bad("moveable", "boolean", emotion.get("moveable"))
    file_type = str(emotion.get("fileType") or "")
    if file_type not in FILE_TYPES:
        raise _bad("fileType", FILE_TYPES, file_type)

    # INV-08/09/15：格式与动态性必须自洽。
    if file_type in ALWAYS_MOVEABLE and emotion["moveable"] is not True:
        raise EmotionError("EXT_1008_MOVEABLE_MISMATCH",
                           f"fileType={file_type} 必须是动态的")
    if file_type in ALWAYS_STATIC and emotion["moveable"] is not False:
        raise EmotionError("EXT_1008_MOVEABLE_MISMATCH",
                           f"fileType={file_type} 必须是静态的")
    if file_type == "none":
        for forbidden in ("url", "thumbnailUrl", "data"):
            if emotion.get(forbidden):
                raise EmotionError("EXT_1008_MOVEABLE_MISMATCH",
                                   f"fileType=none 不得带 {forbidden}")

    frames = emotion.get("frameCount")
    if frames is not None:
        if not isinstance(frames, int) or frames < 1:
            raise _bad("frameCount", ">=1", frames)
        # INV-10：多帧当且仅当动态。
        if (frames > 1) != (emotion["moveable"] is True):
            raise EmotionError("EXT_1008_MOVEABLE_MISMATCH", "frameCount 与 moveable 矛盾")
    elif file_type in NEEDS_FRAME_COUNT:
        raise EmotionError("EXT_1001_SCHEMA_INVALID", f"fileType={file_type} 必须给出 frameCount")

    if emotion_type == "emoji":
        unicode_text = str(emotion.get("unicode") or "")
        points = emotion.get("codepoints")
        if not unicode_text or not isinstance(points, list):
            raise _bad("unicode/codepoints", "present", None)
        if codepoints(unicode_text) != [int(p) for p in points if isinstance(p, int)]:
            raise EmotionError("EXT_1001_SCHEMA_INVALID", "codepoints 与 unicode 不一致", field="codepoints")
        if emotion["moveable"] is not False:
            raise EmotionError("EXT_1008_MOVEABLE_MISMATCH", "emoji 必须是静态的")

    if emotion_type == "face":
        identifier = str(emotion.get("id") or "")
        if not identifier.isdigit() or not (1 <= len(identifier) <= 6):
            raise _bad("id", "1-6 位十进制", identifier)
        if emotion["moveable"] is not False:
            raise EmotionError("EXT_1008_MOVEABLE_MISMATCH", "face 必须是静态的")

    if emotion_type == "super":
        if not str(emotion.get("id") or "").isdigit():
            raise _bad("id", "十进制数字", emotion.get("id"))

    if emotion_type == "special":
        url = str(emotion.get("url") or "")
        if not url.startswith("https://"):
            raise EmotionError("EXT_1004_URL_NOT_HTTPS", "special 的 url 必须是 https")
        digest = emotion.get("hash") or {}
        if not isinstance(digest, dict) or not digest.get("value"):
            raise _bad("hash", "存在", digest)

    # INV-16：同一事实的两份来源必须一致。比较前统一成字符串——QQ 的
    # AniStickerId 是字符串，而 pack.index 按规范是整数，直接比会永远不等。
    qq = emotion.get("qq") or {}
    if emotion_type == "face" and str(qq.get("qfaceId") or "") not in ("", str(emotion.get("id") or "")):
        raise _bad("qq.qfaceId", emotion.get("id"), qq.get("qfaceId"))
    pack = emotion.get("pack") or {}
    for key, qq_key in (("id", "aniStickerPackId"), ("index", "aniStickerId")):
        left = pack.get(key)
        right = qq.get(qq_key)
        if left in (None, "") or right in (None, ""):
            continue
        if str(left) != str(right):
            raise _bad(f"pack.{key}", right, left)


def mark_degraded(emotion: dict, *reasons: str) -> dict:
    """标出"不完整"而不改写事实。

    收侧宽：缺名字、缺 URL 都要留下对象并说明原因，不能编造（docs/04 §7）。
    """
    if not reasons:
        return emotion
    emotion["degraded"] = True
    existing = list(emotion.get("degradedReasons") or [])
    for reason in reasons:
        if reason not in existing:
            existing.append(reason)
    emotion["degradedReasons"] = existing
    return emotion
