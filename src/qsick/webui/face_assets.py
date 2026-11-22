"""QQ 系统表情资源：定位、内置库兜底。

表情 PNG 优先从项目自带的 ``assets/qq-faces`` 读取（随项目分发，不依赖运行机器
的 QQ 客户端），缺失时再回退到实例自己的 QQ 数据目录。

QQ 的系统表情本身是静态 PNG，客户端不提供 GIF/APNG 版本；动态表情只出现在商城
表情（``mface``）里，那需要网络图片地址，这里不臆造。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

RELATIVE_ROOT = Path("nt_qq") / "nt_data" / "Emoji" / "BaseEmojiSyastems" / "EmojiSystermResource"
EMBEDDED_ROOT = Path(__file__).resolve().parents[3] / "assets" / "qq-faces"
_SAFE_ID = re.compile(r"^[0-9]{1,4}$")


def _normalise(face_id: object) -> str:
    """Canonical decimal id, so "014" and "14" hit the same file."""
    text = str(face_id if face_id is not None else "").strip()
    if not _SAFE_ID.fullmatch(text):
        return ""
    try:
        return str(int(text))
    except ValueError:
        return ""


def embedded_face_path(face_id: object) -> Path | None:
    """项目内置表情库里的图片。

    内置库按字面 id 命名：数字表情是 ``14.png``，emoji 字符表情是 ``😊.png``。
    字符型 id 直接拼文件名，但会先确认结果仍在库目录内。
    """
    text = str(face_id if face_id is not None else "").strip()
    if not text or len(text) > 8:
        return None
    candidates = []
    key = _normalise(text)
    if key:
        candidates.append(key)
    candidates.append(text)
    for name in candidates:
        if not name or "/" in name or "\\" in name or name in (".", ".."):
            continue
        for suffix in (".png", ".gif", ".webp"):
            candidate = EMBEDDED_ROOT / f"{name}{suffix}"
            if candidate.is_file() and candidate.stat().st_size > 0:
                return candidate
    return None


_INDEX_NAME = "index.json"
_INDEX_CACHE: dict[str, tuple[float, frozenset[str]]] = {}


def indexed_ids() -> frozenset[str] | None:
    """导入索引里登记过的表情标识。

    库现在由 ``tools/import_emotions.py`` 严格按语料索引导入，索引文件本身就是
    权威清单：凡不在索引里的 id，库里那张图也一定不是从语料来的。早先那套
    sha256 校验清单已随之废弃——它校验的是同一批文件，现在索引已经说明了来源。
    """
    target = EMBEDDED_ROOT / _INDEX_NAME
    if not target.is_file():
        return None
    try:
        stamp = target.stat().st_mtime
    except OSError:
        return None
    cache_key = str(target)
    cached = _INDEX_CACHE.get(cache_key)
    if cached and cached[0] == stamp:
        return cached[1]
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, list):
        return None
    ids: set[str] = set()
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        for key in ("id", "unicode"):
            value = str(entry.get(key) or "").strip()
            if value:
                ids.add(value)
                if value.isdigit():
                    ids.add(str(int(value)))
    _INDEX_CACHE[cache_key] = (stamp, frozenset(ids))
    return _INDEX_CACHE[cache_key][1]


def is_verified(face_id: object) -> bool:
    """这张内置图是否来自导入索引；索引不可用时放行。"""
    known = indexed_ids()
    if known is None:
        return True
    text = str(face_id if face_id is not None else "").strip()
    return _normalise(text) in known or text in known


def verified_face_path(face_id: object) -> Path | None:
    """只返回索引里登记过的那张内置图。

    闸门防止有人手工往库里塞一张来源不明的图：那种图会让聊天里画出一张错的
    脸，比不显示更糟。
    """
    if not is_verified(face_id):
        return None
    return embedded_face_path(face_id)


def instance_qq_roots(headless_root: str | Path) -> list[Path]:
    """所有实例的 QQ 表情资源目录，最近的优先。"""
    root = Path(headless_root).expanduser()
    if not root.is_dir():
        return []
    found: list[tuple[float, Path]] = []
    for instance in root.iterdir():
        if not instance.is_dir() or not re.fullmatch(r"C\d+", instance.name):
            continue
        qq = instance / "data" / "qq"
        if not qq.is_dir():
            continue
        for account in qq.iterdir():
            if not account.is_dir():
                continue
            resource = account / RELATIVE_ROOT
            if resource.is_dir():
                try:
                    found.append((resource.stat().st_mtime, resource))
                except OSError:
                    continue
    return [path for _, path in sorted(found, reverse=True)]


def client_face_path(headless_root: str | Path, face_id: object) -> Path | None:
    """实例自己的 QQ 资源树里 QQ 为这个 id 提供的图。

    这是最权威的来源：客户端自己画的正是这张。QQ 把表情图放在两处，都要查：

    - ``<account>/nt_data/Emoji/BaseEmojiSyastems/EmojiSystermResource/<id>/png/``
      —— 账号目录下按 id 分文件夹
    - ``qq/nt_qq/global/nt_data/Emoji/emoji-resource/sysface_res/{static,apng}/s<id>.png``
      —— 客户端全局资源，编号也不在账号目录里

    只查前者会漏掉只有后者才有的表情（329 就是），于是明明能收发的表情被显示成
    「无资源」。
    """
    key = _normalise(face_id)
    if not key:
        return None
    for resource in instance_qq_roots(headless_root):
        for candidate in (resource / key / "png" / f"{key}.png", resource / key / f"{key}.png"):
            if candidate.is_file() and candidate.stat().st_size > 0:
                return candidate
    for emoji_root in _global_face_roots(headless_root):
        for variant in ("apng", "static"):
            candidate = emoji_root / variant / f"s{key}.png"
            if candidate.is_file() and candidate.stat().st_size > 0:
                return candidate
    return None


def _global_face_roots(headless_root: str | Path) -> list[Path]:
    """客户端全局的 ``sysface_res`` 目录（与账号无关）。"""
    root = Path(headless_root).expanduser()
    if not root.is_dir():
        return []
    found: list[Path] = []
    for instance in root.iterdir():
        if not instance.is_dir() or not re.fullmatch(r"C\d+", instance.name):
            continue
        for candidate in (instance / "data" / "qq").rglob("sysface_res"):
            if candidate.is_dir():
                found.append(candidate)
        # Newer layouts keep the client tree one level shallower.
        for candidate in instance.rglob("emoji-resource/sysface_res"):
            if candidate.is_dir() and candidate not in found:
                found.append(candidate)
    return found


def face_image_path(headless_root: str | Path, face_id: object) -> Path | None:
    """返回某个系统表情的图片路径，找不到就返回 None。

    QQ 客户端资源先行：它是唯一能证明「这个 id 长什么样」的来源。内置快照
    里来源存疑的 id 也能在这里得到纠正；只有客户端拿不到时，才用内置库里
    经清单校验过的那张，绝不放任未校验的图冒充。
    """
    live = client_face_path(headless_root, face_id)
    if live is not None:
        return live
    return verified_face_path(face_id)


def media_type_of(path: Path) -> str:
    suffix = path.suffix.lower()
    return {".png": "image/png", ".gif": "image/gif", ".webp": "image/webp"}.get(
        suffix, "application/octet-stream"
    )
