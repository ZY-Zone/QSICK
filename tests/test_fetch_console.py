"""The dedicated download window.

The requirement is that the QQ-client download gets its own console window, and that this holds for
both launch shapes: a source run (`python`, console subsystem) and the frozen exe (`--noconsole`,
GUI subsystem, where `sys.stdout` is ``None``).

Nothing here creates a real window during the test run -- `spawn` is checked against a stubbed
`Popen`, and the console-attachment logic is checked by its decision rules rather than by opening a
console in the test process, which would be visible and would detach pytest's own output.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for entry in (str(ROOT), str(ROOT / "src")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from qsick import fetch_console  # noqa: E402


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv(fetch_console.DISABLE_ENV, raising=False)
    monkeypatch.delenv(fetch_console.WORKER_ENV, raising=False)


class FakeProcess:
    pid = 4321

    def __init__(self, code: int = 0) -> None:
        self.code = code
        self.waited = False

    def wait(self, timeout=None):  # noqa: ARG002
        self.waited = True
        return self.code


def test_a_source_run_uses_the_module_form(monkeypatch):
    monkeypatch.setattr(fetch_console.sys, "frozen", False, raising=False)
    command = fetch_console.worker_command(Path("/tmp/rt"))
    assert command[0] == sys.executable
    assert command[1:3] == ["-m", "qsick.fetch_worker"], "源码模式要走模块入口"
    assert command[-1].endswith("rt")


def test_a_frozen_run_re_uses_the_exe(monkeypatch):
    """The exe has no importable package path of its own, so it must re-exec itself."""
    monkeypatch.setattr(fetch_console.sys, "frozen", True, raising=False)
    command = fetch_console.worker_command(Path("/tmp/rt"))
    assert command[0] == sys.executable
    assert command[1] == "--fetch-worker", "冻结版靠这个开关回到 launcher 的 worker 分支"
    assert command[2].endswith("rt")


def test_the_child_never_spawns_a_grandchild(monkeypatch):
    """Without this guard, `--fetch-worker` typed by hand would fork windows forever."""
    monkeypatch.setenv(fetch_console.WORKER_ENV, "1")
    monkeypatch.setattr(fetch_console.subprocess, "Popen", _explode)
    assert fetch_console.spawn(Path("/tmp/rt")) is None


def test_the_switch_that_silences_the_window_is_honoured(monkeypatch):
    """`获取QQnt.bat` already has a console; it sets this so the progress stays in its window."""
    monkeypatch.setenv(fetch_console.DISABLE_ENV, "1")
    monkeypatch.setattr(fetch_console.subprocess, "Popen", _explode)
    assert fetch_console.spawn(Path("/tmp/rt")) is None


def test_spawn_uses_a_new_console_and_passes_the_worker_flag(monkeypatch):
    seen = {}

    def fake_popen(command, **kwargs):
        seen["command"] = command
        seen["flags"] = kwargs.get("creationflags")
        seen["env"] = kwargs.get("env")
        return FakeProcess()

    monkeypatch.setattr(fetch_console.subprocess, "Popen", fake_popen)
    process = fetch_console.spawn(Path("/tmp/rt"))
    assert isinstance(process, FakeProcess)
    assert seen["flags"] == getattr(fetch_console.subprocess, "CREATE_NEW_CONSOLE", 0)
    assert seen["flags"], "必须带 CREATE_NEW_CONSOLE，否则没有独立窗口"
    assert seen["env"][fetch_console.WORKER_ENV] == "1"
    assert "--fetch-worker" in seen["command"] or "-m" in seen["command"]


def test_a_spawn_failure_is_not_fatal(monkeypatch):
    """No window is a downgrade, not an outage: the caller then downloads in-process."""
    monkeypatch.setattr(fetch_console.subprocess, "Popen", _explode)
    assert fetch_console.spawn(Path("/tmp/rt")) is None


def test_the_worker_writes_to_conout_when_stdout_is_unwired(monkeypatch):
    """The frozen build is GUI-subsystem: `sys.stdout` is None there, so `print` is a no-op.

    This is the rule that makes the window show anything at all, so it is asserted directly rather
    than inferred from a successful download.
    """
    monkeypatch.setattr(fetch_console.sys, "stdout", None)
    assert fetch_console._stdout_usable() is False


def test_output_is_left_alone_when_it_already_works(monkeypatch, tmp_path):
    """Source runs already have a working stdout; replacing it would bypass redirection.

    A real file handle stands in for the console: the rule under test is "has a fileno and accepts
    a write", not "is a terminal". `capsys` cannot be used here because it swaps in an object whose
    `fileno()` raises, which the function correctly classifies as unusable.
    """
    handle = (tmp_path / "out.txt").open("w", encoding="utf-8")
    try:
        monkeypatch.setattr(fetch_console.sys, "stdout", handle)
        assert fetch_console._stdout_usable() is True
    finally:
        handle.close()


def test_an_object_without_a_fileno_is_treated_as_unwired(monkeypatch):
    import io

    monkeypatch.setattr(fetch_console.sys, "stdout", io.StringIO())
    assert fetch_console._stdout_usable() is False


def test_a_log_file_is_usable_but_is_not_a_console(monkeypatch, tmp_path):
    """The exact condition that left the frozen worker's window blank.

    `packaging/launcher.py` points `sys.stdout` at `logs/console.log`. That stream is writable and
    has a fileno, so a "can I print?" check says yes -- and the first version therefore concluded
    there was nothing to do, while the console window it had been given stayed empty. The file case
    must be distinguished so output is teed to ``CONOUT$`` as well.
    """
    handle = (tmp_path / "console.log").open("w", encoding="utf-8")
    try:
        monkeypatch.setattr(fetch_console.sys, "stdout", handle)
        assert fetch_console._stdout_usable() is True
        assert fetch_console._stdout_is_console() is False
    finally:
        handle.close()


def test_the_tee_writes_to_both_sinks_and_survives_a_dead_one():
    import io

    log = io.StringIO()
    window = io.StringIO()
    tee = fetch_console._Tee(window, log)
    tee.write("hello")
    tee.flush()
    assert window.getvalue() == "hello"
    assert log.getvalue() == "hello"

    class Dead:
        def write(self, text):
            raise OSError("gone")

        def flush(self):
            raise OSError("gone")

    # One broken sink must not lose the other: the window closing is a normal event, not a reason
    # to stop logging.
    tee = fetch_console._Tee(Dead(), log)
    tee.write("still")
    assert "still" in log.getvalue()


def test_a_pinned_source_is_carried_into_the_child(monkeypatch):
    """The "server only" launcher must not reopen the GitHub route in the child.

    `ensure_runtime(console=True, base_url=...)` spawns a worker; without forwarding the address the
    child would re-derive the default order and try GitHub first, which is exactly what the operator
    ruled out by using that launcher.
    """
    monkeypatch.setattr(fetch_console.sys, "frozen", False, raising=False)
    command = fetch_console.worker_command(Path("/tmp/rt"), "http://server.example/x")
    assert "--base" in command
    assert command[command.index("--base") + 1] == "http://server.example/x"


def test_no_base_flag_when_no_source_is_pinned(monkeypatch):
    monkeypatch.setattr(fetch_console.sys, "frozen", False, raising=False)
    command = fetch_console.worker_command(Path("/tmp/rt"))
    assert "--base" not in command


def test_the_build_ships_a_server_only_launcher():
    """`build.ps1` must generate the bat into the output root, next to the exe.

    Asserted on the generator rather than on a build artefact: the artefact only exists after a
    build, while the rule ("this script is generated, and it calls --server") holds in the source.
    """
    script = (ROOT / "build.ps1").read_text(encoding="utf-8-sig")
    assert "仅服务器源下载QQnt.bat" in script
    assert "--fetch-runtime --server" in script


def _explode(*args, **kwargs):  # pragma: no cover - only used to prove a path is not taken
    raise AssertionError("这里不该派生进程")
