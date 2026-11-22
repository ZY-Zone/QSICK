"""OneBot adapter for `get_qzone_share_ark`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `get_qzone_share_ark` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class GetQzoneShareArkAction(PayloadAction):
    op = "get_qzone_share_ark"
