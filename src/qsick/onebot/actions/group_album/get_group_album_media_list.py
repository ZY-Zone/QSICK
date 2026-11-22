"""OneBot adapter for `get_group_album_media_list`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `get_group_album_media_list` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class GetGroupAlbumMediaListAction(PayloadAction):
    op = "get_group_album_media_list"
