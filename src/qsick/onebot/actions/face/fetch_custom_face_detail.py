"""OneBot adapter for `fetch_custom_face_detail`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `fetch_custom_face_detail` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class FetchCustomFaceDetailAction(PayloadAction):
    op = "fetch_custom_face_detail"
