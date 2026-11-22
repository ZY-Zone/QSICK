"""OneBot adapter for `add_custom_face`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `add_custom_face` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class AddCustomFaceAction(PayloadAction):
    op = "add_custom_face"
