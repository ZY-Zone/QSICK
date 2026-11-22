"""Limits have to announce themselves.

The bug that cost the most time in this project was a limit that reported itself as something else: `ReadAllText`
refused files over 4 MB, `results.jsonl` passed 4 MB, and from then on *every* action timed out while the UI said
"not logged in" and no log mentioned size. Once found, the same shape turned up in three more places -- a reply
buffer that silently cut large results in half, a shared state file that one session overwrote for all of them,
and an event stream nobody ever pruned.

These tests pin the invalidation of each one.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from qsick import headless as headless_mod  # noqa: E402

PROBE = REPO / "native" / "qsick-pipe" / "src" / "ipc_probe.cpp"
HTTP = REPO / "native" / "qsick-pipe" / "src" / "qsick_http.cpp"
CJS = REPO / "src" / "qsick" / "headless_actions.cjs"


class TestTheQueueIsReadFromTheTail:
    def test_a_tail_reader_exists(self):
        text = PROBE.read_text(encoding="utf-8", errors="replace")
        assert "static BOOL ReadTailText" in text

    def test_the_result_waiter_uses_it(self):
        # This is the one that broke: the waiter needs the newest lines, never the whole history.
        text = PROBE.read_text(encoding="utf-8", errors="replace")
        assert "ReadTailText(resultsPath" in text
        assert "ReadAllText(resultsPath" not in text, "整读结果文件会让动作随队列增长而变慢，并在 4 MB 处失败"

    def test_the_task_reads_use_it_too(self):
        text = PROBE.read_text(encoding="utf-8", errors="replace")
        assert "ReadTailText(tasksPath" in text

    def test_the_tail_read_starts_on_a_line_boundary(self):
        """A window that begins mid-line would hand the caller half a JSON object."""
        text = PROBE.read_text(encoding="utf-8", errors="replace")
        start = text.index("static BOOL ReadTailText")
        body = text[start:start + 2600]
        assert "probe == '\\n'" in body, "要跳过窗口起点的半行"


class TestATruncatedResultSaysSo:
    def test_the_flag_exists(self):
        assert "g_resultTruncated" in PROBE.read_text(encoding="utf-8", errors="replace")

    def test_the_copy_loops_set_it(self):
        text = PROBE.read_text(encoding="utf-8", errors="replace")
        # Both branches: a JSON string result, and a raw object/array.
        assert text.count("InterlockedExchange(&g_resultTruncated, 1)") >= 2

    def test_the_http_layer_reports_it_instead_of_serving_half_json(self):
        text = HTTP.read_text(encoding="utf-8", errors="replace")
        assert '\\"truncated\\":true' in text
        assert "g_resultTruncated" in text

    def test_the_reply_buffer_is_not_the_old_128k(self):
        text = HTTP.read_text(encoding="utf-8", errors="replace")
        assert "1024 * 1024" in text


class TestTheEventStreamIsPruned:
    def test_a_large_stream_is_rotated(self, tmp_path):
        root = tmp_path
        stream = root / headless_mod.PIPE_INSTANCE_ID / "owndata" / "events.jsonl"
        stream.parent.mkdir(parents=True)
        stream.write_bytes(b"x" * (17 * 1024 * 1024))
        assert headless_mod.rotate_pipe_events(root) is True
        assert not stream.exists()
        assert stream.with_suffix(".jsonl.1").stat().st_size == 17 * 1024 * 1024

    def test_a_small_stream_is_left_alone(self, tmp_path):
        stream = tmp_path / headless_mod.PIPE_INSTANCE_ID / "owndata" / "events.jsonl"
        stream.parent.mkdir(parents=True)
        stream.write_bytes(b"x" * 1024)
        assert headless_mod.rotate_pipe_events(tmp_path) is False
        assert stream.exists()

    def test_only_one_generation_is_kept(self, tmp_path):
        """Two generations would just move the unbounded growth one file over."""
        stream = tmp_path / headless_mod.PIPE_INSTANCE_ID / "owndata" / "events.jsonl"
        stream.parent.mkdir(parents=True)
        stream.write_bytes(b"a" * (17 * 1024 * 1024))
        headless_mod.rotate_pipe_events(tmp_path)
        stream.write_bytes(b"b" * (17 * 1024 * 1024))
        headless_mod.rotate_pipe_events(tmp_path)
        rotated = stream.with_suffix(".jsonl.1")
        assert rotated.stat().st_size == 17 * 1024 * 1024
        assert rotated.read_bytes()[:1] == b"b", "第二次轮转应替换第一次"

    def test_a_missing_stream_is_not_an_error(self, tmp_path):
        assert headless_mod.rotate_pipe_events(tmp_path) is False


class TestOneBotSendNames:
    def test_the_three_names_are_mapped(self):
        text = CJS.read_text(encoding="utf-8")
        for name in ("send_group_msg", "send_private_msg", "send_msg"):
            assert name in text, f"{name} 必须能调用"

    def test_the_conversion_runs_before_dispatch(self):
        """A name swap alone is not enough: OneBot passes `group_id` and `message`, not `chat` and `elements`."""
        text = CJS.read_text(encoding="utf-8")
        assert "normalizeOneBotSend(op, params)" in text
        assert "op = 'message.send'" in text

    def test_a_string_message_becomes_one_text_element(self):
        text = CJS.read_text(encoding="utf-8")
        assert "typeof message === 'string'" in text
        assert "type: 'text', data: { text: message }" in text

    def test_a_missing_target_is_rejected_clearly(self):
        text = CJS.read_text(encoding="utf-8")
        assert "requires ${chat.kind === 'group' ? 'group_id' : 'user_id'}" in text.replace('\\', '')
