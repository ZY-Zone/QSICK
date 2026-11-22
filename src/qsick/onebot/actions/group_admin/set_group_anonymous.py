"""OneBot adapter for `set_group_anonymous`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `set_group_anonymous` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class SetGroupAnonymousAction(PayloadAction):
    op = "set_group_anonymous"
