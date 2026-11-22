"""OneBot adapter for `clean_stream_temp_file`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `clean_stream_temp_file` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class CleanStreamTempFileAction(PayloadAction):
    op = "clean_stream_temp_file"
