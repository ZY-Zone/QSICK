"""OneBot adapter for `fetch_super_face_id`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `fetch_super_face_id` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class FetchSuperFaceIdAction(PayloadAction):
    op = "fetch_super_face_id"
