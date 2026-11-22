"""OneBot adapter for `check_url_safely`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `check_url_safely` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class CheckUrlSafelyAction(PayloadAction):
    op = "check_url_safely"
