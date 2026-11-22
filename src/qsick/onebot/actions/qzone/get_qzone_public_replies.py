"""OneBot adapter for `get_qzone_public_replies`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `get_qzone_public_replies` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class GetQzonePublicRepliesAction(PayloadAction):
    op = "get_qzone_public_replies"
