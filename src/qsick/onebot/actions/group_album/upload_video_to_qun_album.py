"""OneBot adapter for `upload_video_to_qun_album`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `upload_video_to_qun_album` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class UploadVideoToQunAlbumAction(PayloadAction):
    op = "upload_video_to_qun_album"
