"""Downloads fetched by QSICK itself, then handed over as a local file.

Why a proxy at all: `gchat.qpic.cn` validates `Referer`, and a page served from `127.0.0.1` is rejected with
`{"retcode":-5503014,"retmsg":"invalid url"}`. Measured: the same URL returns 200 without a Referer, 200 with a
`qzone` Referer, 400 with the local page's. `rel="noreferrer"` was the first attempt and Chrome does not honour it
for a plain link navigation.

So the fetch happens here, where the headers are ours to choose, and the browser is redirected to the saved file.
That also means the button works when the page is opened from another machine -- a local path would not.

Files land in `<QSICK root>/downloads`, named after the element, so a downloaded picture keeps its name.
"""
from __future__ import annotations

import asyncio
import hashlib
import pathlib
import re
import unicodedata
import urllib.error
import urllib.request
from typing import Any

EXPORT_DIR = "downloads"
#: Refuse anything larger than this. A message cache can hold arbitrarily large attachments, and a proxy that
#: writes them to disk without a limit is a way to fill the drive by accident.
MAX_BYTES = 512 * 1024 * 1024
DOWNLOAD_TIMEOUT = 120.0

#: Characters Windows refuses in a file name, plus the ones that break a URL.
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
#: Device names Windows still reserves, with or without an extension.
_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def export_dir(root: pathlib.Path | str) -> pathlib.Path:
    """`<QSICK root>/downloads`, created on demand."""
    directory = pathlib.Path(root) / EXPORT_DIR
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def safe_name(name: str, *, fallback: str) -> str:
    """A file name that is legal on Windows and safe in a URL path.

    Not a security boundary on its own -- `resolve_under` is -- but a name that cannot traverse or collide with a
    device is worth having before the file exists.
    """
    cleaned = unicodedata.normalize("NFC", str(name or "")).strip()
    cleaned = _UNSAFE.sub("_", cleaned).strip(". ")
    if not cleaned:
        return fallback
    stem = cleaned.rsplit(".", 1)[0].lower()
    if stem in _RESERVED:
        cleaned = "_" + cleaned
    # Long names are rejected by some filesystems; keep the extension visible.
    if len(cleaned) > 120:
        head, dot, tail = cleaned.rpartition(".")
        cleaned = (head[:110] + "." + tail) if dot and len(tail) <= 8 else cleaned[:120]
    return cleaned


def resolve_under(directory: pathlib.Path, name: str) -> pathlib.Path | None:
    """The file at `directory/name`, or None if that would escape `directory`.

    The check is on the resolved path, not the string: `..`, a symlink or a Windows short name all have to be
    resolved before the answer is knowable.
    """
    try:
        base = directory.resolve()
        target = (base / str(name)).resolve()
    except (OSError, ValueError):
        return None
    if target == base or base not in target.parents:
        return None
    return target


def extension_for(url: str, default: str) -> str:
    """The extension the URL suggests, when it is a plausible one."""
    path = str(url).split("?", 1)[0]
    tail = path.rsplit("/", 1)[-1]
    if "." in tail:
        candidate = tail.rsplit(".", 1)[-1].lower()
        if 1 <= len(candidate) <= 5 and candidate.isalnum():
            return "." + candidate
    return default


async def save_remote(url: str, directory: pathlib.Path, name: str, *,
                      timeout: float = DOWNLOAD_TIMEOUT) -> dict[str, Any]:
    """Fetch `url` into `directory/name` and report what happened.

    Returns `{"ok": bool, "path": Path|None, "size": int, "error": str}`.

    No `Referer` is sent, deliberately -- that is the whole reason this function exists. A file that is already
    there at the same size is reused, so clicking the same button twice does not re-fetch.

    Written with `urllib` on a worker thread rather than an async client: measured against this host, `httpx` got an
    empty body for every 400 while `urllib` got the JSON `{"retcode":...,"retmsg":...}`. That body is the only thing
    separating "the token is stale" from "this file id is not yours", so it is worth a thread.
    """
    target = resolve_under(directory, name)
    if target is None:
        return {"ok": False, "path": None, "size": 0, "error": "refusing a name outside the download directory"}

    def fetch() -> dict[str, Any]:
        request = urllib.request.Request(url, headers={
            # A browser-like agent: some endpoints answer differently to a bare client. No Referer -- see above.
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) QSICK",
            "Accept": "*/*",
        })
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=timeout) as response:
                declared = int(response.headers.get("Content-Length") or 0)
                if declared > MAX_BYTES:
                    return {"ok": False, "path": None, "size": 0, "error": f"too large: {declared} bytes"}
                if declared and target.is_file() and target.stat().st_size == declared:
                    return {"ok": True, "path": target, "size": declared, "error": "", "reused": True}

                written = 0
                partial = target.with_name(target.name + ".part")
                try:
                    with partial.open("wb") as handle:
                        while True:
                            chunk = response.read(64 * 1024)
                            if not chunk:
                                break
                            written += len(chunk)
                            if written > MAX_BYTES:
                                raise ValueError("exceeded the size limit")
                            handle.write(chunk)
                    # Rename last: a half-written file must never look finished.
                    partial.replace(target)
                except BaseException:
                    # A dropped connection used to leave `name.part` behind, so the next attempt found a file that
                    # looked like an in-progress download from a run that had already given up.
                    try:
                        partial.unlink(missing_ok=True)
                    except OSError:
                        pass
                    raise
                return {"ok": True, "path": target, "size": written, "error": "", "reused": False}
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read(400)
            except Exception:  # noqa: BLE001 - a body we cannot read must not hide the status
                body = b""
            detail = body.decode("utf-8", "replace").strip() or "(empty body)"
            return {"ok": False, "path": None, "size": 0, "error": f"HTTP {exc.code}: {detail[:240]}"}
        except Exception as exc:  # noqa: BLE001 - the caller turns this into a readable page
            return {"ok": False, "path": None, "size": 0, "error": f"{type(exc).__name__}: {exc}"}

    return await asyncio.to_thread(fetch)


def name_for(message_id: int, index: int, suggested: str, url: str) -> str:
    """The name to save under: the element's own name, else a stable one derived from the message.

    Collisions are broken by message id, so two different files that happen to share a name do not overwrite each
    other, while the same file keeps one name across clicks.
    """
    default_ext = extension_for(url, ".bin")
    stem = str(suggested or "").strip()
    if stem:
        return safe_name(stem, fallback=f"{message_id}_{index}{default_ext}")
    digest = hashlib.sha1(f"{message_id}:{index}".encode()).hexdigest()[:8]
    return safe_name(f"{message_id}_{index}_{digest}{default_ext}", fallback=f"{message_id}_{index}{default_ext}")
