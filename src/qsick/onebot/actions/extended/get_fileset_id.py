"""OneBot adapter for `get_fileset_id`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `get_fileset_id` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class GetFilesetIdAction(PayloadAction):
    op = "get_fileset_id"
