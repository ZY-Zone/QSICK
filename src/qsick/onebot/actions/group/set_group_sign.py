"""OneBot adapter for `set_group_sign`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `set_group_sign` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class SetGroupSignAction(PayloadAction):
    op = "set_group_sign"
