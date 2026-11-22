"""OneBot adapter for `fetch_custom_face`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `fetch_custom_face` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class FetchCustomFaceAction(PayloadAction):
    op = "fetch_custom_face"
