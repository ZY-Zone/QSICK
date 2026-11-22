"""Turn reference-only media into something a consumer can actually fetch.

Order of preference:

1. the payload's own URL builder (it knows the current appid/route),
2. an rkey-signed image URL (the one case the core can build on its own),
3. nothing — the segment still carries ``file_id`` so a later call can retry.
"""

from __future__ import annotations

from typing import Any

from ..log import get_logger
from ..transport.base import Transport
from .elements import Element
from .rkey import RKeyManager

log = get_logger("Protocol.Media")

IMAGE_HOST = "https://gchat.qpic.cn"
APPID_PRIVATE = "1406"
APPID_GROUP = "1407"


class MediaResolver:
    def __init__(self, transport: Transport, rkey: RKeyManager) -> None:
        self._transport = transport
        self._rkey = rkey

    async def url_for(self, element: Element, *, scope: str = "group", account: str = "") -> str | None:
        # 1. payload-side builder
        payload_url = await self._payload_url(element, scope)
        if payload_url:
            return payload_url
        # 2. image URL from rkey
        if element.type == "image":
            return await self._image_url(element, scope, account)
        return None

    async def _payload_url(self, element: Element, scope: str) -> str | None:
        params: dict[str, Any] = {
            "kind": element.type,
            "scope": scope,
            "file_id": element.data.get("file_id") or element.data.get("file") or "",
        }
        for key in ("file_name", "name", "file_size", "element_id", "msg_id", "file_set_id"):
            if element.data.get(key) is not None:
                params[key] = element.data[key]
        try:
            result = await self._transport.send("media.url", params)
        except Exception as exc:  # transport may be mid-reconnect
            log.trace("media.url failed", kind=element.type, error=str(exc))
            return None
        if not result.ok:
            log.trace("media.url rejected", kind=element.type, reason=result.message)
            return None
        url = result.data.get("url") if isinstance(result.data, dict) else None
        return str(url) if url else None

    async def _image_url(self, element: Element, scope: str, account: str = "") -> str | None:
        file_id = element.data.get("file_id") or element.data.get("file")
        if not file_id:
            return None
        appid = str(element.data.get("appid") or (APPID_GROUP if scope == "group" else APPID_PRIVATE))
        rkey = await self._rkey.get("group" if scope == "group" else "private", account)
        if not rkey:
            log.debug("image has no rkey yet; leaving file_id", file_id=str(file_id)[:16])
            return None
        return f"{IMAGE_HOST}/download?appid={appid}&fileid={file_id}&rkey={rkey}"
