"""QQ 系统表情的分栏目录。

分栏的唯一来源是 ``assets/qq-faces/index.json``：它由
``tools/import_emotions.py`` 从上游语料（``表情索引.csv``）严格导入，逐条带着
分类、名称、Unicode、真实文件格式。**不再读 ``normal_emojiids.json`` 那批旧
分组文件**——它们是历代快照，与索引里的 id 集合早已不一致，同时读两份会得到
互相矛盾的选择器。

``recent`` 分组由调用方根据自己的最近使用记录提供。
"""

from __future__ import annotations

import json
from pathlib import Path

from .face_assets import EMBEDDED_ROOT, embedded_face_path, instance_qq_roots

INDEX_NAME = "index.json"

# 索引里的分类 -> 选择器分栏。超级表情与拓展合并成一栏，与 QQ 客户端一致。
GROUPS = (
    ("normal", "小黄脸", ("小黄脸",)),
    ("super", "超级表情", ("超级表情", "超级表情-拓展")),
    ("emoji", "emoji", ("emoji",)),
)

_CACHE: dict[str, tuple[float, list[dict]]] = {}


def _load_index(directory: Path) -> list[dict]:
    """读取并缓存 ``index.json``。"""
    target = directory / INDEX_NAME
    if not target.is_file():
        return []
    try:
        stamp = target.stat().st_mtime
    except OSError:
        return []
    cache_key = str(target)
    cached = _CACHE.get(cache_key)
    if cached and cached[0] == stamp:
        return cached[1]
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    entries = [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []
    _CACHE[cache_key] = (stamp, entries)
    return entries


def _directories(headless_root: str | Path) -> list[Path]:
    """内置目录优先，其次才是各实例的 QQ 资源目录。"""
    dirs: list[Path] = []
    if EMBEDDED_ROOT.is_dir():
        dirs.append(EMBEDDED_ROOT)
    dirs.extend(instance_qq_roots(headless_root))
    return dirs


def catalog(headless_root: str | Path) -> dict[str, list[dict[str, str]]]:
    """按分栏返回表情清单，来源是导入索引。

    每条都是 ``describe_index`` 的结果，格式与动态性直接取自导入时的字节嗅探，
    不再二次推断：同一份资源被导入工具看过一次就够了，运行时再猜一遍只会制造
    两份可能不一致的结论。
    """
    entries: list[dict] = []
    for directory in _directories(headless_root):
        entries = _load_index(directory)
        if entries:
            break

    # index.json 只描述资源文件（格式、尺寸、动态性），发送所需的贴纸三元组在
    # QQ 自己的 face_config 里。两者按 id 合并，否则选择器无从判断"这个动态表情
    # 到底发不发得出去"。
    stickers = emoji_index()
    result: dict[str, list[dict[str, object]]] = {key: [] for key, _label, _cats in GROUPS}
    for entry in entries:
        category = str(entry.get("category") or "")
        merged = dict(entry)
        side = stickers.get(str(entry.get("id") or ""))
        if isinstance(side, dict):
            for flag in ("AniStickerPackId", "AniStickerId", "AniStickerType"):
                if side.get(flag) not in (None, ""):
                    merged[flag] = side[flag]
        for key, _label, categories in GROUPS:
            if category in categories:
                result[key].append(describe_index(merged))
                break
    return result


def describe_index(entry: dict) -> dict[str, object]:
    """把一条表情记录变成选择器/发送方要用的描述。

    记录来自两份来源，字段名不同，这里统一兼容：

    * ``index.json`` —— ``id`` / ``name`` / ``fileType``
    * ``face_config.json``（``emoji_index`` 的条目）—— ``QSid`` / ``QDes`` / ``fileType``

    只认一种会让 ``id`` 变成空串，进而把动态表情判成静态、把命名整条丢掉。
    """
    identifier = str(entry.get("unicode") or entry.get("id") or entry.get("QSid") or "")
    moveable = bool(entry.get("moveable"))
    file_type = str(entry.get("fileType") or entry.get("file_type") or "png")
    name = str(entry.get("name") or entry.get("QDes") or "").lstrip("/")
    described: dict[str, object] = {
        "id": identifier,
        "format": "animated" if moveable else "static",
        "animated": moveable,
        "file_type": file_type,
        "name": name,
        "category": str(entry.get("category") or ""),
    }
    for key in ("width", "height", "size", "frameCount"):
        if entry.get(key) is not None:
            described[key] = entry[key]
    extras = entry.get("extras") or {}
    if extras:
        described["renditions"] = sorted(extras)
    # 能否按"动效"发出去，取决于有没有贴纸三元组：QQNT 的 AniSticke 通道要求
    # pack/sticker/type 三件齐全，缺一个就只能当静态黄脸发（会被 QQ 重映射成
    # 另一张画）。选择器据此标注，免得用户点了才在发送时收到拒绝。
    triple = (entry.get("AniStickerPackId"), entry.get("AniStickerId"),
              entry.get("AniStickerType"))
    described["sendable_as_animated"] = all(
        str(value or "").strip() for value in triple) and bool(described["animated"])
    unicode_text = str(entry.get("unicode") or "")
    if unicode_text:
        described["unicode"] = unicode_text
        described["codepoints"] = [ord(char) for char in unicode_text
                                   if char not in ("\ufe0e", "\ufe0f")]
    return described


_EMOJI_INDEX: dict[str, dict] | None = None


def emoji_index() -> dict[str, dict]:
    """QSid/字符 -> 索引条目。

    发送与命名都靠它。数据源是导入后的 ``index.json`` 与派生出的
    ``face_config.json``——前者给真实格式与名称，后者给 QQ 侧的编号映射。
    """
    global _EMOJI_INDEX
    if _EMOJI_INDEX is not None:
        return _EMOJI_INDEX
    index: dict[str, dict] = {}
    for directory in (EMBEDDED_ROOT,):
        target = directory / "face_config.json"
        if not target.is_file():
            continue
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        # Both groups matter. ``sysface`` holds the numbered faces, while
        # ``emoji`` holds the same faces keyed by their Unicode glyph — those are
        # what QQ matches on when a user types the character itself, so leaving
        # the group out silently made every glyph-keyed face unsendable.
        for group in ("sysface", "emoji"):
            for entry in payload.get(group) or []:
                qsid = str(entry.get("QSid") or "")
                if qsid:
                    index[qsid] = entry
        break
    _EMOJI_INDEX = index
    return index


_GLYPHS: frozenset[str] | None = None


def glyph_faces() -> frozenset[str]:
    """可以被字符本身匹配的表情（QQ 的 ``emoji`` 分组以 Unicode 字符为 id）。

    用户直接输入或粘贴这些字符时，QQ 会把它当作表情；发送路径也照做，
    否则同一个字符在这里发出去就是纯文本，和客户端表现不一致。
    """
    global _GLYPHS
    if _GLYPHS is None:
        _GLYPHS = frozenset(key for key in emoji_index() if key and not key.isdigit())
    return _GLYPHS


def face_send_meta(face_id: object) -> dict[str, object]:
    """构造发送 face 段所需的字段。

    动态表情必须带贴纸三元组，否则 QQ 会静默改写 id。索引里没有三元组的表情
    （纯静态黄脸）就不带——空串不是"0"，硬转 int 会直接抛异常。
    """
    key = str(face_id or "").strip()
    entry = emoji_index().get(key)
    if not entry:
        return {"id": key}
    meta: dict[str, object] = {"id": key}
    description = str(entry.get("QDes") or "").lstrip("/")
    if description:
        meta["text"] = description
    sticker_type = str(entry.get("AniStickerType") or "").strip()
    if sticker_type.isdigit():
        meta["sticker_type"] = int(sticker_type)
        sticker_id = str(entry.get("AniStickerId") or "").strip()
        if sticker_id:
            meta["sticker_id"] = sticker_id
        pack_id = str(entry.get("AniStickerPackId") or "").strip()
        if pack_id:
            meta["pack_id"] = pack_id
    return meta


def labels() -> dict[str, str]:
    """分栏键 -> 显示名，供前端渲染标签。"""
    return {key: label for key, label, _categories in GROUPS}
