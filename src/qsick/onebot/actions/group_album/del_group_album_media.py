"""OneBot adapter for `del_group_album_media`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `del_group_album_media` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class DelGroupAlbumMediaAction(PayloadAction):
    op = "del_group_album_media"
