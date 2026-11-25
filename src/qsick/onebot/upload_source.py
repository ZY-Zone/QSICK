"""Bounded URI-to-local handoff for group uploads; never owns caller files.

Milky specifies file://, http(s):// and base64://. data: is deliberately not
accepted. In-flight/uncertain uploads must retain their temporary source until
an operator clears the uploads cache, rather than race QQ's file reader.
"""
from __future__ import annotations

import base64
import binascii
import re
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from qsick import tempdir

MAX_BYTES = 100 * 1024 * 1024
DOWNLOAD_TIMEOUT = 120.0
CHUNK_BYTES = 64 * 1024


@dataclass(frozen=True)
class UploadSource:
    path: Path
    temporary: bool = False

    def cleanup(self) -> None:
        """Only unlink files created here, never the supplied local source."""
        if self.temporary:
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                pass  # A locked file can be cleared with the uploads cache later.


def file_uri_path(uri: str) -> str:
    """Decode once, preserving Windows drive letters and UNC authorities."""
    parts = urlsplit(uri)
    if parts.scheme.lower() != "file" or parts.query or parts.fragment:
        raise ValueError("file URI must not contain a query or fragment")
    authority = parts.netloc
    path = unquote(parts.path, errors="strict")
    if authority and authority.lower() != "localhost":
        if re.fullmatch(r"[A-Za-z]:", authority):
            path = authority + path  # legacy file://C:/path
        elif any(c in authority for c in "@:%\\"):
            raise ValueError("invalid file URI authority")
        else:
            path = "//" + authority + path
    if re.match(r"^/[A-Za-z]:[/\\]", path):
        path = path[1:]
    if not path or "\x00" in path:
        raise ValueError("invalid file URI path")
    return path


def _http_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise ValueError("upload download requires an explicit http(s) URL")
    if parts.username is not None or parts.password is not None:
        raise ValueError("upload URL credentials are not supported")


class _HttpRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> Any:
        _http_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download(url: str, handle: Any, *, opener: Any, max_bytes: int,
              timeout: float) -> None:
    _http_url(url)
    request = urllib.request.Request(url, headers={"Accept": "*/*", "User-Agent": "QSICK"})
    deadline = time.monotonic() + timeout
    with opener.open(request, timeout=timeout) as response:
        _http_url(response.geturl())
        declared = int(response.headers.get("Content-Length") or 0)
        if declared < 0 or declared > max_bytes:
            raise ValueError("upload source exceeds the size limit")
        size = 0
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError("upload source download timed out")
            chunk = response.read(min(CHUNK_BYTES, max_bytes - size + 1))
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                raise ValueError("upload source exceeds the size limit")
            handle.write(chunk)
        if not size:
            raise ValueError("upload source is empty")
        if declared and size != declared:
            raise ValueError("upload source download is incomplete")


def resolve_upload_source(reference: str, *, opener: Any = None,
                          max_bytes: int = MAX_BYTES,
                          timeout: float = DOWNLOAD_TIMEOUT) -> UploadSource:
    """Blocking preparation, run on a worker thread by the action.

    ``opener`` is injectable so tests never need the network. No implicit URL
    schemes, environment proxies, unbounded reads or user-selected temp names.
    """
    if not isinstance(reference, str) or not reference or "\x00" in reference:
        raise ValueError("file, file_uri or file_path must be a non-empty string")
    if max_bytes <= 0 or timeout <= 0:
        raise ValueError("upload limits must be positive")
    scheme = urlsplit(reference).scheme.lower() if not re.match(r"^[A-Za-z]:[/\\]", reference) else ""
    if scheme in {"http", "https", "base64"}:
        data = None
        if scheme == "base64":
            if not reference.startswith("base64://"):
                raise ValueError("expected base64://")
            encoded = reference[len("base64://"):]
            if len(encoded) > 4 * ((max_bytes + 2) // 3):
                raise ValueError("upload source exceeds the size limit")
            try:
                data = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise ValueError("invalid base64 upload source") from exc
            if not data or len(data) > max_bytes:
                raise ValueError("upload source is empty or exceeds the size limit")
        else:
            _http_url(reference)
        with tempdir.make_file(prefix="group-", category="uploads") as handle:
            source = UploadSource(Path(handle.name).resolve(), temporary=True)
            try:
                if data is not None:
                    handle.write(data)
                else:
                    if opener is None:
                        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _HttpRedirects())
                    _download(reference, handle, opener=opener, max_bytes=max_bytes, timeout=timeout)
            except BaseException:
                handle.close()
                source.cleanup()
                raise
        return source
    if scheme not in {"", "file"}:
        raise ValueError(f"unsupported upload source scheme: {scheme}")
    path = Path(file_uri_path(reference) if scheme == "file" else reference).resolve()
    if not path.is_file():
        raise ValueError("upload source must be a readable regular file")
    size = path.stat().st_size
    if not size or size > max_bytes:
        raise ValueError("upload source is empty or exceeds the size limit")
    with path.open("rb") as handle:
        handle.read(1)
    return UploadSource(path)
