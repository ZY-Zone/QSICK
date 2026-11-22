"""OneBot adapter for `do_group_album_comment`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `do_group_album_comment` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class DoGroupAlbumCommentAction(PayloadAction):
    op = "do_group_album_comment"
