"""OneBot adapter for `get_qzone_feeds`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `get_qzone_feeds` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class GetQzoneFeedsAction(PayloadAction):
    op = "get_qzone_feeds"
