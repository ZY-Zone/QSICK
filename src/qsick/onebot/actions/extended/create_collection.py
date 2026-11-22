"""OneBot adapter for `create_collection`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `create_collection` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class CreateCollectionAction(PayloadAction):
    op = "create_collection"
