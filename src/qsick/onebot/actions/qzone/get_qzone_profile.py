"""空间资料（`get_qzone_profile`），走老 CGI。

测过：`cgi_userinfo_get_all` 回 JSONP 包裹的 `{"code":0,"message":"获取成功", ...}`，不需要 `x-oidb`。
"""
from __future__ import annotations

from typing import Any

from ._common import QzoneAction, client


class GetQzoneProfileAction(QzoneAction):
    async def handle(self, ctx: Any, **_ignored: Any) -> Any:
        data = await client(ctx).profile()
        return {"name": str(data.get("name") or data.get("spacename") or ""),
                "description": str(data.get("description") or data.get("signature") or ""),
                "uin": str(data.get("uin") or ""), "raw": data}
