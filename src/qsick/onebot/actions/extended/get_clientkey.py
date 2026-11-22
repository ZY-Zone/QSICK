"""OneBot adapter for `get_clientkey`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `get_clientkey` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class GetClientkeyAction(PayloadAction):
    op = "get_clientkey"
