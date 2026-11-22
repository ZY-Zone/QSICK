"""OneBot adapter for `get_qzone_home_feeds`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `get_qzone_home_feeds` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class GetQzoneHomeFeedsAction(PayloadAction):
    op = "get_qzone_home_feeds"
