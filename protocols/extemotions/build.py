"""从本地表情库构造 EXTemotions 对象。

``docs/04`` 的识别顺序很重要：先看 Unicode，再看黄脸，最后看动效标记。同一个
数字 ID 可能既是 ``face`` 又是 ``super``（QQ 有 39 例），判据是**有没有动效
资源**，不是 ID 落在哪个区间。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .emotion import (
    SCHEMA,
    VERSION,
    build_uid,
    codepoints,
    detect_file_type,
    emoji_uid,
    mark_degraded,
    moves,
    png_size,
)

# AniStickerType 的语义（附录 A §5）：2 是互动型，结果由平台随机产生。
INTERACTIVE_STICKER_TYPE = 2


def _read_head(path: Path, limit: int = 65536) -> bytes:
    """只读文件头。规范要求嗅探不得整文件读入。"""
    try:
        with path.open("rb") as handle:
            return handle.read(limit)
    except OSError:
        return b""


def _media_fields(path: Path | None) -> dict[str, Any]:
    """从本地资源补出 fileType / moveable / 尺寸 / 帧数 / hash / size。"""
    if path is None or not path.is_file():
        return {}
    head = _read_head(path)
    file_type, info = detect_file_type(head)
    fields: dict[str, Any] = {
        "fileType": file_type,
        "moveable": moves(file_type, info.get("frameCount") if isinstance(info.get("frameCount"), int) else None),
        "size": path.stat().st_size,
    }
    if file_type in ("png", "apng"):
        size = png_size(head)
        if size:
            fields["width"], fields["height"] = size
    if isinstance(info.get("frameCount"), int) and info["frameCount"] > 0:
        fields["frameCount"] = info["frameCount"]
    if "loop" in info:
        fields["loop"] = info["loop"]
    try:
        fields["hash"] = {"alg": "md5", "value": hashlib.md5(path.read_bytes()).hexdigest()}
    except OSError:
        pass
    return fields


def face_emotion(face_id: str, *, name: str = "", entry: dict | None = None,
                 image: Path | None = None, lottie: Path | None = None,
                 animated: bool = False) -> dict[str, Any]:
    """构造一个 ``face`` 或 ``super`` 对象。

    ``animated=True`` 时类型是 ``super``：同一个 QSid 的静态图与动效版是**两个
    不同对象**，``uid`` 也不同（``face:5`` vs ``super:5``）。

    ``fileType`` 永远照实报——Lottie 就是 ``lottie``，静态预览图就是 ``png``。
    把 PNG 说成 APNG 能让 ``moveable`` 自洽，但那是 INV-11 明令禁止的伪造；
    宁可标 ``degraded`` 说"只有静态首帧"，也不谎报格式。
    """
    entry = entry or {}
    sticker_type = entry.get("AniStickerType")
    has_sticker = animated or sticker_type not in (None, "")
    emotion_type = "super" if has_sticker else "face"

    emotion: dict[str, Any] = {
        "schema": SCHEMA,
        "version": VERSION,
        "type": emotion_type,
        "uid": build_uid(emotion_type, str(face_id)),
        "id": str(face_id),
        "source": "system",
    }
    if name:
        emotion["name"] = name

    # Lottie 是 QQ 动效表情的驱动文件，只有动效表情才该用它当主呈现。
    # 黄脸即使库里有同名 JSON 也是别的东西，套上去会把静态表情报成动态。
    if has_sticker and lottie is not None and lottie.is_file():
        emotion["fileType"] = "lottie"
        emotion["moveable"] = True
        emotion["size"] = lottie.stat().st_size
        preview = _media_fields(image)
        if preview:
            rendition: dict[str, Any] = {
                "fileType": preview.get("fileType", "png"),
                "moveable": False,
            }
            if image is not None:
                rendition["size"] = preview.get("size")
            for key in ("width", "height"):
                if key in preview:
                    rendition[key] = preview[key]
            emotion["renditions"] = [rendition]
            for key in ("width", "height"):
                if key in preview:
                    emotion[key] = preview[key]
    else:
        media = _media_fields(image)
        if media:
            emotion.update(media)
        else:
            emotion["fileType"] = "none"
            emotion["moveable"] = False
            mark_degraded(emotion, "url-missing")
        # 动效表情本地只有静态字节时，如实报静态并说明"能看但不会动"。
        # 伪造 APNG 能让 INV-08 过关，但那是 INV-11 禁止的谎报格式。
        if has_sticker and emotion.get("moveable") is False:
            mark_degraded(emotion, "frames-missing")
    if emotion_type == "super" and emotion["fileType"] == "none":
        emotion["fileType"] = "lottie"
        emotion["moveable"] = True

    qq: dict[str, Any] = {"qfaceId": str(face_id)}
    for source_key, target_key in (("EMCode", "emCode"), ("IQLid", "iqlId"), ("AQLid", "aqlId")):
        value = entry.get(source_key)
        if value not in (None, ""):
            qq[target_key] = str(value)
    if has_sticker:
        for source_key, target_key in (("AniStickerPackId", "aniStickerPackId"),
                                       ("AniStickerId", "aniStickerId"),
                                       ("AniStickerPackName", "aniStickerPackName")):
            value = entry.get(source_key)
            if value not in (None, ""):
                qq[target_key] = str(value)
        if sticker_type not in (None, ""):
            qq["aniStickerType"] = int(sticker_type)
        pack_id = entry.get("AniStickerPackId")
        if pack_id not in (None, ""):
            pack: dict[str, Any] = {"id": str(pack_id)}
            if entry.get("AniStickerPackName"):
                pack["name"] = str(entry["AniStickerPackName"])
            if entry.get("AniStickerId") not in (None, ""):
                pack["index"] = int(str(entry["AniStickerId"]))
            emotion["pack"] = pack
        # 互动型表情的结果由平台生成，标记出来以免被当普通图片发。
        if sticker_type not in (None, "") and int(sticker_type) == INTERACTIVE_STICKER_TYPE:
            emotion["interactive"] = {"kind": "random"}

    if qq:
        emotion["qq"] = qq
    emotion["platform"] = "qq"
    emotion.setdefault("summary", "[动画表情]" if emotion["moveable"] else "[表情]")
    return emotion


def emoji_emotion(char: str, *, name: str = "", entry: dict | None = None,
                  image: Path | None = None) -> dict[str, Any]:
    """构造一个 ``emoji`` 对象。``uid`` 用码点，不用平台 ID。"""
    entry = entry or {}
    emotion: dict[str, Any] = {
        "schema": SCHEMA,
        "version": VERSION,
        "type": "emoji",
        "uid": emoji_uid(char),
        "unicode": char,
        "codepoints": codepoints(char),
        "moveable": False,
        "fileType": "none",
        "source": "system",
    }
    if name:
        emotion["name"] = name
    media = _media_fields(image)
    if media:
        # emoji 恒静态，即便资源里有 APNG 也只能报静态：INV-04。
        media["moveable"] = False
        emotion.update(media)
    qq: dict[str, Any] = {}
    for source_key, target_key in (("EMCode", "emCode"), ("QCid", "qCid"), ("AQLid", "aqlId")):
        value = entry.get(source_key)
        if value not in (None, ""):
            qq[target_key] = str(value)
    if qq:
        emotion["qq"] = qq
    emotion["platform"] = "qq"
    return emotion


def special_emotion(url: str, *, name: str = "", content: bytes | None = None,
                    digest: str = "") -> dict[str, Any]:
    """构造一个 ``special`` 对象。用户自定义表情的资源不在内置表里，必须带 URL 与 hash。"""
    value = digest or (hashlib.md5(content).hexdigest() if content else "")
    if not value:
        # 没有内容就算不出 md5；用 URL 派生一个稳定但不会与真实内容哈希混淆的值。
        value = hashlib.md5(url.encode("utf-8")).hexdigest()
    emotion: dict[str, Any] = {
        "schema": SCHEMA,
        "version": VERSION,
        "type": "special",
        "uid": build_uid("special", f"md5-{value}"),
        "id": f"md5-{value}",
        "moveable": False,
        "fileType": "other",
        "source": "user",
        "url": url,
        "hash": {"alg": "md5", "value": value},
        "platform": "qq",
    }
    if name:
        emotion["name"] = name
    if content:
        file_type, info = detect_file_type(content[:65536])
        emotion["fileType"] = file_type
        emotion["moveable"] = moves(file_type, info.get("frameCount") if isinstance(info.get("frameCount"), int) else None)
        emotion["size"] = len(content)
        if isinstance(info.get("frameCount"), int) and info["frameCount"] > 0:
            emotion["frameCount"] = info["frameCount"]
        size = png_size(content)
        if size:
            emotion["width"], emotion["height"] = size
    else:
        emotion = mark_degraded(emotion, "filetype-unknown")
    if not name:
        emotion = mark_degraded(emotion, "name-missing")
    return emotion


def to_text_fallback(emotion: dict) -> str:
    """降级成文本时的占位符，优先级见 docs/01 §2.8。"""
    summary = str(emotion.get("summary") or "")
    if summary:
        return summary
    name = str(emotion.get("name") or "")
    if name:
        return f"[{name}]"
    return "[动画表情]" if emotion.get("moveable") else "[表情]"
