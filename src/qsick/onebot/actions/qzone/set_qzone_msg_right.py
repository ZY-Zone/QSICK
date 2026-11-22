"""改单条说说的可见范围（`set_qzone_msg_right`）。"""
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
        allowed = [str(item) for item in (target_uins or [])] if target_uins else None
        return await client(ctx).set_msg_right(tid, right, target_uins=allowed)
