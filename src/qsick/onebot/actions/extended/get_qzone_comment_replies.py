"""OneBot adapter for `get_qzone_comment_replies`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `get_qzone_comment_replies` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class GetQzoneCommentRepliesAction(PayloadAction):
    op = "get_qzone_comment_replies"
