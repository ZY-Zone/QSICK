"""Isolation between pipe sessions.

Pipe used to be a singleton, on the reasoning that every instance shared the system-configured data root
(reports/06) and two of them would fight over one login state. That is no longer true: the injected DLL
rewrites QQ's data directory to the instance's own `owndata`, and reports/11 shows QQ separating accounts
by session_id under that root. Multiple sessions now coexist.

What these tests protect did not change with the rule -- it got more important:

  * each session owns its own directory, its own DLL copy and its own control port
  * starting one session does not rewrite another session's config
"""

from pathlib import Path

import pytest

from qsick.headless import HeadlessManager
from qsick import headless as headless_module
import qsick.instance_template as instance_template
import json

PIPE_DLL_NAME = "qsick_pipe.dll"
PIPE_DLL_CONFIG = "qsick_pipe.json"


def _spy(monkeypatch) -> list[list[str]]:
    """Record each launch as [exe, command-line arguments].

    The launch path is 	ransport.inject.launch_and_inject now -- CREATE_SUSPENDED, inject, resume -- because
    that order is the only one that catches QQ's napi burst before the DLL patches land.
    """
    calls: list[list[str]] = []

    class FakeInjection:
        pid = 1234
        hprocess = 0
        hthread = 0
        module_base = 0x1000
        pending_verification = False
        resumed = True

    def fake_launch(exe, dll, arguments="", workdir=None, extra_env=None,
                    resume_after=True, before_resume=None):
        calls.append([str(exe), arguments])
        injection = FakeInjection()
        if before_resume is not None:
            before_resume(injection)
        return injection

    monkeypatch.setattr("qsick.transport.inject.launch_and_inject", fake_launch)
    monkeypatch.setattr("qsick.transport.inject.find_module", lambda *a, **k: None)
    return calls


@pytest.fixture
def manager(tmp_path: Path, monkeypatch):
    """A minimal client tree so a pipe instance can be created."""
    client = tmp_path / "client"
    version = "9.9.33-52230"
    app = client / "versions" / version / "resources" / "app"
    app.mkdir(parents=True)
    (client / "QQ.exe").write_bytes(b"MZ")
    (app / "package.json").write_text(
        json.dumps({"name": "qq", "version": version}), encoding="utf-8")
    (client / "versions" / "config.json").write_text(
        json.dumps({"curVersion": version}), encoding="utf-8")

    fake_dll = tmp_path / "repo_dll" / PIPE_DLL_NAME
    fake_dll.parent.mkdir(parents=True)
    fake_dll.write_bytes(b"fake dll payload")

    monkeypatch.setattr(instance_template, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(instance_template, "template_root",
                        lambda: tmp_path / "instanceTemplates")
    monkeypatch.setattr(instance_template, "_source_client", lambda: client)
    monkeypatch.setattr("qsick.headless.pipe_dll", lambda: fake_dll)
    monkeypatch.setenv("QSICK_QQ_DATA_ROOT", str(tmp_path / "qqdata"))
    return HeadlessManager(tmp_path / "headlessClient")


def test_pipe_instance_has_its_own_dll_and_config(manager, monkeypatch):
    """The DLL and its config live per instance, not in a shared location.

    Why it matters: the config carries owndata, so a shared config would point two instances
    at one directory. With pipe now a singleton this cannot arise between two pipe instances,
    but the rule still holds for the pair that can coexist.
    """
    _spy(monkeypatch)
    pipe = manager.create(name="管道", kind="pipe")
    manager.start(pipe["id"])

    # Each launch gets its own throwaway directory under Pipe/dll/<tmp_id>/, holding the DLL copy and
    # the config that sits next to it. A shared directory would let two launches overwrite each other's
    # port, and the losing session would be unreachable while still looking started.
    scratch = manager.session_dll_dir()
    session_dirs = [item for item in scratch.iterdir() if item.is_dir()]
    assert session_dirs, f"{scratch} 下应当有本次启动的临时目录"
    latest = max(session_dirs, key=lambda item: item.stat().st_mtime)

    dll = latest / PIPE_DLL_NAME
    assert dll.is_file(), f"临时目录 {latest.name} 应当有一份 DLL 副本"

    config = json.loads((latest / PIPE_DLL_CONFIG).read_text(encoding="utf-8"))
    # The new shape nests the per-launch identity under "session"; the flat keys stay for the DLL that
    # is already deployed and reads them.
    assert config["session"]["tmp_id"] == latest.name
    assert int(config["session"]["port"]) > 0
    assert config["port"] == config["session"]["port"]


def test_starting_one_instance_does_not_rewrite_another_config(manager, monkeypatch):
    """A later launch must not disturb an earlier launch's DLL config.

    The original race: reading the config at start time and writing it back meant whichever instance
    started last won, and the earlier one silently pointed at the wrong port. Per-launch directories
    remove the possibility rather than scheduling around it, which is what this pins.
    """
    _spy(monkeypatch)
    pipe = manager.create(name="管道", kind="pipe")
    headless_instance = manager.create(name="无头", kind="headless")

    manager.start(pipe["id"])
    scratch = manager.session_dll_dir()
    first = [item for item in scratch.iterdir() if item.is_dir()]
    assert first
    first_dir = first[0]
    before = json.loads((first_dir / PIPE_DLL_CONFIG).read_text(encoding="utf-8"))

    manager.start(headless_instance["id"])

    after = json.loads((first_dir / PIPE_DLL_CONFIG).read_text(encoding="utf-8"))
    assert after == before, "启动另一个实例改动了 pipe 的 DLL 配置"


def test_second_pipe_client_is_refused(manager):
    """Only one client copy may exist.

    Sessions are repeated launches of `Pipe/` (`start_session`), not copies of it. A second copy would cost
    ~1.3 GB and split the session list across directories, so `create(kind="pipe")` must refuse while one
    is present.
    """
    manager.create(name="管道", kind="pipe")

    with pytest.raises(ValueError) as caught:
        manager.create(name="再来一个", kind="pipe")

    message = str(caught.value)
    assert "只能有一份" in message
    # The message has to say what to do instead, or the operator is stuck.
    assert "session" in message

    pipe_dirs = [item.name for item in manager.root.iterdir()
                 if item.is_dir() and item.name.startswith("Pipe")]
    assert pipe_dirs == ["Pipe"], f"多出了客户端副本: {pipe_dirs}"


def test_sessions_are_separate_directories_with_separate_ports(manager):
    """Two launches get two directories and two ports under the one client.

    What has to hold for multiple sessions to work at all: distinct scratch directories (so one launch's
    config cannot be overwritten by the next) and distinct ports (or the second DLL cannot bind and that
    session is unreachable while still looking started).
    """
    manager.create(name="管道", kind="pipe")
    scratch = manager.session_dll_dir()

    def make_session(port: int) -> str:
        tmp_id = headless_module.new_session_tmp_id()
        entry = scratch / tmp_id
        entry.mkdir(parents=True)
        (entry / PIPE_DLL_NAME).write_bytes(b"MZ")
        (entry / PIPE_DLL_CONFIG).write_text(
            json.dumps({"session": {"tmp_id": tmp_id, "port": port}, "port": port}),
            encoding="utf-8")
        return tmp_id

    first_id = make_session(7540)
    second_id = make_session(7541)

    assert first_id != second_id
    sessions = {item["tmp_id"]: item for item in headless_module.read_session_dirs(manager.root)}
    assert set(sessions) == {first_id, second_id}
    assert sessions[first_id]["port"] != sessions[second_id]["port"]
    for item in sessions.values():
        assert headless_module.PIPE_PORT_BASE <= int(item["port"]) <= headless_module.PIPE_PORT_MAX


def test_closing_a_session_removes_its_directory(manager):
    """The close button must take the row with it.

    Killing the process is not enough: the directory is what the list is read from, so leaving it would
    show a session that can never be reached again.
    """
    manager.create(name="管道", kind="pipe")
    scratch = manager.session_dll_dir()
    tmp_id = headless_module.new_session_tmp_id()
    entry = scratch / tmp_id
    entry.mkdir(parents=True)
    (entry / PIPE_DLL_CONFIG).write_text(
        json.dumps({"session": {"tmp_id": tmp_id, "port": 0}}), encoding="utf-8")

    assert headless_module.close_session(manager.root, tmp_id) is True

    assert not entry.exists()
    assert headless_module.read_session_dirs(manager.root) == []


def test_closing_an_unknown_session_reports_nothing_done(manager):
    manager.create(name="管道", kind="pipe")
    assert headless_module.close_session(manager.root, "0" * 16) is False
