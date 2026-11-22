"""OneBot adapter for `get_rkey_server`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `get_rkey_server` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class GetRkeyServerAction(PayloadAction):
    op = "get_rkey_server"
