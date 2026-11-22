"""How a pipe session's command line is built.

`_spawn_and_inject` no longer launches through `subprocess.Popen`. It goes through
`transport.inject.launch_and_inject`, which uses `CreateProcessW` with `CREATE_SUSPENDED`, injects, binds the
Job Object, then resumes. That order is the only one that catches QQ's napi burst before the DLL's import
patches land -- the older "start, freeze, inject" sequence left the injected script with no JS environment, so
every action timed out.

One consequence is that arguments now travel as a single command-line string instead of a list, and
`launch_suspended` splices that string in verbatim. So the quoting that `Popen` used to do has to happen here.
These tests pin exactly that.

Regression this guards: an earlier version built a command string and re-split it, turning
`--user-data-dir="C:\\my path\\ud"` into `['--user-data-dir="C:\\my', 'path\\ud']`. QQ then started with a
truncated data directory, which is indistinguishable from "the instance has no login".
"""
from __future__ import annotations

from pathlib import Path

from qsick import headless


def test_a_spaced_path_stays_one_argument():
    """A user-data path containing spaces must be quoted, not split."""
    path = r"D:\downloads\qq\Tencent Files\ud"
    rendered = headless._command_line_arguments(["--no-sandbox", f"--user-data-dir={path}"])

    assert rendered == f'--no-sandbox --user-data-dir="{path}"'


def test_an_unspaced_argument_is_left_alone():
    """Quoting something that does not need it would be passed through to QQ verbatim."""
    rendered = headless._command_line_arguments(["--no-sandbox", "--user-data-dir=D:\\x\\ud"])
    assert rendered == "--no-sandbox --user-data-dir=D:\\x\\ud"


def test_non_ascii_paths_are_not_altered():
    """The command line is UTF-16 all the way to CreateProcessW; no escaping is needed or wanted."""
    rendered = headless._command_line_arguments(["--user-data-dir=D:\\号主\\数据 目录"])
    assert rendered == '--user-data-dir="D:\\号主\\数据 目录"'


def test_each_argument_stays_separate():
    """Three arguments in, three arguments out -- quote-aware, not a re-split."""
    rendered = headless._command_line_arguments(
        ["--no-sandbox", "--flag=two words", "--unicode=中文 路径"])
    assert rendered == '--no-sandbox --flag="two words" --unicode="中文 路径"'


def test_the_job_is_bound_before_the_process_is_resumed(tmp_path: Path, fake_launcher, monkeypatch):
    """Binding after resume would leave QQ's renderers outside the job.

    QQ spawns them within milliseconds of starting, and a child inherits the job at creation. Bind late and
    closing QSICK kills only the main process -- which is the exact symptom the job exists to prevent.

    The fixture calls `before_resume` the way `launch_and_inject` does, so what this asserts is that
    `_spawn_and_inject` actually supplies one.
    """
    bound: list[int] = []

    class FakeJob:
        active = True
        error = ""

        def assign(self, pid):
            bound.append(pid)
            return True

    monkeypatch.setattr("qsick.job_guard.acquire", lambda key: FakeJob())

    headless._spawn_and_inject(
        tmp_path / "QQ.exe", tmp_path / "qsick_pipe.dll",
        ["--user-data-dir=C:\\ud"], tmp_path, "Pipe")

    assert fake_launcher, "没有发起启动"
    assert fake_launcher[0]["before_resume"] is not None, "没有在恢复前挂 Job"
    assert bound == [4242], "Job 没有拿到被注入进程的 pid"


def test_the_executable_goes_through_untouched(tmp_path: Path, fake_launcher, monkeypatch):
    """QQ derives its install root, versions directory and data root from its own path.

    A relative or rewritten path makes it compute a set of wrong locations; an earlier bug had the command
    line carry `headlessClient\\C1\\QQ.exe`.
    """
    class FakeJob:
        active = False
        error = "no job"

    monkeypatch.setattr("qsick.job_guard.acquire", lambda key: FakeJob())

    exe = tmp_path / "a dir with spaces" / "QQ.exe"
    headless._spawn_and_inject(exe, tmp_path / "p.dll", ["--x=1"], tmp_path, "Pipe")

    assert fake_launcher[0]["exe"] == str(exe)
