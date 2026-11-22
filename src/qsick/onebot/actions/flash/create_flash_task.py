"""OneBot adapter for `create_flash_task`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `create_flash_task` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class CreateFlashTaskAction(PayloadAction):
    op = "create_flash_task"
