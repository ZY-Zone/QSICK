from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import PREFIX, MarketServer  # noqa: E402

TOKEN = "test-token"


@pytest.fixture()
def server(tmp_path: Path):
    instance = MarketServer(("127.0.0.1", 0), str(tmp_path / "market.db"), TOKEN)
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{instance.server_port}{PREFIX}"
    finally:
        instance.shutdown()
        instance.server_close()


def call(base: str, path: str, method: str = "GET", payload: dict | None = None, token: str | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(base + path, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("X-Market-Token", token)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


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
