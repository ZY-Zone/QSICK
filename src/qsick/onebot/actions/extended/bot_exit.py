"""OneBot adapter for `bot_exit`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `bot_exit` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class BotExitAction(PayloadAction):
    op = "bot_exit"
