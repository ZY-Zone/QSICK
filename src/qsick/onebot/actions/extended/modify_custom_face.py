"""OneBot adapter for `modify_custom_face`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `modify_custom_face` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class ModifyCustomFaceAction(PayloadAction):
    op = "modify_custom_face"
