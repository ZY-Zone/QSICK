"""OneBot adapter for `get_qzone_public_feeds`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `get_qzone_public_feeds` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class GetQzonePublicFeedsAction(PayloadAction):
    op = "get_qzone_public_feeds"
