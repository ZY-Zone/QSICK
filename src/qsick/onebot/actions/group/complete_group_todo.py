"""OneBot adapter for `complete_group_todo`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `complete_group_todo` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class CompleteGroupTodoAction(PayloadAction):
    op = "complete_group_todo"
