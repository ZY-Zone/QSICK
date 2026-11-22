"""Configuration: one JSON file, environment overrides, sane defaults."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any


#: The port the WebUI listens on unless configured otherwise.
#:
#: Defined once and imported everywhere else. It used to be a literal in seven places, including two call sites
#: that start a headless instance -- so an instance could be told to report events to 5754 while the WebUI was
#: listening on some other port, and every event it produced was lost.
DEFAULT_WEBUI_PORT = 5754


class ConfigError(RuntimeError):
    pass


@dataclass(slots=True)
class OneBotConfig:
    host: str = "127.0.0.1"
    port: int = 3000
    access_token: str = ""
    # Paths are fixed; two transports cannot share one path.
    http_path: str = "/"
    ws_path: str = "/ws"
    ws_reverse_path: str = "/ws/reverse"
    sse_path: str = "/sse"
    enable_http: bool = True
    enable_ws: bool = True
    enable_sse: bool = True
    # OneBot reverse WebSocket: QSICK dials these URLs and acts as a client.
    reverse_ws_urls: list[str] = field(default_factory=list)
    reverse_reconnect_seconds: float = 5.0
    # Drop duplicate message events (QQ re-delivery / receive+sent echo).
    dedupe_seconds: float = 120.0
    dedupe_max: int = 4096
    # Serialise send actions per chat so ordering is preserved.
    serialize_sends: bool = True
    # TTL for read-only caches (group list, friend list); 0 disables.
    cache_seconds: float = 30.0
    # 出站节流：给敏感动作（群/成员列表）与消息发送设最小间隔并加抖动。
    # 关掉它不会让功能变强，只会让请求密度变回"脚本式"——那正是风控最敏感的
    # 特征。测试里关掉是为了不必真的等 30 秒。
    throttle: bool = True


@dataclass(slots=True)
class WebUIConfig:
    host: str = "127.0.0.1"
    port: int = DEFAULT_WEBUI_PORT
    access_token: str = ""
    log_buffer: int = 2000
    # GUI chat layer; off by default because it exposes account actions.
    chat_enabled: bool = False


@dataclass(slots=True)
class TransportConfig:
    """``mock`` runs a synthetic QQ; ``pipe`` attaches to the native payload."""

    mode: str = "mock"
    # Named-pipe prefix. The payload creates ``<prefix>.<pid>.ctl`` / ``.evt``.
    pipe_prefix: str = r"\\.\pipe\qsick"
    payload_path: str = ""
    connect_timeout: float = 5.0
    reply_timeout: float = 30.0
    ack_timeout: float = 5.0
    # Attach to an already-hooked process before trying to inject.
    attach_only: bool = False
    # Pipe authentication: "random" (per-session token, default), "fixed", or "none".
    pipe_auth: str = "random"
    pipe_token: str = ""
    session_dir: str = ""
    # Loopback port of the C0 headless host (see headless_host.js).
    headless_port: int = 9100


@dataclass(slots=True)
class PluginConfig:
    dirs: list[str] = field(default_factory=lambda: ["plugins"])
    enabled: bool = True
    # Plugins declaring a tier above this are refused at load time.
    max_tier: str = "core"
    watch: bool = True
    # Plugin market: metadata lives on the server, archives on GitHub/Gitee.
    market_url: str = "http://sickcc.online/QSICK/plugins-market"
    market_timeout: float = 8.0
    # Installing runs third-party code; requires the WebUI to be authenticated.
    allow_remote_install: bool = True


@dataclass(slots=True)
class LogConfig:
    level: str = "INFO"
    file: str = ""
    json: bool = False
    # Even at INFO, these categories are shown.
    always_show: list[str] = field(default_factory=lambda: ["Core", "OneBot"])


@dataclass(slots=True)
class StorageConfig:
    """``sqlite`` persists message history; ``memory`` is for tests only."""

    backend: str = "sqlite"
    path: str = ""


@dataclass(slots=True)
class RotationConfig:
    """Message store retention; 0 disables the limit."""

    # 0 means keep forever.
    max_messages: int = 0
    max_days: int = 0


@dataclass(slots=True)
class RuntimeConfig:
    data_dir: str = "data"
    onebot: OneBotConfig = field(default_factory=OneBotConfig)
    webui: WebUIConfig = field(default_factory=WebUIConfig)
    transport: TransportConfig = field(default_factory=TransportConfig)
    plugins: PluginConfig = field(default_factory=PluginConfig)
    log: LogConfig = field(default_factory=LogConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    # Root directory holding C0, C1 ... headless client instances.
    headless_root: str = "headlessClient"
    # Whether QSICK manages instances at all: it will honour each instance's own
    # "随 QSICK 启动" switch, and create the first pipe instance on a device that has none.
    #
    # Note what this is NOT: it does not mean "start every instance". Turning it on starts
    # only the instances whose auto_start is set; with none set, nothing is launched. The
    # old name meant "start C0 automatically", which is why the default flips here -- the
    # new behaviour is safe by default, so shipping it enabled is right.
    headless_auto_start: bool = True
    # pipe 的自启动：开关 + 要拉起的那个 session 的 tmp_id。
    #
    # 和 ``headless_auto_start`` 分开，是因为它们的对象不同。那个管的是"随启动拉起哪些无头实例"，
    # 这个是"随启动拉起哪一个 pipe session"——用户需要显式指定是哪一个，所以还要存 id。
    #
    # 默认关闭：拉起一个完整 QQ 客户端要 1 GB 左右内存，没被要求就不该发生。
    pipe_auto_start: bool = False
    # 空串表示"还没选过"，此时开关即使打开也不会拉起任何东西——比猜一个 session 安全。
    pipe_auto_start_session: str = ""
    rotation: RotationConfig = field(default_factory=RotationConfig)
    # Forward "noise" events (typing, input status) to OneBot clients.
    forward_noise: bool = False
    # Attach to the newest QQ process when several are running.
    account_hint: str = ""
    # 这份配置是从哪个文件读来的。WebUI 保存设置时必须写回**同一个文件**：
    # 曾经因为只认 ``QSICK_CONFIG``/``qsick.json``，用 ``--config other.json``
    # 启动后一旦保存设置，端口就被写进了另一份配置，把正在用的那份改坏。
    source_path: str = ""

    def path(self, *parts: str) -> Path:
        root = Path(self.data_dir).expanduser()
        if not root.is_absolute():
            root = Path.cwd() / root
        p = root.joinpath(*parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p


_NESTED: dict[str, type] = {
    "onebot": OneBotConfig,
    "webui": WebUIConfig,
    "transport": TransportConfig,
    "plugins": PluginConfig,
    "log": LogConfig,
    "rotation": RotationConfig,
    "storage": StorageConfig,
}


def _coerce(cls: type, raw: Any) -> Any:
    """Recursively build a dataclass from a plain dict, rejecting unknown keys."""
    if not is_dataclass(cls):
        return raw
    if raw is None:
        return cls()
    if not isinstance(raw, dict):
        raise ConfigError(f"{cls.__name__} must be an object, got {type(raw).__name__}")
    known = {f.name for f in fields(cls)}
    unknown = set(raw) - known
    if unknown:
        raise ConfigError(f"unknown keys in {cls.__name__}: {', '.join(sorted(unknown))}")
    kwargs: dict[str, Any] = {}
    for name, value in raw.items():
        nested = _NESTED.get(name)
        if nested is not None and cls is RuntimeConfig:
            kwargs[name] = _coerce(nested, value)
        elif name == "dirs" and isinstance(value, list):
            kwargs[name] = [str(item) for item in value]
        else:
            kwargs[name] = value
    return cls(**kwargs)


def load_config(path: str | os.PathLike[str] | None = None) -> RuntimeConfig:
    """Load ``qsick.json`` (or ``QSICK_CONFIG``), then layer env overrides."""
    raw: dict[str, Any] = {}
    candidate = Path(path or os.environ.get("QSICK_CONFIG", "qsick.json"))
    if candidate.exists():
        try:
            raw = json.loads(candidate.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{candidate}: invalid JSON: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError(f"{candidate}: top level must be an object")

    cfg = _coerce(RuntimeConfig, raw)
    # 记住来源，保存时才写得回原文件。
    cfg.source_path = str(candidate)

    # A handful of environment overrides for container/CI use.
    env_map = {
        "QSICK_DATA_DIR": ("data_dir", str),
        "QSICK_ONEBOT_HOST": ("onebot.host", str),
        "QSICK_ONEBOT_PORT": ("onebot.port", int),
        "QSICK_ONEBOT_TOKEN": ("onebot.access_token", str),
        "QSICK_WEBUI_HOST": ("webui.host", str),
        "QSICK_WEBUI_PORT": ("webui.port", int),
        "QSICK_TRANSPORT": ("transport.mode", str),
        "QSICK_PAYLOAD": ("transport.payload_path", str),
        "QSICK_LOG_LEVEL": ("log.level", str),
    }
    for env_name, (dotted, cast) in env_map.items():
        value = os.environ.get(env_name)
        if value in (None, ""):
            continue
        section, _, leaf = dotted.partition(".")
        if leaf:
            target = getattr(cfg, section)
        else:
            target, leaf = cfg, section
        try:
            setattr(target, leaf, cast(value))
        except ValueError as exc:
            raise ConfigError(f"{env_name}={value!r}: {exc}") from exc

    cfg.onebot.port = int(cfg.onebot.port)
    cfg.webui.port = int(cfg.webui.port)
    return cfg
