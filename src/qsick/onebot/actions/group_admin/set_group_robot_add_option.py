"""OneBot adapter for `set_group_robot_add_option`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `set_group_robot_add_option` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class SetGroupRobotAddOptionAction(PayloadAction):
    op = "set_group_robot_add_option"
