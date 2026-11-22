"""Account-scoped chat views over persisted messages, without live lookups.

The store has no account-filtered query API. Scan its complete retained history
in pages, then filter and sort by message time/ID, not insertion order. This is
intentionally O(retained messages), rather than hiding quiet accounts behind a
global recent-message limit. No transport is used except an explicit send.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path as _FilePath
from typing import Any, Callable, Iterator
from urllib.parse import quote as urlquote, urlparse

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from ..protocol.extemotion import EmotionMarkupError, split_segments
from ..protocol.message import Element, Message
from ..storage import MemoryStore, MessageStore
from . import face_catalog
from .face_assets import face_image_path, media_type_of
from .face_animation import animation_path, animation_payload
from .face_catalog import face_send_meta, glyph_faces
from .face_table import face_descriptor


def _emotion_for(face_id: str) -> dict[str, Any]:
    """把只有 id 的表情补成完整的 EXTemotions 对象。

    聊天栏只需要知道"用户点了哪个表情"，补全类型、动态性、真实文件格式是这里
    的职责：数据来自导入索引，不猜。补不出来就照实标记，绝不编造。
    """
    key = str(face_id or "").strip()
    entry = face_catalog.emoji_index().get(key) or {}
    described = face_catalog.describe_index(entry) if entry else {}
    meta = face_send_meta(key)

    emotion: dict[str, Any] = {
        "schema": "EXTemotions",
        "version": "1.0",
        "id": key,
    }
    # 类型判定按 docs/04 的顺序：先 Unicode（emoji），再看平台结构标记。
    # 判据是**有没有动效资源**，不是 id 落在哪个区间——同一个 QSid 可能既是
    # 黄脸又是超级表情（QQ 有 39 例），按区间猜必然判错。
    has_sticker = "sticker_type" in meta
    # 索引标了动态（有 APNG/Lottie 资源）但没有贴纸三元组的，也是超级表情：
    # 419+ 那批拓展表情就属于这一类，它们的动效由资源本身承载。
    resource_animated = bool(described.get("animated"))
    if key and not key.isdigit():
        emotion["type"] = "emoji"
        emotion["unicode"] = key
        emotion["codepoints"] = [ord(char) for char in key if char not in ("\ufe0e", "\ufe0f")]
    elif has_sticker or resource_animated:
        emotion["type"] = "super"
    else:
        emotion["type"] = "face"
    emotion["uid"] = f"{emotion['type']}:{key}"

    # ``fileType`` 必须照实报本地真实字节：有的超级表情只有静态首帧 PNG，把它
    # 说成 lottie 就是谎报格式（INV-11）。"会不会动"由 moveable 表达，"有没有
    # 动效文件"由 fileType 表达，两者不能互相顶替。
    emotion["moveable"] = emotion["type"] == "super"
    file_type = str(described.get("file_type") or "")
    if not file_type:
        file_type = "png"
    emotion["fileType"] = file_type

    if described.get("name"):
        emotion["name"] = str(described["name"])
    for source, target in (("width", "width"), ("height", "height"),
                           ("size", "size"), ("frameCount", "frameCount")):
        if described.get(source) is not None:
            emotion[target] = described[source]
    if meta.get("text"):
        emotion.setdefault("name", str(meta["text"]))

    for flag, target in (("pack_id", "pack_id"), ("sticker_id", "sticker_id"),
                         ("sticker_type", "sticker_type")):
        if meta.get(flag) is not None:
            emotion[target] = meta[flag]
    if has_sticker:
        emotion.setdefault("qq", {})
        emotion["qq"].update({
            "qfaceId": key,
            "aniStickerPackId": str(meta.get("pack_id") or ""),
            "aniStickerId": str(meta.get("sticker_id") or ""),
            "aniStickerType": meta.get("sticker_type"),
        })
        emotion["pack"] = {"id": str(meta.get("pack_id") or ""),
                           "index": meta.get("sticker_id")}
    elif key.isdigit():
        emotion["qq"] = {"qfaceId": key}
    emotion["platform"] = "qq"
    return emotion


def _chat_parts(text: str) -> list[dict[str, Any]]:
    """输入框正文 -> 内部部件（协议无关）。

    表情在这里就已经是 EXTemotions 对象，具体编码成哪种协议段由消息处理器按
    该网络配置选定的协议决定。聊天栏因此不必知道对端说的是纯 v11 还是
    v11+EXTemotions。
    """
    try:
        segments = split_segments(text)
    except EmotionMarkupError as exc:
        raise ChatError(str(exc), 400) from exc

    parts: list[dict[str, Any]] = []
    for segment in segments:
        if segment["type"] != "emotion":
            parts.extend(_glyph_parts(str(segment["data"].get("text") or "")))
            continue
        marker = segment["data"]
        emotion = _emotion_for(str(marker.get("id") or ""))
        # ext_emotion 标记里带的是完整 emotion 对象：那是调用方明确写下的意图，
        # 优先于目录。旧 [CQ:face] 只有一个 id（declared=False），此时一律以目录
        # 为准——协议没说的事不该在这里替它决定。
        if marker.get("declared") is not False:
            for key, value in marker.items():
                if key in ("declared", "id") or value in (None, "", {}, []):
                    continue
                emotion[key] = value
        # 声明成动态却没有贴纸三元组：QQNT 的动效通道（AniSticke）要求三件齐全，
        # 缺一个就没法按动效发。这类表情在 419+ 的"拓展"里有一大批，它们的动效
        # 由资源本身承载，QQ 侧没有对应的贴纸编号。
        #
        # 不能因此拒绝——用户只是想发一个表情，整条消息失败是更糟的结果。改为
        # 静态降级：id 照发，movable/fileType 如实改写成"这是静态的"，绝不谎称
        # 自己是动态（谎报格式比降级危险得多）。
        if emotion.get("moveable") is True:
            missing = [flag for flag in ("pack_id", "sticker_id", "sticker_type")
                       if emotion.get(flag) in (None, "")]
            if missing:
                emotion["moveable"] = False
                emotion["degraded"] = True
                reasons = list(emotion.get("degradedReasons") or [])
                if "sticker-missing" not in reasons:
                    reasons.append("sticker-missing")
                emotion["degradedReasons"] = reasons
                # 有 APNG 源就仍报 apng（字节是真的），只是不当动效发。
                if str(emotion.get("fileType") or "") not in ("apng", "lottie", "gif", "webp"):
                    emotion["fileType"] = "png"
        parts.append({"kind": "emotion", "emotion": emotion})
    return parts or [{"kind": "text", "text": text}]


def _encode_chat_parts(text: str) -> list[dict[str, Any]]:
    """把聊天栏正文编码成对端协议的消息段。

    处理器按默认协议编码——聊天栏是本机消费方，它一直说
    ``onebotv11-EXTemotions``。分发到具体网络配置时，各配置按自己选的协议
    另行编码（见 ``protocols/processor.py``）。
    """
    parts = _chat_parts(text)
    processor = _chat_processor()
    return processor.encode_outbound(parts)


_CHAT_PROCESSOR: Any = None


def _chat_processor():
    """聊天栏使用的消息处理器（惰性构造，避免导入期就加载协议包）。

    走 :mod:`qsick.protocol_bridge` 而不是直接 ``import protocols``：``protocols``
    在项目根、不在 ``src`` 下，``uv run qsick`` / ``qsick.exe`` 起的进程里
    ``sys.path`` 不含项目根，直接导入会 ``ModuleNotFoundError``，而这条路径对
    **每一条**消息都会走——纯文字也发不出去。
    """
    global _CHAT_PROCESSOR
    if _CHAT_PROCESSOR is None:
        from ..protocol_bridge import load_processor

        _CHAT_PROCESSOR = load_processor("onebotv11-EXTemotions",
                                         enrich=lambda emotion: emotion)
    return _CHAT_PROCESSOR


def _glyph_parts(text: str) -> list[dict[str, Any]]:
    """把纯文字里的表情字符变成 emotion 部件。

    QQ 的 emoji 分组以字符本身为键，同一个字符当普通文字发出去，客户端却会画成
    表情——所以这里必须拆开，否则"看着一样、实际是两种东西"。
    """
    if not text:
        return []
    glyphs = glyph_faces()
    if not glyphs or not any(char in glyphs for char in text):
        return [{"kind": "text", "text": text}]
    parts: list[dict[str, Any]] = []
    buffer: list[str] = []
    for char in text:
        if char in glyphs:
            if buffer:
                parts.append({"kind": "text", "text": "".join(buffer)})
                buffer = []
            parts.append({"kind": "emotion", "emotion": _emotion_for(char)})
        else:
            buffer.append(char)
    if buffer:
        parts.append({"kind": "text", "text": "".join(buffer)})
    return parts


def _looks_like_image(data: bytes) -> bool:
    """Magic-byte sniffing; the extension is not trusted."""
    return (
        data.startswith(b"\x89PNG\r\n\x1a\n")
        or data.startswith(b"\xff\xd8\xff")
        or data.startswith(b"GIF87a")
        or data.startswith(b"GIF89a")
        or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")
        or data.startswith(b"BM")
    )


#: 魔数 -> MIME。收藏表情在 QQ 的媒体树里经常没有扩展名（文件名是 md5 或
#: res_id），此时 mimetypes 只能猜出 application/octet-stream。
_IMAGE_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
)


def _sniff_image_type(path: Any) -> str:
    """Best-effort MIME from the first bytes of a file ("" when unknown)."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(16)
    except OSError:
        return ""
    for magic, media in _IMAGE_MAGIC:
        if head.startswith(magic):
            return media
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return ""



class ChatError(ValueError):
    def __init__(self, error: str, status: int = 400) -> None:
        super().__init__(error)
        self.status = status


def _identifier(value: Any, name: str) -> str:
    # Reject booleans, floats, objects, zero and non-decimal spellings. Strings
    # preserve QQ/message IDs beyond JavaScript's exact-integer range.
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ChatError(f"{name} must be a positive decimal identifier")
    text = str(value).strip()
    if not re.fullmatch(r"[0-9]{1,20}", text) or int(text) <= 0:
        raise ChatError(f"{name} must be a positive decimal identifier")
    return str(int(text))


def _chat_type(value: Any) -> str:
    if value not in ("group", "private"):
        raise ChatError("chat_type must be group or private")
    return value


def _all_messages(store: MessageStore) -> Iterator[Message]:
    total = store.count()
    if isinstance(store, MemoryStore):
        # MemoryStore.search currently iterates dictionary keys rather than
        # messages. Its public recent(count) method provides a complete snapshot.
        yield from store.recent(max(1, total))
        return
    for offset in range(0, total, 500):
        # Do not stop on a short decoded page: SQLite can skip malformed rows.
        yield from store.search(query="", offset=offset, limit=500)


def _iter_conversation(store: MessageStore, chat_type: str, chat_id: str,
                       account: str) -> Iterator[Message]:
    """只取某个会话的消息，不再全库扫描。

    以前 ``_scoped_messages`` 会把整个库读出来（每条都反序列化成 ``Message``），
    再在 Python 里按会话过滤。库大了之后，每 3 秒一次的轮询都在做这件事，
    聊天栏因此明显卡顿——翻一页 80 条却读了全库。

    实例库现在已经按实例分开（``data/instances/C1/messages.db``），所以这里
    直接按会话查询，顺带省掉按账号过滤的开销。
    """
    depth = 200
    before: Any = None
    while True:
        page = store.page(chat_type, chat_id, limit=depth, before=before) \
            if hasattr(store, "page") else _page_fallback(store, account, chat_type, chat_id, depth, before)
        if not page:
            return
        for message in page:
            if str(message.account) == account or not message.account:
                yield message
        if len(page) < depth:
            return
        before = page[0].message_id


def _page_fallback(store: MessageStore, account: str, chat_type: str, chat_id: str,
                   limit: int, before: Any) -> list[Message]:
    """没有 ``page()`` 的存储（MemoryStore）退回到过滤全量。

    保留这条路径是为了不破坏自定义存储实现；内置的 SQLite/maxStore 都走
    ``page()``，不会再全表扫描。
    """
    rows = [
        message for message in _scoped_messages(store, account)
        if message.chat_type == chat_type and str(message.chat_id) == chat_id
    ]
    if before is not None:
        rows = [message for message in rows if str(message.message_id) < str(before)]
    return rows[-limit:]


def _scoped_messages(store: MessageStore, account: str) -> list[Message]:
    return sorted(
        (
            message for message in _all_messages(store)
            if str(message.account) == account
            and message.chat_type in ("group", "private")
            and message.chat_id > 0
        ),
        key=lambda message: (message.time, message.message_id),
    )


_MEDIA_LABELS = {"image": "图片", "record": "语音", "video": "视频", "file": "文件", "face": "表情",
                 "mface": "表情", "poke": "戳一戳", "dice": "骰子", "rps": "猜拳"}


IMAGE_HOST = "https://gchat.qpic.cn"


def _is_within(target: Any, root: Any) -> bool:
    """``target`` 是否落在 ``root`` 之内（含 root 本身）。

    比较前两边都要 ``resolve()``：符号链接与 ``..`` 都是绕过路径检查的常用手段，
    只做字符串前缀判断会被 ``/allowed/../../etc/passwd`` 之类骗过。
    """
    try:
        target.relative_to(root)
        return True
    except (ValueError, OSError):
        return False


def _media_roots(runtime: Any) -> list[Any]:
    """允许被 ``/api/chat/media`` 读取的根目录。

    QQ 的媒体树在实例目录下（``headlessClient/C0/data/qq/...``）。读取时不假设
    具体层级，而是从 headless_root 出发收集所有实例的 data 目录——那里既有
    下载的图片，也有 QQ 自己缓存的缩略图。
    """
    roots: list[Any] = []
    root = _FilePath(str(getattr(runtime.config, "headless_root", "") or "")).expanduser()
    if not root.is_absolute():
        root = _FilePath.cwd() / root
    if not root.is_dir():
        return roots
    try:
        for instance in sorted(root.iterdir()):
            data = instance / "data"
            if data.is_dir():
                roots.append(data)
    except OSError:
        return roots
    return roots


def _absolute_media_url(url: str) -> str:
    """把 QQ 的媒体地址变成浏览器真的能加载的东西。

    QQ 返回的是 ``/download?appid=1407&fileid=...&spec=0`` 这种只有它自己主机才
    认得的裸路径。以前这里把它拼成 ``gchat.qpic.cn/download?...`` 直接交给浏览器，
    但那个地址需要随请求轮换的 rkey，裸访问必然失败——Network 里那些
    ``download?appid=... ERR_BLOCKED`` 就是这么来的。

    现在改走同源代理：服务端带着自己的凭据去取，浏览器只和本机通信。代理解不
    出来时（没 rkey、图片过期）会返回一个明确的错误，前端据此显示占位并允许
    重试，而不是静默画一个破图。
    """
    if not url:
        return ""
    if url.startswith("/download") or url.startswith("/channel"):
        target = IMAGE_HOST + url
        return "/api/chat/remote-media?url=" + urlquote(target, safe="")
    if url.startswith("//"):
        return "/api/chat/remote-media?url=" + urlquote("https:" + url, safe="")
    if url.startswith("http://") or url.startswith("https://"):
        return "/api/chat/remote-media?url=" + urlquote(url, safe="")
    if url.startswith("/"):
        return IMAGE_HOST + url
    return url


def _segment(element: Any, quote: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
             face_root: str = "") -> dict[str, Any] | None:
    """One renderable segment for the chat UI.

    The browser must never need to re-parse text to find structure: mentions,
    quotes, markdown and media are emitted as typed segments. Text stays raw and
    is rendered with textContent.
    """
    kind = element.type
    data = element.data or {}
    if kind == "text":
        value = str(data.get("text", ""))
        return {"type": "text", "text": value} if value else None
    if kind == "at":
        return {
            "type": "at",
            "user_id": str(data.get("user_id", "")),
            "uid": str(data.get("uid", "")),
            "name": str(data.get("name", "")),
        }
    if kind == "reply":
        message_id = str(data.get("message_id", data.get("id", "")) or "")
        segment = {
            "type": "reply",
            "message_id": message_id,
            # Native replies from other members often carry only a sequence and
            # sender (no replayMsgId), so both are forwarded for fallback lookup.
            "seq": str(data.get("seq", "") or ""),
            "sender_uin": str(data.get("sender_uin", "") or ""),
        }
        # Resolve the quoted message so the UI can show its real content instead
        # of a placeholder; "found": false tells the client it cannot jump there.
        segment["quote"] = (quote(segment) if quote else None) or {"found": False}
        return segment
    if kind == "markdown":
        return {"type": "markdown", "content": str(data.get("content", data.get("text", "")))}
    if kind in ("image", "mface", "face"):
        # 表情（sub_type 1/2）的默认标签不能是"图片"：QQ 常常只给一个空的
        # summary，而这里的兜底在判断 sticker 之前就执行了，于是动画表情在
        # 聊天栏里永远显示成 [图片]。先判定子类型，再决定兜底文案。
        sub_type = str(data.get("sub_type", data.get("pic_sub_type", "0")) or "0")
        is_sticker = kind in ("mface", "face") or sub_type in ("1", "2")
        fallback = "动画表情" if is_sticker else _MEDIA_LABELS.get(kind, kind)
        segment = {
            "type": kind,
            "file_id": str(data.get("file_id", data.get("file", ""))),
            "url": str(data.get("url", "")),
            "summary": str(data.get("summary", "")) or fallback,
        }
        if kind == "image":
            segment["width"] = data.get("width")
            segment["height"] = data.get("height")
            segment["file_name"] = str(data.get("file_name", ""))
            segment["element_id"] = str(data.get("element_id", ""))
            # QQ returns a GPU-relative path such as "/download?appid=.."; the
            # browser would resolve that against the WebUI origin and 404.
            #
            # 三个候选按可靠性排序：``local_path`` 是 QQ 已下载的副本，
            # ``source_path`` 是它记录的原始落盘位置（旧消息常常只有这个），
            # 缩略图次之。任一个真实存在就不必让用户再点「重新获取」。
            local = ""
            for key in ("local_path", "source_path"):
                candidate = str(data.get(key) or "")
                if candidate and _FilePath(candidate).is_file():
                    local = candidate
                    break
            if not local:
                for _spec, candidate in ((data.get("thumbnails") or {}).get("$map") or []):
                    text = str(candidate or "")
                    if text and _FilePath(text).is_file():
                        local = text
                        break
            segment["local_path"] = local
            if local:
                # Prefer the file QQ actually downloaded: the remote URL needs a
                # rotating rkey that the headless bridge cannot fetch.
                segment["url"] = "/api/chat/media?path=" + urlquote(local, safe="")
            elif segment["url"]:
                # No downloaded copy: fall back to QQ's URL. It needs a rotating
                # rkey, so it may fail to load — the client then shows the label.
                segment["url"] = _absolute_media_url(segment["url"])
                segment["media_pending"] = True
            # Animated stickers arrive as picture elements with sub_type 1.
            segment["sticker"] = sub_type in ("1", "2")
            if segment["sticker"] and not segment["summary"]:
                segment["summary"] = "动画表情"
        if kind == "face":
            face_id = str(data.get("id", data.get("face_id", "")))
            segment["id"] = face_id
            segment["result_id"] = str(data.get("result_id", ""))
            # Render the built-in QQ face index instead of a "[表情]" placeholder.
            described = face_descriptor(face_id)
            segment["name"] = described["name"]
            segment["emoji"] = described["emoji"]
            # Prefer QQ's own PNG so the UI shows the authentic face; when the
            # library has no art for this id the client shows a version notice.
            image = face_image_path(face_root or "", face_id)
            segment["face_available"] = image is not None
            if image is not None:
                segment["face_url"] = "/api/chat/face?id=" + urlquote(face_id, safe="")
                # Animated faces are APNG (acTL chunk) or gif/webp; the client must
                # not lazily defer them or they freeze on the first frame.
                suffix = image.suffix.lower()
                animated = suffix in (".gif", ".webp")
                if suffix == ".png":
                    try:
                        with image.open("rb") as handle:
                            animated = b"acTL" in handle.read(64 * 1024)
                    except OSError:
                        animated = False
                segment["face_animated"] = animated
            # Real QQ motion comes from the Lottie file shipped beside the faces;
            # the PNG stays as the static poster while that loads.
            animation = animation_payload(face_root or "", face_id)
            if animation is not None:
                segment["face_animation"] = animation["url"]
                segment["face_animated"] = True
                if animation["duration"]:
                    segment["face_duration"] = animation["duration"]
            if not segment["summary"] or segment["summary"] == _MEDIA_LABELS["face"]:
                segment["summary"] = described["name"]
        if kind == "mface":
            emoji_id = str(data.get("emoji_id", ""))
            segment["emoji_id"] = emoji_id
            segment["key"] = str(data.get("key", ""))
            segment["market_face"] = True
            # QQ's stored-face URLs are keyed on a rotating rkey, and the old
            # "parcel/item" template only ever answered 404. Never synthesise a
            # URL: show the face name and let the client offer a download instead.
            segment["id"] = emoji_id
            if segment["url"]:
                segment["url"] = _absolute_media_url(segment["url"])
            segment["summary"] = segment["summary"] or "表情包"
        return segment
    if kind in ("record", "video", "file", "forward", "json", "xml", "share", "node"):
        return {
            "type": kind,
            "file_id": str(data.get("file_id", "")),
            "url": str(data.get("url", "")),
            "name": str(data.get("name", data.get("file_name", ""))),
            "summary": str(data.get("summary", "")) or _MEDIA_LABELS.get(kind, kind),
        }
    return {"type": "unsupported", "original_type": kind, "summary": _MEDIA_LABELS.get(kind, kind)}


def _segments(message: Message, quote: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
              face_root: str = "") -> list[dict[str, Any]]:
    segments = []
    for element in message.elements:
        produced = _expand_text_face(element, face_root)
        if produced is None:
            segment = _segment(element, quote, face_root)
            if segment:
                segments.append(segment)
            continue
        segments.extend(produced)
    return segments or [{"type": "unsupported", "original_type": "", "summary": "空消息"}]


# QQ sometimes hands a system face over as markup inside a text element. Messages
# stored before the codec learned to split it still hold that markup, so the read
# path splits it too instead of showing the raw tag to the reader.
_TEXT_FACE = re.compile(r'<faceType=(\d+),faceId="(\d+)"(?:,ext="([^"]*)")?>')


def _expand_text_face(element: Any, face_root: str) -> list[dict[str, Any]] | None:
    if element.type != "text":
        return None
    content = str(element.data.get("text") or "")
    if "<faceType=" not in content:
        return None
    produced: list[dict[str, Any]] = []
    cursor = 0
    for match in _TEXT_FACE.finditer(content):
        if match.start() > cursor:
            produced.append({"type": "text", "text": content[cursor:match.start()]})
        # Reuse the ordinary face rendering so the face art, name and animation
        # all follow the same path as a face that arrived properly.
        segment = _segment(Element("face", {"id": match.group(2)}), None, face_root)
        if segment:
            produced.append(segment)
        cursor = match.end()
    if cursor < len(content):
        produced.append({"type": "text", "text": content[cursor:]})
    return produced or None


def _text(message: Message) -> str:
    # Human-readable preview: mentions keep their display name, everything else
    # becomes a bracketed label instead of a bare element type.
    parts = []
    for element in message.elements:
        if element.type == "text":
            parts.append(str(element.data.get("text", "")))
        elif element.type == "at":
            name = str(element.data.get("name", "")).strip() or str(element.data.get("user_id", ""))
            parts.append("@" + (name.lstrip("@") or "全体成员"))
        elif element.type == "face":
            parts.append("[" + face_descriptor(element.data.get("id", element.data.get("face_id", "")))["name"] + "]")
        elif element.type == "markdown":
            parts.append(str(element.data.get("content", "")))
        else:
            parts.append("[" + _MEDIA_LABELS.get(element.type, element.type) + "]")
    return "".join(parts).strip()


def _sender(message: Message) -> str:
    return message.sender_name or message.card or str(message.sender_uin)


def _recent_in_chat(store: MessageStore, chat_id: str, limit: int = 60) -> list[Message]:
    """某个私聊会话里最近的若干条消息。

    有 ``page()`` 的存储走索引查询；没有的（MemoryStore、自定义实现）退回到
    过滤一次，数据量小，代价可接受。
    """
    if hasattr(store, "page"):
        return list(store.page("private", chat_id, limit=limit, before=None))
    return [message for message in _all_messages(store)
            if message.chat_type == "private" and str(message.chat_id) == str(chat_id)][-limit:]


def _conversations(store: MessageStore, account: str, pins: dict[str, Any] | None = None) -> dict[str, Any]:
    pins = pins or {}
    # 只取每个会话的最新一条，不再把整个库读出来。maxStore 用窗口函数一次算完；
    # 其它存储退回到原来的遍历（数据量小，代价可接受）。
    summaries = store.conversation_summaries(account) if hasattr(store, "conversation_summaries") else None
    latest: dict[tuple[str, int], Message] = {}
    private_names: dict[int, str] = {}

    if summaries is not None:
        for item in summaries:
            message = item["message"]
            if str(message.account) not in ("", account):
                continue
            latest[(message.chat_type, message.chat_id)] = message
    else:
        for message in _scoped_messages(store, account):
            latest[(message.chat_type, message.chat_id)] = message

    # 私聊标题要用对端昵称。自己发出的私聊消息里 sender 就是自己，不能用它命名；
    # 而对端昵称往往只出现在更早的消息里，所以两种来源都要看：
    #   1. 最新一条若来自对端（sender_uin == chat_id），直接用；
    #   2. 否则按会话往前找一条来自对端的消息（走 page()，不全库扫描）。
    for (chat_type, chat_id), message in latest.items():
        if chat_type != "private":
            continue
        if message.sender_uin == chat_id and (message.sender_name or message.card):
            private_names[chat_id] = message.sender_name or message.card
            continue
        for older in _recent_in_chat(store, chat_id, limit=60):
            if older.sender_uin == chat_id and (older.sender_name or older.card):
                private_names[chat_id] = older.sender_name or older.card
                break

    items = []
    for message in sorted(latest.values(), key=lambda item: (item.time, item.message_id), reverse=True):
        title = f"群 {message.chat_id}" if message.is_group else private_names.get(message.chat_id, f"QQ {message.chat_id}")
        key = f"{account}:{message.chat_type}:{message.chat_id}"
        hidden_marker = pins.get("hidden:" + key)
        # A hidden conversation reappears as soon as a newer message arrives.
        # Compare message IDs, not wall-clock time: stored messages can carry
        # historical timestamps and would otherwise stay hidden forever.
        if hidden_marker is not None and str(hidden_marker) == str(message.message_id):
            continue
        items.append({
            "id": str(message.chat_id),
            "chat_type": message.chat_type,
            "title": title,
            "preview": _text(message)[:160],
            "last_time": message.time,
            "sender": _sender(message),
            "last_message_id": str(message.message_id),
            "pinned": bool(pins.get(key)),
        })
    # Pinned conversations float to the top, keeping recency order within groups.
    items.sort(key=lambda item: (not item["pinned"], -item["last_time"]))
    return {"conversations": items}


def _history_after(store: MessageStore, account: str, chat_type: str, chat_id: str, limit: int,
                   after: str, face_root: str = "") -> dict[str, Any]:
    """增量：只返回比 ``after`` 更新的消息（聊天栏轮询用）。

    轮询走这条路径时不再整页重拉：后端只读一小段，前端只追加新的几条，
    渲染量从"每次 80 条重建"降到"通常 0 条"。
    """
    if not str(after).isdigit():
        raise ChatError("after must be a message id", 400)
    cursor = int(after)
    if hasattr(store, "page_after"):
        messages = store.page_after(chat_type, str(chat_id), after_id=cursor,
                                    limit=max(1, min(200, limit)), account=account)
    else:
        messages = [
            message for message in _scoped_messages(store, account)
            if message.chat_type == chat_type and str(message.chat_id) == chat_id
            and int(message.message_id) > cursor
        ][: max(1, limit)]
    # 与完整版走同一个渲染函数，保证两条路径产出的结构一模一样。
    return {
        "messages": [{
            "message_id": str(message.message_id),
            "seq": str(message.seq),
            "sender_uin": str(message.sender_uin),
            "sender_uid": message.sender_uid,
            "sender": _sender(message),
            "card": message.card,
            "role": message.role,
            "text": _text(message),
            "segments": _segments(message, None, face_root),
            "time": message.time,
            "outgoing": str(message.sender_uin) == account,
        } for message in messages],
        "has_more": False,
        "next_before": None,
        "incremental": True,
    }


def _history(store: MessageStore, account: str, chat_type: str, chat_id: str, limit: int, before: str | None,
             face_root: str = "") -> dict[str, Any]:
    # 只取需要的那一段，不再把整个会话（更不是整个库）读进内存。
    #
    # 做法：先按会话把游标定位出来，再取它之前的一页。
    #   - 没给 before：取最新的一页（多留些余量给引用解析）；
    #   - 给了 before：先确认它属于这个会话，再取它之前的一页。
    # 之所以不用"取一页再过滤"，是因为库里可能混着别的账号的消息（升级前的
    # 主库就是这样），窗口会被别人的消息占满，于是"取一页"变成"取不到"。
    window = max(limit * 4, 200)
    if hasattr(store, "page"):
        if before is not None:
            # 游标校验：它必须在这个会话里，否则"往后翻"和"从头再来"会混淆。
            head = store.page(chat_type, str(chat_id), limit=max(window, 400), before=None, account=account)
            if not any(str(message.message_id) == str(before) for message in head):
                raise ChatError("before cursor not found in this conversation", 404)
            cursor: Any = int(before) if str(before).isdigit() else before
        else:
            cursor = None
        # 账号过滤下推到查询里（page 的 account 参数）：库里混着别的账号时，
        # 先取一页再筛会让窗口被别人的消息占满。
        messages = store.page(chat_type, str(chat_id), limit=window, before=cursor, account=account)
    else:
        messages = [
            message for message in _scoped_messages(store, account)
            if message.chat_type == chat_type and str(message.chat_id) == chat_id
        ]
        if before is not None:
            index = next((i for i, message in enumerate(messages) if str(message.message_id) == before), None)
            if index is None:
                raise ChatError("before cursor not found in this conversation", 404)
            messages = messages[:index]
    has_more = len(messages) > limit
    page = messages[-limit:]
    # Quote lookups search the whole conversation, not just this page, so a reply
    # to an older message still shows its content.
    by_id = {str(message.message_id): message for message in messages}
    by_seq: dict[str, list[Message]] = {}
    for message in messages:
        by_seq.setdefault(str(message.seq), []).append(message)

    def locate(reference: dict[str, Any]) -> Message | None:
        """Find the quoted message.

        Native replies from other members frequently omit replayMsgId and carry
        only replayMsgSeq plus the original sender, so a pure ID lookup was why
        every third-party quote failed while self-quotes worked.
        """
        message_id = str(reference.get("message_id") or "")
        if message_id:
            found = by_id.get(message_id)
            if found is not None:
                return found
        seq = str(reference.get("seq") or "")
        if not seq or seq == "0":
            return None
        candidates = by_seq.get(seq) or []
        if not candidates:
            return None
        sender = str(reference.get("sender_uin") or "")
        if sender:
            matching = [m for m in candidates if str(m.sender_uin) == sender]
            if matching:
                # Prefer the newest message older than the quoting one.
                return matching[-1]
            return None
        # Without a sender only an unambiguous sequence match is safe.
        return candidates[0] if len(candidates) == 1 else None

    def quote(reference: dict[str, Any]) -> dict[str, Any] | None:
        target = locate(reference)
        if target is None:
            return None
        return {
            "found": True,
            "message_id": str(target.message_id),
            "sender": _sender(target),
            "sender_uin": str(target.sender_uin),
            "text": _text(target)[:200],
            "segments": _segments(target),
            "time": target.time,
        }

    return {
        "messages": [{
            "message_id": str(message.message_id),
            "seq": str(message.seq),
            "sender_uin": str(message.sender_uin),
            "sender_uid": message.sender_uid,
            "sender": _sender(message),
            "card": message.card,
            "role": message.role,
            "text": _text(message),
            "segments": _segments(message, quote, face_root),
            "time": message.time,
            "outgoing": str(message.sender_uin) == account,
        } for message in page],
        "has_more": has_more,
        "next_before": str(page[0].message_id) if has_more else None,
    }


def _pins_path(runtime: Any) -> _FilePath:
    root = getattr(runtime.config, "headless_root", None) or runtime.config.data_dir
    return _FilePath(str(root)) / "chat-ui-state.json"


def _pins(runtime: Any) -> dict[str, Any]:
    try:
        data = json.loads(_pins_path(runtime).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_pins(runtime: Any, pins: dict[str, Any]) -> None:
    path = _pins_path(runtime)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(pins, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


async def _json(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ChatError("invalid JSON") from None
    if not isinstance(payload, dict):
        raise ChatError("JSON object required")
    return payload


def _account_is_known(runtime: Any, account: Any) -> bool:
    """这个账号是否属于某个活动实例。

    以前这里的判据是 ``account == runtime.self_id``，多实例下它只认同主账号，
    于是 C1 的点赞/撤回一律被 409 挡掉。
    """
    wanted = str(account or "").strip()
    if not wanted:
        return False
    instances = getattr(runtime, "instances", None)
    if instances is not None:
        for name in instances.names():
            context = instances.get(name)
            if context is not None and str(context.account) == wanted:
                return True
    # 还没有逐实例信息时（mock/单通道），退回到主账号。
    return wanted == str(runtime.self_id)


def _store_for(runtime: Any, account: Any) -> Any:
    """按账号找到它所属实例的数据库。

    请求里的 ``account`` 是 QQ 号（WebUI 一直这么用）；这里把它映射到实例，
    再取那个实例自己的 maxStore 库。每个实例各有一个库，所以这一步不是优化
    而是正确性：拿错库就会看到另一个号的消息。

    找不到对应实例时退回 ``runtime.store``（主实例的库），保持旧行为而不是
    报错——升级前收到的消息都在那里。
    """
    instances = getattr(runtime, "instances", None)
    if instances is None:
        return runtime.store
    wanted = str(account or "").strip()
    if wanted:
        for name in instances.names():
            context = instances.get(name)
            if context is not None and str(context.account) == wanted:
                return context.store
    return runtime.store


def register_chat_routes(app: FastAPI, runtime: Any, guard: Callable[[Request], Response | None]) -> None:
    """Register new routes without changing the legacy group-only API."""
    def denied(request: Request) -> Response | None:
        response = guard(request)
        if response is not None:
            return response
        if not runtime.config.webui.chat_enabled:
            return JSONResponse({"ok": False, "error": "chat disabled"}, status_code=403)
        return None

    def error(exc: ChatError) -> JSONResponse:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=exc.status)

    @app.get("/api/chat/conversations")
    async def conversations(request: Request) -> Response:
        rejection = denied(request)
        if rejection is not None:
            return rejection
        try:
            account = _identifier(request.query_params.get("account"), "account")
            data = await run_in_threadpool(_conversations, _store_for(runtime, account), account, _pins(runtime))
            if getattr(getattr(runtime, "transport", None), "name", "") == "headless":
                data["send_private_supported"] = _account_is_known(runtime, account)
            return JSONResponse(data)
        except ChatError as exc:
            return error(exc)
        except Exception as exc:  # noqa: BLE001 - 传输层/宿主抛的异常类型很杂
            # 不带兜底的话，任何底层异常都会变成裸 500，前端拿到 HTML
            # 再当 JSON 解析，用户只看到 "Unexpected token 'I'"。
            return JSONResponse({"ok": False, "error": f"操作失败：{exc}"}, status_code=502)

    @app.get("/api/chat/history")
    async def history(request: Request) -> Response:
        rejection = denied(request)
        if rejection is not None:
            return rejection
        try:
            params = request.query_params
            account = _identifier(params.get("account"), "account")
            chat_type = _chat_type(params.get("chat_type"))
            chat_id = _identifier(params.get("chat_id"), "chat_id")
            limit_text = params.get("limit", "100")
            if not re.fullmatch(r"[0-9]{1,3}", limit_text) or not 1 <= int(limit_text) <= 200:
                raise ChatError("limit must be an integer between 1 and 200")
            before = _identifier(params["before"], "before") if "before" in params else None
            after = _identifier(params["after"], "after") if "after" in params else None
            if after is not None:
                # 增量轮询：只要"比这个 id 更新"的消息。
                #
                # 聊天栏每 3 秒轮询一次，以前每次都整页重拉 80 条并重新渲染；
                # 这里让它只取新增的那几条，后端也只需读一小段。
                return JSONResponse(await run_in_threadpool(
                    _history_after, _store_for(runtime, account), account, chat_type, chat_id,
                    int(limit_text), after, getattr(runtime.config, "headless_root", ""),
                ))
            return JSONResponse(await run_in_threadpool(
                _history, _store_for(runtime, account), account, chat_type, chat_id, int(limit_text), before,
                    getattr(runtime.config, "headless_root", ""),
            ))
        except ChatError as exc:
            return error(exc)
        except Exception as exc:  # noqa: BLE001 - 传输层/宿主抛的异常类型很杂
            # 不带兜底的话，任何底层异常都会变成裸 500，前端拿到 HTML
            # 再当 JSON 解析，用户只看到 "Unexpected token 'I'"。
            return JSONResponse({"ok": False, "error": f"操作失败：{exc}"}, status_code=502)

    @app.get("/api/chat/faces")
    async def chat_faces(request: Request) -> Response:
        """Emoji picker catalogue, grouped the way QQ itself groups faces."""
        rejection = denied(request)
        if rejection is not None:
            return rejection
        headless_root = getattr(runtime.config, "headless_root", "")
        groups = face_catalog.catalog(headless_root)
        labels = face_catalog.labels()

        # ``describe`` already recorded whether QQ ships a Lottie for the face,
        # so the animation URL is derived from that instead of probing the
        # filesystem a second time per face. Probing again made this endpoint
        # take long enough that the picker came up empty.
        def with_animation(face: dict[str, object]) -> dict[str, object]:
            if face.get("file_type") == "lottie":
                return dict(face, animation="/api/chat/face/animation?id=" + urlquote(str(face["id"]), safe=""))
            return face

        return JSONResponse({
            "groups": [
                {"key": key, "label": labels.get(key, key),
                 "faces": [with_animation(face) for face in faces]
                 }
                for key, faces in groups.items()
            ]
        })

    @app.get("/api/chat/favorites")
    async def chat_favorites(request: Request) -> Response:
        """The account's own favourite (收藏) emoticons.

        QQ reports both a remote URL and the local files it already downloaded;
        the local copies are preferred because the URL needs a rotating rkey.
        """
        rejection = denied(request)
        if rejection is not None:
            return rejection
        try:
            if getattr(getattr(runtime, "transport", None), "name", "") != "headless":
                raise ChatError("收藏表情需要 headless 通道", 409)
            # 收藏表情 is a host-only operation with no OneBot equivalent, so it
            # goes through the transport directly. Routing it through the OneBot
            # registry returned "unknown action" every time, which is why the
            # favourite column stayed empty.
            sent = await runtime.send("emoji.favorites", {"count": 200})
            if not getattr(sent, "ok", False):
                return JSONResponse({"ok": False, "error": str(getattr(sent, "message", "") or "读取收藏表情失败")},
                                    status_code=409)
            data = getattr(sent, "data", None)
            rows = (data or {}).get("emoticons") or []
            faces = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                res_id = str(row.get("res_id") or "")
                if not res_id:
                    continue
                # Prefer the original picture, then the thumbnail; a path that no
                # longer exists on disk would render as a broken image.
                # 优先本地文件；没有本地副本时才交给服务端代理。
                #
                # 以前这里直接把 QQ 的远程 URL 交给浏览器，但那是跨域地址：
                # 前端要发收藏表情得先 fetch 成 Blob，而 QQ 的 CDN 不返回 CORS
                # 头，fetch 必然 TypeError: Failed to fetch——用户看到的就是
                # "收藏表情无法读取"。改走同源代理后，浏览器只和本机通信。
                local = ""
                for key in ("original_path", "path", "thumb_path"):
                    candidate = str(row.get(key) or "")
                    if candidate and _FilePath(candidate).is_file():
                        local = candidate
                        break
                if local:
                    url = "/api/chat/media?path=" + urlquote(local, safe="")
                else:
                    remote = str(row.get("url") or "")
                    # 只代理 http(s)；其它形态（空串、相对路径、javascript:）一律丢掉，
                    # 留着会让前端 fetch 到页面本身或更糟的东西。
                    url = ("/api/chat/remote-media?url=" + urlquote(remote, safe="")
                           if remote.startswith(("http://", "https://")) else "")
                # QQ prunes the local copies, so the remote URL is the fallback
                # that keeps the picker usable.
                faces.append({
                    "res_id": res_id,
                    "id": str(row.get("emoji_id") or ""),
                    "md5": str(row.get("md5") or ""),
                    "desc": str(row.get("desc") or ""),
                    "url": url,
                    "local": bool(local),
                    "is_mark_face": bool(row.get("is_mark_face")),
                })
            return JSONResponse({"ok": True, "faces": faces})
        except ChatError as exc:
            return error(exc)
        except Exception as exc:  # noqa: BLE001 - 传输层异常类型很杂
            # 收藏读不出来时要说清原因。漏捕会变成裸 500，前端拿到 HTML 再当
            # JSON 解析，用户只看到 "Failed to fetch"/"Unexpected token"。
            return JSONResponse(
                {"ok": False, "error": f"读取收藏表情失败：{exc}"}, status_code=502)

    @app.get("/api/chat/face/animation")
    async def chat_face_animation(request: Request, id: str = "") -> Response:
        """Serve the Lottie file that carries a face's real animation.

        QQ ships motion for its system faces as Lottie JSON, not APNG, so the
        browser needs the raw document to play it.
        """
        rejection = denied(request)
        if rejection is not None:
            return rejection
        from fastapi.responses import FileResponse
        target = animation_path(runtime.config.headless_root, id)
        if target is None:
            return JSONResponse({"ok": False, "error": "no animation for this face"}, status_code=404)
        return FileResponse(str(target), media_type="application/json",
                            headers={"Cache-Control": "private, max-age=86400"})

    @app.get("/api/chat/face")
    async def chat_face(request: Request, id: str = "") -> Response:
        """Serve QQ's own system-face PNG.

        These files ship with the QQ client, so the browser shows the real face
        instead of a Unicode approximation.
        """
        rejection = denied(request)
        if rejection is not None:
            return rejection
        from fastapi.responses import FileResponse
        image = face_image_path(runtime.config.headless_root, id)
        if image is None:
            return JSONResponse({"ok": False, "error": f"找不到表情 {id!r} 的图片资源"},
                                status_code=404)
        # The face library has been corrected more than once, and a long blind
        # cache kept showing the old, wrong art for the same id. Keying the cache
        # on the file's own mtime+size means a corrected picture is picked up
        # immediately while a stable one stays cached.
        try:
            stamp = image.stat()
            etag = 'W/"%x-%x"' % (int(stamp.st_mtime), stamp.st_size)
        except OSError:
            etag = ""
        headers = {"Cache-Control": "private, max-age=86400"}
        if etag:
            headers["ETag"] = etag
        return FileResponse(str(image), media_type=media_type_of(image), headers=headers)

    @app.get("/api/chat/media")
    async def chat_media(request: Request, path: str = "") -> Response:
        """Serve a downloaded picture/file from QQ's media tree.

        Local files are preferred over QQ URLs because those require a rotating
        rkey. This route reads files from disk, so it must be gated exactly like
        the other chat routes: without the guard any caller could read arbitrary
        files by guessing a path.
        """
        rejection = denied(request)
        if rejection is not None:
            return rejection
        if not path:
            return JSONResponse({"ok": False, "error": "缺少 path 参数（要读取的媒体文件路径）"}, status_code=400)
        target = _FilePath(path)
        if not target.is_file():
            return JSONResponse({"ok": False, "error": f"媒体文件不存在：{path}"}, status_code=404)
        # 这个路由把调用方给的路径原样读盘，所以必须限定在允许的根目录之内。
        # 只靠「需要登录」不够：任何一个拿到 WebUI 会话的人都能用 ../../ 读到
        # 系统里的任意文件。允许的根目录是 QQ 的媒体树与本次新增的 media-cache。
        resolved = target.resolve()
        allowed_roots = [runtime.config.path("media-cache").resolve()]
        for root in _media_roots(runtime):
            try:
                allowed_roots.append(root.resolve())
            except OSError:
                continue
        if not any(_is_within(resolved, root) for root in allowed_roots):
            return JSONResponse({"ok": False, "error": "media path is outside the allowed roots"},
                                status_code=403)
        import mimetypes
        from fastapi.responses import FileResponse
        media_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if not media_type.startswith("image/"):
            # 收藏表情的本地副本常常没有扩展名（文件名是 md5/res_id），
            # guess_type 只能给出 octet-stream。前端拿到非 image/* 的 blob 会把它
            # 当成"文件"而不是图片，picSubType 随之丢失——表情又变回图片。
            # 所以这里按魔数补一次，扩展名永远只是提示。
            sniffed = _sniff_image_type(target)
            if sniffed:
                media_type = sniffed
        return FileResponse(str(target), media_type=media_type, headers={"Cache-Control": "private, max-age=600"})

    @app.get("/api/chat/remote-media")
    async def chat_remote_media(request: Request, url: str = "") -> Response:
        """Same-origin proxy for one QQ CDN picture.

        为什么需要它：收藏表情要发出去，前端必须先把它取成 Blob，而 QQ 的 CDN
        （``multimedia.nt.qq.com.cn`` 等）不返回 CORS 头，浏览器直连必然
        ``TypeError: Failed to fetch``。走本机代理后浏览器只和同源通信。

        安全边界：只允许 QQ 自己的 CDN 主机，只允许 https，只接受图片类型，
        大小有上限。服务端会主动出网，所以这份白名单就是攻击面本身——它必须
        是封闭的，不能让调用方指定任意地址。
        """
        rejection = denied(request)
        if rejection is not None:
            return rejection
        # 只有 QQ 自己的图片 CDN。新增域名要显式加进来，不做子域通配，
        # 否则 ``evil.qq.com`` 之类也会被放行。
        allowed_hosts = {
            "multimedia.nt.qq.com.cn",
            "gchat.qpic.cn",
            "c2cpicdw.qpic.cn",
            "p.qpic.cn",
            "q1.qlogo.cn",
        }
        if not url:
            return JSONResponse({"ok": False, "error": "url is required"}, status_code=400)
        parsed = urlparse(url)
        if parsed.scheme != "https":
            return JSONResponse({"ok": False, "error": f"只代理 https 地址，收到的是 {parsed.scheme or '（无协议）':s}"},
                                status_code=400)
        if parsed.hostname not in allowed_hosts:
            return JSONResponse({"ok": False,
                                     "error": f"不允许代理 {parsed.hostname or '（无主机名）'}；"
                                              f"可代理：{', '.join(sorted(allowed_hosts))}"},
                                status_code=403)

        import asyncio
        import urllib.request

        limit = 10 * 1024 * 1024

        def _fetch() -> tuple[bytes, str]:
            request_ = urllib.request.Request(url, headers={
                # QQ 的 CDN 对空 UA 会返回 403；带上一个普通客户端标识即可。
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                "Referer": "https://im.qq.com/",
            })
            with urllib.request.urlopen(request_, timeout=10) as response:
                length = response.headers.get("Content-Length")
                if length and int(length) > limit:
                    raise ValueError("remote media too large")
                media = response.headers.get_content_type() or "application/octet-stream"
                data = response.read(limit + 1)
            if len(data) > limit:
                raise ValueError("remote media too large")
            return data, media

        try:
            data, media_type = await asyncio.to_thread(_fetch)
        except Exception as exc:  # noqa: BLE001 - 网络层异常类型很杂
            return JSONResponse({"ok": False, "error": f"读取远程图片失败：{exc}"},
                                status_code=502)
        if not media_type.startswith("image/"):
            return JSONResponse({"ok": False, "error": f"上游返回的不是图片（{media_type}）"},
                                status_code=415)
        return Response(content=data, media_type=media_type,
                        headers={"Cache-Control": "private, max-age=600"})

    @app.post("/api/chat/download-media")
    async def download_media(request: Request) -> Response:
        """Ask the host to fetch one picture that was not downloaded yet."""
        rejection = denied(request)
        if rejection is not None:
            return rejection
        try:
            payload = await _json(request)
            account = _identifier(payload.get("account"), "account")
            message_id = _identifier(payload.get("message_id"), "message_id")
            element_id = str(payload.get("element_id") or "")
            chat_type = _chat_type(payload.get("chat_type"))
            chat_id = _identifier(payload.get("chat_id"), "chat_id")
            if not element_id:
                raise ChatError("element_id is required")
            if not _account_is_known(runtime, account):
                raise ChatError("unsupported account", 409)
            # 走 transport 而不是 registry：``media.download`` 是宿主侧的动作名，
            # OneBot 那边没有这个动作（那边叫 get_image，语义也不同——它只返回
            # URL，不负责下载）。用 runtime.invoke 会得到
            # ``unknown action 'media.download'``。
            #
            # ``file_path`` 必须由调用方给出：宿主用 ``text(..., allowEmpty=false)``
            # 校验它，空串会被直接拒绝（"file_path must be a non-empty string
            # without NUL"）。它最终原样传给 QQ 的 downloadRichMedia 作为落盘位置。
            # 这里让文件落进 data_dir 下的 media 缓存目录，按消息与元素命名，
            # 这样同一个元素重复下载会覆盖同一份文件，不会越攒越多。
            cache_dir = runtime.config.path("media-cache")
            cache_dir.mkdir(parents=True, exist_ok=True)
            safe_message = re.sub(r"[^0-9A-Za-z_-]", "", str(message_id)) or "0"
            safe_element = re.sub(r"[^0-9A-Za-z_-]", "", element_id) or "0"
            target_path = cache_dir / f"{safe_message}_{safe_element}"
            sent = await runtime.send("media.download", {
                "message_id": str(message_id),
                "element_id": element_id,
                "chat": {"type": chat_type, "id": str(chat_id)},
                "file_path": str(target_path),
            })
            # 失败要如实说失败。以前这里返回 ``{"ok": true, "url": "", "error": ...}``，
            # 而前端只在 ``ok === false`` 时报错，于是"没下到图"被当成成功：界面
            # 毫无反应，用户以为点击没生效。``ok`` 与 ``error`` 不能互相矛盾。
            if not getattr(sent, "ok", False):
                return JSONResponse({"ok": False, "url": "",
                                     "error": str(getattr(sent, "message", "") or "下载失败")},
                                    status_code=502)
            data = getattr(sent, "data", None)
            path = str((data or {}).get("file_path") or "")
            if not path:
                return JSONResponse({"ok": False, "url": "", "error": "宿主未返回本地路径"},
                                    status_code=502)
            try:
                await run_in_threadpool(_store_for(runtime, account).set_media_path, int(message_id), element_id, path)
            except Exception:  # noqa: BLE001 - the URL below still works
                pass
            return JSONResponse({"ok": True, "url": "/api/chat/media?path=" + urlquote(path, safe="")})
        except ChatError as exc:
            return error(exc)
        except Exception as exc:  # noqa: BLE001 - 传输层/宿主抛的异常类型很杂
            # 不带兜底的话，任何底层异常都会变成裸 500，前端拿到 HTML
            # 再当 JSON 解析，用户只看到 "Unexpected token 'I'"。
            return JSONResponse({"ok": False, "error": f"操作失败：{exc}"}, status_code=502)

    @app.post("/api/chat/like")
    async def like_user(request: Request) -> Response:
        """Give a friend profile likes (QQ 名片点赞)."""
        rejection = denied(request)
        if rejection is not None:
            return rejection
        try:
            payload = await _json(request)
            account = _identifier(payload.get("account"), "account")
            user_id = _identifier(payload.get("user_id"), "user_id")
            times = payload.get("times", 10)
            if not isinstance(times, int) or isinstance(times, bool) or not 1 <= times <= 20:
                raise ChatError("times must be an integer between 1 and 20")
            if not _account_is_known(runtime, account):
                raise ChatError("unsupported account: not an active instance", 409)
            result = await runtime.invoke("send_like",
                                          {"account": account, "user_id": int(user_id), "times": times})
            if isinstance(result, dict) and result.get("status") == "failed":
                return JSONResponse({"ok": False, "error": result.get("message") or "QQ 拒绝了点赞"},
                                    status_code=409)
            return JSONResponse(result)
        except ChatError as exc:
            return error(exc)
        except Exception as exc:  # noqa: BLE001 - 传输层/宿主抛的异常类型很杂
            # 不带兜底的话，任何底层异常都会变成裸 500，前端拿到 HTML
            # 再当 JSON 解析，用户只看到 "Unexpected token 'I'"。
            return JSONResponse({"ok": False, "error": f"操作失败：{exc}"}, status_code=502)

    @app.post("/api/chat/recall")
    async def recall(request: Request) -> Response:
        """Recall a message through QQ (needs a cached native reference)."""
        rejection = denied(request)
        if rejection is not None:
            return rejection
        try:
            payload = await _json(request)
            account = _identifier(payload.get("account"), "account")
            message_id = _identifier(payload.get("message_id"), "message_id")
            if not _account_is_known(runtime, account):
                raise ChatError("unsupported account: not an active instance", 409)
            result = await runtime.invoke("delete_msg", {"account": account, "message_id": int(message_id)})
            if isinstance(result, dict) and result.get("status") == "failed":
                return JSONResponse({"ok": False, "error": result.get("message") or "QQ 拒绝撤回"}, status_code=409)
            return JSONResponse(result)
        except ChatError as exc:
            return error(exc)
        except Exception as exc:  # noqa: BLE001 - 传输层/宿主抛的异常类型很杂
            # 不带兜底的话，任何底层异常都会变成裸 500，前端拿到 HTML
            # 再当 JSON 解析，用户只看到 "Unexpected token 'I'"。
            return JSONResponse({"ok": False, "error": f"操作失败：{exc}"}, status_code=502)

    @app.post("/api/chat/delete-message")
    async def delete_message(request: Request) -> Response:
        """Delete a message from QSICK's local history only (no QQ side effect)."""
        rejection = denied(request)
        if rejection is not None:
            return rejection
        try:
            payload = await _json(request)
            account = _identifier(payload.get("account"), "account")
            message_id = int(_identifier(payload.get("message_id"), "message_id"))
            removed = await run_in_threadpool(_store_for(runtime, account).delete, message_id)
            return JSONResponse({"ok": True, "deleted": bool(removed)})
        except ChatError as exc:
            return error(exc)
        except Exception as exc:  # noqa: BLE001 - 传输层/宿主抛的异常类型很杂
            # 不带兜底的话，任何底层异常都会变成裸 500，前端拿到 HTML
            # 再当 JSON 解析，用户只看到 "Unexpected token 'I'"。
            return JSONResponse({"ok": False, "error": f"操作失败：{exc}"}, status_code=502)

    @app.post("/api/chat/delete-conversation")
    async def delete_conversation(request: Request) -> Response:
        """Delete every stored message of one conversation."""
        rejection = denied(request)
        if rejection is not None:
            return rejection
        try:
            payload = await _json(request)
            account = _identifier(payload.get("account"), "account")
            chat_type = _chat_type(payload.get("chat_type"))
            chat_id = int(_identifier(payload.get("chat_id"), "chat_id"))
            removed = await run_in_threadpool(_store_for(runtime, account).delete_chat, chat_type, chat_id)
            pins = _pins(runtime)
            pins.pop(f"{account}:{chat_type}:{chat_id}", None)
            pins.pop(f"hidden:{account}:{chat_type}:{chat_id}", None)
            _save_pins(runtime, pins)
            return JSONResponse({"ok": True, "deleted": int(removed)})
        except ChatError as exc:
            return error(exc)
        except Exception as exc:  # noqa: BLE001 - 传输层/宿主抛的异常类型很杂
            # 不带兜底的话，任何底层异常都会变成裸 500，前端拿到 HTML
            # 再当 JSON 解析，用户只看到 "Unexpected token 'I'"。
            return JSONResponse({"ok": False, "error": f"操作失败：{exc}"}, status_code=502)

    @app.post("/api/chat/pin")
    async def pin_conversation(request: Request) -> Response:
        """Pin/unpin a conversation; the order is UI state, not QQ state."""
        rejection = denied(request)
        if rejection is not None:
            return rejection
        try:
            payload = await _json(request)
            account = _identifier(payload.get("account"), "account")
            chat_type = _chat_type(payload.get("chat_type"))
            chat_id = _identifier(payload.get("chat_id"), "chat_id")
            pinned = payload.get("pinned", True)
            if not isinstance(pinned, bool):
                raise ChatError("pinned must be a boolean")
            pins = _pins(runtime)
            key = f"{account}:{chat_type}:{chat_id}"
            if pinned:
                pins[key] = True
            else:
                pins.pop(key, None)
            _save_pins(runtime, pins)
            return JSONResponse({"ok": True, "pinned": pinned})
        except ChatError as exc:
            return error(exc)
        except Exception as exc:  # noqa: BLE001 - 传输层/宿主抛的异常类型很杂
            # 不带兜底的话，任何底层异常都会变成裸 500，前端拿到 HTML
            # 再当 JSON 解析，用户只看到 "Unexpected token 'I'"。
            return JSONResponse({"ok": False, "error": f"操作失败：{exc}"}, status_code=502)

    @app.post("/api/chat/hide")
    async def hide_conversation(request: Request) -> Response:
        """Remove a conversation from the list until a newer message arrives."""
        rejection = denied(request)
        if rejection is not None:
            return rejection
        try:
            payload = await _json(request)
            account = _identifier(payload.get("account"), "account")
            chat_type = _chat_type(payload.get("chat_type"))
            chat_id = _identifier(payload.get("chat_id"), "chat_id")
            pins = _pins(runtime)
            key = f"hidden:{account}:{chat_type}:{chat_id}"
            if payload.get("hidden", True) is False:
                pins.pop(key, None)
            else:
                marker = payload.get("last_message_id")
                if marker is None:
                    # Fall back to the newest stored message of this conversation.
                    latest = [m for m in _scoped_messages(_store_for(runtime, account), account)
                              if m.chat_type == chat_type and str(m.chat_id) == chat_id]
                    marker = str(latest[-1].message_id) if latest else "0"
                pins[key] = str(marker)
            _save_pins(runtime, pins)
            return JSONResponse({"ok": True})
        except ChatError as exc:
            return error(exc)
        except Exception as exc:  # noqa: BLE001 - 传输层/宿主抛的异常类型很杂
            # 不带兜底的话，任何底层异常都会变成裸 500，前端拿到 HTML
            # 再当 JSON 解析，用户只看到 "Unexpected token 'I'"。
            return JSONResponse({"ok": False, "error": f"操作失败：{exc}"}, status_code=502)

    @app.post("/api/chat/send-media")
    async def send_media(request: Request) -> Response:
        """Send an uploaded picture or file, optionally with a text caption.

        The browser posts multipart/form-data: file + account + chat_type +
        chat_id + optional text. The upload is written to a temp file, handed to
        the native sender as a local path, and removed once the send settles.
        """
        rejection = denied(request)
        if rejection is not None:
            return rejection
        from .. import tempdir
        from pathlib import Path as _Path
        try:
            form = await request.form()
            account = _identifier(form.get("account"), "account")
            chat_type = _chat_type(form.get("chat_type"))
            chat_id = _identifier(form.get("chat_id"), "chat_id")
            text = form.get("text")
            if text is not None and (not isinstance(text, str) or len(text) > 10_000):
                raise ChatError("text must be at most 10000 characters")
            upload = form.get("file")
            if upload is None or not hasattr(upload, "read"):
                raise ChatError("file is required")
            name = str(getattr(upload, "filename", "") or "upload.bin")
            # 0 = image, 1 = file (QSICK segment kinds)
            kind = str(form.get("kind") or "auto")
            if kind not in ("auto", "image", "file"):
                raise ChatError("kind must be auto, image or file")
            if not _account_is_known(runtime, account):
                raise ChatError("unsupported account: not an active instance", 409)
            if chat_type == "private" and getattr(getattr(runtime, "transport", None), "name", "") != "headless":
                raise ChatError("private sending is unsupported for this transport", 501)
            data = await upload.read()
            if not data:
                raise ChatError("uploaded file is empty")
            if len(data) > 100 * 1024 * 1024:
                raise ChatError("uploaded file exceeds 100 MB")
            suffix = _Path(name).suffix[:16]
            handle = tempdir.make_file(prefix="upload-", suffix=suffix, category="uploads")
            try:
                handle.write(data)
                handle.close()
                resolved = "image" if kind == "image" else "file" if kind == "file" else "auto"
                if resolved == "auto":
                    resolved = "image" if _looks_like_image(data) else "file"
                elements: list[dict[str, Any]] = []
                reply_to = form.get("reply_to")
                if reply_to:
                    elements.append({"type": "reply", "data": {"id": _identifier(reply_to, "reply_to")}})
                if isinstance(text, str) and text.strip():
                    elements.append({"type": "text", "data": {"text": text}})
                payload_data: dict[str, Any] = {"path": handle.name, "name": name}
                # 收藏表情要以"表情"身份发出，而不是一张图片。QQ 用 picSubType
                # 区分两者：0 = 普通图片，1 = 表情。前端在发表情包时带上这个标记，
                # 否则接收端没有任何依据把它渲染成表情，动图也只是一张图片。
                sticker = str(form.get("pic_sub_type") or "").strip()
                if sticker:
                    try:
                        value = int(sticker)
                    except ValueError:
                        raise ChatError("pic_sub_type must be an integer") from None
                    if value not in (0, 1, 2):
                        raise ChatError("pic_sub_type must be 0, 1 or 2")
                    payload_data["pic_sub_type"] = value
                elements.append({"type": resolved, "data": payload_data})
                result = await runtime.invoke(
                    "send_private_msg" if chat_type == "private" else "send_group_msg",
                    # account 必须带上：多实例下不带它就落到主实例，用户在 C1 的
                    # 聊天栏里发图会用 C0 的号发出去（或直接被宿主拒绝）。
                    {"account": account,
                     ("user_id" if chat_type == "private" else "group_id"): int(chat_id),
                     "message": elements},
                )
                return JSONResponse(result)
            finally:
                try:
                    _Path(handle.name).unlink(missing_ok=True)
                except OSError:
                    pass
        except ChatError as exc:
            return error(exc)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return error(ChatError("invalid form body"))
        except Exception as exc:  # noqa: BLE001 - 传输层/宿主抛的异常类型很杂
            # 上传失败时同样要让原因看得见，不能变成裸 500。
            return JSONResponse({"status": "failed", "retcode": 1200,
                                 "message": f"发送失败：{exc}", "data": None},
                                status_code=502)

    @app.post("/api/chat/send-text")
    async def send_text(request: Request) -> Response:
        rejection = denied(request)
        if rejection is not None:
            return rejection
        try:
            try:
                payload = await request.json()
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise ChatError("invalid JSON") from None
            if not isinstance(payload, dict):
                raise ChatError("JSON object required")
            account = _identifier(payload.get("account"), "account")
            chat_type = _chat_type(payload.get("chat_type"))
            chat_id = _identifier(payload.get("chat_id"), "chat_id")
            text = payload.get("text")
            if not isinstance(text, str) or not text.strip() or len(text) > 10_000:
                raise ChatError("text must contain 1 to 10000 characters and not be blank")
            if not _account_is_known(runtime, account):
                raise ChatError("unsupported account: not an active instance", 409)
            if chat_type == "private" and getattr(getattr(runtime, "transport", None), "name", "") != "headless":
                raise ChatError("private sending is unsupported for this transport", 501)
            segments: list[dict[str, Any]] = []
            reply_to = payload.get("reply_to")
            if reply_to is not None:
                reply_id = _identifier(reply_to, "reply_to")
                # 标准 OneBot v11 的 reply 段：接收端渲染成原生引用，可跳回原消息。
                segments.append({"type": "reply", "data": {"id": reply_id}})
            segments.extend(_encode_chat_parts(text))
            result = await runtime.invoke(
                "send_private_msg" if chat_type == "private" else "send_group_msg",
                # 同上传路径：带上 account，动作才会落在当前聊天栏所属的实例上。
                {"account": account,
                 ("user_id" if chat_type == "private" else "group_id"): int(chat_id),
                 "message": segments},
            )
            return JSONResponse(result)
        except ChatError as exc:
            return error(exc)
        except Exception as exc:  # noqa: BLE001 - 传输层抛的异常类型很杂
            # 送不出去时必须给出看得懂的原因。早先这里漏捕，底层异常直接变成
            # 裸的 500 "Internal Server Error"，前端再拿它当 JSON 解析，用户
            # 看到的就是 "Unexpected token 'I'"，真正的失败被埋掉了。
            detail = str(exc) or type(exc).__name__
            return JSONResponse(
                {"status": "failed", "retcode": 1200,
                 "message": f"发送失败：{detail}", "data": None},
                status_code=502,
            )
