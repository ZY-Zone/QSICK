"""改单条说说的可见范围（`set_qzone_msg_right`）。

这个 CGI 是**重写整条说说**而不是打补丁，所以要把它现在的正文原样带回去；不带会被服务端以
``code -10005 您未输入内容`` 拒绝。
"""
from __future__ import annotations

from typing import Any

from ... import models
from ...registry import ActionError
from ._common import QzoneAction, client, find_feed


class SetQzoneMsgRightAction(QzoneAction):
    async def handle(self, ctx: Any, ugc_right: int = 1, content: str = "", target_uins: Any = None,
                     **params: Any) -> Any:
        right = int(ugc_right or 1)
        if right not in (1, 4, 16, 64, 128):
            raise ActionError("qzone: ugc_right must be one of 1/4/16/64/128", models.BAD_REQUEST)
        ref, feed = await find_feed(ctx, params)
        text = str(content or (feed or {}).get("content") or "").strip()
        if not text:
            raise ActionError(
                "qzone: this call rewrites the post, so its text is required; pass `content` "
                "or a `feed_ref` from a listing that still carries it", models.BAD_REQUEST)
        union = ref.get("union_id") if isinstance(ref.get("union_id"), dict) else {}
        tid = str(union.get("feed_id") or params.get("tid") or "")
        allowed = [str(item) for item in (target_uins or [])] if target_uins else None
        return await client(ctx).set_msg_right(tid, right, content=text, target_uins=allowed)
