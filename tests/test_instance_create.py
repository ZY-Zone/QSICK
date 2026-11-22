"""实例创建：名称唯一、类型可选、pipe 实例铺模板。

三条都容易在重构里悄悄坏掉，所以分开断言：

* 名称必须唯一，且与目录名解耦（改名字不能让已有配置失联）；
* ``kind`` 只接受 headless/pipe，老实例（instance.json 里没有该字段）按
  headless 处理——升级不该把已有实例判成别的类型；
* pipe 实例要铺出可启动的目录（QQ.exe + launch.bat + 对齐版本），失败时不能
  留下半成品目录。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from qsick import instance_template
from qsick.headless import HeadlessManager


@pytest.fixture()
def manager(tmp_path: Path, monkeypatch):
    """实例管理器，带一个假的模板目录（不碰真实 1.1 GB 客户端）。"""
    root = tmp_path / "headlessClient"
    templates = tmp_path / "instanceTemplates"
    monkeypatch.setattr(instance_template, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(instance_template, "template_root", lambda: templates)
    # 源客户端也指到 tmp：build_template 不该去摸真实磁盘上的 QQ。
    #
    # Same lookup the real function performs -- `QQNT-*` beside the project -- but rooted at `tmp_path`.
    # Tests here create their own client with the version they then assert on (one of them deliberately
    # writes a bogus version), so pinning a single shared fake would overwrite what each test set up.
    # The conftest fixture pins `_source_client` for isolation; this restores per-test discovery inside
    # the temp tree, which is the narrower and correct behaviour for this file.
    def discover() -> Path | None:
        for candidate in sorted(tmp_path.glob("QQNT-*")):
            if (candidate / "QQ.exe").is_file():
                return candidate
        return None

    monkeypatch.setattr(instance_template, "_source_client", discover)
    return HeadlessManager(root)


def _fake_client(tmp_path: Path) -> Path:
    """造一份最小可用的客户端树：与真实结构同形，但只有几个文件。

    Idempotent: the `manager` fixture points `_source_client` at this, and it may be called more than
    once per test, so creating the tree twice must not raise.
    """
    client = tmp_path / "QQNT-9.9.33-52230"
    app = client / "versions" / "9.9.33-52230" / "resources" / "app"
    app.mkdir(parents=True, exist_ok=True)
    (client / "QQ.exe").write_bytes(b"MZ fake")
    (app / "wrapper.node").write_bytes(b"wrapper")
    (app / "package.json").write_text('{"version": "9.9.33-52230"}', encoding="utf-8")
    return client


def test_create_assigns_id_and_defaults_to_headless(manager):
    item = manager.create(name="客服A")
    assert item["id"] == "C0"
    assert item["name"] == "客服A"
    assert item["kind"] == "headless"
    meta = json.loads((manager.root / "C0" / "instance.json").read_text(encoding="utf-8"))
    assert meta["name"] == "客服A"
    assert meta["kind"] == "headless"


def test_create_without_name_falls_back_to_id(manager):
    item = manager.create()
    assert item["name"] == "C0"
    assert (manager.root / "C0" / "host.js").is_file(), "headless 实例必须带 host.js"


def test_duplicate_name_is_rejected_case_insensitively(manager):
    manager.create(name="Alpha")
    with pytest.raises(ValueError, match="已被占用"):
        manager.create(name="Alpha")
    with pytest.raises(ValueError, match="已被占用"):
        manager.create(name="alpha")


def test_name_validation_rejects_empty_looking_and_bad_chars(manager):
    with pytest.raises(ValueError, match="只能包含"):
        manager.create(name="坏/名字")
    with pytest.raises(ValueError, match="最多"):
        manager.create(name="x" * 33)


def test_legacy_instance_without_kind_is_headless(manager):
    """升级前建的实例只有 id/qq/avatar/port，不能被判成 pipe。

    A legacy directory still needs its `host.js` to be recognised: that file is what marks the directory as
    a headless instance, and a real pre-upgrade instance always has one because `create()` wrote it.
    """
    path = manager.root / "C0"
    (path / "data").mkdir(parents=True)
    (path / "host.js").write_bytes(b"// legacy host")
    (path / "instance.json").write_text(
        json.dumps({"id": "C0", "qq": "12345", "avatar": "", "port": 9100}),
        encoding="utf-8")
    items = manager.instances()
    assert items[0]["kind"] == "headless"
    assert items[0]["name"] == "C0"


def test_a_directory_without_a_host_is_not_an_instance(manager):
    """A name alone does not make an instance.

    A half-created directory or an unrelated folder must not be offered as one: it can never start, and the
    operator has no way to tell it from a real instance until it fails.
    """
    path = manager.root / "C0"
    (path / "data").mkdir(parents=True)
    (path / "instance.json").write_text(json.dumps({"id": "C0"}), encoding="utf-8")

    assert manager.instances() == []


def test_pipe_instance_lays_out_a_launchable_tree(manager, tmp_path: Path):
    client = _fake_client(tmp_path)
    item = manager.create(name="主力号", kind="pipe")
    assert item["kind"] == "pipe"
    # Fixed directory name, not C0: the pipe instance is a singleton and PIPE_INSTANCE_ID
    # says so plainly.
    directory = manager.root / "Pipe"
    assert item["id"] == "Pipe"

    assert (directory / "QQ.exe").is_file()
    assert (directory / "launch.bat").is_file()
    assert (directory / "versions" / "9.9.33-52230" / "resources" / "app" / "wrapper.node").is_file()
    # pipe 实例不跑 host.js：它的宿主是自己那份 QQ.exe。
    assert not (directory / "host.js").exists()

    launch = (directory / "launch.bat").read_text(encoding="utf-8")
    # 临时文件目录在实例内，避免与真实客户端互相干扰。
    assert "--user-data-dir" in launch

    config = json.loads((directory / "config.json").read_text(encoding="utf-8"))
    assert config["kind"] == "pipe"
    assert config["qqExe"].endswith("QQ.exe")
    assert config["version"] == "9.9.33-52230"


def test_pipe_template_is_reused_across_instances(manager, tmp_path: Path):
    """Building the template twice reuses it instead of copying the client again.

    This used to create two pipe instances, which the singleton rule now forbids. The
    property under test is the template cache, not instance count, so exercise it directly:
    two builds must leave exactly one template directory.
    """
    from qsick.instance_template import build_template

    _fake_client(tmp_path)

    build_template()
    build_template()

    templates = list((tmp_path / "instanceTemplates").iterdir())
    assert len(templates) == 1, f"模板应只生成一份，实际 {templates}"


def test_pipe_instance_leaves_mismatched_manifest_untouched(manager, tmp_path: Path):
    """版本号不一致时**不要**去改 package.json。

    早先这里会自动把 version 改成目录名。那是个错误：``package.json`` 在客户端的
    完整性校验清单里，改一个字节（哪怕只动缩进）启动就报「文件已损坏」。

    现在只核对、只告警——版本号不一致说明这份客户端本身残缺（目录名被手工改过），
    改不改都起不来，至少保证不是我们改的。
    """
    client = tmp_path / "QQNT-9.9.33-52230"
    app = client / "versions" / "9.9.33-52230" / "resources" / "app"
    app.mkdir(parents=True)
    (client / "QQ.exe").write_bytes(b"MZ fake")
    (app / "wrapper.node").write_bytes(b"wrapper")
    # 故意写成另一个版本
    original = '{"version": "0.0.0-bogus"}'
    (app / "package.json").write_text(original, encoding="utf-8")

    manager.create(name="X", kind="pipe")
    # The pipe instance's directory is the fixed name "Pipe", not C0 -- see PIPE_INSTANCE_ID.
    produced = (manager.root / "Pipe" / "versions" / "9.9.33-52230" / "resources" / "app"
                / "package.json")
    assert produced.read_text(encoding="utf-8") == original, (
        "实例不该改写 package.json——它受完整性校验保护")


def test_missing_source_client_fails_without_leaving_junk(manager, monkeypatch, tmp_path: Path):
    monkeypatch.setattr(instance_template, "_source_client", lambda: None)
    with pytest.raises(instance_template.TemplateError):
        manager.create(name="Y", kind="pipe")
    leftovers = list(manager.root.iterdir()) if manager.root.is_dir() else []
    assert leftovers == [], f"失败后不该留下目录：{leftovers}"
