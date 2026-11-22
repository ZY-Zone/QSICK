"""OneBot adapter for `send_custom_face`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `send_custom_face` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class SendCustomFaceAction(PayloadAction):
    op = "send_custom_face"
