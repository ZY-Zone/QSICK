"""删说说（`delete_qzone_msg`）。

CGI 接受的是 `tid`，而调用方手里通常是一个 feed 引用，所以 tid 从引用里取。
"""
from __future__ import annotations

from typing import Any

from ._common import QzoneAction, client, resolve_ref


class DeleteQzoneMsgAction(QzoneAction):
    async def handle(self, ctx: Any, **params: Any) -> Any:
        ref = await resolve_ref(ctx, params)
        union = ref.get("union_id") if isinstance(ref.get("union_id"), dict) else {}
        tid = str(union.get("feed_id") or params.get("tid") or "")
        return await client(ctx).delete_msg(tid)
