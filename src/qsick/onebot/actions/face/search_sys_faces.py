"""OneBot adapter for `search_sys_faces`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `search_sys_faces` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class SearchSysFacesAction(PayloadAction):
    op = "search_sys_faces"
