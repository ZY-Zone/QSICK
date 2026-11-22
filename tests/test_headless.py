from __future__ import annotations

from pathlib import Path

from qsick.config import RuntimeConfig
from qsick.headless import HeadlessManager
from qsick.runtime import Runtime
from qsick.webui.app import create_app

import httpx
import pytest


def client(runtime: Runtime) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(runtime)), base_url="http://ui")


def test_manager_copies_instances(tmp_path: Path):
    manager = HeadlessManager(tmp_path / "headlessClient")
    first = manager.create()
    second = manager.create()
    assert first["id"] == "C0" and second["id"] == "C1"
    assert (tmp_path / "headlessClient" / "C0" / "host.js").is_file()
    assert [item["id"] for item in manager.instances()] == ["C0", "C1"]
    assert manager.delete("C1") is True
    assert [item["id"] for item in manager.instances()] == ["C0"]
    assert list((tmp_path / "headlessClient" / ".trash").iterdir())


def test_prepared_runtime_is_the_source_of_truth(tmp_path: Path, monkeypatch):
    """A runtime copy already on disk must not be relabelled by the installed QQ.

    The repo ships no runtime; a user may drop a pinned build (e.g. 9.9.33-52230)
    into the runtime directory while the installed client has already updated.
    The instance config must then describe the binaries on disk.
    """
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "wrapper.node").write_bytes(b"wrapper")
    (runtime / "package.json").write_text('{"version": "9.9.33-52230"}', encoding="utf-8")
    (runtime / "major.node").write_bytes(b"QQAppId/537379411\x00rest")
    (runtime / "QQNT.dll").write_bytes(b"shim")

    manager = HeadlessManager(tmp_path / "headlessClient", runtime=runtime)
    monkeypatch.setattr("qsick.headless.host_shim", lambda: None)
    # runtime 已就绪时必须直接返回，连下载都不该尝试。
    def _boom(*_a, **_k):
        raise AssertionError("runtime 已存在时不应触发下载")
    monkeypatch.setattr("qsick.runtime_fetch.ensure_runtime", _boom)

    path, version, appid, qua = manager.prepare_runtime()
    assert path == runtime
    assert version == "9.9.33-52230"
    assert appid == "537379411"
    assert qua == "V1_WIN_NQ_9.9.33_52230_GW_B"


def test_prepare_runtime_locates_app_layer_inside_full_client(tmp_path: Path, monkeypatch):
    """A full-client runtime must load from versions/<ver>/resources/app.

    The published archive is a whole client (QQ.exe, versions/, extension/), so
    wrapper.node sits two levels down. prepare_runtime() reports that layer, and
    — importantly — leaves runtime/ untouched: it stays a pristine client copy
    so it can be hardlinked as a template for pipe instances.
    """
    runtime = tmp_path / "runtime"
    app = runtime / "versions" / "9.9.33-52230" / "resources" / "app"
    app.mkdir(parents=True)
    (app / "wrapper.node").write_bytes(b"wrapper")
    (app / "package.json").write_text('{"version": "9.9.33-52230"}', encoding="utf-8")
    (app / "major.node").write_bytes(b"QQAppId/537379411\x00rest")
    # The real client's own DLL: must be left alone.
    real_dll = runtime / "versions" / "9.9.33-52230" / "QQNT.dll"
    real_dll.write_bytes(b"real-client-dll")
    (runtime / "QQ.exe").write_bytes(b"exe")

    shim = tmp_path / "shim.dll"
    shim.write_bytes(b"shim")

    manager = HeadlessManager(tmp_path / "headlessClient", runtime=runtime)
    monkeypatch.setattr("qsick.headless.host_shim", lambda: shim)

    path, version, appid, qua = manager.prepare_runtime()
    assert path == app, "应当返回 app 层，宿主从这里 require wrapper.node"
    assert version == "9.9.33-52230"
    assert appid == "537379411"
    assert qua == "V1_WIN_NQ_9.9.33_52230_GW_B"

    # runtime/ 必须保持纯客户端：垫片不该写进去，原库也不能被动。
    assert not (app / "QQNT.dll").exists(), "垫片不该写进 runtime"
    assert not (runtime / "QQNT.dll").exists()
    assert real_dll.read_bytes() == b"real-client-dll"


def test_start_puts_shim_in_instance_dir_and_on_path(tmp_path: Path, monkeypatch):
    """The shim travels with the instance, not with runtime/.

    Measured Windows behaviour: a .node loaded by absolute path does NOT search
    its own directory first; the QQNT.dll that wins is the one in the current
    working directory or on PATH. host.js chdir's to the app layer, where there
    is no shim, so the instance dir must be prepended to PATH for the 15 KB shim
    to beat the client's own 204 MB QQNT.dll.

    This asserts the wiring (copy + PATH), not the loader's behaviour.
    """
    runtime = tmp_path / "runtime"
    app = runtime / "versions" / "9.9.33-52230" / "resources" / "app"
    app.mkdir(parents=True)
    (app / "wrapper.node").write_bytes(b"wrapper")
    (app / "package.json").write_text('{"version": "9.9.33-52230"}', encoding="utf-8")
    (app / "major.node").write_bytes(b"QQAppId/537379411\x00rest")

    shim = tmp_path / "shim.dll"
    shim.write_bytes(b"shim-bytes")

    manager = HeadlessManager(tmp_path / "headlessClient", runtime=runtime)
    monkeypatch.setattr("qsick.headless.host_shim", lambda: shim)

    captured: dict[str, object] = {}

    class FakeProc:
        pid = 4242

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["env"] = kwargs.get("env") or {}
        captured["cwd"] = kwargs.get("cwd")
        return FakeProc()

    monkeypatch.setattr("qsick.headless.subprocess.Popen", fake_popen)
    monkeypatch.setattr("qsick.headless.shutil.which", lambda name: "node.exe")
    # Status polling would spin for 10s; make it a no-op.
    monkeypatch.setattr("qsick.headless.time.sleep", lambda _s: None)

    instance = manager.create()
    instance_id = instance["id"]
    manager.start(instance_id, auto_login=False)

    instance_dir = (tmp_path / "headlessClient" / instance_id).resolve()
    # 垫片复制进了实例目录
    assert (instance_dir / "QQNT.dll").read_bytes() == b"shim-bytes"
    # 实例目录被加进 PATH 的最前面
    path_parts = captured["env"]["PATH"].split(";") if ";" in captured["env"]["PATH"] else captured["env"]["PATH"].split(":")
    assert path_parts[0] == str(instance_dir)
    # runtime/ 仍然干净
    assert not (app / "QQNT.dll").exists()


def test_prepare_runtime_uses_the_local_runtime_without_downloading(tmp_path: Path, monkeypatch):
    """A runtime that is already present is used as-is, and nothing is fetched.

    `runtime/` is the only client source. A copy that is already there is authoritative -- it is the
    locked build the injected DLL matches -- so the download must not run at all.
    """
    runtime = tmp_path / "runtime"
    app = runtime / "versions" / "9.9.33-52230" / "resources" / "app"
    app.mkdir(parents=True)
    (app / "wrapper.node").write_bytes(b"wrapper")
    (app / "package.json").write_text('{"version": "9.9.33-52230"}', encoding="utf-8")
    (app / "major.node").write_bytes(b"QQAppId/123456\x00")
    (app / "audio.dll").write_bytes(b"dll")
    (runtime / "QQ.exe").write_bytes(b"MZ")
    shim = tmp_path / "shim.dll"
    shim.write_bytes(b"shim")

    downloaded: list[int] = []
    monkeypatch.setattr("qsick.headless.host_shim", lambda: shim)
    monkeypatch.setattr("qsick.runtime_fetch.ensure_runtime",
                        lambda *a, **k: downloaded.append(1) or True)

    manager = HeadlessManager(tmp_path / "headlessClient", runtime=runtime)
    path, version, appid, qua = manager.prepare_runtime()

    assert downloaded == [], "runtime 已存在时不该触发下载"
    assert version == "9.9.33-52230"
    assert path == app
    assert appid == "123456"
    assert qua == "V1_WIN_NQ_9.9.33_52230_GW_B"
    # 垫片不再随 runtime 落盘：它由 start() 复制进实例目录并加进 PATH。
    assert not (app / "QQNT.dll").exists()


def test_prepare_runtime_downloads_when_missing(tmp_path: Path, monkeypatch):
    """A missing runtime is fetched from the project's release host.

    This replaces the old fallback to the machine's own QQ install. That fallback produced a runtime whose
    version followed whatever the machine had auto-updated to, while the injected DLL stays built for the
    locked version -- measured as a stable ntdll 0xc0000374 (heap corruption) crash once the local QQ had
    moved to 9.9.37-54395.
    """
    runtime = tmp_path / "runtime"
    shim = tmp_path / "shim.dll"
    shim.write_bytes(b"shim")

    def fake_download(target, *args, **kwargs):
        app = target / "versions" / "9.9.33-52230" / "resources" / "app"
        app.mkdir(parents=True, exist_ok=True)
        (app / "wrapper.node").write_bytes(b"wrapper")
        (app / "package.json").write_text('{"version": "9.9.33-52230"}', encoding="utf-8")
        (app / "major.node").write_bytes(b"QQAppId/123456\x00")
        (target / "QQ.exe").write_bytes(b"MZ")
        return True

    monkeypatch.setattr("qsick.headless.host_shim", lambda: shim)
    monkeypatch.setattr("qsick.runtime_fetch.ensure_runtime", fake_download)
    # The machine's own install must never be consulted. A detector that would happily answer stands in
    # for the real one; reaching it at all is the failure.

    manager = HeadlessManager(tmp_path / "headlessClient", runtime=runtime)
    path, version, _appid, _qua = manager.prepare_runtime()

    assert version == "9.9.33-52230"
    assert path.name == "app"


def test_prepare_runtime_never_falls_back_to_the_local_install(tmp_path: Path, monkeypatch):
    """When the download fails there is no second source: it raises instead of borrowing the machine's QQ.

    Borrowing is what made the runtime version depend on the operator's machine. Failing loudly sends the
    operator to "go online" or "drop the locked client here" rather than to a crash inside QQ later.
    """
    manager = HeadlessManager(tmp_path / "headlessClient", runtime=tmp_path / "runtime")
    monkeypatch.setattr("qsick.headless.host_shim", lambda: None)
    monkeypatch.setattr("qsick.runtime_fetch.ensure_runtime", lambda *a, **k: False)

    with pytest.raises(RuntimeError, match="runtime"):
        manager.prepare_runtime()


@pytest.fixture()
async def runtime(tmp_path: Path):
    config = RuntimeConfig(data_dir=str(tmp_path / "data"))
    config.transport.mode = "mock"
    config.headless_root = str(tmp_path / "headlessClient")
    instance = Runtime(config)
    await instance.start(serve=False)
    try:
        yield instance
    finally:
        await instance.stop()


async def test_accounts_page_and_api(runtime: Runtime):
    async with client(runtime) as web:
        # Startup may create a first pipe instance on a device that has none, and that
        # happens on a background task -- so the instance list can still be settling when
        # this test begins. Assert about the instances this test itself controls rather than
        # about the exact set at a random moment.
        first = await web.post("/api/accounts")
        assert first.status_code == 200
        first_id = first.json()["account"]["id"]
        assert first_id

        page = await web.get("/accounts")
        assert 'data-action="login"' in page.text

        listed = (await web.get("/api/accounts")).json()["accounts"]
        assert first_id in [item["id"] for item in listed]

        created_second = await web.post("/api/accounts")
        second_id = created_second.json()["account"]["id"]
        assert second_id != first_id
        # 删除限制已经取消：任何实例都能删，包括原先受保护的 C0 / Pipe。
        # 目录会移进 .trash，所以误删可恢复。
        removed = await web.delete(f"/api/accounts/{second_id}")
        assert removed.status_code == 200

        qr = await web.get(f"/api/accounts/{first_id}/qrcode")
        assert qr.status_code == 200
        assert qr.json()["account"]["phase"] == "stopped"
        assert 'data-action="login"' in page.text
        assert "login-panel" not in page.text
        assert "modal-backdrop" not in page.text
        assert 'id="qr-float"' in page.text
        assert 'id="qr-float-close"' not in page.text
        assert 'hx-trigger="every 5s"' in page.text
        assert "warnmsg" not in page.text

        login = await web.get(f"/accounts/{first_id}/login")
        assert login.status_code == 200
        assert 'risk-note' in login.text
        # A blank QR box must explain itself instead of showing empty space.
        assert 'id="qr-placeholder"' in login.text
        assert 'id="refresh"' in login.text
        assert 'risk-note' in login.text
        embedded = await web.get(f"/accounts/{first_id}/login?embedded=1")
        assert embedded.status_code == 200
        assert 'id="close-top"' not in embedded.text


async def test_account_table_shows_auto_relogin_switch_with_help(tmp_path):
    from qsick.headless import Instance
    from qsick.webui.app import templates
    account = Instance("C1", tmp_path).to_dict()
    html = templates.env.get_template("partials/accounts_table.html").render(accounts=[account])
    assert "data-auto-relogin" in html
    assert "自动重新登陆" in html
    # The help marker must carry the explanation as a tooltip.
    assert "help-dot" in html
    assert "自动结束它的子进程并重新启动" in html
    # Off by default.
    assert "checked" not in html.split("data-auto-relogin", 1)[0].rsplit("<input", 1)[-1]


async def test_kicked_status_is_labelled_not_generic_error(tmp_path):
    import json as _json
    from qsick.headless import Instance
    from qsick.webui.app import templates
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "status.json").write_text(_json.dumps({
        "phase": "kicked", "pid": 5, "updated_at": 9_999_999_999,
        "error": "账号在另一台设备登录", "security_kicked_type": 1,
    }), encoding="utf-8")
    account = Instance("C1", tmp_path).to_dict()
    html = templates.env.get_template("partials/accounts_table.html").render(accounts=[account])
    assert "被安全策略踢下线" in html
    assert "登录异常" not in html
