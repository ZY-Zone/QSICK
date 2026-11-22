"""OneBot adapter for `delete_custom_face`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `delete_custom_face` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class DeleteCustomFaceAction(PayloadAction):
    op = "delete_custom_face"
