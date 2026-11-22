"""OneBot adapter for `_get_model_show`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `_get_model_show` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class GetModelShowAction(PayloadAction):
    op = "_get_model_show"
