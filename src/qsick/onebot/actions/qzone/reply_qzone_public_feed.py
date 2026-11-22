"""OneBot adapter for `reply_qzone_public_feed`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `reply_qzone_public_feed` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class ReplyQzonePublicFeedAction(PayloadAction):
    op = "reply_qzone_public_feed"
