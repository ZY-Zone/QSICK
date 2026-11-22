"""OneBot adapter for `forward_qzone_public_feed`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `forward_qzone_public_feed` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class ForwardQzonePublicFeedAction(PayloadAction):
    op = "forward_qzone_public_feed"
