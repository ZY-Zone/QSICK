"""OneBot adapter for `set_qzone_public_follow`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `set_qzone_public_follow` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class SetQzonePublicFollowAction(PayloadAction):
    op = "set_qzone_public_follow"
