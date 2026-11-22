"""OneBot adapter for `fetch_face_entity`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `fetch_face_entity` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class FetchFaceEntityAction(PayloadAction):
    op = "fetch_face_entity"
