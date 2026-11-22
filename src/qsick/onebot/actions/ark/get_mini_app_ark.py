"""OneBot adapter for `get_mini_app_ark`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `get_mini_app_ark` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class GetMiniAppArkAction(PayloadAction):
    op = "get_mini_app_ark"
