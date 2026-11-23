"""改单条说说的可见范围（`set_qzone_msg_right`）。

这个 CGI 是**重写整条说说**而不是打补丁：正文、图片、`ugcright_id` 全都要原样送回去。所以客户端会先读该说说
的详情，再把整条表单重建；重建不了（转发、视频、音乐）时直接拒绝，而不是改写成一条更小的说说——
实测只送 `tid`/`con`/`ugc_right` 也能"成功"，代价是**附带的图片被静默删掉**。
"""
from __future__ import annotations

from typing import Any

from ... import models
from ...registry import ActionError
from ._common import QzoneAction, client, resolve_ref


class SetQzoneMsgRightAction(QzoneAction):
    async def handle(self, ctx: Any, ugc_right: int = 1, target_uins: Any = None, **params: Any) -> Any:
        right = int(ugc_right or 1)
        if right not in (1, 4, 16, 64, 128):
            raise ActionError("qzone: ugc_right must be one of 1/4/16/64/128", models.BAD_REQUEST)
        ref = await resolve_ref(ctx, params)
        union = ref.get("union_id") if isinstance(ref.get("union_id"), dict) else {}
        tid = str(union.get("feed_id") or params.get("tid") or "")
        allowed = [str(item) for item in (target_uins or []) if str(item).strip()] if target_uins else None
        if right in (16, 128) and not allowed:
            raise ActionError("qzone: ugc_right 16/128 needs target_uins", models.BAD_REQUEST)
        return await client(ctx).set_msg_right(tid, right, target_uins=allowed)
