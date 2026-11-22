"""OneBot adapter for `comment_qzone_public_feed`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `comment_qzone_public_feed` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class CommentQzonePublicFeedAction(PayloadAction):
    op = "comment_qzone_public_feed"
