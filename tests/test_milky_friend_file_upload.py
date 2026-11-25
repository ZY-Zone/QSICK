"""One internal event may become several wire events, and `friend_file_upload` is derived.

A friend's file reaches QSICK only as a file element inside the message that delivered it --
QQ NT has no separate notice for it. Milky defines `friend_file_upload` as its own event, so
the Milky adapter derives it from that message. The v11 stream must stay unchanged: adding a
notice there would put a non-v11 event on every OneBot client.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for entry in (str(ROOT), str(ROOT / "src")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from protocols.milky import events  # noqa: E402
from protocols.milky.adapter import MilkyProtocol  # noqa: E402

SELF_UIN = 3905548034
FRIEND = 1927676487


def private_message(*segments, post_type="message"):
    return {
        "post_type": post_type, "message_type": "private", "sub_type": "friend",
        "time": 1700000000, "self_id": SELF_UIN, "user_id": FRIEND, "target_id": SELF_UIN,
        "message_seq": 7, "message": list(segments),
    }


FILE = {"type": "file", "data": {"file": "fid-1", "name": "报告.pdf", "file_size": 12345}}
TEXT = {"type": "text", "data": {"text": "看这个"}}


def test_private_file_message_yields_both_events():
    produced = MilkyProtocol().encode_events(private_message(TEXT, FILE))
    assert [item["event_type"] for item in produced] == ["message_receive", "friend_file_upload"]
    upload = produced[1]["data"]
    assert upload["user_id"] == FRIEND
    assert upload["file_id"] == "fid-1"
    assert upload["file_name"] == "报告.pdf"
    assert upload["file_size"] == 12345
    assert upload["is_self"] is False


def test_message_without_a_file_yields_only_the_message():
    produced = MilkyProtocol().encode_events(private_message(TEXT))
    assert [item["event_type"] for item in produced] == ["message_receive"]


def test_group_file_message_does_not_become_a_friend_upload():
    """A group file belongs to `group_file_upload`; the friend event must not fire for it."""
    event = private_message(FILE)
    event.update({"message_type": "group", "group_id": 2169010946, "sub_type": "normal"})
    assert events.friend_file_upload(event) is None


def test_own_sent_file_is_marked_self():
    upload = events.friend_file_upload(private_message(FILE, post_type="message_sent"))
    assert upload is not None and upload["data"]["is_self"] is True


def test_file_without_ids_is_not_reported():
    """Without a usable file_id and name the event cannot be acted on, so it is not emitted."""
    assert events.friend_file_upload(private_message({"type": "file", "data": {"file_size": 1}})) is None
    assert events.friend_file_upload(private_message({"type": "file", "data": {"file": "x"}})) is None


def test_unknown_hash_is_empty_not_substituted():
    """`file_hash` is TriSHA1; an MD5 in that field is a wrong value, so it stays empty."""
    upload = events.friend_file_upload(
        private_message({"type": "file", "data": {"file": "f", "name": "n", "md5": "abc"}}))
    assert upload is not None and upload["data"]["file_hash"] == ""


def test_hash_is_carried_when_the_client_reported_one():
    upload = events.friend_file_upload(
        private_message({"type": "file", "data": {"file": "f", "name": "n", "file_hash": "trisba1"}}))
    assert upload is not None and upload["data"]["file_hash"] == "trisba1"


class _Single:
    def encode_event(self, event):
        return {"event_type": "one"}


class _Empty:
    def encode_events(self, event):
        return []


def test_pipeline_normalizes_single_event_adapters():
    """A protocol that only returns one event keeps working through the plural entry point."""
    from qsick.onebot.pipeline import ProtocolPipelines

    pipelines = ProtocolPipelines(_protocols={"x": _Single()}, _errors={})
    assert pipelines.encode_events("x", {}) == [{"event_type": "one"}]
    assert pipelines.encode_event("x", {}) == {"event_type": "one"}


def test_pipeline_reports_an_encoder_that_produces_nothing():
    from qsick.onebot.pipeline import ProtocolPipelines

    pipelines = ProtocolPipelines(_protocols={"x": _Empty()}, _errors={})
    assert pipelines.encode_events("x", {}) == []
    assert "x" in pipelines._errors
    assert pipelines.encode_event("x", {}) is None