"""OneBot adapter for `set_qq_profile`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `set_qq_profile` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class SetQqProfileAction(PayloadAction):
    op = "set_qq_profile"
