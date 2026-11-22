"""OneBot adapter for `get_guild_list`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `get_guild_list` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class GetGuildListAction(PayloadAction):
    op = "get_guild_list"
