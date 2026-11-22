"""OneBot adapter for `comment_qzone`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `comment_qzone` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class CommentQzoneAction(PayloadAction):
    op = "comment_qzone"
