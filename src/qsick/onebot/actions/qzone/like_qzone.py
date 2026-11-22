"""OneBot adapter for `like_qzone`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `like_qzone` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class LikeQzoneAction(PayloadAction):
    op = "like_qzone"
