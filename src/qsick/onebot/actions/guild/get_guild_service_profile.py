"""OneBot adapter for `get_guild_service_profile`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `get_guild_service_profile` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class GetGuildServiceProfileAction(PayloadAction):
    op = "get_guild_service_profile"
