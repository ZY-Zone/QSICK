"""OneBot adapter for `share_peer`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `share_peer` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class SharePeerAction(PayloadAction):
    op = "share_peer"
