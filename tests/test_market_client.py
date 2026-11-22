from __future__ import annotations

import io
import json
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import httpx
import pytest

from qsick.config import RuntimeConfig
from qsick.runtime import Runtime
from qsick.webui.app import create_app


def plugin_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "demo-main/basic.json",
            json.dumps(
                {
                    "id": "demo",
                    "name": "Demo",
                    "version": "1.0.0",
                    "api": 1,
                    "entry": "plugin.py:DemoPlugin",
                    "tier": "standard",
                    "permissions": ["events.read"],
                }
            ),
        )
        archive.writestr("demo-main/plugin.py", "class DemoPlugin:\n    pass\n")
    return buffer.getvalue()


ARCHIVE = plugin_zip()


class MarketHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence test output
        pass

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        base = f"http://127.0.0.1:{self.server.server_port}"
        if path == "/api/plugins":
            self._json(
                {
                    "total": 1,
                    "page": 1,
                    "pages": 1,
                    "items": [
                        {
                            "id": "demo",
                            "name": "Demo",
                            "repo_url": "https://github.com/example/demo",
                            "description": "demo plugin",
                            "icon_url": "",
                            "version": "1.0.0",
                            "published_at": "2026-01-01T00:00:00+00:00",
                            "updated_at": "2026-01-02T00:00:00+00:00",
                            "stars": 3,
                            "views": 2,
                            "likes": 1,
                            "matches_client": True,
                            "archive_url": base + "/demo.zip",
                        }
                    ],
                }
            )
            return
        if path == "/api/plugins/demo/install":
            self._json({"ok": True, "id": "demo", "archive_url": base + "/demo.zip", "version": "1.0.0"})
            return
        if path == "/demo.zip":
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(len(ARCHIVE)))
            self.end_headers()
            self.wfile.write(ARCHIVE)
            return
        self._json({"ok": False, "error": "not found"}, 404)

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/plugins/demo/like":
            self._json({"ok": True, "liked": True, "likes": 2})
            return
        self._json({"ok": False, "error": "not found"}, 404)


@pytest.fixture()
def market_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), MarketHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture()
async def runtime(tmp_path: Path, market_server: str):
    config = RuntimeConfig(data_dir=str(tmp_path / "data"))
    config.transport.mode = "mock"
    config.plugins.dirs = [str(tmp_path / "plugins")]
    config.plugins.watch = False
    config.plugins.market_url = market_server
    config.plugins.market_timeout = 3.0
    instance = Runtime(config)
    await instance.start(serve=False)
    try:
        yield instance
    finally:
        await instance.stop()


async def test_market_page_lists_remote_plugins(runtime: Runtime):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(runtime)), base_url="http://ui") as web:
        response = await web.get("/plugins/market")
    assert response.status_code == 200
    assert "Demo" in response.text
    assert "安装 / 更新" in response.text


async def test_install_downloads_and_loads_plugin(runtime: Runtime):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(runtime)), base_url="http://ui") as web:
        response = await web.post("/plugins/market/demo/install", follow_redirects=False)
    assert response.status_code == 303
    from urllib.parse import unquote

    assert "已安装" in unquote(response.headers["location"])
    target = Path(runtime.config.plugins.dirs[0]) / "demo"
    assert (target / "basic.json").is_file()
    assert "demo" in runtime.plugins._loaded


async def test_like_proxies_to_market(runtime: Runtime):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(runtime)), base_url="http://ui") as web:
        response = await web.post("/plugins/market/demo/like", follow_redirects=False)
    assert response.status_code == 303
