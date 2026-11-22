"""OneBot adapter for `get_fileset_info`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `get_fileset_info` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class GetFilesetInfoAction(PayloadAction):
    op = "get_fileset_info"
