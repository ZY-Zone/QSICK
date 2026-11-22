"""OneBot adapter for `set_qzone_public_upvote`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `set_qzone_public_upvote` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class SetQzonePublicUpvoteAction(PayloadAction):
    op = "set_qzone_public_upvote"
