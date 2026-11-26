"""Network profiles shown by the WebUI.

Profiles are persisted next to the runtime data. When no file exists they are
derived from the legacy ``onebot`` section, so an existing install still shows
its HTTP/WS/Reverse endpoints on the new page.

Each profile also picks the **chat protocol** it speaks (``protocols`` 下的目录名）。
协议决定消息怎么编码：纯 OneBot v11 只能表达基础黄脸，而
目前只有 ``onebotv11`` 一份实现。可选清单由
``protocols/enabled_protocol.json`` 维护，``maxStore`` 是存储协议，永远不会出现
在这个下拉框里。
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from .config import RuntimeConfig

FILE_NAME = "network_profiles.json"
#: 每个账户一个文件，形如 ``network_profiles/3905548034.json``。
#: SnowLuma 用 ``onebot_<uin>.json``；这里沿用同一思路，按账号分文件，避免一条配置被所有账号共用。
PROFILE_DIR = "network_profiles"
KINDS = ("http", "ws_client", "ws_server")

#: 默认实例名。历史配置（没有 instance 字段）都属于它。
DEFAULT_INSTANCE = "C0"


def instance_choices(config: RuntimeConfig) -> list[dict[str, str]]:
    """网络配置的实例候选。

    目录扫描与 ``HeadlessManager.instances()`` 同源（``headlessClient/C<n>``），
    但这里只读 ``instance.json`` 的展示信息，不启动、不碰 QQ 数据——
    网络配置页每次渲染都会调用它。
    """
    root = Path(str(getattr(config, "headless_root", "") or "")).expanduser()
    if not root.is_absolute():
        root = Path.cwd() / root
    choices: list[dict[str, str]] = [{"id": DEFAULT_INSTANCE, "label": DEFAULT_INSTANCE + " · 默认", "qq": ""}]
    if not root.is_dir():
        return choices
    try:
        entries = sorted(root.iterdir(), key=lambda item: item.name)
    except OSError:
        return choices
    seen = {DEFAULT_INSTANCE}
    for entry in entries:
        if not entry.is_dir() or not entry.name.startswith("C") or not entry.name[1:].isdigit():
            continue
        qq = ""
        meta = entry / "instance.json"
        if meta.is_file():
            try:
                payload = json.loads(meta.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    qq = str(payload.get("qq") or "")
            except (OSError, json.JSONDecodeError):
                qq = ""
        label = f"{entry.name} · {qq}" if qq else entry.name
        if entry.name in seen:
            # C0 已经作为默认项列过了，这里只补 QQ 号，不重复一行。
            choices[0] = {"id": DEFAULT_INSTANCE, "label": label + " · 默认", "qq": qq}
            continue
        seen.add(entry.name)
        choices.append({"id": entry.name, "label": label, "qq": qq})
    return choices


def instance_ids(config: RuntimeConfig) -> set[str]:
    """存在的实例名，用于校验配置归属的实例是否真实存在。"""
    return {item["id"] for item in instance_choices(config) if item["id"]}


def _valid_instance(config: RuntimeConfig, value: Any) -> str:
    """校验配置归属的实例名。

    空值回落到默认实例（``C0``）而不是"不属于任何实例"：每条网络配置都必须有
    主人，否则它收到的消息不知道该进哪个库、动作也不知道该由谁执行。
    未知实例名直接报错，不静默改成 C0——那会让用户以为配到了 C1，实际全在 C0。
    """
    identifier = str(value or "").strip()
    if not identifier:
        return DEFAULT_INSTANCE
    # An owner is an account (a QQ number) or, in a store written by an older build, an instance name. A QQ
    # number is not in `instance_ids` -- it is not an instance -- so validating against that set alone used to
    # reject every real account and push the profile back onto `C0`, which is exactly how one account's config
    # ended up active for all of them.
    if identifier.isdigit():
        return identifier
    known = instance_ids(config) | set(accounts_in_store(config))
    if known and identifier not in known:
        raise ValueError(f"未知账户/实例 {identifier!r}，可选：{', '.join(sorted(known))}")
    return identifier


#: 协议清单与注册表都在项目根的 ``protocols/`` 下；这里按文件路径读，避免
#: 为了一个下拉框把整层协议注册表 import 进运行时。
PROTOCOLS_ROOT = Path(__file__).resolve().parents[2] / "protocols"


def chat_protocols() -> list[dict[str, Any]]:
    """可选的聊天协议，供网络配置的下拉菜单使用。

    读不到清单时返回空列表：前端据此禁用下拉框，而不是给一个哑的默认值让
    用户以为选过了。
    """
    manifest = PROTOCOLS_ROOT / "enabled_protocol.json"
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    entries = payload.get("chat") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return []
    described: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        identifier = str(entry.get("id") or "").strip()
        if not identifier:
            continue
        described.append({
            "id": identifier,
            "name": str(entry.get("name") or identifier),
            "description": str(entry.get("description") or ""),
            "default": bool(entry.get("default")),
        })
    return described


def default_protocol() -> str:
    """默认聊天协议 id；清单不可用时返回空串。"""
    protocols = chat_protocols()
    for item in protocols:
        if item["default"]:
            return item["id"]
    return protocols[0]["id"] if protocols else ""


def _valid_protocol(value: object) -> str:
    """校验协议 id。非法值一律报错，不静默回落到默认值。

    静默回落会让用户以为选的是某个富协议，实际却在按纯 v11 发消息——表情
    依旧是对的 id、错的语义，问题会以"偶尔画错"的形式重现，极难查。
    """
    identifier = str(value or "").strip()
    known = {item["id"] for item in chat_protocols()}
    if not identifier:
        return default_protocol()
    if identifier == "maxStore":
        raise ValueError("maxStore 是存储协议，不能作为聊天协议")
    if known and identifier not in known:
        raise ValueError(f"未知协议 {identifier!r}，可选：{', '.join(sorted(known))}")
    return identifier


def _default_profiles(config: RuntimeConfig) -> list[dict[str, Any]]:
    onebot = config.onebot
    protocol = default_protocol()
    profiles = [
        {
            "id": "default-http",
            "kind": "http",
            "name": "默认 HTTP 服务器",
            "enabled": bool(onebot.enable_http),
            "host": onebot.host,
            "port": int(onebot.port),
            "path": onebot.http_path,
            "token": onebot.access_token,
            # Carry the flag over from the global config. Without it, a store derived from a config
            # that had SSE on would silently drop the event stream on the first save -- the checkbox
            # only becomes meaningful once it is actually stored, so the derived profile must state
            # what the old behaviour was.
            "enable_sse": bool(onebot.enable_sse),
            "protocol": protocol,
            "instance": DEFAULT_INSTANCE,
        },
        {
            "id": "default-ws",
            "kind": "ws_server",
            "name": "默认 WS 服务器",
            "enabled": bool(onebot.enable_ws),
            "host": onebot.host,
            "port": int(onebot.port),
            "path": onebot.ws_path,
            "token": onebot.access_token,
            "protocol": protocol,
            "instance": DEFAULT_INSTANCE,
        },
    ]
    for index, url in enumerate(onebot.reverse_ws_urls):
        profiles.append(
            {
                "id": f"default-client-{index}",
                "kind": "ws_client",
                "name": f"反向上游 {index + 1}",
                "enabled": True,
                "url": url,
                "protocol": protocol,
                "instance": DEFAULT_INSTANCE,
            }
        )
    return profiles


def load(config: RuntimeConfig) -> list[dict[str, Any]]:
    """Every profile, from every account.

    The union is what the rest of the runtime wants: it asks "which listeners should be up", and that question
    crosses accounts. The settings page uses `load_for` instead, so one account never shows another's config.
    """
    _migrate_legacy_store(config)
    directory = _profile_dir(config)
    if directory.is_dir():
        merged: list[dict[str, Any]] = []
        try:
            for account_file in sorted(directory.glob("*.json")):
                try:
                    raw = json.loads(account_file.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if not isinstance(raw, list):
                    continue
                for item in raw:
                    if not isinstance(item, dict) or item.get("kind") not in KINDS:
                        continue
                    entry = dict(item)
                    # The file name is authoritative for ownership: a profile cannot be filed under one account
                    # and claim another.
                    entry["instance"] = account_file.stem
                    if not str(entry.get("protocol") or "").strip():
                        entry["protocol"] = default_protocol()
                    merged.append(entry)
        except OSError:
            merged = []
        if merged:
            return merged

    path = config.path(FILE_NAME)
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                fallback = default_protocol()
                kept = [dict(item) for item in raw
                        if isinstance(item, dict) and item.get("kind") in KINDS]
                # 老配置没有 protocol 字段：补上默认值而不是留空，否则下发时
                # 会退化成"没有协议"，处理链在那里只能猜。
                for item in kept:
                    if not str(item.get("protocol") or "").strip():
                        item["protocol"] = fallback
                    # 老配置没有 account 字段：默认"不指定"，保持既有行为。
                    item["instance"] = str(item.get("instance") or item.get("account") or DEFAULT_INSTANCE)
                return kept
        except (OSError, json.JSONDecodeError):
            pass
    return _default_profiles(config)


def save(config: RuntimeConfig, profiles: list[dict[str, Any]]) -> None:
    """Write every profile into its own account's file.

    Rewriting all files on every save is deliberate: a profile can change owner (the dialog offers it), and a
    leftover copy in the old owner's file would be a second source of truth for the same id.
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in profiles:
        grouped.setdefault(owner_of(item), []).append(dict(item))
    directory = _profile_dir(config)
    directory.mkdir(parents=True, exist_ok=True)
    for owner, items in grouped.items():
        _write_account_file(config, owner, items)
    # Drop ids that no longer exist anywhere, and files that ended up empty.
    try:
        existing = {item.get("id") for item in profiles}
        for account_file in directory.glob("*.json"):
            owner = account_file.stem
            if owner in grouped:
                continue
            try:
                raw = json.loads(account_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(raw, list):
                continue
            kept = [item for item in raw if isinstance(item, dict) and item.get("id") in existing]
            if kept:
                _write_account_file(config, owner, kept)
            else:
                account_file.unlink(missing_ok=True)
    except OSError:
        pass


def _first_enabled(profiles: list[dict[str, Any]], kind: str) -> dict[str, Any] | None:
    return next((item for item in profiles if item.get("kind") == kind and item.get("enabled")), None)



def _profile_dir(config: RuntimeConfig) -> Path:
    return config.path(PROFILE_DIR)


def owner_of(profile: dict[str, Any]) -> str:
    """The account a profile belongs to.

    `instance` is the persisted field name (it predates the per-account model), and older stores wrote instance
    names such as `C0` there. Both spellings mean "owner"; an empty owner falls back to the default key so an
    ancient store is still readable.
    """
    return str(profile.get("instance") or profile.get("account") or DEFAULT_INSTANCE)


def load_for(config: RuntimeConfig, owner: str) -> list[dict[str, Any]]:
    """Profiles belonging to one account."""
    wanted = str(owner or "").strip()
    return [dict(item) for item in load(config) if owner_of(item) == wanted]


def store_exists(config: RuntimeConfig) -> bool:
    """True when the operator has a store on disk, in either layout.

    Needed because "no store" has a distinct meaning in the runtime: it means every profile was *derived* from
    the single global config, so it may be adopted by whichever account logs in. Checking only the legacy file
    name made that answer permanently "no store" once profiles moved into per-account files -- and then every
    account adopted every profile, which is the exact leak the per-account layout exists to prevent.
    """
    if config.path(FILE_NAME).is_file():
        return True
    directory = _profile_dir(config)
    try:
        return directory.is_dir() and any(directory.glob("*.json"))
    except OSError:
        return False


def accounts_in_store(config: RuntimeConfig) -> list[str]:
    """Every account that has a profile file, whether or not it is signed in right now."""
    directory = _profile_dir(config)
    if not directory.is_dir():
        return []
    try:
        return sorted(item.stem for item in directory.glob("*.json") if item.is_file())
    except OSError:
        return []


def _write_account_file(config: RuntimeConfig, owner: str, profiles: list[dict[str, Any]]) -> None:
    directory = _profile_dir(config)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{owner}.json"
    path.write_text(json.dumps(profiles, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _migrate_legacy_store(config: RuntimeConfig) -> bool:
    """Split the old single `network_profiles.json` into one file per owner. Returns True if it did anything.

    Kept as a real migration rather than a compatibility read path: leaving the old file in place while writing
    per-account files would make `load()` have to merge two sources, and every future question about "where did
    this profile come from" would have two answers.
    """
    legacy = config.path(FILE_NAME)
    if not legacy.is_file():
        return False
    try:
        raw = json.loads(legacy.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(raw, list):
        return False
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in raw:
        if not isinstance(item, dict) or item.get("kind") not in KINDS:
            continue
        grouped.setdefault(owner_of(item), []).append(dict(item))

    # A store written before accounts had their own files groups everything under `C0`. When exactly one real
    # account is present, those entries are that account's; when several are, `C0` stays its own owner rather
    # than guessing which account was meant.
    unowned = grouped.pop(DEFAULT_INSTANCE, [])
    real = [key for key in grouped if key != DEFAULT_INSTANCE]
    if unowned and len(real) == 1:
        target = real[0]
        # Match on id *and* endpoint: the legacy copy of a profile was written with a fresh id, so comparing ids
        # alone keeps a second identical entry that the operator then sees twice and cannot tell apart.
        known_ids = {item.get("id") for item in grouped[target]}
        known_endpoints = {
            (item.get("kind"), str(item.get("url") or f"{item.get('host')}:{item.get('port')}"))
            for item in grouped[target]
        }
        for item in unowned:
            if item.get("id") in known_ids:
                continue
            endpoint = (item.get("kind"), str(item.get("url") or f"{item.get('host')}:{item.get('port')}"))
            if endpoint in known_endpoints:
                continue
            grouped[target].append(item)
    elif unowned:
        grouped[DEFAULT_INSTANCE] = unowned
    try:
        for owner, items in grouped.items():
            path = _profile_dir(config) / f"{owner}.json"
            # Never overwrite: a per-account file written by a newer build wins over the legacy copy.
            if path.exists():
                continue
            _write_account_file(config, owner, items)
        target = legacy.with_suffix(".json.migrated")
        try:
            target.unlink()
        except OSError:
            pass
        legacy.rename(target)
    except OSError:
        return False
    return True


def profiles_for(config: RuntimeConfig, instance: str) -> list[dict[str, Any]]:
    """属于某个实例的全部网络配置。

    配置的归属写在 ``instance`` 字段上。历史配置没有这个字段，归默认实例 ——
    它们诞生于单实例时代，那时候也确实只有 C0。
    """
    wanted = str(instance or DEFAULT_INSTANCE)
    return [
        dict(profile) for profile in load(config)
        if str(profile.get("instance") or profile.get("account") or DEFAULT_INSTANCE) == wanted
    ]


def instances_in_use(config: RuntimeConfig) -> list[str]:
    """当前有网络配置的实例名。"""
    names: set[str] = set()
    for profile in load(config):
        names.add(str(profile.get("instance") or profile.get("account") or DEFAULT_INSTANCE))
    return sorted(names)


def apply_to_config(config: RuntimeConfig, profiles: list[dict[str, Any]]) -> tuple[dict[str, Any], list[str]]:
    """Map profiles back onto the single OneBot listener.

    The current runtime owns one HTTP/WS/SSE listener. HTTP and WS server
    profiles can therefore share a port; if they point at different ports the
    first enabled profile wins and a warning is returned.
    """
    warnings: list[str] = []
    http = _first_enabled(profiles, "http")
    ws = _first_enabled(profiles, "ws_server")
    onebot = config.onebot

    if http:
        onebot.enable_http = True
        onebot.host = str(http.get("host") or onebot.host)
        onebot.port = int(http.get("port") or onebot.port)
        onebot.http_path = str(http.get("path") or "/")
        onebot.access_token = str(http.get("token") or "")
    else:
        onebot.enable_http = False

    if ws:
        onebot.enable_ws = True
        if not http:
            onebot.host = str(ws.get("host") or onebot.host)
            onebot.port = int(ws.get("port") or onebot.port)
        elif str(ws.get("host") or "") != onebot.host or int(ws.get("port") or 0) != onebot.port:
            warnings.append("WS 服务器与 HTTP 服务器端口不同，当前内核只监听其中一个端口；已按 HTTP 配置应用。")
        onebot.ws_path = str(ws.get("path") or "/ws")
    elif http and http.get("enable_websocket"):
        # The http profile's own checkbox: accept WebSocket clients on the same listener. It was
        # stored and then never read, so ticking it did nothing at all.
        onebot.enable_ws = True
    else:
        onebot.enable_ws = False

    # SSE is served at the HTTP endpoint itself, not at a path of its own.
    #
    # OneBot v11 (go-cqhttp and NapCat both do this) puts the event stream on the same URL as the
    # action endpoint, separated by method: POST runs an action, GET streams events. A separate
    # `{path}/sse` is this project's invention, and it made one configured address answer two
    # different things depending on a field the operator never saw.
    #
    # The checkbox on the http profile decides *whether* SSE is offered. It used to be ignored --
    # `enable_sse = bool(http)` -- so SSE was always on for any http profile and the box had no
    # effect either way.
    if http and http.get("enable_sse", False):
        onebot.enable_sse = True
        onebot.sse_path = onebot.http_path
    else:
        onebot.enable_sse = False

    clients = [str(item.get("url")) for item in profiles if item.get("kind") == "ws_client" and item.get("enabled")]
    onebot.reverse_ws_urls = [url for url in clients if url.startswith(("ws://", "wss://"))]

    data = {
        "host": onebot.host,
        "port": onebot.port,
        "enable_http": onebot.enable_http,
        "enable_ws": onebot.enable_ws,
        "enable_sse": onebot.enable_sse,
        "http_path": onebot.http_path,
        "ws_path": onebot.ws_path,
        "access_token": onebot.access_token,
        "reverse_ws_urls": onebot.reverse_ws_urls,
    }
    return data, warnings


def upsert(profiles: list[dict[str, Any]], payload: dict[str, Any],
           config: RuntimeConfig | None = None) -> dict[str, Any]:
    kind = str(payload.get("kind") or "")
    if kind not in KINDS:
        raise ValueError("invalid profile kind")
    profile_id = str(payload.get("id") or "")
    default_names = {
        "http": "HTTP \u670d\u52a1\u5668",
        "ws_client": "WS \u5ba2\u6237\u7aef",
        "ws_server": "WS \u670d\u52a1\u5668",
    }
    item = {
        "id": profile_id or uuid.uuid4().hex[:12],
        "kind": kind,
        "name": str(payload.get("name") or default_names.get(kind, kind)),
        "enabled": bool(payload.get("enabled", True)),
        "protocol": _valid_protocol(payload.get("protocol")),
        # 配置归属的实例。动作不再需要 account 字段：它从哪条连接来，就由那个
        # 实例的处理器执行——归属在配置上写死一次，比每次请求都带一个可伪造的
        # 账号名更可靠。
        "instance": _valid_instance(config, payload.get("instance", payload.get("account")))
        if config is not None
        else str(payload.get("instance") or payload.get("account") or DEFAULT_INSTANCE).strip(),
        "updated_at": int(time.time()),
        # Report the bot's own messages as message_sent events. Off by default: feeding a bot its own
        # output back in is how loops start.
        "report_self_message": bool(payload.get("report_self_message", False)),
    }
    # Cap on one inbound WebSocket frame. Stored only when set, so an absent value means "transport
    # default" rather than a number someone has to keep in step with the code.
    max_payload = payload.get("max_payload_bytes")
    if max_payload not in (None, "", 0):
        try:
            item["max_payload_bytes"] = int(max_payload)
        except (TypeError, ValueError):
            raise ValueError("max_payload_bytes 必须是整数") from None
    if kind == "ws_client":
        item["url"] = str(payload.get("url") or "")
        # Reconnect cadence. A server never reconnects, so this field belongs here and nowhere else.
        reconnect = payload.get("reconnect_interval_ms")
        if reconnect not in (None, ""):
            try:
                item["reconnect_interval_ms"] = max(500, int(reconnect))
            except (TypeError, ValueError):
                raise ValueError("reconnect_interval_ms 必须是整数") from None
    else:
        item.update(
            {
                "host": str(payload.get("host") or "127.0.0.1"),
                "port": int(payload.get("port") or 3000),
                "path": str(payload.get("path") or ("/" if kind == "http" else "/ws")),
                "token": str(payload.get("token") or ""),
            }
        )
        # The two HTTP-only switches. They describe the HTTP listener, so storing them on a ws server
        # would be a setting nothing reads.
        if kind == "http":
            if "enable_websocket" in payload:
                item["enable_websocket"] = bool(payload.get("enable_websocket"))
            if "enable_sse" in payload:
                item["enable_sse"] = bool(payload.get("enable_sse"))
    for index, current in enumerate(profiles):
        if current.get("id") == item["id"]:
            profiles[index] = item
            return item
    profiles.append(item)
    return item


def remove(profiles: list[dict[str, Any]], profile_id: str) -> None:
    profiles[:] = [item for item in profiles if item.get("id") != profile_id]
