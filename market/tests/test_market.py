from __future__ import annotations

import http.cookiejar
import io
import json
import sys
import threading
import urllib.error
import urllib.request
import urllib.parse
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import PREFIX, MarketServer, password_hash  # noqa: E402

TOKEN = "test-token"
ADMIN_PASSWORD = "test-admin-password"


@pytest.fixture()
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("QSICK_MARKET_ADMIN_HASH", password_hash(ADMIN_PASSWORD))
    monkeypatch.setenv("QSICK_MARKET_ADMIN_SECRET", "test-admin-secret")
    instance = MarketServer(("127.0.0.1", 0), str(tmp_path / "market.db"), TOKEN)
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{instance.server_port}{PREFIX}"
    finally:
        instance.shutdown()
        instance.server_close()


def call(
    base: str,
    path: str,
    method: str = "GET",
    payload: dict | None = None,
    token: str | None = None,
    opener: urllib.request.OpenerDirector | None = None,
):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(base + path, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("X-Market-Token", token)
    try:
        target = opener.open(request, timeout=10) if opener else urllib.request.urlopen(request, timeout=10)
        with target as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def admin_login(base: str):
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    status, body = call(base, "/api/admin/login", "POST", {"password": ADMIN_PASSWORD}, opener=opener)
    assert status == 200, body
    return opener


def plugin(plugin_id: str = "guard", **overrides) -> dict:
    data = {
        "id": plugin_id,
        "name": "Guard",
        "repo_url": "https://github.com/example/qsick-guard",
        "description": "white/black list",
        "icon_url": "",
        "version": "1.0.0",
        "qsick_min": "0.2.0",
        "qsick_max": "0.4.0",
    }
    data.update(overrides)
    return data


def test_index_and_health(server: str):
    with urllib.request.urlopen(server + "/", timeout=10) as response:
        html = response.read().decode("utf-8")
    assert "QSICK 插件市场" in html
    status, body = call(server, "/api/health")
    assert status == 200 and body["plugins"] == 0


def test_admin_required(server: str):
    status, body = call(server, "/api/plugins", "POST", plugin())
    assert status == 401
    status, body = call(server, "/api/plugins", "POST", plugin(), token="wrong")
    assert status == 401


def test_push_search_sort_and_install(server: str):
    status, _ = call(server, "/api/plugins", "POST", plugin(), token=TOKEN)
    assert status == 200
    call(server, "/api/plugins", "POST", plugin("alpha", name="Alpha", stars=5, version="1.2.0"), token=TOKEN)
    call(server, "/api/plugins", "POST", plugin("zeta", name="Zeta", stars=1), token=TOKEN)

    status, body = call(server, "/api/plugins?per=10")
    assert status == 200 and body["total"] == 3

    status, body = call(server, "/api/plugins?q=alp")
    assert body["total"] == 1 and body["items"][0]["id"] == "alpha"

    status, body = call(server, "/api/plugins?sort=name&order=asc")
    assert [item["id"] for item in body["items"]] == ["alpha", "guard", "zeta"]

    status, body = call(server, "/api/plugins?sort=stars&order=desc")
    assert body["items"][0]["id"] == "alpha"
    assert body["items"][0]["stars"] == 5

    status, body = call(server, "/api/plugins?client=0.3.0&sort=match&order=desc")
    assert body["items"][0]["matches_client"] is True

    status, body = call(server, "/api/plugins?client=9.9.9")
    assert body["items"][0]["matches_client"] is False

    status, body = call(server, "/api/plugins/guard/install")
    assert status == 200
    assert body["archive_url"].endswith("/archive/refs/heads/main.zip")

    status, body = call(server, "/api/plugins/guard?view=1")
    assert body["views"] == 1


def test_like_toggles(server: str):
    call(server, "/api/plugins", "POST", plugin(), token=TOKEN)
    status, body = call(server, "/api/plugins/guard/like", "POST", {"voter": "tester"})
    assert status == 200 and body["liked"] is True and body["likes"] == 1
    status, body = call(server, "/api/plugins/guard/like", "POST", {"voter": "tester"})
    assert body["liked"] is False and body["likes"] == 0


def test_full_push_replaces_all(server: str):
    call(server, "/api/plugins", "POST", plugin(), token=TOKEN)
    status, body = call(
        server,
        "/api/plugins",
        "PUT",
        {"items": [plugin("one", name="One"), plugin("two", name="Two")]},
        token=TOKEN,
    )
    assert status == 200 and body["count"] == 2
    status, body = call(server, "/api/plugins/all")
    assert body["total"] == 2


def test_invalid_payload_rejected(server: str):
    bad = plugin(repo_url="https://example.com/not-a-repo")
    status, _ = call(server, "/api/plugins", "POST", bad, token=TOKEN)
    assert status == 400


def _plugin_zip(manifest: dict, extra: dict[str, bytes] | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("basic.json", json.dumps(manifest, ensure_ascii=False))
        for name, data in (extra or {}).items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _multipart(fields: dict[str, str], filename: str, data: bytes):
    boundary = "----qsick-market-test"
    chunks: list[bytes] = []
    for key, value in fields.items():
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
        )
    chunks.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: application/zip\r\n\r\n".encode()
        + data
        + b"\r\n"
    )
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _raw_call(base: str, path: str, body: bytes, content_type: str, token: str | None = None):
    request = urllib.request.Request(base + path, data=body, method="POST")
    request.add_header("Content-Type", content_type)
    if token:
        request.add_header("X-Market-Token", token)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_admin_login_and_pages(server: str):
    status, _ = call(server, "/api/admin/plugins")
    assert status == 401
    status, _ = call(server, "/api/admin/login", "POST", {"password": "wrong"})
    assert status == 401
    opener = admin_login(server)
    status, body = call(server, "/api/admin/plugins", opener=opener)
    assert status == 200 and body["total"] == 0
    with urllib.request.urlopen(server + "/admin", timeout=10) as response:
        html = response.read().decode("utf-8")
    assert "市场管理" in html and "插件管理" in html


def test_upload_plugin_archive(server: str):
    manifest = {
        "id": "uploaded",
        "name": "Uploaded",
        "version": "1.2.3",
        "description": "from zip",
    }
    archive_data = _plugin_zip(manifest, {"plugin.py": b"VALUE = 1\n"})
    body, content_type = _multipart({"repo_url": "https://github.com/example/uploaded"}, "uploaded.zip", archive_data)

    status, payload = _raw_call(server, "/api/plugins/upload", body, content_type)
    assert status == 200, payload
    plugin = payload["plugin"]
    assert payload["pending"] is True
    assert plugin["id"] == "uploaded"
    assert plugin["version"] == "1.2.3"
    assert plugin["status"] == "pending"
    assert plugin["archive_size"] == len(archive_data)
    assert len(plugin["archive_sha256"]) == 64

    # 公开市场在审核通过前看不到、也装不了。
    status, _ = call(server, "/api/plugins/uploaded/install")
    assert status == 404
    status, body = call(server, "/api/plugins?per=10")
    assert body["total"] == 0

    opener = admin_login(server)
    status, body = call(server, "/api/admin/plugins", opener=opener)
    assert status == 200 and body["total"] == 1 and body["items"][0]["status"] == "pending"

    status, body = call(server, "/api/admin/plugins/uploaded/approve", "POST", opener=opener)
    assert status == 200 and body["plugin"]["status"] == "approved"

    status, body_json = call(server, "/api/plugins/uploaded/install")
    assert status == 200
    archive_url = body_json["archive_url"]
    assert archive_url.startswith("http://127.0.0.1:") and archive_url.endswith(".zip")
    with urllib.request.urlopen(archive_url, timeout=10) as response:
        assert response.read() == archive_data

    status, body = call(
        server,
        "/api/admin/plugins/uploaded",
        "PATCH",
        {"name": "Uploaded Edited", "description": "edited"},
        opener=opener,
    )
    assert status == 200 and body["plugin"]["name"] == "Uploaded Edited"

    status, _ = call(server, "/api/admin/plugins/uploaded/unpublish", "POST", opener=opener)
    assert status == 200
    status, _ = call(server, "/api/plugins/uploaded/install")
    assert status == 404
    status, body = call(server, "/api/plugins?per=10")
    assert body["total"] == 0

    status, _ = call(server, "/api/admin/plugins/uploaded/delete", "POST", opener=opener)
    assert status == 200
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(archive_url, timeout=10)
    assert exc.value.code == 404


def test_upload_rejects_bad_archive(server: str):
    body, content_type = _multipart({}, "bad.zip", b"not a zip")
    status, _ = _raw_call(server, "/api/plugins/upload", body, content_type)
    assert status == 400

    empty_zip = _plugin_zip({"name": "No Id"})
    body, content_type = _multipart({}, "bad.zip", empty_zip)
    status, _ = _raw_call(server, "/api/plugins/upload", body, content_type)
    assert status == 400
