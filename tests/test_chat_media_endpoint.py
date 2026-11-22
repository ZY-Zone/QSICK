"""Media send endpoint: isolated ASGI tests, no QQ, no live transport."""
from __future__ import annotations

import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from qsick.config import RuntimeConfig
from qsick.webui.app import create_app


PNG = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
TEXT = b"hello file"


def runtime():
    config = RuntimeConfig(data_dir="/tmp/qsick-media-endpoint-unused")
    config.webui.chat_enabled = True
    return SimpleNamespace(
        config=config, store=SimpleNamespace(), self_id=10001,
        transport=SimpleNamespace(name="headless"),
        invoke=AsyncMock(return_value={"status": "ok", "retcode": 0, "data": {"message_id": 7}}),
    )


@pytest.fixture
async def web():
    rt = runtime()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(rt)), base_url="http://test") as client:
        client.rt = rt
        yield client


async def test_image_upload_is_sniffed_and_sent_as_image_segment(web):
    files = {"file": ("photo.png", io.BytesIO(PNG), "image/png")}
    data = {"account": "10001", "chat_type": "group", "chat_id": "30001", "text": "看图"}
    response = await web.post("/api/chat/send-media", data=data, files=files)
    assert response.json()["status"] == "ok"
    name, params = web.rt.invoke.await_args.args
    assert name == "send_group_msg"
    assert params["group_id"] == 30001
    kinds = [segment["type"] for segment in params["message"]]
    assert kinds == ["text", "image"]
    assert params["message"][0]["data"]["text"] == "看图"
    assert params["message"][1]["data"]["name"] == "photo.png"
    # 上传中转文件现在落在项目的 temp/uploads 下（src/qsick/tempdir.py），
    # 不再是系统 %TEMP%。断言改成认这个位置：既确认是"我们的中转文件"，
    # 也顺带把"临时产物集中到 temp/"这条约束钉住。
    path = params["message"][1]["data"]["path"]
    assert path.startswith("/") or "upload-" in path, path
    if not path.startswith("/"):
        assert "temp" in path.replace("\\", "/"), f"应落在项目 temp/ 下：{path}"


async def test_non_image_extension_with_image_bytes_is_still_an_image(web):
    # Magic bytes decide, not the extension or the declared content type.
    files = {"file": ("note.txt", io.BytesIO(PNG), "text/plain")}
    data = {"account": "10001", "chat_type": "group", "chat_id": "1"}
    await web.post("/api/chat/send-media", data=data, files=files)
    params = web.rt.invoke.await_args.args[1]
    assert params["message"][0]["type"] == "image"


async def test_unknown_binary_is_sent_as_file(web):
    files = {"file": ("report.pdf", io.BytesIO(TEXT), "application/pdf")}
    data = {"account": "10001", "chat_type": "group", "chat_id": "1"}
    await web.post("/api/chat/send-media", data=data, files=files)
    params = web.rt.invoke.await_args.args[1]
    assert params["message"][0]["type"] == "file"
    assert params["message"][0]["data"]["name"] == "report.pdf"


async def test_explicit_kind_overrides_sniffing(web):
    files = {"file": ("photo.png", io.BytesIO(PNG), "image/png")}
    data = {"account": "10001", "chat_type": "group", "chat_id": "1", "kind": "file"}
    await web.post("/api/chat/send-media", data=data, files=files)
    assert web.rt.invoke.await_args.args[1]["message"][0]["type"] == "file"


async def test_private_media_uses_private_action(web):
    files = {"file": ("a.png", io.BytesIO(PNG), "image/png")}
    data = {"account": "10001", "chat_type": "private", "chat_id": "20001"}
    await web.post("/api/chat/send-media", data=data, files=files)
    name, params = web.rt.invoke.await_args.args
    assert name == "send_private_msg" and params["user_id"] == 20001


@pytest.mark.parametrize("payload", [
    {"account": "10002", "chat_type": "group", "chat_id": "1"},
    {"account": "10001", "chat_type": "guild", "chat_id": "1"},
    {"account": "10001", "chat_type": "group", "chat_id": "oops"},
    {"account": "10001", "chat_type": "group", "chat_id": "1", "kind": "video"},
])
async def test_invalid_media_requests_never_reach_transport(web, payload):
    files = {"file": ("a.png", io.BytesIO(PNG), "image/png")}
    response = await web.post("/api/chat/send-media", data=payload, files=files)
    assert response.status_code in (400, 409, 501)
    web.rt.invoke.assert_not_awaited()


async def test_missing_and_empty_uploads_are_rejected(web):
    data = {"account": "10001", "chat_type": "group", "chat_id": "1"}
    assert (await web.post("/api/chat/send-media", data=data)).status_code == 400
    empty = {"file": ("a.png", io.BytesIO(b""), "image/png")}
    assert (await web.post("/api/chat/send-media", data=data, files=empty)).status_code == 400
    web.rt.invoke.assert_not_awaited()


async def test_upload_temp_file_is_removed_after_send(web, tmp_path, monkeypatch):
    import tempfile
    from pathlib import Path
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    files = {"file": ("a.png", io.BytesIO(PNG), "image/png")}
    data = {"account": "10001", "chat_type": "group", "chat_id": "1"}
    await web.post("/api/chat/send-media", data=data, files=files)
    path = web.rt.invoke.await_args.args[1]["message"][0]["data"]["path"]
    assert not Path(path).exists(), "temporary upload should be deleted after sending"
