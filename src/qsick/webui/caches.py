"""What QSICK keeps on disk and in memory, how much room it takes, and how to throw it away.

## What counts as a cache

Something the app can rebuild or refetch, whose loss costs time rather than data you cannot get back. Message
history written by this run qualifies; the config, the installed plugins and the login password do not, and they are
not listed here at all -- a "clean everything" button that can delete the password is a trap.

## Accounting

Sizes are measured on the filesystem, not estimated from row counts: SQLite's `-wal` sidecar is frequently larger
than the database it belongs to (measured 13 MB against 106 MB here after a busy run), and a "cache: 106 MB" line
that ignores the 13 MB beside it is simply wrong.

Freed space is reported as *before minus after*, measured around the clear, rather than as a number the clearing
code promises. A DELETE without VACUUM leaves the pages allocated, so the promise and the disk can disagree; the
difference cannot.

## Deleting

Through the owning object where one exists (`store.clear()`, the log buffer, the auth store) rather than by
unlinking files behind the app's back. A SQLite file removed while a connection is open leaves the process writing
into a deleted inode -- the space does not come back until restart, which looks exactly like the button doing
nothing.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass(slots=True)
class CacheItem:
    """One clearable thing."""

    id: str
    label: str
    detail: str
    #: "disk" or "memory" -- shown in the table, because it decides whether the number survives a restart.
    where: str
    paths: tuple[Path, ...] = ()
    #: Called to empty it. May return anything; the freed figure comes from measuring, not from this.
    clear: Callable[[], Any] | None = None


def tree_bytes(path: Path) -> int:
    """Total size of a file or of everything under a directory; 0 when it is not there."""
    try:
        if path.is_file():
            return path.stat().st_size
        if not path.is_dir():
            return 0
    except OSError:
        return 0
    total = 0
    for child in path.rglob("*"):
        try:
            if child.is_file():
                total += child.stat().st_size
        except OSError:
            # A file can vanish between rglob() and stat(); that is a zero, not an error worth surfacing.
            continue
    return total


def sqlite_family(db: Path) -> tuple[Path, ...]:
    """A SQLite database plus the sidecars holding its not-yet-checkpointed pages."""
    return (db, db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm"))


def _remove(paths: tuple[Path, ...]) -> None:
    for path in paths:
        try:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            elif path.exists():
                path.unlink()
        except OSError:
            continue


def collect(runtime: Any) -> list[CacheItem]:
    """Every cache this runtime owns."""
    from .. import log as logmod

    config = runtime.config
    items: list[CacheItem] = []

    # ---- message stores ---------------------------------------------------------------------------------------
    def clear_messages() -> None:
        try:
            runtime.store.clear()
        except Exception:  # noqa: BLE001 - a store that cannot clear shows up as an unchanged size
            pass

    items.append(CacheItem(
        id="messages",
        label="消息缓存库",
        detail="当前实例缓存的消息，消息页读的就是它",
        where="disk",
        paths=sqlite_family(config.path("messages.db")),
        clear=clear_messages,
    ))

    instances_root = config.path("instances")
    try:
        instance_dbs = sorted(instances_root.glob("*/messages.db"))
    except OSError:
        instance_dbs = []
    if instance_dbs:
        def clear_instances() -> None:
            for name in runtime.instances.names():
                context = runtime.instances.get(name)
                store = getattr(context, "store", None)
                if store is None:
                    continue
                try:
                    store.clear()
                except Exception:  # noqa: BLE001
                    continue

        items.append(CacheItem(
            id="instances",
            label="各账号原始消息库",
            detail=f"{len(instance_dbs)} 个账号的原始消息，各库独立",
            where="disk",
            paths=tuple(path for db in instance_dbs for path in sqlite_family(db)),
            clear=clear_instances,
        ))

    # ---- media ------------------------------------------------------------------------------------------------
    items.append(CacheItem(
        id="media",
        label="媒体文件缓存",
        detail="下载到本地的图片、语音与文件",
        where="disk",
        paths=(config.path("media-cache"),),
    ))

    # ---- memory -----------------------------------------------------------------------------------------------
    items.append(CacheItem(
        id="logs",
        label="日志缓冲区",
        detail="内存里保留的最近日志，不影响日志文件",
        where="memory",
        clear=logmod.clear_buffer,
    ))

    # ---- webui auth history -----------------------------------------------------------------------------------
    auth_path = config.path("webui", "auth.json")

    def clear_logins() -> None:
        store = getattr(runtime, "auth_store", None)
        if store is None:
            return
        store.logins.clear()
        store.save()

    if auth_path.exists():
        items.append(CacheItem(
            id="logins",
            label="WebUI 登录记录",
            detail="时间与来源 IP 的历史，不动密码本身",
            where="disk",
            paths=(auth_path,),
            clear=clear_logins,
        ))

    return items


def size_of(item: CacheItem) -> int:
    """Bytes this item occupies right now."""
    if item.id == "logs":
        from .. import log as logmod

        return logmod.buffer_bytes()
    if item.id == "logins":
        # The file also holds the password hash, which is not cache; count the records' share only.
        return 0 if not item.paths else _logins_bytes(item.paths[0])
    return sum(tree_bytes(path) for path in item.paths)


def _logins_bytes(auth_path: Path) -> int:
    try:
        import json

        data = json.loads(auth_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    logins = data.get("logins") or []
    try:
        # Measure the encoded history on its own, without the password record next to it.
        return len(json.dumps(logins, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        return 0


def measure(items: list[CacheItem]) -> list[dict[str, Any]]:
    """Rows for the page, largest first."""
    rows = [
        {
            "id": item.id,
            "label": item.label,
            "detail": item.detail,
            "where": item.where,
            "where_label": "磁盘" if item.where == "disk" else "内存",
            "bytes": size_of(item),
        }
        for item in items
    ]
    for row in rows:
        row["human"] = human_size(row["bytes"])
    rows.sort(key=lambda row: (row["bytes"], row["id"]), reverse=True)
    return rows


def clear(items: list[CacheItem], item_id: str) -> dict[str, Any]:
    """Clear one cache, or all of them. Freed space is measured before and after."""
    targets = items if item_id == "all" else [item for item in items if item.id == item_id]
    if not targets:
        return {"ok": False, "error": f"没有名为 {item_id} 的缓存项"}

    before = {item.id: size_of(item) for item in targets}
    for item in targets:
        try:
            if item.clear is not None:
                item.clear()
            elif item.paths:
                _remove(item.paths)
        except Exception:  # noqa: BLE001 - one item failing must not abort the rest of "clear all"
            continue
    after = {item.id: size_of(item) for item in targets}

    freed = sum(max(0, before[key] - after[key]) for key in before)
    return {
        "ok": True,
        "freed": freed,
        "freed_human": human_size(freed),
        "cleared": [item.id for item in targets],
    }


def human_size(size: int) -> str:
    """1024-based, two significant units. Digits after the point are noise at KB scale."""
    value = float(max(0, int(size)))
    if value < 1024:
        return f"{value:.0f} B"
    for unit in ("KB", "MB", "GB"):
        value /= 1024.0
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "KB" else f"{value:.1f} {unit}"
    return f"{value:.1f} GB"
