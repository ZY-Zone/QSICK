"""OneBot adapter for `ArkSharePeer`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `ArkSharePeer` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class ArkSharePeerAction(PayloadAction):
    op = "ArkSharePeer"
