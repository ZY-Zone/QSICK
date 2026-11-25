"""URI handoff tests: local fixtures and fake HTTP only; no QQ or network."""
from __future__ import annotations

import asyncio
import io
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qsick.onebot import upload_source as sources
from qsick.onebot.actions.group_file.upload_group_file import UploadGroupFileAction
from qsick.onebot.registry import ActionError


@pytest.fixture(autouse=True)
def upload_temp(tmp_path, monkeypatch):
    monkeypatch.setenv("QSICK_TEMP_DIR", str(tmp_path / "temp"))


@pytest.mark.parametrize("uri,expected", [
    ("file:///C:/some%20folder/%E4%B8%AD.txt", "C:/some folder/中.txt"),
    ("file://C:/some%20folder/a.txt", "C:/some folder/a.txt"),
    ("file://localhost/C:/x.txt", "C:/x.txt"),
    ("file://server/share/a%20b.txt", "//server/share/a b.txt"),
    ("file:///tmp/a%2520b", "/tmp/a%20b"),
])
def test_file_uri_decoding(uri, expected):
    assert sources.file_uri_path(uri) == expected


def test_local_file_never_deleted(tmp_path):
    path = tmp_path / "中文 a.txt"
    path.write_bytes(b"hello")
    for ref in (str(path), path.as_uri()):
        result = sources.resolve_upload_source(ref)
        assert result.path == path.resolve()
        result.cleanup()
        assert path.read_bytes() == b"hello"


@pytest.mark.parametrize("ref", ["ftp://host/x", "data:text/plain;base64,YQ==", "base64://!!!", "base64://", "https:relative", "file:///x?q=1", "file:///x#fragment"])
def test_invalid_sources(ref):
    with pytest.raises(ValueError):
        sources.resolve_upload_source(ref)


def test_base64_bounded_and_owned(tmp_path):
    result = sources.resolve_upload_source("base64://aGVsbG8=", max_bytes=5)
    assert result.path.read_bytes() == b"hello"
    assert result.path.parent == (tmp_path / "temp" / "uploads").resolve()
    result.cleanup()
    assert not result.path.exists()
    with pytest.raises(ValueError):
        sources.resolve_upload_source("base64://aGVsbG8=", max_bytes=4)


class Response(io.BytesIO):
    def __init__(self, body=b"hello", length=None, url="https://example.test/file"):
        super().__init__(body)
        self.headers = {} if length is None else {"Content-Length": str(length)}
        self.url = url

    def geturl(self):
        return self.url

    def read(self, size=-1):
        assert 0 < size <= sources.CHUNK_BYTES
        return super().read(size)


class Opener:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def open(self, request, *, timeout):
        self.calls.append((request.full_url, timeout))
        return self.response


def test_http_stream_with_fake_opener():
    opener = Opener(Response())
    result = sources.resolve_upload_source("https://example.test/file", opener=opener, timeout=2)
    assert result.path.read_bytes() == b"hello"
    assert opener.calls == [("https://example.test/file", 2)]
    result.cleanup()


@pytest.mark.parametrize("response", [Response(length=100), Response(body=b"123456"), Response(body=b""), Response(length=4), Response(url="file:///secret")])
def test_bad_download_cleans_owned_partial(response, tmp_path):
    with pytest.raises(ValueError):
        sources.resolve_upload_source("https://example.test/file", opener=Opener(response), max_bytes=5)
    assert not list((tmp_path / "temp" / "uploads").iterdir())


def test_reject_non_http_redirect():
    with pytest.raises(ValueError):
        sources._HttpRedirects().redirect_request(None, None, 302, "", {}, "ftp://host/file")


def test_download_deadline(monkeypatch, tmp_path):
    ticks = iter([0, 3])
    monkeypatch.setattr(sources.time, "monotonic", lambda: next(ticks))
    with pytest.raises(TimeoutError):
        sources.resolve_upload_source("https://example.test/file", opener=Opener(Response()), timeout=2)
    assert not list((tmp_path / "temp" / "uploads").iterdir())


@pytest.mark.parametrize("alias", ["file", "file_uri", "file_path"])
def test_action_calls_shared_base_with_account_and_cleans(alias):
    seen = []

    async def call(op, params, *, timeout):
        path = Path(params["file_path"])
        assert path.read_bytes() == b"hello"
        assert params["account"] == "123"
        assert "file_uri" not in params
        assert params["name"] == "hello.txt"
        seen.append(path)
        return SimpleNamespace(ok=True, data={"file_id": "id"})

    ctx = SimpleNamespace(account="123", call=call)
    result = asyncio.run(UploadGroupFileAction().run(ctx, **{alias: "base64://aGVsbG8=", "name": "hello.txt"}))
    assert result == {"file_id": "id"}
    assert not seen[0].exists()


@pytest.mark.parametrize("failure", [TimeoutError("uncertain"), asyncio.CancelledError(), ActionError("upstream timeout")])
def test_uncertain_upload_retains_source(failure, tmp_path):
    seen = []

    async def call(op, params, *, timeout):
        seen.append(Path(params["file_path"]))
        raise failure

    with pytest.raises(type(failure)):
        asyncio.run(UploadGroupFileAction().run(SimpleNamespace(account="123", call=call), file_uri="base64://aGVsbG8="))
    assert seen[0].read_bytes() == b"hello"


def test_pending_success_retains_source_for_qq_reader():
    seen = []
    async def call(op, params, *, timeout):
        seen.append(Path(params["file_path"]))
        return SimpleNamespace(ok=True, data={"file_id": "", "file_id_pending": True})
    result = asyncio.run(UploadGroupFileAction().run(
        SimpleNamespace(account="123", call=call), file_uri="base64://aGVsbG8="))
    assert result["file_id_pending"] is True
    assert seen[0].read_bytes() == b"hello"


def test_invalid_action_does_not_invoke_transport():
    async def call(*args, **kwargs):
        pytest.fail("invalid URI must not reach payload")

    with pytest.raises(ActionError):
        asyncio.run(UploadGroupFileAction().run(SimpleNamespace(call=call), file_uri="data:text/plain,a"))
