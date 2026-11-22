"""OneBot adapter for `set_essence_msg`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `set_essence_msg` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class SetEssenceMsgAction(PayloadAction):
    op = "set_essence_msg"
