"""OneBot adapter for `cancel_group_album_media_like`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `cancel_group_album_media_like` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class CancelGroupAlbumMediaLikeAction(PayloadAction):
    op = "cancel_group_album_media_like"
