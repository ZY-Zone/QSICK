"""OneBot adapter for `set_qzone_profile`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `set_qzone_profile` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class SetQzoneProfileAction(PayloadAction):
    op = "set_qzone_profile"
