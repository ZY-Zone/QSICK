"""OneBot adapter for `nc_get_rkey`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `nc_get_rkey` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class NcGetRkeyAction(PayloadAction):
    op = "nc_get_rkey"
