"""OneBot adapter for `request_decrypt_key`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `request_decrypt_key` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class RequestDecryptKeyAction(PayloadAction):
    op = "request_decrypt_key"
