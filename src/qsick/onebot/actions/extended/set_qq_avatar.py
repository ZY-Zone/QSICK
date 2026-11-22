"""OneBot adapter for `set_qq_avatar`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `set_qq_avatar` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class SetQqAvatarAction(PayloadAction):
    op = "set_qq_avatar"
