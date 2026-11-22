"""OneBot adapter for `_set_model_show`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `_set_model_show` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class SetModelShowAction(PayloadAction):
    op = "_set_model_show"
