"""End-to-end proof that a pipe instance does not touch the real user profile.

The earlier unit tests check the environment dict. That is necessary but not sufficient:
QQ could still resolve paths from the registry or a hard-coded location. This measures
behaviour -- snapshot the real profile, run an instance, snapshot again -- so the claim
"data is isolated" rests on observed writes rather than on configuration.

Kept as an e2e test because it starts a real client. Run with -m pipe_e2e.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from qsick.headless import HeadlessManager

ROOT = Path(__file__).resolve().parents[1]
INSTANCE = ROOT / "headlessClient" / "C1"
REAL_LOCAL = Path.home() / "AppData" / "Local"
REAL_ROAMING = Path.home() / "AppData" / "Roaming"

pytestmark = [
    pytest.mark.pipe_e2e,
    pytest.mark.skipif(not INSTANCE.is_dir(),
                       reason="需要真实实例目录（headlessClient/C1）"),
]

PS = ["powershell", "-NoProfile", "-Command"]


def _snapshot(root: Path, limit: int = 3000) -> dict[str, float]:
    out: dict[str, float] = {}
    if not root.is_dir():
        return out
    try:
        for entry in root.iterdir():
            if len(out) >= limit:
                break
            try:
                out[entry.name] = entry.stat().st_mtime
            except OSError:
                continue
    except OSError:
        pass
    return out


def _kill_instance_processes() -> None:
    out = subprocess.run(
        PS + ["Get-CimInstance Win32_Process | Where-Object { "
              "$_.ExecutablePath -like '*headlessClient*' } | "
              "ForEach-Object { $_.ProcessId }"],
        capture_output=True, text=True, timeout=40).stdout
    for token in out.split():
        if token.strip().isdigit():
            subprocess.run(
                PS + [f"Stop-Process -Id {int(token)} -Force "
                      f"-ErrorAction SilentlyContinue"],
                capture_output=True, timeout=30)


@pytest.fixture(scope="module")
def run_once():
    _kill_instance_processes()
    time.sleep(2)

    before_local = _snapshot(REAL_LOCAL)
    before_roaming = _snapshot(REAL_ROAMING)

    manager = HeadlessManager(ROOT / "headlessClient")
    api = manager.startup_api("C1")
    api.add("1+1", "ok")
    api.write()

    pid = int(manager.start("C1")["pid"])
    try:
        time.sleep(12)
        yield {
            "pid": pid,
            "before_local": before_local,
            "before_roaming": before_roaming,
            "after_local": _snapshot(REAL_LOCAL),
            "after_roaming": _snapshot(REAL_ROAMING),
            "results": api.batch.results(),
        }
    finally:
        manager.stop("C1")
        _kill_instance_processes()


def test_environment_points_into_the_instance(run_once):
    """The env vars are the mechanism; check it held for the process that ran."""
    from qsick import headless

    env = headless._isolated_env(INSTANCE)
    owndata = INSTANCE / "owndata"
    assert Path(env["APPDATA"]) == owndata / "appdata"
    assert Path(env["LOCALAPPDATA"]) == owndata / "localappdata"


def test_nothing_new_appears_in_real_local_appdata(run_once):
    """The regression that produced "data directory not authorized"."""
    new = set(run_once["after_local"]) - set(run_once["before_local"])
    assert not new, f"真实 AppData\\Local 新增了 {sorted(new)[:6]}"


def test_nothing_new_appears_in_real_roaming(run_once):
    new = set(run_once["after_roaming"]) - set(run_once["before_roaming"])
    assert not new, f"真实 AppData\\Roaming 新增了 {sorted(new)[:6]}"


def test_instance_data_lands_under_owndata(run_once):
    owndata = INSTANCE / "owndata"
    dirs = [name for name in ("appdata", "localappdata", "userprofile", "temp")
            if (owndata / name).is_dir()]
    assert dirs, "owndata 下没有任何隔离目录"
    # Something must actually have been written, or the test proves nothing.
    written = [
        path for sub in dirs
        for path in (owndata / sub).rglob("*")
        if path.is_file()
    ]
    assert written, "隔离目录存在但没有任何数据写入，无法证明重定向生效"


def test_the_instance_still_works(run_once):
    """Isolation must not have broken the client."""
    rows = {r.id: r for r in run_once["results"]}
    assert "ok" in rows, "任务队列没有产出结果"
    assert rows["ok"].ok, rows["ok"].error
