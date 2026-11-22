"""OneBot adapter for `get_robot_uin_range`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `get_robot_uin_range` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class GetRobotUinRangeAction(PayloadAction):
    op = "get_robot_uin_range"
