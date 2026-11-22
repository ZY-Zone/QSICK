"""OneBot adapter for `get_profile_like`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `get_profile_like` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class GetProfileLikeAction(PayloadAction):
    op = "get_profile_like"
