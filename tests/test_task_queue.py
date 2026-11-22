"""Task queue and StartupApi behaviour.

The mechanism these cover is a workaround for a measured limit: QQ's JS main thread
stops executing napi calls a couple of seconds after injection, so work has to be
queued before the instance starts rather than requested afterwards. The tests here fix
down the file protocol and the error handling, not the timing of QQ itself (which no
unit test can stand in for).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from qsick.transport import tasks
from qsick.transport.kernel_api import StartupApi


# -- TaskBatch protocol ----------------------------------------------------

def test_write_creates_the_queue_file(tmp_path: Path):
    batch = tasks.TaskBatch(tmp_path)
    batch.add("1+1")
    path = batch.write()

    assert path == tmp_path / tasks.TASKS_FILE
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["code"] == "1+1"
    assert rows[0]["id"]


def test_write_clears_stale_results(tmp_path: Path):
    """A previous run's answers must not be mistaken for this run's."""
    stale = tmp_path / tasks.RESULTS_FILE
    stale.write_text('{"id":"old","ok":true,"result":"1"}\n', encoding="utf-8")

    tasks.TaskBatch(tmp_path).write()

    assert not stale.is_file()


def test_task_ids_are_unique_and_sequential(tmp_path: Path):
    batch = tasks.TaskBatch(tmp_path)
    ids = [batch.add("x") for _ in range(3)]
    assert ids == ["t1", "t2", "t3"]


def test_explicit_task_id_is_kept(tmp_path: Path):
    batch = tasks.TaskBatch(tmp_path)
    assert batch.add("x", "mine") == "mine"


def test_results_parse_ok_and_error(tmp_path: Path):
    (tmp_path / tasks.RESULTS_FILE).write_text(
        '{"id":"a","ok":true,"result":"2"}\n'
        '{"id":"b","ok":false,"error":"boom"}\n',
        encoding="utf-8")

    got = tasks.TaskBatch(tmp_path).results()

    assert [r.id for r in got] == ["a", "b"]
    assert got[0].ok and got[0].value == "2"
    assert not got[1].ok and got[1].error == "boom"


def test_results_skip_malformed_lines(tmp_path: Path):
    """A truncated final line is normal when the window dies mid-write."""
    (tmp_path / tasks.RESULTS_FILE).write_text(
        '{"id":"a","ok":true,"result":"1"}\n{"id":"b","ok":tr\n',
        encoding="utf-8")

    assert [r.id for r in tasks.TaskBatch(tmp_path).results()] == ["a"]


def test_missing_results_file_is_empty_not_an_error(tmp_path: Path):
    assert tasks.TaskBatch(tmp_path).results() == []


def test_wait_returns_when_all_ids_arrive(tmp_path: Path):
    batch = tasks.TaskBatch(tmp_path)
    batch.add("a", "t1")
    batch.add("b", "t2")
    batch.write()
    (tmp_path / tasks.RESULTS_FILE).write_text(
        '{"id":"t1","ok":true,"result":"1"}\n{"id":"t2","ok":true,"result":"2"}\n',
        encoding="utf-8")

    got = batch.wait(timeout=0.1, interval=0.01)
    assert {r.id for r in got} == {"t1", "t2"}


def test_wait_raises_when_nothing_ran(tmp_path: Path, monkeypatch):
    """No results at all means the window was missed -- not that a task failed."""
    monkeypatch.setattr(tasks.time, "sleep", lambda s: None)
    batch = tasks.TaskBatch(tmp_path)
    batch.add("1+1")

    with pytest.raises(tasks.TaskWindowMissed):
        batch.wait(timeout=0.05, interval=0)


def test_wait_tolerates_partial_results(tmp_path: Path, monkeypatch):
    """Some answers with none missing is success; partial is not an exception."""
    monkeypatch.setattr(tasks.time, "sleep", lambda s: None)
    batch = tasks.TaskBatch(tmp_path)
    batch.add("a", "t1")
    batch.add("b", "t2")
    (tmp_path / tasks.RESULTS_FILE).write_text(
        '{"id":"t1","ok":true,"result":"1"}\n', encoding="utf-8")

    got = batch.wait(timeout=0.05, interval=0)
    assert [r.id for r in got] == ["t1"]


# -- StartupApi ------------------------------------------------------------

def test_stup_api_queues_before_start(tmp_path: Path):
    api = StartupApi(tmp_path)
    api.add("code here", "x")
    api.write()

    rows = (tmp_path / "owndata" / tasks.TASKS_FILE).read_text(
        encoding="utf-8").splitlines()
    assert json.loads(rows[0])["code"] == "code here"


def test_add_methods_uses_the_full_class_name(tmp_path: Path):
    api = StartupApi(tmp_path)
    api.add_methods("msg", "m1")
    api.write()

    code = json.loads(
        (tmp_path / "owndata" / tasks.TASKS_FILE).read_text(
            encoding="utf-8").splitlines()[0])["code"]
    assert "NodeIKernelMsgService" in code
    assert "__qsick.methods" in code


def test_add_methods_passes_through_a_full_name(tmp_path: Path):
    api = StartupApi(tmp_path)
    api.add_methods("NodeIKernelGroupService", "m2")
    api.write()

    code = json.loads(
        (tmp_path / "owndata" / tasks.TASKS_FILE).read_text(
            encoding="utf-8").splitlines()[0])["code"]
    assert "NodeIKernelGroupService" in code


def test_add_exports_asks_for_the_key_list(tmp_path: Path):
    api = StartupApi(tmp_path)
    api.add_exports("e1")
    api.write()

    code = json.loads(
        (tmp_path / "owndata" / tasks.TASKS_FILE).read_text(
            encoding="utf-8").splitlines()[0])["code"]
    assert "__qsick.exports()" in code


# -- method_names error semantics ------------------------------------------

def test_method_names_raises_when_the_window_passed(tmp_path: Path, monkeypatch):
    """An empty list would read as "this service has no methods" -- a wrong answer."""
    from qsick.transport.kernel_api import KernelError

    api = StartupApi(tmp_path)
    api.add_methods("msg", "methods_msg")
    api.write()

    def fake_wait(timeout=0):
        raise tasks.TaskWindowMissed("window gone")

    monkeypatch.setattr(api.batch, "wait", fake_wait)

    with pytest.raises(KernelError, match="启动窗口"):
        api.method_names("msg")


def test_method_names_raises_on_a_failed_task(tmp_path: Path, monkeypatch):
    from qsick.transport.kernel_api import KernelError

    api = StartupApi(tmp_path)
    api.add_methods("msg", "methods_msg")
    api.write()

    monkeypatch.setattr(
        api.batch, "wait",
        lambda timeout=0: [tasks.TaskResult(id="methods_msg", ok=False,
                                            error="boom")])

    with pytest.raises(KernelError, match="boom"):
        api.method_names("msg")


def test_method_names_returns_the_parsed_list(tmp_path: Path, monkeypatch):
    api = StartupApi(tmp_path)
    api.add_methods("msg", "methods_msg")
    api.write()

    monkeypatch.setattr(
        api.batch, "wait",
        lambda timeout=0: [tasks.TaskResult(id="methods_msg", ok=True,
                                            value='["sendMsg","getMsg"]')])

    assert api.method_names("msg") == ["sendMsg", "getMsg"]


def test_method_names_writes_the_queue_if_not_yet_written(tmp_path: Path,
                                                          monkeypatch):
    """Someone who forgot write() should still get the expected behaviour."""
    api = StartupApi(tmp_path)
    monkeypatch.setattr(
        api.batch, "wait",
        lambda timeout=0: [tasks.TaskResult(id="methods_msg", ok=True,
                                            value="[]")])

    api.method_names("msg")

    assert (tmp_path / "owndata" / tasks.TASKS_FILE).is_file()
