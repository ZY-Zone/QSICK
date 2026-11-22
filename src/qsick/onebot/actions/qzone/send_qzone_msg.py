"""发说说（`send_qzone_msg`）。

纯文字与带图走同一条 CGI：`emotion_cgi_publish_v6` 接受 `richval`，并在回答里给出新说说的 `tid`。
图片先经 `cgi_upload_image` 换成 `richval`。

`images` 接受本地路径或 http(s) 地址 —— 后者会先取回到临时文件，因为上传接口要的是 base64 内容。
"""
from __future__ import annotations

import tempfile
from typing import Any
from urllib.parse import urlparse
from urllib.request import urlopen

from ...registry import ActionError
from ... import models
from ._common import QzoneAction, client

#: 与 NapCat 一致：1 所有人 / 4 好友 / 16 部分好友 / 64 仅自己 / 128 部分好友不可见。
UGC_RIGHTS = (1, 4, 16, 64, 128)


class SendQzoneMsgAction(QzoneAction):
    async def handle(self, ctx: Any, content: str = "", images: Any = None, ugc_right: int = 1,
                     target_uins: Any = None, **_ignored: Any) -> Any:
        text = str(content or "").strip()
        if not text:
            raise ActionError("qzone: content cannot be empty", models.BAD_REQUEST)
        right = int(ugc_right or 1)
        if right not in UGC_RIGHTS:
            raise ActionError(f"qzone: ugc_right must be one of {UGC_RIGHTS}", models.BAD_REQUEST)
        allowed = [str(item) for item in (target_uins or []) if str(item).strip()] if target_uins else None
        if right in (16, 128) and not allowed:
            raise ActionError("qzone: ugc_right 16/128 needs target_uins", models.BAD_REQUEST)

        qzone = client(ctx)
        richvals: list[str] = []
        for source in (images or []):
            path = str(source)
            if path.startswith(("http://", "https://")):
                path = _fetch(path)
            richvals.append(await qzone.upload_image(path))
        return await qzone.publish(text, images=richvals, ugc_right=right, target_uins=allowed)


def _fetch(url: str) -> str:
    """把远程图片落到临时文件，上传接口要的是内容而不是地址。"""
    suffix = urlparse(url).path.rsplit(".", 1)[-1][:5] or "png"
    handle = tempfile.NamedTemporaryFile(delete=False, suffix="." + suffix)
    try:
        with urlopen(url, timeout=30) as response:  # noqa: S310 - caller supplied the URL
            handle.write(response.read())
    finally:
        handle.close()
    return handle.name
