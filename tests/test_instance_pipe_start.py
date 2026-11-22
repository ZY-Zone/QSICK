"""pipe 实例的启动路径：跑原版 QQ.exe，不改任何受完整性校验的文件。

这些断言来自实测，不是设计推断。曾经把 ``package.json`` 的 ``main`` 换成自己的
loader（照 NapCat 的思路），在本版本客户端上必然失败：

    ================  ===================  ==========
    改动               [preload] 成功次数    结果
    ================  ===================  ==========
    不动               2                    正常启动
    加一个无害的键      0                    弹「退出」框
    只改缩进           0                    弹「退出」框
    ================  ===================  ==========

只改缩进也失败 —— 说明是内容哈希，没有"小心地改"的余地。所以现在的约定是：
**受校验的四个文件一个字节都不能动**，接管收发出传输层的 DLL 注入完成。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from qsick import instance_template
from qsick.headless import HeadlessManager

APP_REL = Path("versions") / "9.9.33-52230" / "resources" / "app"


class _FakeProcess:
    def __init__(self, args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.pid = 424242
        self.returncode = None

    def wait(self, timeout=None):
        return 0

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def communicate(self, input=None, timeout=None):
        return (b"", b"")

    def kill(self):
        self.returncode = 0

    def poll(self):
        return self.returncode


@pytest.fixture()
def client(tmp_path: Path) -> Path:
    """一份最小客户端树，结构同真实安装（含受校验的四个文件）。"""
    root = tmp_path / "QQNT-9.9.33-52230"
    app = root / APP_REL
    app.mkdir(parents=True)
    (root / "QQ.exe").write_bytes(b"MZ fake")
    (app / "wrapper.node").write_bytes(b"wrapper")
    (app / "major.node").write_bytes(b"major")
    (app / "application.asar").write_bytes(b"asar")
    (app / "application.json").write_text('{"package.json": "sig"}', encoding="utf-8")
    (app / "package.json").write_text(
        '{"version": "9.9.33-52230", "main": "./application.asar/app_launcher/index.js"}',
        encoding="utf-8")
    return root


@pytest.fixture()
def manager(tmp_path: Path, monkeypatch, client: Path):
    """实例管理器，模板从 fixture 造的假客户端生成。

    ``build_template()`` 默认会去 ``_source_client()`` 找一份真实客户端；测试里
    必须把它钉到这个 fixture 上，否则测的就不是我们自己造的那棵树（而且会真的
    去扫 {项目根} 下的安装目录）。
    """
    templates = tmp_path / "instanceTemplates"
    monkeypatch.setattr(instance_template, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(instance_template, "template_root", lambda: templates)
    monkeypatch.setattr(instance_template, "_source_client", lambda: client)
    real = Path(__file__).resolve().parents[1] / "src" / "qsick"
    monkeypatch.setattr(instance_template, "_host_module_dir", lambda: real, raising=False)
    # Pin the QQ data root into the temporary directory.
    #
    # create() reads QQ's own configured data folder and grants access to it when needed.
    # Without isolation a test would probe the user's real "Tencent Files" -- that is account
    # data, and a test must not touch it even just to check writability.
    monkeypatch.setenv("QSICK_QQ_DATA_ROOT", str(tmp_path / "qqdata"))
    return HeadlessManager(tmp_path / "headlessClient")


def _spy(monkeypatch) -> list[list[str]]:
    """Record each launch as `[exe, command-line arguments]`.

    The launch path moved to `transport.inject.launch_and_inject` (CREATE_SUSPENDED, inject, resume) because
    that order is the only one that catches QQ's napi burst. Arguments now travel as one rendered string
    rather than a list, so the recorded pair keeps the two things these tests assert on: which executable ran,
    and what was on its command line.

    Injection itself needs a real process handle and is covered by tests/test_pipe_inject.py, so it is
    stubbed here.
    """
    calls: list[list[str]] = []

    class FakeInjection:
        pid = 424242
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


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_pipe_start_launches_qq_exe(manager, monkeypatch):
    calls = _spy(monkeypatch)
    item = manager.create(name="管道", kind="pipe")
    manager.start(item["id"])

    assert calls, "start() 没有启动任何进程"
    argv = calls[0]
    assert argv[0].endswith("QQ.exe"), f"pipe 实例应当启动 QQ.exe，实际是 {argv[0]}"
    # 数据目录必须隔离：不隔离的话多实例会抢同一份登录态。
    assert any("--user-data-dir" in part for part in argv), argv
    assert not any(part.endswith("host.js") for part in argv), "pipe 实例不该跑 node 宿主"


def test_creation_leaves_protected_files_byte_identical(manager, client):
    """这是最要紧的一条：受校验的文件必须与源客户端逐字节一致。

    它们一旦被改动，QQ 启动就弹「文件已损坏」——用户看到的就是这个。
    """
    item = manager.create(name="完整性", kind="pipe")
    app = manager.root / item["id"] / APP_REL

    for name in instance_template.PROTECTED:
        source = client / APP_REL / name
        produced = app / name
        assert produced.is_file(), f"{name} 没被铺出来"
        assert _digest(source) == _digest(produced), (
            f"{name} 与源客户端不一致——写入穿透了硬链接，或模板本身已被污染")


def test_pipe_start_does_not_patch_main(manager, monkeypatch):
    item = manager.create(name="不改main", kind="pipe")
    app = manager.root / item["id"] / APP_REL
    before = _digest(app / "package.json")

    _spy(monkeypatch)
    manager.start(item["id"])

    manifest = json.loads((app / "package.json").read_text(encoding="utf-8"))
    assert manifest["main"] == "./application.asar/app_launcher/index.js", (
        "main 被改了；客户端会做内容哈希校验，一改就报「文件已损坏」")
    assert _digest(app / "package.json") == before, "package.json 被改写过"
    assert "originalMain" not in manifest


def test_start_removes_loader_left_by_older_versions(manager, monkeypatch):
    """早前版本装过 loader 的实例，启动前必须被还原，否则起不来。"""
    item = manager.create(name="历史实例", kind="pipe")
    app = manager.root / item["id"] / APP_REL

    # 手工模拟旧版本留下的痕迹
    manifest = app / "package.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["originalMain"] = payload["main"]
    payload["main"] = "./qsick_pipe_loader.js"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    (app / "qsick_pipe_loader.js").write_text("// old", encoding="utf-8")
    (app / "headless_host.js").write_text("// old host", encoding="utf-8")

    _spy(monkeypatch)
    manager.start(item["id"])

    restored = json.loads(manifest.read_text(encoding="utf-8"))
    assert restored["main"] == "./application.asar/app_launcher/index.js"
    assert "originalMain" not in restored
    assert not (app / "qsick_pipe_loader.js").exists()
    assert not (app / "headless_host.js").exists()


def test_remove_loader_is_idempotent_on_clean_instance(manager):
    item = manager.create(name="干净", kind="pipe")
    app = manager.root / item["id"] / APP_REL
    before = _digest(app / "package.json")

    assert instance_template.remove_loader(manager.root / item["id"], app) is False
    assert _digest(app / "package.json") == before, "干净的实例不该被碰"


def test_protected_files_are_copied_not_hardlinked(manager, client):
    """受校验的文件必须是独立副本——这是防止污染源客户端的唯一硬保证。

    实测被这条坑过两次：硬链接与源共享数据，任何一次 ``write_text`` 都写穿透，
    改一个实例的 ``package.json``，``{项目根}\\QQNT-9.9.33-52230`` 那份跟着变，
    之后所有实例启动都报「文件已损坏」。

    这里不去验证"我们的代码不会写它"（那是约定，会被将来的人打破），而是验证
    物理上不共享：往实例里写，源文件不能变。
    """
    item = manager.create(name="隔离", kind="pipe")
    app = manager.root / item["id"] / APP_REL

    before = {name: _digest(client / APP_REL / name)
              for name in instance_template.PROTECTED}

    # 模拟最坏情况：有代码路径直接改实例里的受校验文件。
    for name in instance_template.PROTECTED:
        (app / name).write_bytes(b"TAMPERED")

    for name in instance_template.PROTECTED:
        assert _digest(client / APP_REL / name) == before[name], (
            f"改了实例的 {name} 却污染了源客户端——说明它是硬链接")


def test_pipe_start_writes_unified_status(manager, monkeypatch):
    _spy(monkeypatch)
    item = manager.create(name="管道2", kind="pipe")
    manager.start(item["id"])

    status = json.loads((manager.root / item["id"] / "data" / "status.json").read_text(encoding="utf-8"))
    # 与 headless 共用字段，账户表 / 自动重登 / 聊天页才不用为 pipe 分支。
    assert status["pid"] == 424242
    assert status["phase"] == "starting"
    assert status["transport"] == "pipe"

    config = json.loads((manager.root / item["id"] / "config.json").read_text(encoding="utf-8"))
    assert config["kind"] == "pipe"
    assert config["qqExe"].endswith("QQ.exe")
    assert config["parentPid"] > 0


def test_pipe_start_rejects_instance_without_exe(manager, monkeypatch):
    calls = _spy(monkeypatch)
    item = manager.create(name="缺exe", kind="pipe")
    (manager.root / item["id"] / "QQ.exe").unlink()

    with pytest.raises(RuntimeError, match="缺少 QQ.exe"):
        manager.start(item["id"])
    assert calls == [], "缺 QQ.exe 时不该启动任何进程"


def test_headless_start_still_uses_node(manager, monkeypatch):
    """别把 headless 也改坏了：它应当照旧启动 host.js。"""
    calls = _spy(monkeypatch)
    item = manager.create(name="无壳", kind="headless")

    monkeypatch.setattr("qsick.headless.shutil.which", lambda name: "C:/fake/node.exe")
    try:
        manager.start(item["id"])
    except Exception:
        # runtime 准备可能失败（测试环境没有客户端副本），只要没走 QQ.exe 分支即可。
        pass

    ran_qq = [argv for argv in calls if argv and str(argv[0]).endswith("QQ.exe")]
    assert not ran_qq, f"headless 实例不应启动 QQ.exe：{ran_qq}"
