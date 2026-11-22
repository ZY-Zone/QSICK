"""OneBot adapter for `.get_word_slices`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `.get_word_slices` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class GetWordSlicesAction(PayloadAction):
    op = ".get_word_slices"
