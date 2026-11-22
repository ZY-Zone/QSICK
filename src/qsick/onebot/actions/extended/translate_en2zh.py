"""OneBot adapter for `translate_en2zh`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `translate_en2zh` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class TranslateEn2zhAction(PayloadAction):
    op = "translate_en2zh"
