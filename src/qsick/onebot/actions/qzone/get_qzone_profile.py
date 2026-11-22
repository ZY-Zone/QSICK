"""空间资料（`get_qzone_profile`），走老 CGI。

测过：`cgi_userinfo_get_all` 回 JSONP 包裹的 `{"code":0,"message":"获取成功", ...}`，不需要 `x-oidb`。
`target_uin` 不传时读自己的。
"""
from __future__ import annotations

from typing import Any

from ._common import QzoneAction, client


class GetQzoneProfileAction(QzoneAction):
    async def handle(self, ctx: Any, target_uin: str = "", **_ignored: Any) -> Any:
        data = await client(ctx).profile(str(target_uin or ""))
        return {"name": str(data.get("spacename") or data.get("name") or ""),
                "description": str(data.get("desc") or data.get("description") or data.get("signature") or ""),
                "uin": str(data.get("uin") or target_uin or ""),
                "nickname": str(data.get("nickname") or ""), "raw": data}
