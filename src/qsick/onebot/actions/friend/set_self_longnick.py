"""OneBot adapter for `set_self_longnick`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `set_self_longnick` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class SetSelfLongnickAction(PayloadAction):
    op = "set_self_longnick"
