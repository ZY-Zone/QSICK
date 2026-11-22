"""OneBot adapter for `get_emoji_likes`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `get_emoji_likes` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class GetEmojiLikesAction(PayloadAction):
    op = "get_emoji_likes"
