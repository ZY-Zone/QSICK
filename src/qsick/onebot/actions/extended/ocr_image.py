"""OneBot adapter for `ocr_image`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `ocr_image` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class OcrImageAction(PayloadAction):
    op = "ocr_image"
