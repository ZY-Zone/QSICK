"""A picture reply must be allowed to finish uploading.

The consumer log's most confusing symptom -- "I sent a command and the bot ignored it" -- was a send that had
been cut off, not a command that had been missed:

    [12:09:03] 3905548034 <= 2208948865  好友消息：#帮助      <- received, processed, image generated
    [12:09:03] 3905548034 => 2208948865  发送好友消息         <- send issued
    [12:09:24] 发送消息错误  send message: headless action failed:      <- killed at 20 s, empty message
    [12:09:24] 完成20秒844

The empty error text is `asyncio.wait_for` raising a bare `TimeoutError()`. The action layer called the
transport with no timeout, so the transport's 20 s default applied -- while the native sender budgets 15 s plus
a 60 s media grace for an upload. It was still working when Python gave up.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from qsick.onebot.actions import base  # noqa: E402


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, float | None]] = []

    async def call(self, op, params=None, *, timeout=None):  # noqa: ANN001, ANN003
        from qsick.transport.base import SendResult

        self.calls.append((op, dict(params or {}), timeout))
        return SendResult(ok=True, data={"message_id": 1, "message_id_str": "1"})


def chat(kind: str = "private", ident: int = 2208948865) -> dict:
    return {"type": kind, "id": ident}


class TestMediaGetsRoomToUpload:
    async def test_a_picture_send_gets_the_long_budget(self):
        ctx = _Recorder()
        await base.send_message(ctx, chat(), [{"type": "image", "data": {"file": "base64://..."}}], False)
        _op, _params, timeout = ctx.calls[-1]
        assert timeout == base.MEDIA_SEND_TIMEOUT
        assert timeout > 60, "上传的媒体可能要用掉一分钟以上，超时必须高过它"

    async def test_a_text_send_keeps_the_short_budget(self):
        # Only media is slow. A wedged session should still be reported in the usual time rather than making every
        # plain reply wait a minute and a half.
        ctx = _Recorder()
        await base.send_message(ctx, chat(), [{"type": "text", "data": {"text": "hi"}}], False)
        _op, _params, timeout = ctx.calls[-1]
        assert timeout is None, "纯文本不该套用媒体超时"

    async def test_forward_and_file_count_as_slow(self):
        for element in ({"type": "file", "data": {"file": "x"}},
                        {"type": "video", "data": {"file": "x"}},
                        {"type": "record", "data": {"file": "x"}}):
            ctx = _Recorder()
            await base.send_message(ctx, chat(), [element], False)
            assert ctx.calls[-1][2] == base.MEDIA_SEND_TIMEOUT, element["type"]

    async def test_a_mixed_message_counts_as_slow(self):
        ctx = _Recorder()
        await base.send_message(ctx, chat(), [
            {"type": "text", "data": {"text": "look"}},
            {"type": "image", "data": {"file": "base64://..."}},
        ], False)
        assert ctx.calls[-1][2] == base.MEDIA_SEND_TIMEOUT
