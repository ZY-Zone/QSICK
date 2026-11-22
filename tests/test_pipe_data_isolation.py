"""Environment handling for pipe instances.

History matters here, because this file used to assert the opposite.

An earlier design redirected APPDATA / LOCALAPPDATA into the instance's owndata and had the
DLL rewrite the Documents shell folder, hoping QQ's data would land beside the instance. It
did not work, and reports/06-安装目录与数据目录.md explains why: QQ's business data root is
read from

    <CSIDL_COMMON_DOCUMENTS>/Tencent/QQ/UserDataInfo.ini
        [UserDataSet] UserDataSavePath

Documents is only the fallback when that config is missing. So the redirect changed a path
QQ never consults, while the real config still pointed at the shared folder -- and QQ, finding
that folder hidden from it, refused to start with "个人文件夹保存位置无效".

The current behaviour is deliberately unexciting: the environment is the system's, and the
configured data root is granted access when an instance is created. These tests pin that down.
They never start QQ.
"""

from __future__ import annotations

import os
from pathlib import Path

from qsick import headless


def test_temp_goes_into_the_instance(tmp_path: Path):
    """Scratch space is the one thing worth containing: it is not account data."""
    workdir = tmp_path / "C9"
    workdir.mkdir()

    env = headless._isolated_env(workdir)
    owndata = workdir / "owndata"

    assert Path(env["TEMP"]) == owndata / "temp"
    assert Path(env["TMP"]) == owndata / "temp"
    assert (owndata / "temp").is_dir()


def test_profile_vars_keep_the_system_values(tmp_path: Path, monkeypatch):
    """APPDATA / LOCALAPPDATA must NOT be redirected.

    Redirecting them is what made Electron resolve an appData path it later rejected, and it
    never contained the data anyway -- QQ reads its root from the INI config.
    """
    monkeypatch.setenv("APPDATA", r"C:\system\roaming")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\system\local")

    workdir = tmp_path / "C9"
    workdir.mkdir()
    env = headless._isolated_env(workdir)

    assert env["APPDATA"] == r"C:\system\roaming"
    assert env["LOCALAPPDATA"] == r"C:\system\local"


def test_userprofile_is_not_redirected(tmp_path: Path, monkeypatch):
    """USERPROFILE stays real, and this is why.

    app_launcher/index.js calls getMacShareSandBoxPath, which resolves Electron's
    app.getPath('appData'). With USERPROFILE redirected that lookup throws

        Error: Failed to get 'appData' path

    and the exception reaches QQ's UI as a transient JS error popup. Measured by redirecting
    one variable at a time:

        all isolated                    -> error x1
        restore APPDATA only            -> error x1
        restore USERPROFILE only        -> error x0   <- the cause
        restore APPDATA + USERPROFILE   -> error x0
    """
    monkeypatch.setenv("USERPROFILE", r"C:\real\user")

    workdir = tmp_path / "C9"
    workdir.mkdir()
    env = headless._isolated_env(workdir)

    assert env["USERPROFILE"] == r"C:\real\user"


def test_existing_environment_is_preserved(tmp_path: Path, monkeypatch):
    """PATH and friends must survive, or QQ cannot find its own DLLs."""
    monkeypatch.setenv("QSICK_TEST_MARKER", "kept")

    workdir = tmp_path / "C9"
    workdir.mkdir()
    env = headless._isolated_env(workdir)

    assert env["QSICK_TEST_MARKER"] == "kept"
    if "PATH" in os.environ:
        assert env["PATH"] == os.environ["PATH"]


def test_caller_environment_is_not_mutated(tmp_path: Path, monkeypatch):
    """A returned copy, not a patch on os.environ -- instances must not affect each other."""
    monkeypatch.setenv("APPDATA", r"C:\real\appdata")

    workdir = tmp_path / "C9"
    workdir.mkdir()
    headless._isolated_env(workdir)

    assert os.environ["APPDATA"] == r"C:\real\appdata"


def test_two_instances_get_separate_temp_roots(tmp_path: Path):
    """Per-instance scratch space, so concurrent instances do not share temp files."""
    first = tmp_path / "C1"
    second = tmp_path / "C2"
    first.mkdir()
    second.mkdir()

    env1 = headless._isolated_env(first)
    env2 = headless._isolated_env(second)

    assert env1["TEMP"] != env2["TEMP"]
    assert str(first) in env1["TEMP"]
    assert str(second) in env2["TEMP"]


def test_data_root_override_is_honoured(tmp_path: Path, monkeypatch):
    """QSICK_QQ_DATA_ROOT exists so the data root is configurable, not hard-coded.

    Users may keep their data on another drive, and tests need to exercise the grant logic
    without touching a real account folder.
    """
    target = tmp_path / "custom-root"
    monkeypatch.setenv("QSICK_QQ_DATA_ROOT", str(target))

    assert headless.qq_data_root() == target


def test_data_root_reads_the_ini_qq_actually_uses(tmp_path: Path, monkeypatch):
    """Without an override, read the INI at CSIDL_COMMON_DOCUMENTS (reports/06)."""
    public = tmp_path / "Public"
    ini = public / "Documents" / "Tencent" / "QQ" / "UserDataInfo.ini"
    ini.parent.mkdir(parents=True)
    ini.write_text(
        "[UserDataSet]\n"
        "UserDataSavePathType=2\n"
        "UserDataSavePath=D:\\downloads\\qq\\Tencent Files\n",
        encoding="mbcs")

    monkeypatch.delenv("QSICK_QQ_DATA_ROOT", raising=False)
    monkeypatch.setenv("PUBLIC", str(public))

    assert headless.qq_data_root() == Path(r"D:\downloads\qq\Tencent Files")


def test_missing_ini_yields_none_rather_than_a_guess(tmp_path: Path, monkeypatch):
    """No config means "let QQ use its own fallback", not "invent a path"."""
    monkeypatch.delenv("QSICK_QQ_DATA_ROOT", raising=False)
    monkeypatch.setenv("PUBLIC", str(tmp_path / "no-such-public"))

    assert headless.qq_data_root() is None
