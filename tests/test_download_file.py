"""`download_file` reaches `media.download`, and refuses what that op cannot do.

The action declared `op = "download_file"`, which the payload does not implement, so every call
answered `Unsupported operation: download_file`. Meanwhile `media.download` *was* implemented in the
payload and reachable from no action at all: `get_file`/`get_image`/`get_record` all go through
`media.url`, which is deliberately unresolved.

This matters for private files specifically: there is no download URL we can produce yet (the OIDB
layout for `0xE37_800` is unmapped), so downloading the element is the route that works.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
for entry in (str(ROOT), str(ROOT / "src")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from qsick.onebot.actions.group_file.download_file import DownloadFileAction  # noqa: E402
from qsick.onebot.registry import ActionError  # noqa: E402


class Ctx:
    account = "3905548034"

    def __init__(self) -> None:
        self.seen: list[tuple[str, dict]] = []

    async def call(self, op, params, label="", **kwargs):
        self.seen.append((op, dict(params)))
        return SimpleNamespace(ok=True, data={"path": "C:/tmp/x.txt", "file_path": "C:/tmp/x.txt"})


async def run(**params):
    ctx = Ctx()
    result = await DownloadFileAction().run(ctx, **params)
    return ctx.seen, result


async def test_it_calls_media_download_not_a_missing_op():
    seen, _ = await run(message_id="7712464421138058359", element_id="7712464421138058358",
                        chat_type="private", chat_id=2208948865)
    assert [op for op, _ in seen] == ["media.download"]
    payload = seen[0][1]
    assert payload["message_id"] == "7712464421138058359"
    assert payload["element_id"] == "7712464421138058358"
    assert payload["chat"] == {"type": "private", "id": 2208948865}


async def test_a_private_chat_can_be_named_by_user_id():
    """The payload wants one chat object; a caller may only have the friend's number."""
    seen, _ = await run(message_id="1", element_id="2", user_id=2208948865)
    assert seen[0][1]["chat"] == {"type": "private", "id": 2208948865}


async def test_a_group_chat_can_be_named_by_group_id():
    seen, _ = await run(message_id="1", element_id="2", group_id=2169010946)
    assert seen[0][1]["chat"] == {"type": "group", "id": 2169010946}


async def test_an_explicit_file_path_is_forwarded():
    seen, _ = await run(message_id="1", element_id="2", user_id=2208948865,
                        file_path="D:/tmp/out.txt")
    assert seen[0][1]["file_path"] == "D:/tmp/out.txt"


async def test_no_file_path_means_the_client_chooses():
    seen, _ = await run(message_id="1", element_id="2", user_id=2208948865)
    assert "file_path" not in seen[0][1]


async def test_a_missing_element_id_is_refused_before_calling_out():
    """`element_id` is the correlation key the completion event carries; without it the payload
    cannot tell which download finished, so guessing on the caller's behalf would be worse."""
    ctx = Ctx()
    with pytest.raises(ActionError, match="element_id is required"):
        await DownloadFileAction().run(ctx, message_id="1", user_id=2208948865)
    assert ctx.seen == []


async def test_a_missing_message_id_is_refused():
    ctx = Ctx()
    with pytest.raises(ActionError, match="message_id is required"):
        await DownloadFileAction().run(ctx, element_id="2", user_id=2208948865)
    assert ctx.seen == []


async def test_an_unknown_chat_type_is_refused():
    ctx = Ctx()
    with pytest.raises(ActionError, match="chat_type must be"):
        await DownloadFileAction().run(ctx, message_id="1", element_id="2",
                                       chat_type="temp", chat_id=1)
    assert ctx.seen == []


async def test_no_target_at_all_is_refused():
    ctx = Ctx()
    with pytest.raises(ActionError, match="chat_type must be"):
        await DownloadFileAction().run(ctx, message_id="1", element_id="2")
    assert ctx.seen == []


def test_the_payload_actually_implements_the_op_it_names():
    """The failure this action already had: naming an op that does not exist."""
    from pathlib import Path as _Path

    source = (_Path(ROOT) / "src" / "qsick" / "headless_actions.cjs").read_text(encoding="utf-8")
    start = source.index("const REQUIREMENTS = Object.freeze({")
    end = source.index("});", start)
    assert "'media.download':" in source[start:end], "media.download is not in REQUIREMENTS"
    assert "op === 'media.download'" in source, "media.download has no handler"
