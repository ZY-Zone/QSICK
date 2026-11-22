"""OneBot adapter for `fetch_ptt_text`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `fetch_ptt_text` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class FetchPttTextAction(PayloadAction):
    op = "fetch_ptt_text"
