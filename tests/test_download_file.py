"""`download_file` reaches `media.download`, and can resolve the target from the message cache.

Three failures are pinned here, all of them measured rather than imagined:

1. The action declared `op = "download_file"`, which the payload does not implement, so every call
   answered `Unsupported operation: download_file`. `media.download` *was* implemented and reachable
   from no action at all -- `get_file`/`get_image`/`get_record` all go through `media.url`, which is
   deliberately unresolved.

2. `element_id` never reached the cached element, because `to_onebot`'s media whitelist dropped it.
   The cache stores exactly the `to_onebot` shape, so a cached private file could not be downloaded
   at all -- and `set_media_path`, which matches on the same field, silently returned False for
   every file.

3. With the cache fixed, `element_id` no longer has to be supplied by the caller: a `message_id`
   alone is enough, which is what makes a *cached* private file downloadable.
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
from qsick.protocol.elements import normalize, to_onebot  # noqa: E402
from qsick.protocol.message import Message  # noqa: E402


class Ctx:
    account = "3905548034"

    def __init__(self, cached=None) -> None:
        self.seen: list[tuple[str, dict]] = []
        self.store = SimpleNamespace(get=lambda _mid: cached) if cached is not None else SimpleNamespace(
            get=lambda _mid: None)

    async def call(self, op, params, label="", **kwargs):
        self.seen.append((op, dict(params)))
        return SimpleNamespace(ok=True, data={"file_path": "C:/tmp/x.txt", "file_size": 264})


def cached_message(elements, *, chat_type="private", chat_id=2208948865, message_id=7712464421138059):
    return Message.from_payload({
        "message_id": message_id, "time": 1, "chat": {"type": chat_type, "id": chat_id},
        "elements": elements, "sender": {"uin": chat_id, "nickname": "x"}, "account": "3905548034",
    })


async def run(ctx, **params):
    result = await DownloadFileAction().run(ctx, **params)
    return ctx.seen, result


# --------------------------------------------------------------- explicit parameters


async def test_it_calls_media_download_not_a_missing_op():
    ctx = Ctx()
    seen, _ = await run(ctx, message_id="7712464421138059", element_id="7712464421138058",
                        chat_type="private", chat_id=2208948865)
    assert [op for op, _ in seen] == ["media.download"]
    payload = seen[0][1]
    assert payload["message_id"] == "7712464421138059"
    assert payload["element_id"] == "7712464421138058"
    assert payload["chat"] == {"type": "private", "id": 2208948865}


async def test_a_private_chat_can_be_named_by_user_id():
    ctx = Ctx()
    seen, _ = await run(ctx, message_id="1", element_id="2", user_id=2208948865)
    assert seen[0][1]["chat"] == {"type": "private", "id": 2208948865}


async def test_a_group_chat_can_be_named_by_group_id():
    ctx = Ctx()
    seen, _ = await run(ctx, message_id="1", element_id="2", group_id=2169010946)
    assert seen[0][1]["chat"] == {"type": "group", "id": 2169010946}


async def test_an_explicit_file_path_is_forwarded():
    ctx = Ctx()
    seen, _ = await run(ctx, message_id="1", element_id="2", user_id=2208948865,
                        file_path="D:/tmp/out.txt")
    assert seen[0][1]["file_path"] == "D:/tmp/out.txt"


async def test_no_file_path_means_the_client_chooses():
    ctx = Ctx()
    seen, _ = await run(ctx, message_id="1", element_id="2", user_id=2208948865)
    assert "file_path" not in seen[0][1]


# -------------------------------------------------------------- resolved from the cache


async def test_element_id_and_chat_come_from_the_cache_when_only_a_message_id_is_given():
    """The reported gap: a cached private file was not downloadable without the wire event."""
    message = cached_message([{"type": "file", "data": {
        "file": "81e2a983", "file_name": "c2c-inbound.txt", "file_size": "264",
        "element_id": "7712464421138058"}}])
    ctx = Ctx(cached=message)
    seen, _ = await run(ctx, message_id="7712464421138059")
    assert seen[0][1]["element_id"] == "7712464421138058"
    assert seen[0][1]["chat"] == {"type": "private", "id": 2208948865}


async def test_a_group_message_resolves_the_same_way():
    message = cached_message([{"type": "file", "data": {"file": "f", "element_id": "42"}}],
                             chat_type="group", chat_id=2169010946)
    ctx = Ctx(cached=message)
    seen, _ = await run(ctx, message_id="7712464421138059")
    assert seen[0][1]["chat"] == {"type": "group", "id": 2169010946}
    assert seen[0][1]["element_id"] == "42"


async def test_an_explicit_element_id_wins_over_the_cache():
    message = cached_message([{"type": "file", "data": {"file": "f", "element_id": "42"}}])
    ctx = Ctx(cached=message)
    seen, _ = await run(ctx, message_id="7712464421138059", element_id="999")
    assert seen[0][1]["element_id"] == "999"


async def test_a_legacy_row_without_element_id_says_so():
    """Rows recorded before the identity fields were carried have no handle; inventing one would
    correlate the wrong download."""
    message = cached_message([{"type": "file", "data": {"file": "f"}}])
    ctx = Ctx(cached=message)
    with pytest.raises(ActionError, match="no element_id"):
        await run(ctx, message_id="7712464421138059")
    assert ctx.seen == []


async def test_an_unknown_message_is_not_a_crash():
    ctx = Ctx()
    with pytest.raises(ActionError, match="not in the cache"):
        await run(ctx, message_id="7712464421138059")


async def test_a_message_with_nothing_downloadable_is_refused():
    message = cached_message([{"type": "text", "data": {"text": "hi"}}])
    ctx = Ctx(cached=message)
    with pytest.raises(ActionError, match="no downloadable element"):
        await run(ctx, message_id="7712464421138059")


async def test_several_elements_require_a_choice():
    message = cached_message([
        {"type": "file", "data": {"file": "a", "element_id": "1"}},
        {"type": "file", "data": {"file": "b", "element_id": "2"}},
    ])
    ctx = Ctx(cached=message)
    with pytest.raises(ActionError, match="several downloadable elements"):
        await run(ctx, message_id="7712464421138059")


async def test_index_picks_one_of_several():
    message = cached_message([
        {"type": "file", "data": {"file": "a", "element_id": "1"}},
        {"type": "file", "data": {"file": "b", "element_id": "2"}},
    ])
    ctx = Ctx(cached=message)
    seen, _ = await run(ctx, message_id="7712464421138059", index=1)
    assert seen[0][1]["element_id"] == "2"


async def test_a_missing_message_id_is_refused():
    ctx = Ctx()
    with pytest.raises(ActionError, match="message_id is required"):
        await run(ctx, element_id="2", user_id=2208948865)
    assert ctx.seen == []


def test_the_payload_actually_implements_the_op_it_names():
    source = (ROOT / "src" / "qsick" / "headless_actions.cjs").read_text(encoding="utf-8")
    start = source.index("const REQUIREMENTS = Object.freeze({")
    end = source.index("});", start)
    assert "'media.download':" in source[start:end], "media.download is not in REQUIREMENTS"
    assert "op === 'media.download'" in source, "media.download has no handler"


# ------------------------------------------------------------------ element identity


WIRE_FILE = {"type": "file", "data": {
    "file_id": "81e2a983f95b09df4de35ea1c783c368_6a380262", "file_name": "c2c-inbound.txt",
    "file_size": "264", "file_sub_id": "D6EAUDoMCIHFp", "element_id": "7712464421138058"}}


def test_the_segment_keeps_the_element_id():
    """The cache stores this shape; dropping the field is what made cached files undownloadable."""
    segment = to_onebot(normalize(WIRE_FILE))
    assert segment["data"]["element_id"] == "7712464421138058"
    assert segment["data"]["file_sub_id"] == "D6EAUDoMCIHFp"


def test_reading_the_segment_back_keeps_it_too():
    """`_media_in` was a whitelist that dropped the field again in this direction."""
    segment = to_onebot(normalize(WIRE_FILE))
    assert normalize(segment).data["element_id"] == "7712464421138058"


def test_the_cache_round_trip_keeps_it():
    message = Message.from_payload({
        "message_id": 1, "time": 1, "chat": {"type": "private", "id": 2208948865},
        "elements": [WIRE_FILE], "sender": {"uin": 2208948865},
    })
    stored = message.to_dict()
    assert stored["elements"][0]["data"]["element_id"] == "7712464421138058"
    again = Message.from_payload(stored)
    assert again.elements[0].data["element_id"] == "7712464421138058"


def test_images_and_records_carry_it_as_well():
    """`media.download` needs the same handle for every kind it can fetch, not only files."""
    nid = to_onebot(normalize({"type": "image", "data": {"file_id": "a", "element_id": "77"}}))
    assert nid["data"]["element_id"] == "77"
    rec = to_onebot(normalize({"type": "record", "data": {"file_id": "b", "element_id": "88"}}))
    assert rec["data"]["element_id"] == "88"


def test_no_element_id_is_never_invented():
    segment = to_onebot(normalize({"type": "file", "data": {"file_id": "x"}}))
    assert "element_id" not in segment["data"]
