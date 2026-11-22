"""Client + installer for the remote QSICK plugin market.

The market stores metadata only; archives live on GitHub/Gitee. Installing means
downloading the archive, validating ``basic.json`` and moving the plugin into the
local plugins directory. Everything is bounded: size, entry count and paths.
"""

from __future__ import annotations

import io
import shutil
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from . import log as logmod
from .plugins.manifest import ManifestError, PluginManifest

log = logmod.get_logger("Market")

MAX_ARCHIVE = 20 * 1024 * 1024
MAX_ENTRIES = 2000
MAX_UNPACKED = 50 * 1024 * 1024


class MarketError(RuntimeError):
    pass


def _swap_scheme(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme == "https":
        return parsed._replace(scheme="http").geturl()
    if parsed.scheme == "http":
        return parsed._replace(scheme="https").geturl()
    return url


class MarketClient:
    """Small async client with an http/https fallback for misconfigured hosts."""

    def __init__(self, base_url: str, timeout: float = 8.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._working: str | None = None

    async def _request(self, path: str, *, params: dict[str, Any] | None = None, method: str = "GET", payload: dict | None = None) -> dict:
        candidates = [self._working] if self._working else []
        candidates.append(self.base_url)
        if self._working is None:
            candidates.append(_swap_scheme(self.base_url))
        last: Exception | None = None
        seen: set[str] = set()
        for base in candidates:
            if not base or base in seen:
                continue
            seen.add(base)
            url = f"{base}{path}"
            try:
                async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
                    response = await client.request(method, url, params=params, json=payload)
                if response.status_code >= 400:
                    raise MarketError(f"market returned {response.status_code}: {response.text[:160]}")
                data = response.json()
                if not isinstance(data, dict):
                    raise MarketError("market returned a non-object payload")
                self._working = base
                return data
            except (httpx.HTTPError, ValueError, MarketError) as exc:
                last = exc
                self._working = None
                continue
        raise MarketError(f"market unreachable: {last}")

    async def list_plugins(
        self,
        *,
        q: str = "",
        sort: str = "published",
        order: str = "desc",
        client: str = "",
        page: int = 1,
        per: int = 24,
    ) -> dict:
        return await self._request(
            "/api/plugins",
            params={"q": q, "sort": sort, "order": order, "client": client, "page": page, "per": per},
        )

    async def like(self, plugin_id: str, voter: str) -> dict:
        return await self._request(f"/api/plugins/{plugin_id}/like", method="POST", payload={"voter": voter})

    async def install_info(self, plugin_id: str) -> dict:
        return await self._request(f"/api/plugins/{plugin_id}/install")


async def download_archive(url: str, timeout: float = 30.0) -> bytes:
    if not url.startswith(("http://", "https://")):
        raise MarketError("archive URL must be http(s)")
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()
            length = int(response.headers.get("content-length") or 0)
            if length > MAX_ARCHIVE:
                raise MarketError(f"archive too large ({length} bytes)")
            data = response.content
    except httpx.HTTPError as exc:
        raise MarketError(f"download failed: {exc}") from exc
    if len(data) > MAX_ARCHIVE:
        raise MarketError("archive too large")
    return data


def _safe_members(archive: zipfile.ZipFile) -> list[tuple[zipfile.ZipInfo, str]]:
    members: list[tuple[zipfile.ZipInfo, str]] = []
    for info in archive.infolist():
        if info.is_dir():
            continue
        name = info.filename.replace("\\", "/")
        if name.startswith("__MACOSX/") or name.endswith(".DS_Store"):
            continue
        parts = [part for part in name.split("/") if part not in ("", ".")]
        if not parts or any(part == ".." for part in parts):
            raise MarketError(f"unsafe archive entry: {info.filename}")
        members.append((info, "/".join(parts)))
    if not members:
        raise MarketError("archive is empty")
    if len(members) > MAX_ENTRIES:
        raise MarketError("archive has too many files")
    if sum(info.file_size for info, _ in members) > MAX_UNPACKED:
        raise MarketError("archive expands too large")
    return members


def _strip_common_root(members: list[tuple[zipfile.ZipInfo, str]]) -> list[tuple[zipfile.ZipInfo, str]]:
    first = {name.split("/")[0] for _, name in members}
    if len(first) == 1 and all("/" in name for _, name in members):
        prefix = next(iter(first)) + "/"
        return [(info, name[len(prefix):]) for info, name in members]
    return members


def install_archive(data: bytes, plugins_root: Path, *, expected_id: str | None = None) -> str:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise MarketError("not a zip archive") from exc
    with archive:
        members = _strip_common_root(_safe_members(archive))
        names = {name for _, name in members}
        if "basic.json" not in names:
            raise MarketError("archive has no basic.json at its root")
        if expected_id:
            raw = archive.read(next(info for info, name in members if name == "basic.json"))
            import json

            try:
                manifest_id = str(json.loads(raw.decode("utf-8")).get("id", ""))
            except (UnicodeDecodeError, ValueError) as exc:
                raise MarketError("basic.json is not valid JSON") from exc
            if manifest_id != expected_id:
                raise MarketError(f"plugin id mismatch: archive has {manifest_id!r}, market says {expected_id!r}")

        staging = plugins_root / f".staging-{uuid.uuid4().hex}"
        staging.mkdir(parents=True, exist_ok=False)
        try:
            for info, name in members:
                target = staging / name
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, target.open("wb") as sink:
                    shutil.copyfileobj(source, sink, length=64 * 1024)
            try:
                manifest = PluginManifest.load(staging / "basic.json")
            except ManifestError as exc:
                raise MarketError(f"invalid plugin manifest: {exc}") from exc
            target = plugins_root / manifest.id
            if target.exists():
                trash = plugins_root / ".trash"
                trash.mkdir(parents=True, exist_ok=True)
                target.rename(trash / f"{manifest.id}-{int(time.time())}")
            staging.rename(target)
            log.info("plugin installed from market", id=manifest.id, path=str(target))
            return manifest.id
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
