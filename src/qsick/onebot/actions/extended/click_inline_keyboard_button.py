"""OneBot adapter for `click_inline_keyboard_button`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `click_inline_keyboard_button` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class ClickInlineKeyboardButtonAction(PayloadAction):
    op = "click_inline_keyboard_button"
