#!/usr/bin/env python3
"""QSICK plugin market — dependency-free HTTP service (stdlib only).

The server stores only what the owner supplies: repository URL, description, icon
and version. Stars/views/likes are counters managed here.

Run:  python3 app.py            (reads QSICK_MARKET_* environment variables)
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import secrets
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

BASE = Path(__file__).resolve().parent
PREFIX = "/QSICK/plugins-market"
MAX_BODY = 512 * 1024
SLUG = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS plugins (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    repo_url TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    icon_url TEXT NOT NULL DEFAULT '',
    version TEXT NOT NULL DEFAULT '',
    qsick_min TEXT NOT NULL DEFAULT '',
    qsick_max TEXT NOT NULL DEFAULT '',
    archive_url TEXT NOT NULL DEFAULT '',
    published_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    stars INTEGER NOT NULL DEFAULT 0,
    views INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS likes (
    plugin_id TEXT NOT NULL,
    voter TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (plugin_id, voter)
);
CREATE INDEX IF NOT EXISTS idx_plugins_name ON plugins(name);
"""

SORTS = {
    "published": "published_at",
    "name": "name",
    "stars": "stars",
    "views": "views",
    "updated": "updated_at",
    "likes": "likes",
    "match": "match",
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: str) -> sqlite3.Connection:
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    return db


def like_count(db: sqlite3.Connection, plugin_id: str) -> int:
    row = db.execute("SELECT COUNT(*) AS total FROM likes WHERE plugin_id = ?", (plugin_id,)).fetchone()
    return int(row["total"]) if row else 0


def parse_version(text: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in re.split(r"[.+-]", text.strip().lstrip("vV")):
        match = re.match(r"^(\d+)", chunk)
        parts.append(int(match.group(1)) if match else 0)
    return tuple(parts[:4]) or (0,)


def matches_client(entry: dict, client: str) -> bool:
    if not client:
        return True
    current = parse_version(client)
    low = parse_version(entry["qsick_min"]) if entry.get("qsick_min") else (0,)
    high = parse_version(entry["qsick_max"]) if entry.get("qsick_max") else (999999,)
    return low <= current <= high


def archive_for(repo: str, archive: str) -> str:
    if archive:
        return archive
    repo = repo.rstrip("/")
    if "github.com" in repo:
        return repo + "/archive/refs/heads/main.zip"
    if "gitee.com" in repo:
        return repo + "/repository/archive/main.zip"
    return ""


def entry_dict(db: sqlite3.Connection, row: sqlite3.Row, client: str = "") -> dict:
    entry = {
        "id": row["id"],
        "name": row["name"],
        "repo_url": row["repo_url"],
        "description": row["description"],
        "icon_url": row["icon_url"],
        "version": row["version"],
        "qsick_min": row["qsick_min"],
        "qsick_max": row["qsick_max"],
        "archive_url": row["archive_url"] or archive_for(row["repo_url"], ""),
        "published_at": row["published_at"],
        "updated_at": row["updated_at"],
        "stars": row["stars"],
        "views": row["views"],
        "likes": like_count(db, row["id"]),
    }
    entry["matches_client"] = matches_client(entry, client)
    return entry


class MarketServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], db_path: str, token: str) -> None:
        super().__init__(address, MarketHandler)
        self.db_path = db_path
        self.token = token


class MarketHandler(BaseHTTPRequestHandler):
    server: MarketServer
    server_version = "QSICKMarket/1.0"

    # ------------------------------------------------------------------ helpers
    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def _send(self, status: int, body: bytes, content_type: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Access-Control-Allow-Origin", "*")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: object) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"ok": False, "error": message})

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise ValueError("body too large")
        raw = self.rfile.read(length)
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("expected a JSON object")
        return payload

    def _admin(self) -> bool:
        expected = self.server.token
        if not expected:
            return False
        supplied = self.headers.get("X-Market-Token") or ""
        auth = self.headers.get("Authorization") or ""
        if auth.startswith("Bearer "):
            supplied = auth[7:]
        return secrets.compare_digest(supplied, expected)

    def _voter(self, payload: dict) -> str:
        voter = str(payload.get("voter") or "").strip()
        if voter:
            return hashlib.sha256(voter.encode("utf-8")).hexdigest()[:32]
        seed = f"{self.client_address[0]}|{self.headers.get('User-Agent', '')}"
        return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]

    # -------------------------------------------------------------------- GET
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if not path.startswith(PREFIX):
            if path == "/":
                self._send(302, b"", "text/plain", {"Location": PREFIX + "/"})
            else:
                self._error(404, "not found")
            return
        sub = path[len(PREFIX):] or "/"
        query = parse_qs(parsed.query)
        if sub in ("/", "/index.html"):
            html = (BASE / "index.html").read_text(encoding="utf-8").replace("__PREFIX__", PREFIX)
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            return
        if sub.startswith("/static/"):
            self._static(sub[len("/static/"):])
            return
        if sub == "/api/health":
            with connect(self.server.db_path) as db:
                total = db.execute("SELECT COUNT(*) AS n FROM plugins").fetchone()["n"]
            self._json(200, {"ok": True, "plugins": total, "time": now()})
            return
        if sub == "/api/plugins":
            self._list(query)
            return
        if sub == "/api/plugins/all":
            self._all(query)
            return
        match = re.match(r"^/api/plugins/([A-Za-z0-9._-]+)(/install)?$", sub)
        if match and not match.group(2):
            self._detail(match.group(1), bool(query.get("view")))
            return
        if match and match.group(2):
            self._install(match.group(1))
            return
        self._error(404, "not found")

    def _static(self, name: str) -> None:
        target = (BASE / "static" / name).resolve()
        if BASE not in target.parents or not target.is_file():
            self._error(404, "not found")
            return
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self._send(200, target.read_bytes(), content_type, {"Cache-Control": "public, max-age=300"})

    def _list(self, query: dict[str, list[str]]) -> None:
        needle = (query.get("q") or [""])[0].strip().lower()
        sort = (query.get("sort") or ["published"])[0]
        order = (query.get("order") or ["desc"])[0].lower()
        client = (query.get("client") or [""])[0].strip()
        try:
            page = max(1, int((query.get("page") or ["1"])[0]))
            per = min(100, max(1, int((query.get("per") or ["24"])[0])))
        except ValueError:
            self._error(400, "page/per must be integers")
            return
        if sort not in SORTS:
            self._error(400, f"sort must be one of {', '.join(SORTS)}")
            return
        if order not in ("asc", "desc"):
            self._error(400, "order must be asc or desc")
            return

        with connect(self.server.db_path) as db:
            rows = db.execute("SELECT * FROM plugins").fetchall()
            items = [entry_dict(db, row, client) for row in rows]
        if needle:
            items = [
                item
                for item in items
                if needle in item["name"].lower()
                or needle in item["description"].lower()
                or needle in item["id"].lower()
            ]
        reverse = order == "desc"
        if sort == "match":
            items.sort(key=lambda item: (item["matches_client"], item["stars"]), reverse=reverse)
        elif sort == "name":
            items.sort(key=lambda item: item["name"].lower(), reverse=reverse)
        elif sort == "likes":
            items.sort(key=lambda item: item["likes"], reverse=reverse)
        else:
            numeric = sort in ("stars", "views")
            items.sort(
                key=lambda item: item[SORTS[sort]] if item[SORTS[sort]] is not None else (0 if numeric else ""),
                reverse=reverse,
            )
        total = len(items)
        start = (page - 1) * per
        self._json(
            200,
            {
                "total": total,
                "page": page,
                "per": per,
                "pages": max(1, (total + per - 1) // per),
                "sort": sort,
                "order": order,
                "client": client,
                "items": items[start : start + per],
            },
        )

    def _all(self, query: dict[str, list[str]]) -> None:
        client = (query.get("client") or [""])[0].strip()
        with connect(self.server.db_path) as db:
            rows = db.execute("SELECT * FROM plugins ORDER BY published_at DESC").fetchall()
            items = [entry_dict(db, row, client) for row in rows]
        self._json(200, {"total": len(items), "items": items})

    def _detail(self, plugin_id: str, count_view: bool) -> None:
        with connect(self.server.db_path) as db:
            row = db.execute("SELECT * FROM plugins WHERE id = ?", (plugin_id,)).fetchone()
            if row is None:
                self._error(404, "plugin not found")
                return
            if count_view:
                db.execute("UPDATE plugins SET views = views + 1 WHERE id = ?", (plugin_id,))
                db.commit()
                row = db.execute("SELECT * FROM plugins WHERE id = ?", (plugin_id,)).fetchone()
            self._json(200, entry_dict(db, row))

    def _install(self, plugin_id: str) -> None:
        with connect(self.server.db_path) as db:
            row = db.execute("SELECT * FROM plugins WHERE id = ?", (plugin_id,)).fetchone()
            if row is None:
                self._error(404, "plugin not found")
                return
            entry = entry_dict(db, row)
        self._json(
            200,
            {
                "ok": True,
                "id": entry["id"],
                "version": entry["version"],
                "repo_url": entry["repo_url"],
                "archive_url": entry["archive_url"],
                "icon_url": entry["icon_url"],
            },
        )

    # ------------------------------------------------------------------- POST
    def do_POST(self) -> None:  # noqa: N802
        sub = self.path[len(PREFIX):] if self.path.startswith(PREFIX) else self.path
        sub = sub.split("?", 1)[0]
        try:
            payload = self._body()
        except (ValueError, json.JSONDecodeError) as exc:
            self._error(400, f"invalid body: {exc}")
            return
        if sub == "/api/plugins":
            if not self._admin():
                self._error(401, "admin token required")
                return
            self._upsert(payload)
            return
        match = re.match(r"^/api/plugins/([A-Za-z0-9._-]+)/like$", sub)
        if match:
            self._like(match.group(1), payload)
            return
        match = re.match(r"^/api/plugins/([A-Za-z0-9._-]+)/refresh$", sub)
        if match:
            if not self._admin():
                self._error(401, "admin token required")
                return
            self._refresh(match.group(1))
            return
        self._error(404, "not found")

    def do_PUT(self) -> None:  # noqa: N802
        sub = self.path[len(PREFIX):] if self.path.startswith(PREFIX) else self.path
        if sub.split("?", 1)[0] != "/api/plugins":
            self._error(404, "not found")
            return
        if not self._admin():
            self._error(401, "admin token required")
            return
        try:
            payload = self._body()
        except (ValueError, json.JSONDecodeError) as exc:
            self._error(400, f"invalid body: {exc}")
            return
        items = payload.get("items")
        if not isinstance(items, list):
            self._error(400, "items must be a list")
            return
        valid: list[dict] = []
        for item in items:
            entry = self._validate(item)
            if entry:
                valid.append(entry)
        with connect(self.server.db_path) as db:
            db.execute("DELETE FROM plugins")
            db.execute("DELETE FROM likes")
            for entry in valid:
                self._insert(db, entry)
            db.commit()
        self._json(200, {"ok": True, "count": len(valid)})

    def do_DELETE(self) -> None:  # noqa: N802
        sub = self.path[len(PREFIX):] if self.path.startswith(PREFIX) else self.path
        match = re.match(r"^/api/plugins/([A-Za-z0-9._-]+)$", sub.split("?", 1)[0])
        if not match:
            self._error(404, "not found")
            return
        if not self._admin():
            self._error(401, "admin token required")
            return
        with connect(self.server.db_path) as db:
            db.execute("DELETE FROM plugins WHERE id = ?", (match.group(1),))
            db.execute("DELETE FROM likes WHERE plugin_id = ?", (match.group(1),))
            db.commit()
        self._json(200, {"ok": True})

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Market-Token")
        self.end_headers()

    # ---------------------------------------------------------------- mutations
    def _validate(self, item: object, *, require_id: bool = True) -> dict | None:
        if not isinstance(item, dict):
            return None
        plugin_id = str(item.get("id") or "").strip()
        if require_id and not SLUG.match(plugin_id):
            return None
        repo = str(item.get("repo_url") or "").strip()
        if not repo.startswith(("https://github.com/", "https://gitee.com/", "http://github.com/", "http://gitee.com/")):
            return None
        name = str(item.get("name") or "").strip()[:100]
        if not name:
            return None
        instance = {
            "id": plugin_id,
            "name": name,
            "repo_url": repo[:300],
            "description": str(item.get("description") or "").strip()[:1000],
            "icon_url": str(item.get("icon_url") or "").strip()[:500],
            "version": str(item.get("version") or "").strip()[:40],
            "qsick_min": str(item.get("qsick_min") or "").strip()[:40],
            "qsick_max": str(item.get("qsick_max") or "").strip()[:40],
            "archive_url": str(item.get("archive_url") or "").strip()[:500],
            "stars": max(0, int(item.get("stars") or 0)),
        }
        return instance

    def _insert(self, db: sqlite3.Connection, entry: dict) -> None:
        stamp = now()
        db.execute(
            """
            INSERT INTO plugins (id, name, repo_url, description, icon_url, version,
                                 qsick_min, qsick_max, archive_url, published_at, updated_at, stars)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry["id"],
                entry["name"],
                entry["repo_url"],
                entry["description"],
                entry["icon_url"],
                entry["version"],
                entry["qsick_min"],
                entry["qsick_max"],
                entry["archive_url"],
                stamp,
                stamp,
                entry["stars"],
            ),
        )

    def _upsert(self, payload: dict) -> None:
        entry = self._validate(payload)
        if entry is None:
            self._error(400, "invalid plugin: id slug, name and github/gitee repo_url are required")
            return
        with connect(self.server.db_path) as db:
            existing = db.execute("SELECT id FROM plugins WHERE id = ?", (entry["id"],)).fetchone()
            if existing:
                db.execute(
                    """
                    UPDATE plugins SET name=?, repo_url=?, description=?, icon_url=?, version=?,
                        qsick_min=?, qsick_max=?, archive_url=?, stars=?, updated_at=?
                    WHERE id=?
                    """,
                    (
                        entry["name"],
                        entry["repo_url"],
                        entry["description"],
                        entry["icon_url"],
                        entry["version"],
                        entry["qsick_min"],
                        entry["qsick_max"],
                        entry["archive_url"],
                        entry["stars"],
                        now(),
                        entry["id"],
                    ),
                )
            else:
                self._insert(db, entry)
            db.commit()
        self._json(200, {"ok": True, "id": entry["id"]})

    def _like(self, plugin_id: str, payload: dict) -> None:
        voter = self._voter(payload)
        with connect(self.server.db_path) as db:
            if db.execute("SELECT 1 FROM plugins WHERE id = ?", (plugin_id,)).fetchone() is None:
                self._error(404, "plugin not found")
                return
            existing = db.execute(
                "SELECT 1 FROM likes WHERE plugin_id = ? AND voter = ?", (plugin_id, voter)
            ).fetchone()
            if existing:
                db.execute("DELETE FROM likes WHERE plugin_id = ? AND voter = ?", (plugin_id, voter))
                liked = False
            else:
                db.execute(
                    "INSERT INTO likes (plugin_id, voter, created_at) VALUES (?, ?, ?)",
                    (plugin_id, voter, now()),
                )
                liked = True
            db.commit()
            count = like_count(db, plugin_id)
        self._json(200, {"ok": True, "liked": liked, "likes": count})

    def _refresh(self, plugin_id: str) -> None:
        with connect(self.server.db_path) as db:
            row = db.execute("SELECT repo_url FROM plugins WHERE id = ?", (plugin_id,)).fetchone()
        if row is None:
            self._error(404, "plugin not found")
            return
        repo = row["repo_url"]
        api = None
        if "github.com" in repo:
            parts = [part for part in urlparse(repo).path.split("/") if part][:2]
            if len(parts) == 2:
                api = f"https://api.github.com/repos/{parts[0]}/{parts[1]}"
        elif "gitee.com" in repo:
            parts = [part for part in urlparse(repo).path.split("/") if part][:2]
            if len(parts) == 2:
                api = f"https://gitee.com/api/v5/repos/{parts[0]}/{parts[1]}"
        if not api:
            self._error(400, "unsupported repository host")
            return
        try:
            request = urllib.request.Request(api, headers={"User-Agent": "qsick-market/1.0"})
            with urllib.request.urlopen(request, timeout=10) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            self._error(502, f"upstream error: {exc}")
            return
        stars = int(data.get("stargazers_count") or data.get("stargazers") or 0)
        updated = data.get("pushed_at") or data.get("updated_at") or now()
        with connect(self.server.db_path) as db:
            db.execute("UPDATE plugins SET stars = ?, updated_at = ? WHERE id = ?", (stars, updated, plugin_id))
            db.commit()
        self._json(200, {"ok": True, "stars": stars, "updated_at": updated})


def main() -> int:
    host = os.environ.get("QSICK_MARKET_HOST", "127.0.0.1")
    port = int(os.environ.get("QSICK_MARKET_PORT", "8787"))
    db_path = os.environ.get("QSICK_MARKET_DB", str(BASE / "market.db"))
    token = os.environ.get("QSICK_MARKET_TOKEN", "")
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    server = MarketServer((host, port), db_path, token)
    print(f"qsick-market listening on http://{host}:{port}{PREFIX}/  db={db_path}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
