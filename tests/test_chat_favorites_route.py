"""收藏表情 route: it must use the transport, not the OneBot registry.

The column was empty for a real reason: ``runtime.invoke("emoji.favorites")``
goes through the OneBot action registry, where that action does not exist, so it
returned "unknown action" every time. These tests pin the working path.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from qsick.config import RuntimeConfig
from qsick.transport.base import SendResult
from qsick.webui.app import create_app


def make_runtime(sent: SendResult, *, transport: str = "headless"):
    config = RuntimeConfig(data_dir="/tmp/qsick-favorites-route-unused")
    config.webui.chat_enabled = True
    return SimpleNamespace(
        config=config,
        store=SimpleNamespace(),
        self_id=3905548034,
        transport=SimpleNamespace(name=transport),
        # The registry path must never be used for this action.
        invoke=AsyncMock(return_value={"status": "failed", "message": "unknown action"}),
        send=AsyncMock(return_value=sent),
    )


@pytest.fixture()
async def web_factory():
    clients = []

    async def build(rt):
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(rt)), base_url="http://test")
        clients.append(client)
        client.rt = rt
        return client

    yield build
    for client in clients:
        await client.aclose()


async def test_favorites_read_through_the_transport(web_factory):
    rt = make_runtime(SendResult(ok=True, data={"emoticons": [
        {"res_id": "R1", "emoji_id": "0", "md5": "ABC", "desc": "笑",
         "url": "https://p.qpic.cn/x/0", "path": "", "thumb_path": ""},
        {"res_id": "", "url": "https://p.qpic.cn/y"},  # no res_id: unusable
    ]}))
    client = await web_factory(rt)
    response = await client.get("/api/chat/favorites")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert len(body["faces"]) == 1
    assert body["faces"][0]["res_id"] == "R1"
    # QQ prunes the local copies, so a remote URL is the fallback -- but it must
    # go through the same-origin proxy. Handing the browser QQ's CDN URL directly
    # makes the send path's fetch() fail with "TypeError: Failed to fetch"
    # because that CDN sends no CORS headers.
    url = body["faces"][0]["url"]
    assert url.startswith("/api/chat/remote-media?url=")
    assert "https%3A%2F%2Fp.qpic.cn%2Fx%2F0" in url
    rt.send.assert_awaited_once()
    rt.invoke.assert_not_awaited()


async def test_favorites_surfaces_the_host_error(web_factory):
    rt = make_runtime(SendResult.failure("QQ rejected the request", status=1501))
    client = await web_factory(rt)
    response = await client.get("/api/chat/favorites")
    assert response.status_code == 409
    assert "QQ rejected the request" in response.json()["error"]


async def test_favorites_requires_the_headless_transport(web_factory):
    rt = make_runtime(SendResult(ok=True, data={"emoticons": []}), transport="mock")
    client = await web_factory(rt)
    response = await client.get("/api/chat/favorites")
    assert response.status_code == 409
    rt.send.assert_not_awaited()


async def test_favorites_row_without_any_url_is_kept_but_blank(web_factory):
    rt = make_runtime(SendResult(ok=True, data={"emoticons": [
        {"res_id": "R9", "emoji_id": "3", "md5": "D", "path": "/nonexistent/file.jpg"},
    ]}))
    client = await web_factory(rt)
    body = (await client.get("/api/chat/favorites")).json()
    assert body["faces"][0]["res_id"] == "R9"
    # A path that no longer exists must not be served as a link.
    assert body["faces"][0]["url"] == ""
    assert body["faces"][0]["local"] is False

async def test_remote_media_proxy_refuses_anything_but_qq_cdn(web_factory):
    """服务端会主动出网，白名单就是攻击面本身。

    收藏表情没本地副本时要由服务端代取图片，这是一个 SSRF 入口：调用方能指定
    URL，服务端就去访问。所以只允许 QQ 自己的 CDN、只允许 https、不允许别的。
    """
    rt = make_runtime(SendResult(ok=True, data={"emoticons": []}))
    client = await web_factory(rt)

    async def probe(url):
        response = await client.get("/api/chat/remote-media", params={"url": url})
        return response.status_code

    assert await probe("") == 400                                    # 缺参数
    assert await probe("http://gchat.qpic.cn/x.png") == 400          # 非 https
    assert await probe("https://evil.example.com/x.png") == 403      # 非白名单
    assert await probe("https://qq.com.cn.evil.com/x.png") == 403    # 后缀伪装
    assert await probe("https://127.0.0.1/x.png") == 403             # 字面 IP
    assert await probe("https://169.254.169.254/latest/meta-data") == 403  # 云元数据
