"""OneBot adapter for `move_custom_face_to_front`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `move_custom_face_to_front` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class MoveCustomFaceToFrontAction(PayloadAction):
    op = "move_custom_face_to_front"
