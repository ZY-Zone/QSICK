"""The cache page's download button now goes through QSICK, into `<root>/downloads`.

The remote host refuses a `Referer` from the local page (`invalid url`), and `rel="noreferrer"` is not honoured by
Chrome for link navigation. Owning the request is the only reliable answer; it also stops the page from resolving an
rkey per image while rendering.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import pytest  # noqa: E402

from qsick import downloads  # noqa: E402


class TestNames:
    def test_separators_and_question_marks_cannot_reach_the_filesystem(self):
        assert downloads.safe_name('a/b:c*?.jpg', fallback="x") == "a_b_c__.jpg"

    def test_a_windows_device_name_is_prefixed(self):
        # Writing "CON.txt" on Windows does not create a file called that.
        assert downloads.safe_name("CON.txt", fallback="x") == "_CON.txt"
        assert downloads.safe_name("lpt3.log", fallback="x") == "_lpt3.log"

    def test_an_empty_name_falls_back(self):
        assert downloads.safe_name("   ", fallback="m_0.bin") == "m_0.bin"

    def test_a_very_long_name_keeps_its_extension(self):
        made = downloads.safe_name("x" * 300 + ".jpeg", fallback="x")
        assert len(made) <= 120 and made.endswith(".jpeg")

    def test_the_elements_own_name_wins(self):
        assert downloads.name_for(7, 0, "截图 1.jpg", "https://h/x.jpg") == "截图 1.jpg"

    def test_without_a_name_the_message_id_names_it(self):
        made = downloads.name_for(7, 0, "", "https://h/x.jpg")
        assert made.startswith("7_0_") and made.endswith(".jpg")


class TestContainment:
    def test_a_plain_name_resolves_inside(self, tmp_path: Path):
        directory = downloads.export_dir(tmp_path)
        assert downloads.resolve_under(directory, "a.jpg") == (directory / "a.jpg").resolve()

    def test_traversal_is_refused(self, tmp_path: Path):
        directory = downloads.export_dir(tmp_path)
        assert downloads.resolve_under(directory, "../secret.txt") is None
        assert downloads.resolve_under(directory, "..\\secret.txt") is None

    def test_an_absolute_path_is_refused(self, tmp_path: Path):
        directory = downloads.export_dir(tmp_path)
        assert downloads.resolve_under(directory, "C:/Windows/win.ini") is None

    def test_the_directory_itself_is_not_a_file(self, tmp_path: Path):
        directory = downloads.export_dir(tmp_path)
        assert downloads.resolve_under(directory, ".") is None


class _FakeResponse:
    """Stands in for the object `urllib` returns, in the two shapes the code uses."""

    def __init__(self, *, status=200, length=0, chunks=(), body=b"", raise_after=None):
        self.status = status
        self.headers = {"Content-Length": str(length)} if length else {}
        self._chunks = list(chunks)
        self._body = body
        self._raise_after = raise_after

    def read(self, size=-1):
        if self._raise_after is not None and not self._chunks:
            raise OSError(self._raise_after)
        if self._chunks:
            return self._chunks.pop(0)
        if self._body:
            body, self._body = self._body, b""
            return body
        return b""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _opener(response, *, captured=None, error=None):
    class _Opener:
        def open(self, request, timeout=None):
            if captured is not None:
                captured.append(request)
            if error is not None:
                raise error
            return response

    return _Opener()


class TestFetch:
    async def test_a_file_is_written_and_no_referer_is_sent(self, tmp_path: Path, monkeypatch):
        captured: list = []
        response = _FakeResponse(length=5, chunks=[b"hello", b""])
        monkeypatch.setattr(downloads.urllib.request, "build_opener",
                            lambda *a, **k: _opener(response, captured=captured))
        directory = downloads.export_dir(tmp_path)
        result = await downloads.save_remote("https://example.invalid/a.jpg", directory, "a.jpg")

        assert result["ok"] and result["size"] == 5
        assert (directory / "a.jpg").read_bytes() == b"hello"
        # 整个模块存在的理由：不能让 Referer 跟着请求出去
        assert "Referer" not in captured[0].headers

    async def test_a_non_200_is_reported_not_written(self, tmp_path: Path, monkeypatch):
        error = downloads.urllib.error.HTTPError(
            "https://example.invalid/a.jpg", 400, "Bad Request", {},
            _FakeResponse(body=b'{"retcode":-5503010,"retmsg":"invalid rkey"}'))
        monkeypatch.setattr(downloads.urllib.request, "build_opener",
                            lambda *a, **k: _opener(_FakeResponse(), error=error))
        directory = downloads.export_dir(tmp_path)
        result = await downloads.save_remote("https://example.invalid/a.jpg", directory, "a.jpg")

        assert result["ok"] is False
        assert "HTTP 400" in result["error"]
        # retcode 必须能被读出来，否则页面无法区分"票不对"与"这个文件号不是你的"
        assert "invalid rkey" in result["error"]
        assert not (directory / "a.jpg").exists()

    async def test_a_failure_does_not_leave_a_partial_file(self, tmp_path: Path, monkeypatch):
        response = _FakeResponse(chunks=[b"half"], raise_after="connection dropped")
        monkeypatch.setattr(downloads.urllib.request, "build_opener",
                            lambda *a, **k: _opener(response))
        directory = downloads.export_dir(tmp_path)
        result = await downloads.save_remote("https://example.invalid/a.jpg", directory, "a.jpg")

        assert result["ok"] is False
        assert list(directory.iterdir()) == [], "半成品不能以成品的样子留下"

    async def test_an_existing_file_of_the_same_size_is_reused(self, tmp_path: Path, monkeypatch):
        directory = downloads.export_dir(tmp_path)
        (directory / "a.jpg").write_bytes(b"hello")
        monkeypatch.setattr(downloads.urllib.request, "build_opener",
                            lambda *a, **k: _opener(_FakeResponse(length=5)))
        result = await downloads.save_remote("https://example.invalid/a.jpg", directory, "a.jpg")
        assert result["ok"] and result.get("reused") is True
