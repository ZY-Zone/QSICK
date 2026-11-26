#!/usr/bin/env python3
"""QSICK 官网静态服务：挂在 NPS 的 /QSICK 路由后面。

- 只监听 127.0.0.1:8790（NPS local proxy 直连）
- 对外前缀 /QSICK，内部映射到站点根目录
- /QSICK/ 与 /QSICK 都可访问；/QSICK 会 301 到 /QSICK/
- 内置 gzip、ETag/304、安全响应头与 /QSICK/healthz
"""

from __future__ import annotations

import gzip
import hashlib
import json
import mimetypes
import os
import posixpath
import sys
import threading
from email.utils import formatdate
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

HOST = "127.0.0.1"
PORT = int(os.environ.get("QSICK_SITE_PORT", "8790"))
ROOT = os.path.realpath(os.environ.get("QSICK_SITE_ROOT", "/opt/qsick-site/site"))
PREFIX = os.environ.get("QSICK_SITE_PREFIX", "/QSICK").rstrip("/") or "/QSICK"

MIME_FIX = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".webmanifest": "application/manifest+json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".ttf": "font/ttf",
    ".txt": "text/plain; charset=utf-8",
    ".xml": "application/xml; charset=utf-8",
    ".map": "application/json; charset=utf-8",
}
COMPRESSIBLE = (".html", ".css", ".js", ".json", ".svg", ".txt", ".xml", ".webmanifest")

_gzip_cache: dict[tuple[str, int, int], bytes] = {}
_gzip_lock = threading.Lock()

mimetypes.init()


def guess_type(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in MIME_FIX:
        return MIME_FIX[ext]
    ctype = mimetypes.guess_type(path)[0]
    return ctype or "application/octet-stream"


def file_etag(st: os.stat_result) -> str:
    raw = f"{st.st_mtime_ns}-{st.st_size}".encode()
    return '"' + hashlib.sha1(raw).hexdigest()[:20] + '"'


class Handler(BaseHTTPRequestHandler):
    server_version = "qsick-site/1.0"

    # ---------- helpers ----------
    def _security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("Permissions-Policy", "geolocation=(), microphone=(), camera=()")

    def _send_plain(self, status: int, body: bytes, ctype: str, extra: list[tuple[str, str]] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self._security_headers()
        if extra:
            for key, value in extra:
                self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_plain(status, body, "application/json; charset=utf-8", [("Cache-Control", "no-store")])

    def _redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.MOVED_PERMANENTLY)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self._security_headers()
        self.end_headers()

    def _send_file(self, fs_path: str, url_path: str) -> None:
        try:
            st = os.stat(fs_path)
        except OSError:
            self._not_found(url_path)
            return
        etag = file_etag(st)
        if self.headers.get("If-None-Match") == etag:
            self.send_response(HTTPStatus.NOT_MODIFIED)
            self.send_header("ETag", etag)
            self.end_headers()
            return

        ext = os.path.splitext(fs_path)[1].lower()
        ctype = guess_type(fs_path)
        with open(fs_path, "rb") as fh:
            body = fh.read()

        gz_ok = (
            ext in COMPRESSIBLE
            and len(body) > 900
            and "gzip" in (self.headers.get("Accept-Encoding") or "").lower()
        )
        if gz_ok:
            key = (fs_path, st.st_mtime_ns, st.st_size)
            with _gzip_lock:
                cached = _gzip_cache.get(key)
            if cached is None:
                cached = gzip.compress(body, compresslevel=6, mtime=0)
                if len(_gzip_cache) > 256:
                    _gzip_cache.clear()
                with _gzip_lock:
                    _gzip_cache[key] = cached
            send_body = cached
        else:
            send_body = body

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        if gz_ok:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(send_body)))
        self.send_header("ETag", etag)
        self.send_header("Last-Modified", formatdate(st.st_mtime, usegmt=True))
        if ext == ".html":
            self.send_header("Cache-Control", "no-cache")
        elif ext in (".woff2", ".woff", ".ttf", ".webp", ".png", ".jpg", ".ico"):
            self.send_header("Cache-Control", "public, max-age=604800")
        else:
            self.send_header("Cache-Control", "public, max-age=600, must-revalidate")
        self._security_headers()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(send_body)

    def _not_found(self, url_path: str) -> None:
        page = os.path.join(ROOT, "404.html")
        try:
            with open(page, "rb") as fh:
                body = fh.read()
        except OSError:
            body = b"404 Not Found"
        self._send_plain(HTTPStatus.NOT_FOUND, body, "text/html; charset=utf-8", [("Cache-Control", "no-store")])

    # ---------- routing ----------
    def do_GET(self) -> None:  # noqa: N802
        self._handle()

    def do_HEAD(self) -> None:  # noqa: N802
        self._handle()

    def _handle(self) -> None:
        try:
            parsed = urlsplit(self.path)
            path = unquote(parsed.path)
            if not path.startswith("/"):
                path = "/" + path

            if path == PREFIX:
                self._redirect(PREFIX + "/" + (("?" + parsed.query) if parsed.query else ""))
                return

            if not path.startswith(PREFIX + "/"):
                # 直接访问本机端口时给个明确跳转
                self._redirect(PREFIX + "/")
                return

            rel = path[len(PREFIX) + 1:]

            if rel == "healthz":
                self._send_json(200, {"ok": True, "service": "qsick-site", "prefix": PREFIX})
                return

            rel = posixpath.normpath(rel).lstrip("/")
            if rel.startswith("..") or rel in (".", ""):
                if rel == ".":
                    rel = ""
                else:
                    self._not_found(path)
                    return

            fs_path = os.path.realpath(os.path.join(ROOT, rel))
            if not (fs_path == ROOT or fs_path.startswith(ROOT + os.sep)):
                self._not_found(path)
                return

            if os.path.isdir(fs_path):
                if not path.endswith("/"):
                    self._redirect(PREFIX + "/" + rel + "/")
                    return
                index_path = os.path.join(fs_path, "index.html")
                if os.path.isfile(index_path):
                    fs_path = index_path
                else:
                    self._not_found(path)
                    return

            if not os.path.isfile(fs_path):
                self._not_found(path)
                return

            self._send_file(fs_path, path)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # pragma: no cover - 兜底，避免线程崩溃日志刷屏
            sys.stderr.write(f"[qsick-site] error {self.path}: {exc}\n")
            try:
                self._send_plain(HTTPStatus.INTERNAL_SERVER_ERROR, b"500 Internal Server Error", "text/plain; charset=utf-8")
            except Exception:
                pass

    def log_message(self, fmt: str, *args) -> None:  # 安静一点，只留错误
        return


def main() -> None:
    if not os.path.isdir(ROOT):
        sys.stderr.write(f"site root not found: {ROOT}\n")
        raise SystemExit(2)
    mimetypes.add_type("font/woff2", ".woff2")
    mimetypes.add_type("image/webp", ".webp")
    mimetypes.add_type("application/manifest+json", ".webmanifest")
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    httpd.daemon_threads = True
    sys.stdout.write(f"qsick-site serving {ROOT} at http://{HOST}:{PORT}{PREFIX}/\n")
    sys.stdout.flush()
    httpd.serve_forever()


if __name__ == "__main__":
    main()
