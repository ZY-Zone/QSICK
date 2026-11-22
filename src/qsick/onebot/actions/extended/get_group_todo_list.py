"""OneBot adapter for `get_group_todo_list`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `get_group_todo_list` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class GetGroupTodoListAction(PayloadAction):
    op = "get_group_todo_list"
