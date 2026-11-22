"""多实例隔离：每个 pipe 实例必须拿自己那份 DLL 和配置。

这里最容易出错的地方是 DLL 的配置通道。DLL 从**自己旁边**的
``qsick_pipe.json`` 读 owndata 路径（不能走环境变量——CreateProcessW 传非 NULL
``lpEnvironment`` 在本工具链下返回 87），所以一旦多个实例共用同一份 DLL 文件，
后启动的那个就会覆盖先启动那个的配置。

实测过这个 bug：同时起 C1 和 C2 之后，仓库里那份 json 只剩下 C2 的 owndata；
C1 之所以看起来正常，只是因为它恰好在被覆盖前就读完了配置。这类竞态在单实例
测试里永远发现不了，所以单独放在这里盯着。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from qsick import instance_template
from qsick.headless import HeadlessManager, PIPE_DLL_CONFIG, PIPE_DLL_NAME


def _spy(monkeypatch) -> list[list[str]]:
    """Record each launch as `[exe, command-line arguments]`.

    The launch path is `transport.inject.launch_and_inject` (CREATE_SUSPENDED, inject, resume) rather than
    `subprocess.Popen`, because only that order catches QQ's napi burst before the DLL's patches land.
    Recording the pair keeps what these tests care about: which executable, and what was on its command line.
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
    """造一份最小客户端，让 pipe 实例能建起来。"""
    client = tmp_path / "client"
    version = "9.9.33-52230"
    app = client / "versions" / version / "resources" / "app"
    app.mkdir(parents=True)
    (client / "QQ.exe").write_bytes(b"MZ")
    (app / "package.json").write_text(
        json.dumps({"name": "qq", "version": version}), encoding="utf-8")
    (client / "versions" / "config.json").write_text(
        json.dumps({"curVersion": version}), encoding="utf-8")

    # 一份假的 DLL，避免依赖真的构建产物。
    fake_dll = tmp_path / "repo_dll" / PIPE_DLL_NAME
    fake_dll.parent.mkdir(parents=True)
    fake_dll.write_bytes(b"fake dll payload")

    monkeypatch.setattr(instance_template, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(instance_template, "template_root",
                        lambda: tmp_path / "instanceTemplates")
    monkeypatch.setattr(instance_template, "_source_client", lambda: client)
    monkeypatch.setattr("qsick.headless.pipe_dll", lambda: fake_dll)
    # Pin the QQ data root into the temporary tree.
    #
    # create() now reads QQ's configured data folder (reports/06) and grants access to it.
    # Without this the test would probe the user's real "Tencent Files", which is account
    # data and must not be touched by a test.
    monkeypatch.setenv("QSICK_QQ_DATA_ROOT", str(tmp_path / "qqdata"))
    return HeadlessManager(tmp_path / "headlessClient")


def test_owndata_lives_beside_the_instance(manager, monkeypatch):
    """owndata 必须在实例根下，而不是用户目录或别的共享位置。"""
    _spy(monkeypatch)
    item = manager.create(name="目录", kind="pipe")
    manager.start(item["id"])

    own = manager.root / item["id"] / "owndata"
    assert own.is_dir(), "实例根下应当有 owndata 目录"
    config = json.loads((manager.root / item["id"] / "config.json")
                        .read_text(encoding="utf-8"))
    assert Path(config["owndata"]) == own.resolve()

    # Electron 的 userData 也要落在实例里，否则多实例会抢同一份。
    assert (own / "userdata").is_dir()


def test_qq_is_launched_with_isolated_userdata_and_without_no_sandbox(manager, monkeypatch):
    """不带 --no-sandbox，且 userData 指到实例内。

    这个断言的方向反过来了，值得说明。原先这里要求命令行**必须**带
    ``--no-sandbox``，理由是"不加它 QQ 会在 12 秒后以 0x80000003 退出"。观察没错，
    但因果搞反了：真正的原因是实例目录带着 DSH 留下的特殊 ACL，QQ 在那种路径下起
    不来；``--no-sandbox`` 只是绕过了它。而绕过会关掉 Chromium 沙箱，QQ 便会判定
    "个人文件夹未授权"，用弹窗顶替崩溃。

    目录 ACL 正常后 QQ 自己就能起来，这个参数从必需变成了新的故障源，所以现在断言
    它**不在**命令行里。需要时用 QSICK_PIPE_NO_SANDBOX=1 显式打开。
    """
    monkeypatch.delenv("QSICK_PIPE_NO_SANDBOX", raising=False)
    calls = _spy(monkeypatch)
    item = manager.create(name="参数", kind="pipe")
    manager.start(item["id"])

    _exe, line = calls[0]
    assert "--no-sandbox" not in line, (
        f"默认不该带 --no-sandbox（它会关闭沙箱并触发未授权判定）：{line}")
    assert line.count("--user-data-dir=") == 1, f"--user-data-dir 应当恰好一个：{line}"
    # Strip the quotes `_command_line_arguments` adds for spaced paths before comparing.
    value = line.split("--user-data-dir=", 1)[1].strip().strip('"')
    assert str(manager.root / item["id"]) in value, \
        f"userData 没有落在实例目录里：{value}"


def test_no_sandbox_can_still_be_requested_explicitly(manager, monkeypatch):
    """出问题时的退路要留着：环境变量打开后仍然会带这个参数。"""
    monkeypatch.setenv("QSICK_PIPE_NO_SANDBOX", "1")
    calls = _spy(monkeypatch)
    item = manager.create(name="参数", kind="pipe")
    manager.start(item["id"])

    assert "--no-sandbox" in calls[0][1]


def test_stop_marks_instance_stopped(manager, monkeypatch):
    """stop() 之后状态必须变成 stopped，pid 清零。"""
    _spy(monkeypatch)
    monkeypatch.setattr(HeadlessManager, "_kill_pid", lambda self, pid: None)
    monkeypatch.setattr(HeadlessManager, "_pids_on_port", lambda self, port: set())

    item = manager.create(name="停", kind="pipe")
    manager.start(item["id"])
    manager.stop(item["id"])

    status = json.loads((manager.root / item["id"] / "data" / "status.json")
                        .read_text(encoding="utf-8"))
    assert status["phase"] == "stopped"
    assert int(status.get("pid") or 0) == 0
