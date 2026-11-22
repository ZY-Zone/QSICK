"""OneBot adapter for `get_ai_characters`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `get_ai_characters` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class GetAiCharactersAction(PayloadAction):
    op = "get_ai_characters"
