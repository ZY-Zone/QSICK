"""OneBot adapter for `unlike_qzone`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `unlike_qzone` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class UnlikeQzoneAction(PayloadAction):
    op = "unlike_qzone"
