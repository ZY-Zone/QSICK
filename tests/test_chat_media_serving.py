"""Received-media download, local path backfill and serving. No QQ, no network."""
from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from qsick.config import RuntimeConfig
from qsick.protocol.elements import Element
from qsick.protocol.message import Message
from qsick.storage import MemoryStore, SqliteStore
from qsick.webui.chat_api import _absolute_media_url
from qsick.webui.app import create_app

ACCOUNT = "10001"


@pytest.mark.parametrize("given", [
    "/download?appid=1407&fileid=x&spec=0",
    "//gchat.qpic.cn/a",
    "https://already/absolute",
])
def test_media_urls_go_through_the_same_origin_proxy(given):
    """QQ 的图片地址一律交给同源代理。

    以前这里把 ``/download?...`` 拼成 ``https://gchat.qpic.cn/download?...`` 直接
    给浏览器，但那个地址要轮换 rkey，裸访问必然失败（Network 里那些
    ``download?appid=... ERR_BLOCKED`` 就是它）。代理让浏览器只和本机通信。
    """
    proxied = _absolute_media_url(given)
    assert proxied.startswith("/api/chat/remote-media?url=")
    assert "gchat.qpic.cn" in proxied or "already" in proxied


def test_empty_media_url_stays_empty():
    assert _absolute_media_url("") == ""


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_media_path_backfill_reaches_the_element(backend, tmp_path):
    store = MemoryStore() if backend == "memory" else SqliteStore(tmp_path / "m.db")
    message = Message(message_id=1, seq=1, time=1, chat_type="group", chat_id=9, sender_uin=7,
                      account=ACCOUNT, elements=[Element("image", {"element_id": "el1", "url": "/download?x"})])
    store.put(message)
    assert store.set_media_path(1, "el1", "/tmp/pic.png") is True
    assert store.get(1).elements[0].data["local_path"] == "/tmp/pic.png"
    # Unknown element or message must not pretend to succeed.
    assert store.set_media_path(1, "other", "/tmp/x.png") is False
    assert store.set_media_path(2, "el1", "/tmp/x.png") is False
    store.close()


async def test_history_serves_local_file_instead_of_dead_url(tmp_path):
    config = RuntimeConfig(data_dir=str(tmp_path / "data"))
    config.webui.chat_enabled = True
    store = MemoryStore()
    # The file must actually exist: a path that was merely *recorded* has to fall
    # through to the next candidate, otherwise the UI shows a broken image and
    # the user has no way to trigger a re-download.
    local = tmp_path / "p.png"
    local.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    element = Element("image", {"element_id": "el1", "url": "/download?appid=1407&fileid=abc&spec=0",
                                "local_path": str(local), "width": 10, "height": 20})
    store.put(Message(message_id=1, seq=1, time=1, chat_type="group", chat_id=9, sender_uin=7,
                      account=ACCOUNT, elements=[element]))
    runtime = SimpleNamespace(config=config, store=store, self_id=10001,
                              transport=SimpleNamespace(name="headless"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(runtime)), base_url="http://t") as web:
        payload = (await web.get("/api/chat/history", params={
            "account": ACCOUNT, "chat_type": "group", "chat_id": "9"})).json()["messages"][0]["segments"][0]
        assert payload["local_path"] == str(local)
        assert payload["url"].startswith("/api/chat/media?path=")
        # The endpoint reports a missing file rather than inventing bytes.
        missing = await web.get("/api/chat/media", params={"path": str(tmp_path / "absent.png")})
        assert missing.status_code == 404


async def test_media_endpoint_streams_an_existing_file(tmp_path):
    config = RuntimeConfig(data_dir=str(tmp_path / "data"))
    config.webui.chat_enabled = True
    # The file has to live under a root the endpoint is allowed to read: the
    # route serves whatever path it is given, so it only accepts QQ's media tree
    # and the media cache. Putting the fixture elsewhere is exactly the traversal
    # this guard exists to stop.
    config.headless_root = str(tmp_path / "hosts")
    instance_data = tmp_path / "hosts" / "C0" / "data"
    instance_data.mkdir(parents=True)
    image = instance_data / "pic.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20)
    store = MemoryStore()
    runtime = SimpleNamespace(config=config, store=store, self_id=10001,
                              transport=SimpleNamespace(name="headless"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(runtime)), base_url="http://t") as web:
        response = await web.get("/api/chat/media", params={"path": str(image)})
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"
        assert response.content == image.read_bytes()
        assert (await web.get("/api/chat/media")).status_code == 400


async def test_media_endpoint_serves_an_extensionless_sticker_as_an_image(tmp_path):
    """收藏表情的本地副本常常没有扩展名——它仍必须报成 image/*。

    如果这里返回 ``application/octet-stream``，前端会把这个 blob 当成"文件"而
    不是图片，``picSubType`` 随之丢失，表情又变回一张普通图片。文件名是
    md5/res_id，扩展名本来就靠不住，所以类型要从魔数来。
    """
    config = RuntimeConfig(data_dir=str(tmp_path / "data"))
    config.webui.chat_enabled = True
    config.headless_root = str(tmp_path / "hosts")
    instance_data = tmp_path / "hosts" / "C0" / "data"
    instance_data.mkdir(parents=True)
    sticker = instance_data / "0123456789abcdef"
    sticker.write_bytes(b"GIF89a" + b"\x00" * 24)
    runtime = SimpleNamespace(config=config, store=MemoryStore(), self_id=10001,
                              transport=SimpleNamespace(name="headless"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(runtime)), base_url="http://t") as web:
        response = await web.get("/api/chat/media", params={"path": str(sticker)})
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/gif", response.headers["content-type"]
        assert response.content == sticker.read_bytes()
        # 非图片仍然不能被当成图片
        other = instance_data / "notes"
        other.write_bytes(b"just text")
        refused = await web.get("/api/chat/media", params={"path": str(other)})
        assert refused.headers["content-type"].startswith("application/octet-stream")


async def test_media_endpoint_refuses_paths_outside_the_allowed_roots(tmp_path):
    """任意路径读盘是真实的越权风险：只要求登录挡不住 ``../../``。

    这个路由把调用方给的字符串直接交给 FileResponse，所以必须校验它落在允许
    的根目录之内。符号链接与 ``..`` 都要解析后再比，否则前缀匹配会被骗过。
    """
    config = RuntimeConfig(data_dir=str(tmp_path / "data"))
    config.webui.chat_enabled = True
    config.headless_root = str(tmp_path / "hosts")
    (tmp_path / "hosts" / "C0" / "data").mkdir(parents=True)
    secret = tmp_path / "secret.txt"
    secret.write_text("do not serve me", encoding="utf-8")
    runtime = SimpleNamespace(config=config, store=MemoryStore(), self_id=10001,
                              transport=SimpleNamespace(name="headless"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(runtime)), base_url="http://t") as web:
        assert (await web.get("/api/chat/media", params={"path": str(secret)})).status_code == 403
        traversal = str(tmp_path / "hosts" / "C0" / "data" / ".." / ".." / ".." / "secret.txt")
        assert (await web.get("/api/chat/media", params={"path": traversal})).status_code == 403


async def test_animated_stickers_are_flagged_and_plain_pictures_are_not(tmp_path):
    config = RuntimeConfig(data_dir=str(tmp_path / "data"))
    config.webui.chat_enabled = True
    store = MemoryStore()
    store.put(Message(message_id=1, seq=1, time=1, chat_type="group", chat_id=9, sender_uin=7, account=ACCOUNT,
                      elements=[Element("image", {"url": "/download?a", "sub_type": 1, "summary": "[动画表情]"})]))
    store.put(Message(message_id=2, seq=2, time=2, chat_type="group", chat_id=9, sender_uin=7, account=ACCOUNT,
                      elements=[Element("image", {"url": "/download?b", "sub_type": 0})]))
    runtime = SimpleNamespace(config=config, store=store, self_id=10001,
                              transport=SimpleNamespace(name="headless"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(runtime)), base_url="http://t") as web:
        messages = (await web.get("/api/chat/history", params={
            "account": ACCOUNT, "chat_type": "group", "chat_id": "9"})).json()["messages"]
        assert messages[0]["segments"][0]["sticker"] is True
        assert messages[1]["segments"][0]["sticker"] is False


async def test_media_route_is_gated_like_other_chat_routes(tmp_path):
    """Reading files from disk must not be open to unauthenticated callers."""
    config = RuntimeConfig(data_dir=str(tmp_path / "data"))
    config.webui.chat_enabled = False
    store = MemoryStore()
    runtime = SimpleNamespace(config=config, store=store, self_id=10001,
                              transport=SimpleNamespace(name="headless"))
    target = tmp_path / "secret.png"
    target.write_bytes(b"\x89PNG\r\n\x1a\n")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(runtime)), base_url="http://t") as web:
        response = await web.get("/api/chat/media", params={"path": str(target)})
        assert response.status_code == 403
        assert response.content != target.read_bytes()


async def test_face_endpoint_serves_the_real_qq_png(tmp_path):
    """The chat UI must show QQ's own face art, not a Unicode approximation."""
    from qsick.webui.face_assets import face_image_path

    real = face_image_path("headlessClient", 14)
    if real is None:
        pytest.skip("no QQ face resources available on this machine")
    config = RuntimeConfig(data_dir=str(tmp_path / "data"))
    config.webui.chat_enabled = True
    config.headless_root = "headlessClient"
    store = MemoryStore()
    runtime = SimpleNamespace(config=config, store=store, self_id=10001,
                              transport=SimpleNamespace(name="headless"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(runtime)), base_url="http://t") as web:
        response = await web.get("/api/chat/face", params={"id": "14"})
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"
        assert response.content == real.read_bytes()
        assert len(response.content) > 100
        # Unknown and hostile ids are refused, never resolved to a file.
        assert (await web.get("/api/chat/face", params={"id": "99999"})).status_code == 404
        assert (await web.get("/api/chat/face", params={"id": "../config"})).status_code == 404


async def test_face_segment_points_at_the_real_face_route(tmp_path):
    config = RuntimeConfig(data_dir=str(tmp_path / "data"))
    config.webui.chat_enabled = True
    store = MemoryStore()
    store.put(Message(message_id=1, seq=1, time=1, chat_type="group", chat_id=9, sender_uin=7,
                      account=ACCOUNT, elements=[Element("face", {"id": "14"})]))
    runtime = SimpleNamespace(config=config, store=store, self_id=10001,
                              transport=SimpleNamespace(name="headless"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(runtime)), base_url="http://t") as web:
        segment = (await web.get("/api/chat/history", params={
            "account": ACCOUNT, "chat_type": "group", "chat_id": "9"})).json()["messages"][0]["segments"][0]
    assert segment["face_url"] == "/api/chat/face?id=14"
    assert segment["name"] == "微笑"


async def test_missing_face_reports_unavailable_instead_of_a_lookalike(tmp_path):
    config = RuntimeConfig(data_dir=str(tmp_path / "data"))
    config.webui.chat_enabled = True
    config.headless_root = "headlessClient"
    store = MemoryStore()
    store.put(Message(message_id=1, seq=1, time=1, chat_type="group", chat_id=9, sender_uin=7,
                      account=ACCOUNT, elements=[Element("face", {"id": "99999"})]))
    runtime = SimpleNamespace(config=config, store=store, self_id=10001,
                              transport=SimpleNamespace(name="headless"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(runtime)), base_url="http://t") as web:
        segment = (await web.get("/api/chat/history", params={
            "account": ACCOUNT, "chat_type": "group", "chat_id": "9"})).json()["messages"][0]["segments"][0]
    assert segment["face_available"] is False
    assert "face_url" not in segment


async def test_animated_face_is_flagged_for_the_client(tmp_path):
    from qsick.webui.face_assets import embedded_face_path
    animated = embedded_face_path("367")
    if animated is None:
        pytest.skip("animated face asset not present")
    config = RuntimeConfig(data_dir=str(tmp_path / "data"))
    config.webui.chat_enabled = True
    config.headless_root = "headlessClient"
    store = MemoryStore()
    store.put(Message(message_id=1, seq=1, time=1, chat_type="group", chat_id=9, sender_uin=7,
                      account=ACCOUNT, elements=[Element("face", {"id": "367"})]))
    runtime = SimpleNamespace(config=config, store=store, self_id=10001,
                              transport=SimpleNamespace(name="headless"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(runtime)), base_url="http://t") as web:
        segment = (await web.get("/api/chat/history", params={
            "account": ACCOUNT, "chat_type": "group", "chat_id": "9"})).json()["messages"][0]["segments"][0]
    assert segment["face_available"] is True
    assert segment["face_animated"] is True


async def test_embedded_library_survives_without_any_qq_instance(tmp_path):
    """Faces must come from the bundled library, not the user's QQ data dir."""
    from qsick.webui.face_assets import EMBEDDED_ROOT, face_image_path
    if not (EMBEDDED_ROOT / "14.png").is_file():
        # 表情图片不随仓库分发（见 .gitignore 与 tools/import_emotions.py）。
        pytest.skip("QQ 表情图片未导入；运行 tools/import_emotions.py 后可验证")
    path = face_image_path(tmp_path / "no-instances", 14)
    assert path is not None and path.name == "14.png"


async def test_face_catalogue_is_grouped_like_the_qq_client(tmp_path):
    config = RuntimeConfig(data_dir=str(tmp_path / "data"))
    config.webui.chat_enabled = True
    config.headless_root = "headlessClient"
    store = MemoryStore()
    runtime = SimpleNamespace(config=config, store=store, self_id=10001,
                              transport=SimpleNamespace(name="headless"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(runtime)), base_url="http://t") as web:
        payload = (await web.get("/api/chat/faces")).json()
    groups = {group["key"]: group for group in payload["groups"]}
    assert "normal" in groups and groups["normal"]["label"] == "小黄脸"
    assert "super" in groups and groups["super"]["label"] == "超级表情"
    # Only faces with real art are listed.
    assert len(groups["normal"]["faces"]) > 200
    assert len(groups["super"]["faces"]) > 100
    for group in payload["groups"]:
        for face in group["faces"]:
            assert face["id"]


@pytest.mark.parametrize("text,expected", [
    ("hello", [("text", "hello")]),
    ("[CQ:face,id=14]", [("face", "14")]),
    ("a[CQ:face,id=14]b", [("text", "a"), ("face", "14"), ("text", "b")]),
])
def test_composer_face_codes_become_face_segments(text, expected):
    from qsick.webui.chat_api import _encode_chat_parts
    segments = _encode_chat_parts(text)
    kinds = [(seg["type"], seg["data"].get("id") or seg["data"].get("text")) for seg in segments]
    assert kinds == expected

async def test_history_falls_back_to_source_path_when_local_path_is_absent(tmp_path):
    """QQ 常常只记录 ``source_path``，``local_path`` 是空的。

    这类消息以前一律显示成 [图片]，即使图片就在磁盘上——用户点「重新获取」
    还会再花一次网络往返。存在即用，没有才去下载。
    """
    config = RuntimeConfig(data_dir=str(tmp_path / "data"))
    config.webui.chat_enabled = True
    store = MemoryStore()
    on_disk = tmp_path / "from_source.jpg"
    on_disk.write_bytes(b"\xff\xd8\xff" + b"0" * 32)
    element = Element("image", {"element_id": "el9", "url": "/download?spec=0",
                                "local_path": "", "source_path": str(on_disk)})
    store.put(Message(message_id=9, seq=9, time=9, chat_type="group", chat_id=9, sender_uin=7,
                      account=ACCOUNT, elements=[element]))
    runtime = SimpleNamespace(config=config, store=store, self_id=10001,
                              transport=SimpleNamespace(name="headless"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(runtime)), base_url="http://t") as web:
        payload = (await web.get("/api/chat/history", params={
            "account": ACCOUNT, "chat_type": "group", "chat_id": "9"})).json()["messages"][0]["segments"][0]
        assert payload["local_path"] == str(on_disk)
        assert payload["url"].startswith("/api/chat/media?path=")
        assert not payload.get("media_pending")
