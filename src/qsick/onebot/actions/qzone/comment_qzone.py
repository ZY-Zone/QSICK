"""评论或回复（`comment_qzone`）。

带 `comment`（父评论对象）就是回复那一楼，不带就是一级评论 —— 与 NapCat 同一套参数。

只给 `comment_id` 时会先把那条评论读回来：`AddReply` 要用父评论里的 `user` 才能定位回复对象，光有 id
会被服务端以 `retcode 10000 参数错误` 拒掉（实测）。
"""
from __future__ import annotations

from typing import Any

from ... import models
from ...registry import ActionError
from ._common import QzoneAction, client, resolve_ref


class CommentQzoneAction(QzoneAction):
    async def handle(self, ctx: Any, content: Any = "", comment: Any = None, comment_id: Any = "",
                     **params: Any) -> Any:
        text = str(content if content is not None else "").strip()
        if not text:
            raise ActionError("qzone: content cannot be empty", models.BAD_REQUEST)
        ref = await resolve_ref(ctx, params)
        parent = comment if isinstance(comment, dict) else None
        wanted = str(comment_id).strip() if comment_id not in (None, "") else ""
        if parent is None and wanted:
            parent = await _find_comment(client(ctx), ref, wanted)
            if parent is None:
                raise ActionError(
                    f"qzone: comment {wanted} is not in the first page of this feed; pass the full `comment` object",
                    models.NOT_FOUND)
        return await client(ctx).add_comment(ref, text, parent=parent)


async def _find_comment(qzone, ref: dict[str, Any], wanted: str) -> dict[str, Any] | None:
    """The raw comment object for `wanted`, so a reply can name its target."""
    page = await qzone.comments(ref)
    for entry in page.get("comments") or []:
        # `comment_item` keeps the raw object under `comment`.
        raw = entry.get("comment") if isinstance(entry.get("comment"), dict) else {}
        if str(entry.get("comment_id") or raw.get("id") or "") == wanted:
            return raw or None
    return None
