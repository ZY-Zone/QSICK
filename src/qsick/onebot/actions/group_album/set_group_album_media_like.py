"""OneBot adapter for `set_group_album_media_like`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `set_group_album_media_like` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class SetGroupAlbumMediaLikeAction(PayloadAction):
    op = "set_group_album_media_like"
