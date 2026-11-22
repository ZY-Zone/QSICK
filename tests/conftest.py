"""Shared fixtures: a runtime on the mock transport, quiet logs, temp data dir."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from qsick import log as logmod
from qsick.config import RuntimeConfig
from qsick.runtime import Runtime

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def assume_pipe_process_alive(monkeypatch):
    """Treat any recorded pid as alive unless a test says otherwise.

    `Instance.to_dict` asks the OS whether a pipe session's process still exists, which is right in
    production -- the status file alone reported a killed session as `running=True`. But unit tests build a
    status file with a made-up pid and no process behind it, so the honest answer there is "dead" and every
    assertion about a running instance would fail.

    Making the probe a fixture rather than an assertion keeps the behaviour under test: a test that wants
    the dead-process path can ask for `dead_pipe_process`, and the default is the harmless assumption that
    a pid recorded in a fixture refers to something.
    """
    from qsick import headless as headless_module

    monkeypatch.setattr(headless_module, "_ALIVE_PROBE", lambda pid: True)
    yield
    monkeypatch.setattr(headless_module, "_ALIVE_PROBE", None)


@pytest.fixture()
def dead_pipe_process(monkeypatch):
    """The opposite: no process is alive, whatever pid is recorded.

    For tests that pin the pruning behaviour -- a session whose process was killed must not keep reporting
    itself as running.
    """
    from qsick import headless as headless_module

    monkeypatch.setattr(headless_module, "_ALIVE_PROBE", lambda pid: False)


@pytest.fixture(autouse=True)
def isolated_instance_template(tmp_path: Path, monkeypatch):
    """Keep every test away from the machine's real QQ client and the repo's template store.

    `create(kind="pipe")` builds a template from a real client and caches it in
    `instanceTemplates/pipe-<version>`, both of which are **process-global**: the template root is
    `repo_root()/instanceTemplates`, and the source client is whatever `_source_client()` finds (env var,
    `QQNT-*` beside the project, then Program Files).

    So a test that creates a pipe instance without isolating these does two unwanted things:

      * builds a template in the repository from the machine's client, and
      * stamps *that client's version* into it -- which the real application then reuses.

    Measured: running `test_first_run_creates_one_pipe_instance_but_does_not_autostart_it` with a client
    of version 9.9.99-00000 left `instanceTemplates/pipe-9.9.99-00000` behind. The same mechanism is how
    a machine whose QQ had updated to 9.9.37-54395 ended up with a `Pipe/versions/9.9.37-54395` client,
    while this project's DLL is built for 9.9.33-52230 -- an injection/version mismatch that crashed QQ
    in ntdll.

    Autouse rather than per-file because the files that already isolate it do so by hand, and any new
    test that forgets would silently reintroduce the leak. Tests that want a specific client still
    override `_source_client` themselves; their `monkeypatch` runs after this one and wins.
    """
    from qsick import instance_template

    # A synthetic client the fixture owns: enough of the shape for `build_template` to work, with a
    # version no real release will ever have, so a leak is obvious rather than plausible-looking.
    #
    # Pinned via the explicit override rather than by stubbing `runtime_root`, because `runtime/` is the
    # production source: a test that forgot the override would fall through to it and pick up the real
    # locked build, which is the machine-independence the isolation exists to guarantee.
    client = tmp_path / "fake-client"
    app = client / "versions" / "0.0.0-test" / "resources" / "app"
    app.mkdir(parents=True, exist_ok=True)
    (client / "QQ.exe").write_bytes(b"MZ fake")
    (app / "wrapper.node").write_bytes(b"fake wrapper")

    monkeypatch.setattr(instance_template, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(instance_template, "template_root", lambda: tmp_path / "instanceTemplates")
    monkeypatch.setattr(instance_template, "_source_client", lambda: client)
    # Belt and braces: if some path does reach for a runtime, it must find nothing to download either.
    monkeypatch.setenv("QSICK_QQ_SOURCE", str(client))
    yield


@pytest.fixture(autouse=True)
def quiet_logs():
    logmod.configure("TRACE", capacity=500, echo=False, file=None)
    yield
    logmod.configure("INFO", echo=False)


@pytest.fixture(autouse=True)
def network_activation_is_immediate(monkeypatch):
    """A logged-in account is normally served only after a settling delay.

    Waiting it out in every test would add fifteen seconds to each one and make the assertions racy, so the
    configured value is zeroed here. The delay itself is covered by `tests/test_network_activation_delay.py`,
    which sets its own value.
    """
    from qsick import runtime as runtime_module

    monkeypatch.setattr(runtime_module, "network_activation_delay", lambda config: 0.0)


@pytest.fixture(autouse=True)
def isolated_pipe_event_root(tmp_path: Path, monkeypatch):
    """Keep the pipe-event poller away from the developer's real `headlessClient/`.

    `RuntimeConfig.headless_root` defaults to `"headlessClient"`, which resolves against the process working
    directory -- the repository. A runtime started in a test therefore tailed the *real*
    `headlessClient/Pipe/owndata/events.jsonl`, and any session that had recently run on this machine fed
    messages into the test's runtime.

    Measured: `test_duplicate_message_events_are_dropped` asserts `events_seen == 3` and failed whenever a
    real session had run before the test, because the poller had added the real events on top.

    Only *relative* roots are redirected: a test that sets `config.headless_root` to its own tmp_path keeps
    its own value, which is what `test_pipe_event_ingest` relies on.
    """
    from qsick import runtime as runtime_module

    original = runtime_module._pipe_event_root

    def guarded(config) -> str:
        root = original(config)
        if not Path(root).is_absolute():
            return str(tmp_path / root)
        return root

    monkeypatch.setattr(runtime_module, "_pipe_event_root", guarded)
    yield


class FakeInjection:
    """Stand-in for `transport.inject.Injection`."""

    def __init__(self, pid: int = 4242, module_base: int = 0x1000) -> None:
        self.pid = pid
        self.hprocess = 0
        self.hthread = 0
        self.module_base = module_base
        self.pending_verification = False
        self.resumed = True


@pytest.fixture()
def fake_launcher(monkeypatch):
    """Replace the suspended launch, and record what was asked for.

    `_spawn_and_inject` goes through `transport.inject.launch_and_inject` -- `CreateProcessW` with
    `CREATE_SUSPENDED`, then inject, then resume. That replaced `subprocess.Popen` plus a freeze because it is
    the only order that catches QQ's napi burst before the DLL's import patches land; the older order left the
    injected script with no JS environment.

    Tests cannot really launch, so this records instead. Each entry holds the exe, the rendered command-line
    argument string, and the `before_resume` callback -- calling it is what lets a test observe the Job Object
    being bound while the process is still suspended.
    """

    calls: list[dict] = []

    def fake_launch(exe, dll, arguments="", workdir=None, extra_env=None,
                    resume_after=True, before_resume=None):
        calls.append({
            "exe": str(exe),
            "dll": str(dll),
            "arguments": arguments,
            "workdir": str(workdir) if workdir else "",
            "before_resume": before_resume,
        })
        injection = FakeInjection()
        if before_resume is not None:
            before_resume(injection)
        return injection

    monkeypatch.setattr("qsick.transport.inject.launch_and_inject", fake_launch)
    monkeypatch.setattr("qsick.transport.inject.find_module", lambda *a, **k: None)
    return calls


@pytest.fixture()
def config(tmp_path: Path) -> RuntimeConfig:
    cfg = RuntimeConfig(data_dir=str(tmp_path / "data"))
    cfg.transport.mode = "mock"
    cfg.plugins.dirs = [str(ROOT / "plugins")]
    cfg.forward_noise = False
    cfg.log.level = "TRACE"
    return cfg


@pytest.fixture()
async def runtime(config: RuntimeConfig):
    rt = Runtime(config)
    await rt.start(serve=False)
    try:
        yield rt
    finally:
        await rt.stop()


async def wait_for(predicate, timeout: float = 3.0, interval: float = 0.02):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return False
