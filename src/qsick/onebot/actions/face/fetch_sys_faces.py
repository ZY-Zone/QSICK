"""OneBot adapter for `fetch_sys_faces`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `fetch_sys_faces` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class FetchSysFacesAction(PayloadAction):
    op = "fetch_sys_faces"
