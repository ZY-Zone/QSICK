"""空友爱看能力探测（`get_qzone_public_capabilities`）。

实测本账号各项 `public_qzone_*` 均为 false，`GetPublicRecomFeeds` 回 total 0 —— 即在白名单之外。
这里如实返回服务端给的值，而不是假装没有这条路由。
"""
from __future__ import annotations

from typing import Any

from ._common import QzoneAction, client


class GetQzonePublicCapabilitiesAction(QzoneAction):
    async def handle(self, ctx: Any, **_ignored: Any) -> Any:
        data = await client(ctx).capabilities()
        entrances = data.get("entrances") if isinstance(data, dict) else {}
        flags = {key: value for key, value in (entrances or {}).items() if str(key).startswith("public_qzone")}
        return {"uin": str((data or {}).get("uin") or ""), "flags": flags,
                "whitelisted": any(bool(value) for value in flags.values()), "raw": data}
