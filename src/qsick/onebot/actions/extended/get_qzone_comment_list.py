"""OneBot adapter for `get_qzone_comment_list`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `get_qzone_comment_list` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class GetQzoneCommentListAction(PayloadAction):
    op = "get_qzone_comment_list"
