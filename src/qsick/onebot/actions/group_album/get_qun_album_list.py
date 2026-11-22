"""OneBot adapter for `get_qun_album_list`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `get_qun_album_list` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class GetQunAlbumListAction(PayloadAction):
    op = "get_qun_album_list"
