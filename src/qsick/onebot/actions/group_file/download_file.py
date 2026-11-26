"""OneBot adapter for `download_file`: fetch a message's file element to a local path.

Core side: work out *which* element to download, then hand it to the payload's `media.download` op.
Payload side: that op already exists and calls `Msg.downloadRichMedia`, which handles group **and**
private chats -- `chatType: 1` with a resolved peer uid for C2C.

This action used to declare `op = "download_file"`, which the payload does not implement, so every
call answered `Unsupported operation: download_file`. `media.download` was implemented but reachable
from no action at all: `get_file`/`get_image`/`get_record` all go through `media.url`, which is
deliberately unresolved.

For a private file there is no download URL we can produce yet (the OIDB layout for `0xE37_800` is
unmapped, see `headless_privatefile.cjs`), so downloading the element is the route that works today.

`element_id` is optional: given only a `message_id`, the element and the chat are read from the
message cache, which is what makes a *cached* private file downloadable without the caller having to
have kept the wire event.
"""
from __future__ import annotations

from typing import Any

from qsick.onebot import models
from qsick.onebot.actions.base import ActionContext, PayloadAction, call
from qsick.onebot.registry import ActionError

#: Element types whose bytes `media.download` can fetch.
DOWNLOADABLE = ("file", "image", "record", "video")


def _chat_from_message(message: Any) -> tuple[str, Any]:
    chat_type = str(getattr(message, "chat_type", "") or "")
    chat_id = getattr(message, "chat_id", None)
    return chat_type, chat_id


def _element_candidates(message: Any, wanted: str) -> list[Any]:
    """Elements of a cached message that could be the one asked for."""
    elements = list(getattr(message, "elements", None) or [])
    usable = [item for item in elements if str(getattr(item, "type", "")) in DOWNLOADABLE]
    if not wanted:
        return usable
    exact = [item for item in usable
             if str((getattr(item, "data", None) or {}).get("element_id", "")) == wanted]
    return exact or usable


class DownloadFileAction(PayloadAction):
    op = "media.download"

    async def run(self, ctx: ActionContext, **params: Any) -> Any:
        message_id = params.get("message_id")
        if message_id in (None, ""):
            raise ActionError("download_file: message_id is required", models.BAD_REQUEST)
        element_id = str(params.get("element_id") or "").strip()
        chat_type = str(params.get("chat_type") or "").strip()
        chat_id = params.get("chat_id")
        wanted_index = params.get("index")

        # A caller may name the chat three ways; the two short forms are what a caller who only has
        # the message in hand actually knows.
        if not chat_type or chat_id in (None, ""):
            if params.get("group_id") not in (None, ""):
                chat_type, chat_id = "group", params["group_id"]
            elif params.get("user_id") not in (None, ""):
                chat_type, chat_id = "private", params["user_id"]

        # Fill the rest in from the cached message -- this is what makes a cached *private* file
        # downloadable without the caller having kept the wire event, which is where `element_id`
        # lives. Only read when something is actually missing, so a caller that passes everything
        # does not depend on the cache being enabled at all.
        if not element_id or not chat_type or chat_id in (None, ""):
            cached = None
            try:
                cached = ctx.store.get(int(message_id))
            except (TypeError, ValueError):
                raise ActionError("download_file: message_id must be an integer",
                                  models.BAD_REQUEST) from None
            except Exception as exc:  # noqa: BLE001 - the cache is optional infrastructure
                raise ActionError(f"download_file: message cache unavailable ({exc})",
                                  models.UPSTREAM) from exc
            if cached is None:
                raise ActionError(
                    "download_file: message_id is not in the cache; pass element_id, chat_type and "
                    "chat_id explicitly, or enable the message cache", models.NOT_FOUND)
            cached_type, cached_id = _chat_from_message(cached)
            if not chat_type:
                chat_type = cached_type
            if chat_id in (None, ""):
                chat_id = cached_id
            if not element_id:
                candidates = _element_candidates(cached, "")
                if not candidates:
                    raise ActionError(
                        "download_file: this message has no downloadable element", models.NOT_FOUND)
                if wanted_index is not None:
                    try:
                        candidates = [candidates[int(wanted_index)]]
                    except (IndexError, TypeError, ValueError):
                        raise ActionError(
                            f"download_file: index {wanted_index!r} is out of range for "
                            f"{len(candidates)} downloadable element(s)", models.BAD_REQUEST) from None
                elif len(candidates) > 1:
                    raise ActionError(
                        "download_file: this message has several downloadable elements; pass "
                        "element_id or index", models.BAD_REQUEST)
                data = getattr(candidates[0], "data", None) or {}
                element_id = str(data.get("element_id") or "").strip()
                if not element_id:
                    # Rows written before the identity fields were carried have no handle to match
                    # the completion callback against. Say so instead of inventing an id.
                    raise ActionError(
                        "download_file: this cached element has no element_id (recorded before "
                        "elements carried one); re-receive the message or pass element_id",
                        models.NOT_IMPLEMENTED)

        if chat_type not in ("group", "private"):
            raise ActionError("download_file: chat_type must be group or private", models.BAD_REQUEST)
        if chat_id in (None, ""):
            raise ActionError("download_file: chat_id is required", models.BAD_REQUEST)

        payload = {
            "message_id": str(message_id),
            "element_id": element_id,
            "chat": {"type": chat_type, "id": int(chat_id)},
        }
        if params.get("file_path"):
            payload["file_path"] = str(params["file_path"])
        return await call(ctx, self.op, payload, self.name or self.op, timeout=120.0)
