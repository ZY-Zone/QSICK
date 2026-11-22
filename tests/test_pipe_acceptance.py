"""Acceptance tests for the pipe DLL's end-to-end behaviour.

These are the properties that must hold for a healthy pipe instance, and they are the
ones worth regressing on. They need a real QQ instance, so they are marked ``pipe_e2e``
and skipped by default; run with ``-m pipe_e2e`` when a client copy is available.

The properties, and why each one earns a test:

  * the DLL is present and its marker is set -- proves injection, not merely launch
  * the data root is redirected into the instance -- the whole point of the change
  * the main window handle is 0 -- proves suppression, which is what makes it headless
  * wrapper.node and major.node are loaded -- proves Electron still runs, which is the
    architectural decision (suppress windows, keep the main process so wrapper.node
    loads through the official path)
  * code can run inside the process -- the capability the task queue exists for
  * the client's own QQNT.dll is byte-identical to a pristine copy -- the "do not touch
    QQ's files" requirement, checked rather than asserted
"""
from __future__ import annotations

import hashlib
import subprocess
import sys
import time
from pathlib import Path

import pytest

from qsick.headless import HeadlessManager
from qsick.transport import inject

ROOT = Path(__file__).resolve().parents[1]
DLL = ROOT / "native" / "qsick-pipe" / "qsick_pipe.dll"
MANAGER_ROOT = ROOT / "headlessClient"
# The pipe instance's directory is the fixed name "Pipe", not C<n>.
INSTANCE_ID = "Pipe"
INSTANCE = MANAGER_ROOT / INSTANCE_ID
OWNDATA = INSTANCE / "owndata"

pytestmark = [
    pytest.mark.pipe_e2e,
    pytest.mark.skipif(not (MANAGER_ROOT / INSTANCE_ID).is_dir(),
                       reason="需要真实实例目录（headlessClient/Pipe）"),
]

PS = ["powershell", "-NoProfile", "-Command"]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


@pytest.fixture(scope="module")
def running():
    """Starts one instance for the whole module and stops it afterwards."""
    for stray in _qq_pids():
        _kill(stray)

    manager = HeadlessManager(MANAGER_ROOT)
    api = manager.startup_api(INSTANCE_ID)
    api.add("process.version", "node")
    api.add("global.__qsick.exports().length", "exports")
    api.write()

    pid = int(manager.start(INSTANCE_ID)["pid"])
    try:
        # The startup window closes a couple of seconds in; nine is comfortably past it.
        time.sleep(9)
        handle = None
        for _ in range(30):
            handle = inject.open_process(pid)
            if handle:
                break
            time.sleep(0.1)
        yield {"pid": pid, "handle": handle, "results": api.batch.results()}
    finally:
        manager.stop(INSTANCE_ID)


def _qq_pids() -> list[int]:
    out = subprocess.run(
        PS + ["Get-CimInstance Win32_Process -Filter \"Name='QQ.exe'\" | "
              "ForEach-Object { $_.ProcessId }"],
        capture_output=True, text=True, timeout=40).stdout
    return [int(x) for x in out.split() if x.strip().isdigit()]


def _kill(pid: int) -> None:
    subprocess.run(PS + [f"Stop-Process -Id {pid} -Force -ErrorAction SilentlyContinue"],
                   capture_output=True, timeout=30)


def test_dll_is_injected(running):
    assert running["handle"], "无法打开进程句柄"
    found = inject.find_module(running["handle"], "qsick_pipe.dll")
    assert found, "qsick_pipe.dll 不在模块列表里"
    assert found[0] > 0x10000, "模块基址异常"


def test_magic_marker_is_set(running):
    rva = inject.export_rva(DLL, "g_qsickMagic")
    assert rva is not None
    magic = inject.read_u32(running["handle"],
                            inject.find_module(running["handle"],
                                               "qsick_pipe.dll")[0] + rva)
    assert magic == 0xC0DE0001


def test_data_root_is_redirected(running):
    """Runtime artefacts must land in the instance's owndata, not QQ's install."""
    assert (OWNDATA / "napi_probe.json").is_file(), \
        f"{OWNDATA} 下没有 napi_probe.json，数据目录重定向可能失效"


def test_dll_and_config_are_beside_the_instance(running):
    data = INSTANCE / "data"
    for name in ("qsick_pipe.dll", "qsick_pipe.json"):
        assert (data / name).is_file(), f"{data / name} 不存在"


def test_windows_are_hidden(running):
    out = subprocess.run(
        PS + [f"(Get-Process -Id {running['pid']}).MainWindowHandle"],
        capture_output=True, text=True, timeout=30).stdout.strip()
    assert out in ("0", ""), f"主窗口仍在：MainWindowHandle={out}"


def test_electron_still_loads_wrapper(running):
    """The architectural bet: suppress windows, keep the main process.

    If wrapper.node is missing, that bet failed and the whole API path is gone -- so
    this is worth asserting explicitly rather than inferring from another check.
    """
    for module in ("wrapper.node", "major.node"):
        assert inject.find_module(running["handle"], module), f"{module} 未加载"


def test_code_runs_inside_the_process(running):
    rows = {r.id: r for r in running["results"]}
    assert "node" in rows, "任务没有产生结果"
    assert rows["node"].ok, rows["node"].error
    assert rows["node"].value.startswith("v"), rows["node"].value


def test_wrapper_exports_are_all_visible(running):
    rows = {r.id: r for r in running["results"]}
    assert "exports" in rows and rows["exports"].ok
    assert rows["exports"].value == "98", \
        f"wrapper.node 导出数变了：{rows['exports'].value}（预期 98）"


def test_client_files_are_untouched(running):
    """Proves the "no changes to QQ's files" requirement instead of trusting it."""
    pristine = ROOT / "runtime" / "versions" / "9.9.33-52230" / "QQNT.dll"
    used = INSTANCE / "versions" / "9.9.33-52230" / "QQNT.dll"
    if not pristine.is_file() or not used.is_file():
        pytest.skip("缺少对照用的 QQNT.dll")
    assert _sha256(pristine) == _sha256(used), "实例里的 QQNT.dll 与原始副本不一致"
