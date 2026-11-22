"""OneBot adapter for `get_collection_list`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `get_collection_list` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class GetCollectionListAction(PayloadAction):
    op = "get_collection_list"
