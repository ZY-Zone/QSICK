"""Runtime wiring: transport -> messages -> plugins -> OneBot -> clients."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import uvicorn

from . import __version__, log as logmod
from . import network_profiles as netprofiles
from .config import RuntimeConfig, load_config
from .instances import InstanceContext, InstanceHub
from .onebot import actions as action_mod
from .onebot.cache import TTLCache
from .onebot.dedupe import EventDeduper
from .onebot.context import ActionContext
from .onebot.events import EventMapper
from .onebot.registry import ActionRegistry
from .onebot.reverse import ReverseWebSocketClient
from .onebot.server import EventHub, create_app as create_onebot_app
from .plugins.host import PluginHost
from .protocol.media import MediaResolver
from .protocol.message import Message
from .protocol.rkey import RKeyManager
from .storage import MessageStore, create_store, store_info
from .transport.session_link import SessionLink
from .transport.base import LoginState, PacketEvent, SendResult, Transport, TransportError

log = logmod.get_logger("Runtime")

MESSAGE_CMDS = {"message.group", "message.private", "message.sent"}
SEND_ACTIONS = {
    "send_msg",
    "send_group_msg",
    "send_private_msg",
    "send_group_forward_msg",
    "send_private_forward_msg",
}

#: How often the session state is re-read, in seconds.
_ACCOUNT_POLL_SECONDS = 3

#: Retries after a missed poll, as seconds to wait before each extra attempt.
#:
#: One attempt per second for the following two seconds -- not two in a row. A session that misses the
#: scheduled poll gets a second chance one second later and a third two seconds after that. A single slow
#: reply is therefore never mistaken for a logout: `login.info` is answered on QQ's JS thread, which also
#: drains the action queue, and under load it can easily outlast one timeout.
#:
#: If all three miss, the session is treated as not running and polling for it stops until the operator starts
#: it again. That is deliberate: a dead session is not worth a probe every three seconds, and the UI keeps
#: saying 已停止 instead of flickering.
_PROBE_RETRY_DELAYS: tuple[float, ...] = (1.0, 1.0)

#: Consecutive failed polls a session that has never answered is allowed before it is judged dead.
#:
#: QQ needs a minute or two from launch to a listening action API, and the retry ladder is measured in
#: seconds. Without a startup allowance, pressing 启动 would mark the session dead during its own startup and
#: stop polling it. 60 polls at the three-second cadence is several minutes, which outlasts a cold start.
_STARTUP_MISS_LIMIT = 60

#: Consecutive failed polls that still count as "asked and answered". Kept at one because the retry ladder
#: above already provides the tolerance.
_OFFLINE_MISSES = 1

#: Read-only lookups worth caching, with their TTL in seconds.
#:
#: These are the actions `throttle.SENSITIVE_INTERVALS` spaces out because a high request rate is what gets an
#: account flagged. Caching the answer satisfies that intent -- the outbound rate is unchanged, since a cached
#: read never reaches the client -- while keeping the caller responsive.
#:
#: Only genuinely read-only actions belong here. Anything that changes state must never be served from cache,
#: and a mutation should invalidate what it affects (see `_invalidate_action_cache`).
_ACTION_CACHE_TTL: dict[str, float] = {
    "get_group_list": 60.0,
    "get_group_info": 60.0,
    "get_group_member_list": 60.0,
    "get_group_member_info": 60.0,
    "get_group_shut_list": 60.0,
    "get_friend_list": 60.0,
    "get_stranger_info": 60.0,
    "get_recent_contact": 60.0,
}

#: Mutations that make a cached lookup stale, mapped to the prefixes to drop.
_CACHE_INVALIDATIONS: dict[str, tuple[str, ...]] = {
    "set_group_card": ("action:get_group_member_list", "action:get_group_member_info"),
    "set_group_kick": ("action:get_group_member_list", "action:get_group_member_info", "action:get_group_info"),
    "set_group_ban": ("action:get_group_shut_list", "action:get_group_member_info"),
    "set_group_admin": ("action:get_group_member_list", "action:get_group_member_info"),
    "set_group_leave": ("action:get_group_list", "action:get_group_info"),
    "set_group_name": ("action:get_group_list", "action:get_group_info"),
    "set_friend_remark": ("action:get_friend_list", "action:get_stranger_info"),
    "delete_friend": ("action:get_friend_list",),
}


def _human_uptime(seconds: int) -> str:
    days, rest = divmod(max(0, seconds), 86400)
    hours, rest = divmod(rest, 3600)
    minutes, seconds = divmod(rest, 60)
    if days:
        return f"{days}天 {hours}小时 {minutes}分 {seconds}秒"
    if hours:
        return f"{hours}小时 {minutes}分 {seconds}秒"
    if minutes:
        return f"{minutes}分 {seconds}秒"
    return f"{seconds}秒"


def _pipe_event_root(config: Any) -> str:
    """Where the pipe sessions write their event stream.

    A separate function so tests can redirect it. The default, `headlessClient`, is relative to the process
    working directory -- the repository -- so a runtime started in a test read the *developer's* live
    `Pipe/owndata/events.jsonl` and counted those messages as its own. That is what made
    `test_duplicate_message_events_are_dropped` fail only when a real session had recently run: the assertion
    was `events_seen == 3`, and the poller had quietly added the real ones.
    """
    from . import headless as headless_mod

    return str(getattr(config, "headless_root", "") or headless_mod.ROOT_NAME)


def _message_to_parts(message: Message) -> list[dict[str, Any]]:
    """``Message`` -> 协议层的"内部部件"。

    这一层刻意只做结构搬运，不补全任何东西：协议层要什么由它自己决定，
    这里替它猜就是替它裁剪。
    """
    parts: list[dict[str, Any]] = []
    for element in message.elements:
        etype = str(getattr(element, "type", "") or "")
        data = getattr(element, "data", None) or {}
        if etype == "text":
            parts.append({"kind": "text", "text": str(data.get("text") or "")})
        elif etype in ("face", "mface"):
            parts.append({"kind": "emotion", "emotion": dict(data)})
        else:
            # 其它元素原样带过：协议层要么认得，要么按 unknown 保留。
            parts.append({"kind": "unknown", "type": etype, "data": dict(data)})
    return parts or [{"kind": "text", "text": message.text}]


class _LoginView:
    """``mapper.login`` 需要的只读视图：只要有实例在线就算 online。

    消息监听下放到实例后，"是否在线"是逐实例的事实；心跳是全局的，
    因此这里回答的是"是否至少有一个实例已登录"。
    """

    __slots__ = ("_runtime",)

    def __init__(self, runtime: "Runtime") -> None:
        self._runtime = runtime

    @property
    def logged_in(self) -> bool:
        return any(state.logged_in for state in self._runtime._logins.values())

    @property
    def online(self) -> bool:
        return any(state.online for state in self._runtime._logins.values())

    @property
    def kicked(self) -> bool:
        return any(state.kicked for state in self._runtime._logins.values())

    @property
    def security_kicked(self) -> bool:
        return any(state.security_kicked for state in self._runtime._logins.values())

    @property
    def kick_reason(self) -> str:
        for state in self._runtime._logins.values():
            if state.kicked and state.kick_reason:
                return state.kick_reason
        return ""

    @property
    def uin(self) -> str:
        return str(self._runtime.self_id)

    @property
    def nickname(self) -> str:
        return self._runtime.login.nickname


class Runtime:
    def __init__(self, config: RuntimeConfig | None = None) -> None:
        self.config = config or load_config()
        logmod.configure(
            self.config.log.level,
            capacity=self.config.webui.log_buffer,
            file=self.config.log.file or None,
            json_lines=self.config.log.json,
        )
        # 每实例一套：独立数据库（maxStore）+ 独立处理器 + 归属它的网络配置。
        self.instances = InstanceHub(self.config)
        # 账号从登录状态来（宿主亲口告知的事实），而不是靠读某个文件里的字段。
        # getattr：建库发生在 __init__ 早期，_logins 那时可能还没就位。
        self.instances.set_account_source(
            lambda name: (getattr(self, "_logins", {}).get(name).uin
                          if getattr(self, "_logins", {}).get(name) else ""))
        # ``self.store`` 必须是**主实例自己的库对象**，不能另建一个。
        #
        # 以前这里是独立的全局库：消息写进实例库，而 WebUI、插件、测试读
        # ``runtime.store``，两边逐渐分叉——读到的永远是空的。共享同一个对象
        # 之后，"主实例的库"就只是 C0 那个库的别名，不存在第二份真相。
        self.store: MessageStore = self.instances.instance(netprofiles.DEFAULT_INSTANCE).store
        self.registry: ActionRegistry = action_mod.build_registry()
        self.hub = EventHub()
        self.plugins = PluginHost(
            self, [str(path) for path in self.config.plugins.dirs], max_tier=self.config.plugins.max_tier
        )
        self.transport: Transport = self._make_transport()
        # 必须先于 self.login 的属性赋值存在：login 的 setter 会写入这里。
        self._logins: dict[str, LoginState] = {}
        self._primary_instance: str = ""
        self.login: LoginState = LoginState()
        self.rkey = RKeyManager(self.transport)
        self.media = MediaResolver(self.transport, self.rkey)
        self.mapper = EventMapper(self.config, self.store, self.media, self_id=0)
        # mapper.login 决定心跳里的 online 字段。实例化时还一无所知，
        # 交给一个视图对象，登录状态变化后不需要重新赋值。
        self.mapper.login = _LoginView(self)
        self._tasks: list[asyncio.Task[Any]] = []
        self._servers: list[uvicorn.Server] = []
        self._server_tasks: dict[uvicorn.Server, asyncio.Task[None]] = {}
        self._onebot_server: uvicorn.Server | None = None
        self.last_network_apply: dict[str, Any] | None = None
        # Which accounts the network listeners are currently serving.
        #
        # Kept as state rather than recomputed per event so a login transition is a *change* and can be acted
        # on once: recomputing alone cannot tell "already serving this account" from "just started to".
        self._network_accounts: set[str] = set()
        self._network_lock = asyncio.Lock()
        self.last_account_network: dict[str, Any] | None = None
        # Per-session poll state, keyed by tmp_id, plus the last session list read.
        #
        # The three-second poll is the only thing that probes; the UI reads this state. That is what makes
        # "stop polling a session that stopped answering" hold -- a page refresh cannot revive it.
        # One state link per session, keyed by tmp_id. The link owns the WebSocket that the session pushes to.
        self._links: dict[str, SessionLink] = {}
        self._session_list: list[dict[str, Any]] = []
        # True once the WebUI (and therefore the ability to serve) is up. Network listeners are only ever
        # managed when this is set, so `start(serve=False)` never binds anything.
        self._serving = False
        self.reverse = ReverseWebSocketClient(
            self,
            list(self.config.onebot.reverse_ws_urls),
            self.config.onebot.reverse_reconnect_seconds,
        )
        self._started_at = time.time()
        self._stopping = asyncio.Event()
        self.last_event_at = 0.0
        self.events_seen = 0
        self.events_dropped = 0
        self.events_duplicate = 0
        self.deduper = EventDeduper(self.config.onebot.dedupe_seconds, self.config.onebot.dedupe_max)
        self.cache = TTLCache()
        self._send_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._action_stats: dict[str, dict[str, float]] = {}
        # (instance, message_id) -> {(element_id, local path)}，用于"下载完成事件
        # 早于消息入库"的情形。键里必须带实例：两个实例的 message_id 会撞车，
        # 只按 message_id 存会让 C0 的图片路径写进 C1 的消息里（写错数据，而且
        # C0 自己那条永远补不上）。
        self._pending_media: dict[tuple[str, int], set[tuple[str, str]]] = {}
        self._headless_manager = None
        self._headless_started = False

    # ------------------------------------------------------------------ setup

    def _make_transport(self) -> Transport:
        mode = self.config.transport.mode.lower()
        if mode == "mock":
            from .transport.mock import MockTransport

            return MockTransport()
        if mode == "pipe":
            from .transport.pipe import PipeTransport

            return PipeTransport(self.config.transport)
        if mode == "headless":
            from .transport.headless import HeadlessTransport

            return HeadlessTransport(self.config.transport.headless_port, host_root=self.config.headless_root)
        raise TransportError(f"unknown transport mode {mode!r}")

    @property
    def self_id(self) -> int:
        try:
            return int(self.login.uin)
        except (TypeError, ValueError):
            return 0

    @property
    def login(self) -> LoginState:
        """"当前账号"的登录状态。

        多实例下这是"主实例"（最近登录成功的那个）的状态，供顶栏、心跳、
        未指定账号的动作使用。需要精确到某个实例时用 :meth:`login_for`。
        """
        if self._primary_instance and self._primary_instance in self._logins:
            return self._logins[self._primary_instance]
        if self._logins:
            return next(reversed(self._logins.values()))
        return LoginState()

    @login.setter
    def login(self, state: LoginState) -> None:
        # 兼容启动阶段的赋值（transport.start() 之后会回填一次），
        # 把它记到主实例名下，避免出现"属性被覆盖、_logins 却没有"的半状态。
        key = self._primary_instance or "C0"
        self._logins[key] = state
        if state.logged_in:
            self._primary_instance = key

    def login_for(self, instance: str) -> LoginState:
        return self._logins.get(instance) or LoginState()

    def logins(self) -> dict[str, LoginState]:
        return dict(self._logins)

    def instances_online(self) -> list[dict[str, str]]:
        """每个实例的登录摘要，供 WebUI 展示多账号状态。"""
        return [
            {"instance": name, "uin": state.uin, "nickname": state.nickname,
             "logged_in": state.logged_in, "online": state.online,
             "kicked": state.kicked, "reason": state.kick_reason}
            for name, state in sorted(self._logins.items())
        ]

    def network_cards(self) -> list[dict[str, Any]]:
        """At most three configured network endpoints for the dashboard."""
        cfg = self.config.onebot
        server = self._onebot_server
        started = bool(server and getattr(server, "started", False))
        hub = self.hub.stats()
        cards: list[dict[str, Any]] = []

        if cfg.enable_http:
            cards.append({
                "id": "http", "label": "OneBot HTTP",
                "tone": "green" if started else "red",
                "state": "监听中" if started else "未启动",
                "detail": f"http://{cfg.host}:{cfg.port}{cfg.http_path}",
            })
        else:
            cards.append({"id": "http", "label": "OneBot HTTP", "tone": "gray", "state": "未启用", "detail": "HTTP 服务已关闭"})

        if cfg.enable_ws:
            cards.append({
                "id": "ws", "label": "OneBot WebSocket",
                "tone": "green" if started else "red",
                "state": f"{hub['websockets']} 个连接" if started else "未启动",
                "detail": f"ws://{cfg.host}:{cfg.port}{cfg.ws_path}",
            })
        else:
            cards.append({"id": "ws", "label": "OneBot WebSocket", "tone": "gray", "state": "未启用", "detail": "WebSocket 服务已关闭"})

        urls = list(cfg.reverse_ws_urls)
        if urls:
            connected = bool(self.reverse.status().get("connected"))
            cards.append({
                "id": "reverse", "label": "Reverse WS 客户端",
                "tone": "green" if connected else "red",
                "state": "正常" if connected else "异常",
                "detail": f"{'已连接' if connected else '未连接'} · {len(urls)} 个目标",
            })
        else:
            cards.append({"id": "reverse", "label": "Reverse WS 客户端", "tone": "gray", "state": "未启用", "detail": "未配置目标地址"})

        return cards[:3]

    def _last_error(self) -> dict[str, Any] | None:
        for record in reversed(logmod.BUFFER.tail(300)):
            if record.level >= logmod.ERROR:
                return record.to_dict()
        return None

    def now(self) -> int:
        return int(time.time())

    def context(self) -> ActionContext:
        return ActionContext(
            runtime=self,
            transport=self.transport,
            config=self.config,
            store=self.store,
            rkey=self.rkey,
            media=self.media,
            # 传视图而不是快照：动作执行期间账号可能切换/重登，快照会让
            # ctx.self_id 停在旧账号上（多实例下尤其明显）。
            login=_LoginView(self),
            cache=self.cache,
        )

    # ---------------------------------------------------------------- lifecycle

    async def start(self, *, serve: bool = True) -> None:
        # Clear the session port overrides before anything starts.
        #
        # The operator wants that file to be scratch space for the current run: pins from a previous
        # run must not silently apply to a session that has since been recreated. Clearing it here,
        # before any session is launched, means a pin written through the UI during this run survives
        # until the next start.
        try:
            from . import headless as _headless

            _headless.reset_port_override()
            # `Pipe/dll/` is deliberately NOT cleared here.
            #
            # It is the session list: one directory per session, holding that session's DLL copy and its
            # port. Wiping it on startup would erase the record of every session, including the ports they
            # were given, so nothing would be left to show or to close. Leaving it means sessions survive a
            # restart as entries whose process is gone -- which is what the list should say.
        except Exception as exc:  # noqa: BLE001 - a scratch file must not stop startup
            log.warn("session port override reset failed", error=f"{type(exc).__name__}: {exc}")

        await self.transport.start()
        # 先同步传输层已经知道的逐实例状态（headless 场景）。
        #
        # 顺序很重要：``transport.login`` 返回的是"活动实例"的那一份，把它
        # 当成主实例状态写下去，会把 C0 真实的 uin 覆盖成 C1 的——实测
        # `_logins == {'C0': '222', 'C1': '222'}`，两个实例串号。
        # 所以先按实例铺好，再决定主实例。
        sync = getattr(self.transport, "logins", None)
        per_instance = callable(sync)
        if per_instance:
            for name, state in sync().items():
                self._logins[name] = state
        # 单通道载具（mock/pipe）只有一份状态，直接采用它的。
        #
        # 判断依据必须是"有没有逐实例能力"，而不是"``_logins`` 是不是空的"：
        # ``__init__`` 里 setter 已经写入过 C0 的空状态，用空判断永远为假，
        # mock 模式的登录信息就再也没机会进来，``info()['online']`` 一直是 0。
        if not per_instance:
            self.login = self.transport.login
        elif not self._primary_instance:
            # 有逐实例状态但还没确定主实例：挑一个已登录的，而不是无条件覆盖。
            for name, state in self._logins.items():
                if state.logged_in:
                    self._primary_instance = name
                    break
        self.mapper.self_id = self.self_id
        if self.config.plugins.enabled:
            await self.plugins.load_all()
        self._tasks.append(asyncio.create_task(self._consume(), name="qsick-consume"))
        self._tasks.append(asyncio.create_task(self._heartbeat(), name="qsick-heartbeat"))
        # Pipe sessions report messages to a file rather than pushing them here, so something has to
        # read it. Without this task a pipe session could call every action but never deliver a message:
        # the injected side has no HTTP callback into QSICK, only the task queue it reads from.
        #
        # The cutoff is read here, synchronously, before the task can run: everything already in the stream
        # belongs to a previous run. Reading it inside the coroutine would race with anything appended
        # between `create_task` and its first iteration.
        from . import headless as _headless_mod

        # Rotate the event stream before anything opens it.
        #
        # Nothing prunes this file: every session appends to it for as long as it runs, and a busy account had it
        # at 9 MB after a day of testing. Rotation happens here, at start-up, because this is the one moment with
        # no writer -- renaming a file another process is appending to would either fail or silently redirect its
        # writes into the renamed handle.
        try:
            _headless_mod.rotate_pipe_events(_pipe_event_root(self.config))
        except Exception as exc:  # noqa: BLE001 - an unrotated log is not a reason to refuse to start
            log.debug("could not rotate the pipe event stream", error=str(exc))

        try:
            pipe_start = _headless_mod.pipe_event_offset(_pipe_event_root(self.config))
        except Exception as exc:  # noqa: BLE001 - falling back to 0 replays history, which still works
            log.debug("could not locate the end of the pipe event stream", error=str(exc))
            pipe_start = 0
        self._tasks.append(asyncio.create_task(
            self._poll_pipe_events(pipe_start), name="qsick-pipe-events"))
        if self.config.plugins.enabled and self.config.plugins.watch:
            self._tasks.append(asyncio.create_task(self._watch_plugins(), name="qsick-plugins"))
        if serve:
            await self._start_servers()
        # Find out which accounts are logged in before deciding anything.
        #
        # Asking is necessary, not convenient: a pipe session reports nothing on its own, so an account that
        # logged in before this process started is invisible until it happens to send a message. Probing here
        # makes a restart adopt the sessions that are already running instead of waiting for traffic.
        try:
            await self.learn_session_accounts(reason="startup")
        except Exception as exc:  # noqa: BLE001 - startup must not fail over a probe
            log.debug("startup account probe failed", error=str(exc))
        # Network listeners follow the accounts that are logged in. Anything already logged in gets its
        # profiles activated now.
        #
        # `force`: with nothing logged in the account set is empty and unchanged, which would look like
        # "nothing to do". But the config still carries whatever the previous run left in `qsick.json`, so the
        # listener flags and reverse URLs have to be cleared explicitly -- otherwise a stale reverse client
        # would still be described as configured and the first login would have to reconcile it.
        await self.sync_account_network(force=True, reason="startup")
        # The account watcher always runs.
        #
        # It is not part of the auto-start opt-in: it is what keeps the network config truthful about which
        # sessions are logged in, and an installation that never auto-starts anything still needs a session it
        # started by hand to activate its connection -- and to lose it when it logs out.
        self._tasks.append(asyncio.create_task(
            self._watch_account_network(), name="qsick-net-watch"))
        if self.config.headless_auto_start:
            self._tasks.append(asyncio.create_task(self._auto_headless(), name="qsick-headless"))
            self._tasks.append(asyncio.create_task(self._watch_kicked(), name="qsick-kick-watch"))
        if self.config.pipe_auto_start and self.config.pipe_auto_start_session:
            self._tasks.append(asyncio.create_task(
                self._auto_pipe_session(), name="qsick-pipe-autostart"))
        log.info(
            "runtime started",
            transport=self.transport.name,
            uin=self.login.uin,
            actions=len(self.registry._actions),
            plugins=len(self.plugins._loaded),
        )

    async def _auto_pipe_session(self) -> None:
        """Launch the one pipe session the operator nominated for startup.

        Deliberately narrow:

        * only the session whose id is stored. Launching "a" session would create a second one alongside the
          configured one, with a new id and a new port, which is never what "start this session on boot" means.
        * a session that is already running is left alone. Restarting it would interrupt a live login and a
          live connection for no reason.
        * a missing directory is reported and nothing is launched: the operator's choice refers to a session
          that no longer exists, and inventing one under a different id would silently change their config.
        """
        from . import headless as headless_mod

        tmp_id = str(self.config.pipe_auto_start_session or "").strip()
        if not tmp_id:
            return
        try:
            sessions = await asyncio.to_thread(headless_mod.read_session_dirs)
        except Exception as exc:  # noqa: BLE001 - a broken session list must not fail startup
            log.warn("pipe auto-start could not read the session list", error=str(exc))
            return
        target = next((item for item in sessions if str(item.get("tmp_id")) == tmp_id), None)
        if target is None:
            log.warn("pipe auto-start: the chosen session does not exist", tmp_id=tmp_id)
            return
        port = int(target.get("nowport") or target.get("port") or 0)
        probe = await asyncio.to_thread(headless_mod.probe_session_login, port) if port else {}
        if probe.get("running"):
            log.info("pipe auto-start: session is already running", tmp_id=tmp_id, port=port)
            return
        if self._headless_manager is None:
            from .headless import HeadlessManager

            self._headless_manager = HeadlessManager(self.config.headless_root)
        try:
            result = await asyncio.to_thread(
                self._headless_manager.start_session, port, self.config.webui.port, tmp_id)
            log.info("pipe auto-start launched the session", tmp_id=tmp_id, port=port,
                     pid=(result or {}).get("pid"))
        except Exception as exc:  # noqa: BLE001 - startup must not die over this
            log.warn("pipe auto-start failed", tmp_id=tmp_id, error=f"{type(exc).__name__}: {exc}")

    async def _watch_kicked(self) -> None:
        """Restart sessions QQ killed, but only where the user opted in."""
        from .headless import HeadlessManager
        while not self._stopping.is_set():
            await asyncio.sleep(5)
            if self._stopping.is_set():
                return
            manager = HeadlessManager(self.config.headless_root)
            try:
                results = await asyncio.to_thread(manager.watch_kicked, self.config.webui.port)
            except Exception as exc:  # noqa: BLE001 - the watcher must keep running
                log.warn("kick watcher failed", error=f"{type(exc).__name__}: {exc}")
                continue
            for item in results:
                log.info("auto relogin after kick", instance=item.get("id"),
                         relogin=item.get("relogin"), error=item.get("error") or "")

    async def _auto_headless(self) -> None:
        """按实例的 autoStart 开关拉起实例，绝不自作主张。

        三条刻意的约束：

        * **不自动启动任何实例**：QSICK 起来时只有服务在听端口，QQ 一个都不拉起。
          每个实例是否随启动自开，由账户管理页的开关逐实例决定（``autoStart``）。
        * **首次初始化建一个 pipe 实例**：设备上一个实例都没有时，创建 pipe 实例并
          打开它的 autoStart，这样刚拿到项目的人立刻有可用的客户端。
        * **pipe 只保留一个**：QQ 客户端本身支持多开，但 pipe 实例共用同一份系统
          配置的数据根，多开会互相争抢登录态，所以全局只留一个。

        全部过程不阻塞核心：任何一步失败只记日志。
        """
        try:
            from .headless import HeadlessManager

            manager = HeadlessManager(self.config.headless_root)
            self._headless_manager = manager
            if not self.config.headless_auto_start:
                return

            await asyncio.to_thread(self._ensure_first_instance, manager)

            for item in manager.instances():
                if not item.get("autoStart") or item.get("running"):
                    continue
                try:
                    account = await asyncio.to_thread(
                        manager.start, item["id"], self.config.webui.port)
                    log.info("实例已自动启动", instance=item["id"],
                             phase=account.get("phase"))
                except Exception as exc:  # noqa: BLE001 - 一个实例失败不能拖垮其它
                    log.warn("实例自动启动失败", instance=item["id"],
                             error=f"{type(exc).__name__}: {exc}")
            self._headless_started = True
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - headless is optional
            log.warn("实例自动启动流程失败", error=f"{type(exc).__name__}: {exc}")

    @staticmethod
    def _ensure_first_instance(manager: Any) -> None:
        """设备上没有任何实例时，建一个 pipe 实例。

        为什么是 pipe：headless 实例要单独下载 runtime，而 pipe 用的是系统里已经
        装好的 QQ，对刚拿到项目的人是唯一开箱即用的形态。

        **不打开 autoStart。** 这是刻意的：QSICK 启动时不自动拉起任何实例，哪怕这个
        pipe 实例是它自己刚建的。"建出来"和"现在就跑"是两件事——用户可能只是想先看看
        界面，把 QQ 拉起来会立刻占用内存和网络。要它随启动自开，由账户管理页的开关
        自己决定。

        已有实例就什么都不做——包括不额外补建 pipe。用户可能全是 headless，
        那也该由他自己决定要不要加一个 pipe。

        "已有"必须同时看**两类**：headless 实例和 pipe 客户端目录。

        `manager.instances()` 只返回 headless——pipe 从"不再算实例"起就被排除在外。
        只看它的话，装好过一次 pipe 客户端之后每次启动都会判定"没有任何实例"，然后
        尝试再建一个，而 `create()` 的单例检查又会拒绝，日志里每次多一条失败告警，
        看起来像坏了，实际什么都不该发生。
        """
        if manager.instances():
            return
        if (Path(str(manager.root)) / manager.pipe_instance_id()).is_dir():
            return
        try:
            created = manager.create(name="本机 QQ", kind="pipe")
        except Exception as exc:  # noqa: BLE001 - 建不出来就只记日志
            log.warn("首次初始化创建 pipe 实例失败",
                     error=f"{type(exc).__name__}: {exc}")
            return
        log.info("首次初始化已创建 pipe 实例（未设为自动启动）", instance=created["id"])

    def _spawn_server(self, app: Any, host: str, port: int, label: str) -> uvicorn.Server:
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host=host,
                port=port,
                log_level="warning",
                lifespan="off",
                access_log=False,
            )
        )
        task = asyncio.create_task(server.serve(), name=f"qsick-uvicorn-{label}-{port}")
        self._tasks.append(task)
        self._server_tasks[server] = task
        self._servers.append(server)
        return server

    async def _start_servers(self) -> None:
        # The WebUI always starts: it is how the operator sees state and configures everything else, so
        # gating it on a login would leave no way in.
        #
        # The OneBot listener is deliberately NOT started here. It follows the logged-in accounts (see
        # `sync_account_network`), because a listener with no account behind it answers nothing and looks
        # like a working integration until the first action fails.
        from .webui.app import create_app as create_webui_app

        self._serving = True
        self._spawn_server(create_webui_app(self), self.config.webui.host, self.config.webui.port, "webui")
        await asyncio.sleep(0.2)
        for server in self._servers:
            if not getattr(server, "started", False):
                log.warn("server not confirmed started yet", port=server.config.port)
            else:
                log.info("server listening", port=server.config.port)

    async def restart_onebot(self) -> bool:
        """Re-bind the OneBot HTTP/WS/SSE listener with the current config."""
        if self._onebot_server is not None:
            server = self._onebot_server
            task = self._server_tasks.pop(server, None)
            server.should_exit = True
            if task is not None:
                try:
                    await asyncio.wait_for(task, timeout=10)
                except (asyncio.TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
                    task.cancel()
                if task in self._tasks:
                    self._tasks.remove(task)
            if server in self._servers:
                self._servers.remove(server)
            self._onebot_server = None
        enabled = (
            self.config.onebot.enable_http
            or self.config.onebot.enable_ws
            or self.config.onebot.enable_sse
        )
        if not enabled:
            return False
        server = self._spawn_server(
            create_onebot_app(self), self.config.onebot.host, self.config.onebot.port, "onebot"
        )
        for _ in range(30):
            if getattr(server, "started", False):
                break
            await asyncio.sleep(0.1)
        self._onebot_server = server
        started = bool(getattr(server, "started", False))
        if started:
            log.info("onebot listener restarted", host=self.config.onebot.host, port=self.config.onebot.port)
        else:
            log.warn("onebot listener did not start", host=self.config.onebot.host, port=self.config.onebot.port)
        return started

    def _reverse_targets(self) -> list[tuple[str, str]]:
        """`(url, account)` for every reverse connection that should exist right now.

        A reverse connection belongs to one account: its handshake names that account, and only that account's
        events travel over it. The profile store is the source, because ownership is written there; the flat
        `reverse_ws_urls` list is kept as a fallback for a configuration that predates the store.

        This is not cosmetic. With a flat URL list every connection received every account's events, so a
        consumer holding bots for two accounts discarded the ones whose `self_id` it did not expect
        ("找不到对应Bot，忽略消息"), and the reply it then attempted was built against the wrong account.
        """
        from . import network_profiles as netprofiles

        targets: list[tuple[str, str]] = []
        try:
            for account in self._active_accounts():
                for profile in netprofiles.load_for(self.config, account):
                    if profile.get("kind") != "ws_client" or not profile.get("enabled"):
                        continue
                    url = str(profile.get("url") or "").strip()
                    if url.startswith(("ws://", "wss://")):
                        targets.append((url, account))
        except Exception as exc:  # noqa: BLE001 - a broken store must not stop the runtime starting
            log.warn("could not read reverse ws targets from the profile store", error=str(exc))
        if targets:
            return targets
        return [(str(url).strip(), "") for url in self.config.onebot.reverse_ws_urls if str(url).strip()]

    async def reload_reverse(self) -> bool:
        """Drop the current Reverse WS task and reconnect with the current URLs."""
        targets = self._reverse_targets()
        # Keep a working connection when the target set is unchanged.
        #
        # This runs on every login transition, and rebuilding unconditionally dropped the consumer's socket
        # mid-request. Its pending-request table is per-connection, so the answers already in flight arrived on
        # the replacement connection where nothing was waiting for them -- the consumer logged them as
        # "未知消息" and then timed out re-asking. Reconnecting is only justified when the set of connections
        # actually changes.
        current = getattr(self.reverse, "targets", None)
        if current is not None and sorted(current) == sorted(targets) and self.reverse.alive():
            return bool(targets)
        await self.reverse.stop()
        self.reverse = ReverseWebSocketClient(
            self,
            targets,
            self.config.onebot.reverse_reconnect_seconds,
        )
        if targets:
            self.reverse.start()
            return True
        return False

    async def apply_network(self) -> dict[str, Any]:
        onebot = await self.restart_onebot()
        reverse = await self.reload_reverse()
        enabled = (
            self.config.onebot.enable_http
            or self.config.onebot.enable_ws
            or self.config.onebot.enable_sse
        )
        self.last_network_apply = {
            "onebot": onebot,
            "onebot_enabled": enabled,
            "reverse_ws": reverse,
            "at": time.time(),
        }
        log.info("network settings applied", onebot=onebot, reverse_ws=reverse)
        return self.last_network_apply

    # ------------------------------------------------------- per-account network

    def _active_accounts(self) -> list[str]:
        """Accounts that are logged in right now.

        The account is `state.uin`, not the registry key: `_logins` is keyed by instance id for headless
        instances ("C0") and by uin for pipe sessions, but the account is what owns a network profile.
        """
        accounts: set[str] = set()
        for state in self._logins.values():
            if not state.logged_in:
                continue
            uin = str(state.uin or "").strip()
            if not uin or uin == "0":
                continue
            accounts.add(uin)
        return sorted(accounts)

    def _persist_network_profiles(self, profiles: list[dict[str, Any]]) -> None:
        """Write the profile store the first time an account logs in.

        The operator's rule is "look up the account's network config when it logs in; if there is none, create
        an empty json". This is that step, and it has to be careful about what "none" means:

        * nothing configured anywhere -> the derived set is empty and `[]` is written, so nothing activates
          for an account that has not been configured. That is the other half of the requirement.
        * `qsick.json` already describes listeners -> `load()` derives those into profiles, and they are
          written out as they are. Writing `[]` over them instead would silently stop a reverse connection
          that worked before the operator ever opened the settings page, which is the opposite of helpful.

        Only writes when the file is absent: an existing store is the operator's and is never touched here.
        """
        # Any layout counts as "the operator has a store": judging by the legacy file name alone recreated that
        # file on every start once profiles moved into per-account files, leaving a second copy of the truth.
        if netprofiles.store_exists(self.config):
            return
        try:
            # Defaults derived from `qsick.json` are owned by the instance name (`C0`). Writing them out that way
            # persists an owner nobody has, and an ownerless profile is activated for whichever account is
            # serving -- the leak the per-account layout exists to prevent. With a single signed-in account the
            # adoption is unambiguous, so the profile is filed under that account instead.
            #
            # The instance does not have to be the signed-in one: the defaults carry `C0` whether or not anything
            # is logged in on `C0`, so keying the rewrite on the login registry missed every default profile.
            active = self._active_accounts()
            sole_owner = active[0] if len(active) == 1 else ""
            instance_names = {str(name) for name in self._logins} | {netprofiles.DEFAULT_INSTANCE}
            adopted: list[dict[str, Any]] = []
            for profile in profiles:
                item = dict(profile)
                if sole_owner and str(item.get("instance") or "") in instance_names:
                    item["instance"] = sole_owner
                adopted.append(item)
            # `save` writes one file per owning account, which is the layout the settings page reads. Writing the
            # legacy single file here instead produced a store the page never opens again.
            netprofiles.save(self.config, adopted)
            log.info("created the network profile store",
                     path=str(self.config.path(netprofiles.PROFILE_DIR)), profiles=len(adopted))
        except OSError as exc:  # noqa: BLE001 - a missing store is not fatal, load() falls back
            log.warn("could not create the network profile store", error=str(exc))

    def _network_profiles_for(self, accounts: list[str]) -> list[dict[str, Any]]:
        """Every profile owned by one of `accounts`, de-duplicated by id.

        A profile's owner is either a QQ number (what the settings page writes, because one connection cannot
        belong to two accounts) or an instance name (`C0`, which is what the built-in defaults carry). Both
        must match, and an instance name counts as the account currently logged in on it -- otherwise the
        default profiles would never match anything and the listener they describe would silently never open.

        Only logged-in accounts contribute: a profile for an account that is offline must not keep a listener
        open.
        """
        wanted = set(accounts)
        for instance, state in self._logins.items():
            if state.logged_in and str(state.uin or "") in wanted:
                # The instance name is an alias for this account right now.
                wanted.add(str(instance))
        # A store that does not exist yet means every profile was *derived* from the single global config
        # (`qsick.json`), not written by an account. Those are not attributable to one account, so they are
        # adopted by whichever accounts are logging in -- otherwise the first login would persist an empty
        # store and silently drop a reverse connection that was working before anyone opened the settings page.
        adopting_defaults = not netprofiles.store_exists(self.config)
        merged: dict[str, dict[str, Any]] = {}
        for profile in netprofiles.load(self.config):
            owner = str(profile.get("instance") or profile.get("account") or netprofiles.DEFAULT_INSTANCE)
            if owner not in wanted and not adopting_defaults:
                continue
            key = str(profile.get("id") or f"{profile.get('kind')}:{profile.get('port') or profile.get('url')}")
            merged[key] = dict(profile)
        return list(merged.values())

    async def sync_account_network(self, *, force: bool = False, reason: str = "") -> dict[str, Any]:
        """Bring the network listeners in line with which accounts are logged in.

        Called on every login transition (and by the network settings page after a save). The rule it
        implements:

        * an account that just logged in has its profiles read and activated
        * an account that is not logged in contributes nothing, so nothing is listening for it
        * when the last logged-in account that had profiles goes away, the listener is closed

        `force` re-applies even when the account set looks unchanged, which is what the settings page needs
        after editing a profile.

        Everything runs under one lock: two accounts logging in at the same instant would otherwise each
        start a listener, and `restart_onebot` stops the previous one -- so the second start would tear down
        the first's socket.
        """
        async with self._network_lock:
            accounts = self._active_accounts()
            if not force and accounts == sorted(self._network_accounts):
                return {
                    "accounts": accounts,
                    "changed": False,
                    "onebot_enabled": bool((self.last_network_apply or {}).get("onebot_enabled")),
                    "reverse_ws": bool((self.last_network_apply or {}).get("reverse_ws")),
                    "reason": reason,
                }
            previous = sorted(self._network_accounts)
            self._network_accounts = set(accounts)
            # Read first, persist afterwards: `load()` derives profiles from `qsick.json` when the store is
            # absent, and creating the file before reading would throw that derivation away.
            profiles = self._network_profiles_for(accounts)
            if accounts:
                self._persist_network_profiles(profiles)
            else:
                # No account is logged in. Keep reading from the same place, so the next login sees the
                # operator's real configuration rather than an emptied one.
                profiles = []
            # `apply_to_config` is the single mapping from profiles onto `config.onebot`; with an empty list it
            # clears every listener flag, which is exactly the "no account is logged in" state. So activation
            # and deactivation are the same code path -- there is no second way to be wrong.
            _, warnings = netprofiles.apply_to_config(self.config, profiles)
            # Local listeners only when this runtime is actually serving. The reverse client is different: it
            # is an outbound connection, so it has nothing to do with whether a local port is bound -- gating
            # it on `_serving` would leave embedded runs unable to reach their consumer at all.
            onebot = await self.restart_onebot() if self._serving else False
            reverse = await self.reload_reverse()
            enabled = (
                self.config.onebot.enable_http
                or self.config.onebot.enable_ws
                or self.config.onebot.enable_sse
            )
            self.last_network_apply = {
                "onebot": onebot,
                "onebot_enabled": enabled,
                "reverse_ws": reverse,
                "at": time.time(),
            }
            state = {
                "accounts": accounts,
                "previous_accounts": previous,
                "changed": True,
                "profiles": len(profiles),
                "warnings": warnings,
                "onebot_enabled": bool(enabled),
                "reverse_ws": bool(reverse),
                "reason": reason,
            }
            self.last_account_network = state
            log.info(
                "network state follows the logged-in accounts",
                accounts=accounts, previous=previous, profiles=len(profiles),
                onebot=state["onebot_enabled"], reverse=state["reverse_ws"], reason=reason,
            )
            return state

    def mark_account_offline(self, account: str, *, reason: str = "") -> bool:
        """Record that an account is no longer logged in, and let the network config follow.

        Called from the paths that *know* an account went away -- an explicit session close, a logout frame --
        so those do not have to wait for the periodic backstop in `_watch_account_network`.

        Returns True when this call is what changed the state, so a caller can log the transition once.
        """
        key = str(account or "").strip()
        if not key:
            return False
        state = self._logins.get(key)
        if state is None:
            return False
        if not state.logged_in:
            return False
        self._logins[key] = LoginState(
            logged_in=False,
            uin=state.uin or key,
            nickname=state.nickname,
            online=False,
            kicked=state.kicked,
            kick_reason=state.kick_reason,
            security_kicked=state.security_kicked,
        )
        if self._primary_instance == key:
            self._primary_instance = next(
                (name for name, other in self._logins.items() if other.logged_in), None)
        log.info("account went offline", account=key, reason=reason or "unspecified")
        self._tasks.append(asyncio.create_task(
            self.sync_account_network(reason=f"offline:{key}"),
            name=f"qsick-net-off-{key}",
        ))
        return True
    # ------------------------------------------------------------ session state links
    #
    # One WebSocket per session, opened by the session's DLL on the same port the action API uses. The session
    # tells us when it logs in, when its listener attaches, and when it is shutting down; we no longer ask.
    #
    # This replaced a three-second poll of `login.info` with a retry ladder. The ladder existed only because a
    # poll cannot distinguish "busy" from "gone" -- the session can, and says so.

    def session_rows(self) -> list[dict[str, Any]]:
        """The current known state of every session, as its link last reported it."""
        rows: list[dict[str, Any]] = []
        for item in self._session_list:
            tmp_id = str(item.get("tmp_id") or "")
            link = self._links.get(tmp_id)
            row = dict(item)
            if link is None:
                row.update({"running": False, "logged_in": False, "closed": False,
                            "detail": "尚未连接", "account": {}, "probed": False})
            else:
                row.update(link.state.as_row())
            rows.append(row)
        return rows

    def _on_link_change(self, link: Any) -> None:
        """React to a state push without blocking the link's own task."""
        if self._stopping.is_set():
            return
        self._tasks.append(asyncio.create_task(
            self._apply_link_state(link), name="qsick-link-state"))

    async def _apply_link_state(self, link: Any) -> None:
        account = str(link.state.uin or "").strip()
        if account and account != "0" and not link.state.closing:
            state = self._logins.get(account)
            if state is None or not state.logged_in:
                self._logins[account] = LoginState(
                    logged_in=True, uin=account,
                    nickname="" if state is None else state.nickname, online=True)
                if self._primary_instance is None or self.self_id == 0:
                    self._primary_instance = account
                self.instances.refresh_accounts()
                log.info("account reported by its session", account=account)
                await self.sync_account_network(reason="session state link")
        await self.sync_account_network(reason="session state")

    def _known_accounts(self) -> set[str]:
        """Accounts the links say are logged in right now."""
        found: set[str] = set()
        for link in self._links.values():
            account = str(link.state.uin or "").strip()
            if account and account != "0" and not link.state.closing:
                found.add(account)
        return found

    async def sync_session_links(self) -> None:
        """Match the set of links to the set of sessions on disk.

        Enumerating is all this does on the happy path: no probing, no requests. A session's state arrives on
        its own connection, which is the point of the redesign.
        """
        from . import headless as headless_mod

        root = _pipe_event_root(self.config)
        try:
            self._session_list = await asyncio.to_thread(headless_mod.read_session_dirs, root)
        except Exception as exc:  # noqa: BLE001 - a broken session list must not break anything
            log.debug("session enumeration failed", error=str(exc))
            return

        wanted: dict[str, tuple[int, str]] = {}
        for item in self._session_list:
            tmp_id = str(item.get("tmp_id") or "")
            if not tmp_id:
                continue
            config = await asyncio.to_thread(headless_mod.read_session_config, tmp_id, root)
            port = int(item.get("nowport") or 0)
            token = str((config or {}).get("ws_token") or "")
            wanted[tmp_id] = (port, token)

        # Drop links whose session is gone.
        for tmp_id in list(self._links):
            if tmp_id not in wanted:
                await self._links.pop(tmp_id).stop()

        for tmp_id, (port, token) in wanted.items():
            link = self._links.get(tmp_id)
            if port <= 0:
                # Nothing is bound: the session is stopped. Keep the link and its last state -- rebuilding it
                # would reset exactly the information the UI needs, and there is nothing to connect to.
                if link is not None:
                    await link.stop()
                    link.state.closing = True
                    link.state.connected = False
                    # uin is left as it was: the row needs to say "stopped, was logged in as X", and
                    # clearing it here made a stopped session look like one that had never logged in.
                    link.state.detail = link.state.detail or "已停止"
                continue
            if link is not None and (link.port, link.token) == (port, token):
                continue
            # A different port or token means a different process: the old link belongs to the previous launch
            # and would keep reporting its state as if it were this one's.
            if link is not None:
                await link.stop()
            link = SessionLink(port, token, name=tmp_id)
            link.on_change = self._on_link_change
            self._links[tmp_id] = link
            # A brand-new link may be a launch in progress: give it the startup window, which reports 未启动
            # rather than retrying forever if the session never answers.
            link.start(starting=True)
            log.info("session state link opened", session=tmp_id, port=port)

    def resume_session_polling(self, tmp_id: str) -> None:
        """Re-open the state link for a session the operator just started."""
        link = self._links.get(str(tmp_id or ""))
        if link is not None:
            link.state.closing = False
            link.state.connected = False
            link.state.detail = ""
            # 启动 is what opens the five-attempt window; everything else reconnects on the short backoff.
            link.start(starting=True)

    def stop_session_polling(self, tmp_id: str, *, detail: str = "已关闭") -> None:
        """Stop a session's link: the process is gone, so there is nothing left to listen to."""
        link = self._links.get(str(tmp_id or ""))
        if link is not None:
            link.state.closing = True
            link.state.connected = False
            link.state.detail = detail
            # The account is kept on purpose: the row should read "已停止（原账号 3905…）" rather than
            # implying the session never had one. Whether it counts as online is decided by closing.

    async def probe_session_accounts(self) -> set[str]:
        """Accounts currently reported by live links.

        Kept async so callers that treated session discovery as an operation still read the same way; the
        answer now comes from the links rather than from a probe.
        """
        await self.sync_session_links()
        return self._known_accounts()

    async def learn_session_accounts(self, *, reason: str = "") -> bool:
        """Mark every account a live session reports as logged in. True when that changed anything."""
        discovered = await self.probe_session_accounts()
        if not discovered:
            return False
        changed = False
        for account in sorted(discovered):
            state = self._logins.get(account)
            if state is not None and state.logged_in:
                continue
            self._logins[account] = LoginState(
                logged_in=True, uin=account,
                nickname="" if state is None else state.nickname, online=True)
            if self._primary_instance is None or self.self_id == 0:
                self._primary_instance = account
            log.info("account learned from its session", account=account, reason=reason or "probe")
            changed = True
        if changed:
            self.instances.refresh_accounts()
        return changed

    async def _watch_account_network(self) -> None:
        """Keep the set of state links in step with the session directories on disk.

        Deliberately does *not* decide any session's state. That question is answered by the session, over its
        own link: `state` when its login changes and `closing` when it shuts down. An earlier version also
        recomputed the account set here and marked anything missing as offline -- a poll overruling a report,
        which showed up as a logged-in session flipping to 未登录 about eight seconds after login while the
        DLL, correctly, sent nothing because nothing had changed for it.

        What is left is the part nobody pushes: a session directory added or removed.
        """
        while not self._stopping.is_set():
            await asyncio.sleep(_ACCOUNT_POLL_SECONDS)
            if self._stopping.is_set():
                return
            try:
                await self.sync_session_links()
            except Exception as exc:  # noqa: BLE001 - the watcher must keep running
                log.debug("session link sync failed", error=str(exc))

    async def stop(self) -> None:
        self._stopping.set()
        if self._headless_manager is not None:
            try:
                await asyncio.to_thread(self._headless_manager.stop_all)
            except Exception:  # noqa: BLE001 - shutdown must not raise
                pass
        await self.reverse.stop()
        for server in self._servers:
            server.should_exit = True
        self._server_tasks.clear()
        if self.config.plugins.enabled:
            await self.plugins.stop()
        await self.transport.stop()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._tasks.clear()
        try:
            self.store.close()
        except Exception:  # noqa: BLE001 - 关库失败不该掩盖真正的退出异常
            pass
        # 每个实例各有自己的 SQLite 句柄与 WAL，逐个刷盘关闭；以前只关主库，
        # 其它实例的句柄一直挂着，进程退出时 WAL 未必落盘。
        try:
            self.instances.close()
        except Exception:  # noqa: BLE001 - shutdown must not raise
            pass
        log.info("runtime stopped")

    async def wait(self) -> None:
        await self._stopping.wait()

    # ------------------------------------------------------------------ events

    async def _poll_pipe_events(self, initial_offset: int = 0) -> None:
        """Feed messages reported by pipe sessions into the normal event pipeline.

        A pipe session writes what it receives to `<headless root>/Pipe/owndata/events.jsonl`; nothing pushes
        those to QSICK, because the injected DLL has no callback into this process -- only the task queue it
        reads from. So this task tails that file and hands the results to `transport.push`, which is the same
        entry point `/api/headless/event` uses for headless instances. Everything downstream (store, protocol
        encoding, network profiles) is then identical for both kinds of instance.

        Attributes each event to its **account**, not to the client directory. All sessions write into one
        shared stream, so using `Pipe` as the instance would put every account's messages in one context --
        and network configs are owned per account. The uin is carried in each line, written by the injected
        side where it knows which process it is.

        `push` may be absent (the mock and named-pipe transports do not implement it); in that case there is
        nothing to feed and the task simply idles.
        """
        from . import headless as headless_mod

        push = getattr(self.transport, "push", None)
        if not callable(push):
            return
        offsets: dict[str, int] = {}
        # Follow the configured location rather than the module default: the root is configurable, and a
        # poller reading one directory while sessions are created in another would silently see nothing.
        root = _pipe_event_root(self.config)
        # Start at the end of the stream, not at the beginning.
        #
        # The cutoff is computed by `start()` and handed in, never here: this coroutine does not run until the
        # loop gets control, and anything appended in between would be counted as history. See
        # `headless.pipe_event_offset` for why history must not be read as a login.
        offsets["pipe"] = int(initial_offset or 0)
        while not self._stopping.is_set():
            try:
                result = headless_mod.read_pipe_events(root, offsets=offsets)
            except Exception as exc:  # noqa: BLE001 - never let bookkeeping stop the loop
                log.debug("pipe event read failed", error=str(exc))
                result = {"events": [], "error": ""}
            if result.get("error"):
                log.debug("pipe event read error", error=result["error"])
            for item in result.get("events") or []:
                uin = str(item.get("uin") or "").strip()
                if not uin:
                    continue
                event_type = str(item.get("type") or "message")
                if event_type in ("message", "sent"):
                    payload = item.get("protocol")
                    if not isinstance(payload, dict):
                        # Normalisation failed inside the session (the raw message is still in the line for
                        # diagnosis). Nothing to dispatch: a protocol layer needs the mapped shape.
                        continue
                    kind = "message_sent" if event_type == "sent" else "message"
                    try:
                        push({"kind": kind, "instance": uin, "payload": payload})
                    except Exception as exc:  # noqa: BLE001 - one bad message must not stop the rest
                        log.warn("pipe event rejected", error=str(exc))
                    continue
                # Notices and requests carry their own `cmd` and body rather than a normalised message.
                #
                # The session writes them to the same stream -- a recall, a member leaving -- and dropping
                # them here is why a pipe instance reported no such events at all while the identical
                # wrappers in `HeadlessTransport` worked: only the message case was handled.
                cmd = str(item.get("cmd") or "").strip()
                body = item.get("payload")
                if not cmd.startswith(event_type + ".") or not isinstance(body, dict):
                    continue
                try:
                    push({"kind": event_type, "cmd": cmd, "instance": uin, "payload": body})
                except Exception as exc:  # noqa: BLE001
                    log.warn("pipe event rejected", error=str(exc), cmd=cmd)
            await asyncio.sleep(0.4)

    async def _consume(self) -> None:
        try:
            async for event in self.transport.events():
                try:
                    await self._handle(event)
                except Exception as exc:  # noqa: BLE001 - one bad event must not stop the loop
                    log.error("event handling failed", cmd=event.cmd, error=f"{type(exc).__name__}: {exc}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.error("event loop stopped", error=f"{type(exc).__name__}: {exc}")

    async def _handle(self, event: PacketEvent) -> None:
        self.events_seen += 1
        self.last_event_at = time.time()
        # 事件属于哪个实例，就由哪个实例的处理器与数据库处理。以前这里做的是
        # "按账号过滤、共用一个库"；现在是分派，不是过滤——每个实例本来就该有
        # 自己的消息数据库。
        instance = str(getattr(event, "instance", "") or "C0")
        context = self.instances.instance(instance)
        if event.cmd == "message.media":
            # A late media download completed; attach the local path and wake the UI.
            payload = event.json()
            try:
                message_id = int(payload.get("message_id"))
            except (TypeError, ValueError):
                return
            element_id = str(payload.get("element_id") or "")
            path = str(payload.get("path") or "")
            if not element_id or not path:
                return
            updated = context.store.set_media_path(message_id, element_id, path)
            if not updated:
                # 宿主在收到消息后立刻开始下载，``message_media`` 可能比消息入库
                # 更早到达（两条独立的 HTTP 请求，顺序不受控）。以前这里只写一条
                # debug 就把路径丢掉——于是自动下载"看起来经常失败"，而手动点击
                # 总是成功：手动那条路是等消息已经在库里才去要图的。
                # 暂存起来，等消息落库后再补写。
                self._pending_media.setdefault((instance, message_id), set()).add((element_id, path))
                log.debug("media path deferred until the message is stored",
                          instance=instance, message_id=message_id, element_id=element_id)
            else:
                log.debug("media path attached", message_id=message_id, updated=updated)
            return
        if event.cmd in MESSAGE_CMDS:
            payload = event.json()
            message_id = payload.get("message_id")
            if message_id:
                chat = payload.get("chat") or {}
                # 去重键按"事件归属的实例"计算：两个实例可能各有同名群、各自的
                # message_id 也可能相同，只按 chat_id 去重会把合法消息当重复丢掉。
                key = (str(getattr(event, "instance", "") or "C0"),
                       str(chat.get("type", payload.get("chat_type", ""))),
                       str(chat.get("id", payload.get("chat_id", ""))), str(message_id))
                if self.deduper.is_duplicate(key):
                    self.events_duplicate += 1
                    log.debug("duplicate message event skipped", message_id=message_id, chat_id=payload.get("chat_id"))
                    return
        if event.kind == "login":
            payload = event.json()
            instance = getattr(event, "instance", "") or "C0"
            previous = self._logins.get(instance) or LoginState()
            state = LoginState(
                logged_in=bool(payload.get("logged_in", True)),
                uin=str(payload.get("uin", previous.uin)),
                nickname=str(payload.get("nickname", previous.nickname)),
                online=bool(payload.get("online", payload.get("logged_in", False))),
            )
            # 登录状态按实例保存：两个号同时在线时，后一个登录不该覆盖前一个。
            self._logins[instance] = state
            if state.logged_in:
                self._primary_instance = instance
            log.info("login state", instance=instance, uin=state.uin, nickname=state.nickname)
            # 登录之后实例才知道自己的 QQ 号，这里同步给实例中心：WebUI 按账号
            # 找库、动作按实例路由都依赖 ``context.account``。
            self.instances.refresh_accounts()
            # Follow the account in or out with its network config.
            #
            # Both directions arrive as this one event, so one call handles both: `sync_account_network`
            # recomputes which accounts are logged in and opens or closes the listeners to match. Not awaited
            # inline -- a listener restart takes as long as uvicorn needs, and the event loop that carries
            # every other message must not wait for it.
            self._tasks.append(asyncio.create_task(
                self.sync_account_network(reason=f"login:{instance}"),
                name=f"qsick-net-login-{instance}",
            ))

        # 这条事件的 self_id 必须是它所属实例的账号。多实例下全局 self_id
        # 会给 C1 的消息打上 C0 的号，客户端据此认错账号。
        # getattr：合成事件（测试、插件）可能只带 cmd/body/kind。
        event_instance = str(getattr(event, "instance", "") or "")
        event_account = str(getattr(event, "account", "") or "")
        if not event_account and event_instance in self._logins:
            event_account = self._logins[event_instance].uin
        # A message attributed to an account proves that account is logged in, and the runtime's global
        # `self_id` -- which is what the reverse-WS `X-Self-ID` header and every heartbeat report -- is
        # derived from it.
        #
        # Pipe sessions never send a login frame: the injected side has no callback into QSICK, only the task
        # queue it reads from, so a session's account is only ever visible in what it reports. Without this
        # the header stayed "0" while every event carried the real uin, and a OneBot consumer registered the
        # bot under 0 and then dropped every message with "找不到对应Bot".
        #
        # Keyed by instance id, which for a pipe event *is* the uin (`PipeTransport.push` sets both), so this
        # only fires for transports that report no separate login. A headless instance reports `instance="C0"`
        # with `account="111111"` and already has its login recorded under "C0" -- keying that by uin as well
        # would leave a second entry for the same account and break "which instance is this" lookups.
        pipe_account = event_instance and event_instance == event_account
        if pipe_account and event_account.isdigit() and event_account != "0" and (
                event_account not in self._logins or not self._logins[event_account].logged_in):
            self._logins[event_account] = LoginState(
                logged_in=True, uin=event_account, nickname="", online=True)
            if self.self_id == 0 or self._primary_instance is None:
                self._primary_instance = event_account
            log.info("pipe account learned from its first event",
                     instance=event_account, self_id=self.self_id)
            # This is the only "login" a pipe session ever reports, so it is also the moment its network
            # config has to be read and activated. Until now nothing here touched the listeners, which is
            # why a pipe account could be fully usable yet have no OneBot connection of its own.
            self._tasks.append(asyncio.create_task(
                self.sync_account_network(reason=f"pipe-login:{event_account}"),
                name=f"qsick-net-pipe-{event_account}",
            ))
        previous_self_id = self.mapper.self_id
        if event_account:
            try:
                self.mapper.self_id = int(event_account)
            except (TypeError, ValueError):
                pass

        try:
            await self._dispatch_event(event)
        finally:
            # self_id 是"当前事件归属的账号"，用完必须还原：下一轮如果忘了
            # 重新赋值，别的实例的消息就会沿用上一个号的 identity。
            self.mapper.self_id = previous_self_id

    async def _dispatch_event(self, event: PacketEvent) -> None:
        if event.cmd in MESSAGE_CMDS:
            message = Message.from_payload(event.json())
            if not await self.plugins.dispatch_message(message):
                self.events_dropped += 1
                return
            # 先把原始 payload 完整写进该实例的 maxStore 库，再谈分发。
            # 顺序不能反：分发是"按协议裁剪成对方能懂的形状"，一旦先裁剪再存，
            # 历史数据就只剩当时那个协议要的字段，将来别的协议要别的字段时
            # 已经无从找回。这就是 maxStore 存在的理由。
            context = self.instances.instance(str(getattr(event, "instance", "") or "C0"))
            # 写库只有一处：实例自己的 maxStore 库。mapper.message 内部会调
            # store.put，所以把它的目标临时指到实例库，写完立即还原——事件
            # 归属逐条变化，绝不能让它停在某个实例上。
            #
            # 这样"先完整落盘、再按协议分发"的次序天然成立：put 发生在
            # mapper.message 的开头，返回的 OneBot 事件是落盘之后才构造的。
            previous_store = self.mapper.store
            self.mapper.store = context.store
            try:
                ob_event = await self.mapper.message(message, sent=event.cmd == "message.sent")
            finally:
                self.mapper.store = previous_store
            self._apply_pending_media(message.message_id, context)
        else:
            mapped = await self.mapper.map(event)
            if not mapped:
                return
            for item in mapped:
                item = await self.plugins.dispatch_event(item)
                await self._publish(item)
            return

        ob_event = await self.plugins.dispatch_event(ob_event)
        await self._publish(ob_event)

    def _store_message(self, context: "InstanceContext", message: Message) -> None:
        """先把原始 payload 完整写进该实例的 maxStore 库（同步版本）。

        顺序不能反：分发是"按协议裁剪成对方能懂的形状"，一旦先裁剪再存，历史
        数据就只剩当时那个协议要的字段，将来别的协议要别的字段时已经无从找回。
        这就是 maxStore 存在的理由——保留尽量多的信息。
        """
        try:
            context.store.put(message)
        except Exception as exc:  # noqa: BLE001 - 存库失败要吵，但不能吞掉消息本身
            log.error("maxStore append failed", instance=context.name,
                      error=f"{type(exc).__name__}: {exc}")
            raise

    async def _store_message_async(self, context: "InstanceContext", message: Message) -> None:
        """写库挪到线程池。

        以前这一步是同步的，而它就在事件循环里：每条消息都要等 SQLite 提交
        （BEGIN IMMEDIATE + 一次摊平插入），期间循环被完全占住——后面排队的
        消息、动作、WebUI 请求全都得等。这是"收到消息和发送消息都慢"的直接原因。
        """
        await asyncio.to_thread(self._store_message, context, message)

    def encode_for_instance(self, instance: str, message: Message,
                            protocol: str | None = None) -> list[dict[str, Any]]:
        """按某个实例（或其某条配置）的协议，把消息编码成对端能懂的段。

        ``protocol`` 为空时用该实例全部启用配置的协议：一条消息可能同时要喂给
        「纯 v11」和「v11+EXTemotions」两类连接，各取所需。编码失败不抛出——
        一条消息喂不了某个协议，不该让其它连接也收不到。
        """
        context = self.instances.instance(str(instance or "C0"))
        protocols = [protocol] if protocol else (context.protocols_in_use() or [None])
        out: list[dict[str, Any]] = []
        for index, protocol_id in enumerate(protocols):
            try:
                processor = context.processor(protocol_id or "")
                segments = processor.encode_outbound(_message_to_parts(message))
            except Exception as exc:  # noqa: BLE001 - 一个协议失败只影响它自己
                log.warn("protocol encode failed", instance=context.name,
                         protocol=protocol_id or "", error=f"{type(exc).__name__}: {exc}")
                continue
            if protocol_id is None and index == 0 and len(protocols) == 1:
                return segments
            out.extend(segments)
        return out

    def _apply_pending_media(self, message_id: int, context: "InstanceContext | None" = None) -> None:
        """Apply a download that completed before its message was stored.

        宿主收到消息就先发 ``message_media``、随后才发 ``message``；两条请求各自
        独立抵达，先到的那条以前会被直接丢掉。这里在消息落库后补写，并对同一个
        消息的多个元素都生效（一条消息可以有多张图）。

        键里带实例名：两个实例的 message_id 会撞车，只按 message_id 取会把别的
        实例的路径写进这条消息。
        """
        instance = context.name if context is not None else "C0"
        pending = self._pending_media.pop((instance, message_id), None)
        if not pending:
            return
        target = context.store if context is not None else self.store
        for element_id, path in pending:
            try:
                updated = target.set_media_path(message_id, element_id, path)
            except Exception as exc:  # noqa: BLE001 - a store hiccup must not kill the loop
                log.warn("deferred media path failed", message_id=message_id, error=str(exc))
                continue
            if updated:
                log.debug("deferred media path attached", message_id=message_id, element_id=element_id)

    async def _publish(self, event: dict[str, Any]) -> None:
        """Fan one event out to forward clients and reverse endpoints alike."""
        await self.hub.broadcast(event)
        await self.reverse.broadcast(event)

    async def _heartbeat(self) -> None:
        try:
            while not self._stopping.is_set():
                await asyncio.sleep(15)
                await self._publish(
                    {
                        "post_type": "meta_event",
                        "meta_event_type": "heartbeat",
                        "time": self.now(),
                        "self_id": self.self_id,
                        "status": {"online": self.login.logged_in, "good": self.login.logged_in},
                        "interval": 15000,
                    }
                )
        except asyncio.CancelledError:
            raise

    async def _watch_plugins(self) -> None:
        try:
            while not self._stopping.is_set():
                await asyncio.sleep(2)
                await self.plugins.watch_once()
        except asyncio.CancelledError:
            raise

    # ------------------------------------------------------------------ actions

    async def invoke(self, action: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = params or {}
        # account 指定动作作用在哪个号上。多实例下合法取值是"任一已登录实例"，
        # 而不是只有主实例——只认同主账号会把 C1 的动作挡在 409 上。
        requested = str(payload.get("account") or "").strip()
        if self.transport.name == "headless" and requested:
            known = {str(state.uin) for state in self._logins.values() if state.uin and state.uin != "0"}
            if known and requested not in known:
                from .onebot.models import fail, CONFLICT
                return fail("selected account does not match any active session", CONFLICT)
        lock = self._send_lock(action, payload)
        started = time.perf_counter()
        # Read-only lookups: answer repeats from cache, so the throttle below only ever spaces out genuine
        # outbound requests.
        #
        # The throttle used to be the only protection, and it worked by making the *caller* wait. That kept
        # the request rate down but made every consumer command take tens of seconds: measured
        # `get_group_member_list` at 24-46 s per call while the same request, once the client had the list
        # cached, took 0.2 s. A bot consumer asks for members on every message, so "slow actions" was this
        # and nothing else.
        #
        # Caching keeps the outbound rate exactly where the throttle wanted it and makes repeats instant --
        # the same trade the reference implementation makes (a ~60 s member-list cache, chosen because
        # high-frequency 0xfe7 calls are what gets accounts flagged).
        cache_key = self._action_cache_key(action, payload)
        if cache_key is not None:
            cached = self.cache.get(cache_key)
            if cached is not None:
                self._record_action(action, started, ok=True)
                return cached
        # 出站节流：敏感动作（群/成员列表）与消息发送都按节奏放行。上层代码
        # 反复调用同一个接口是最典型的脚本特征，而服务端统计到的密度是真实的
        # ——本地再怎么隐藏也改变不了这一点。
        if getattr(self.config.onebot, "throttle", True):
            from .throttle import THROTTLE

            try:
                await THROTTLE.gate(action)
            except asyncio.CancelledError:
                raise
        try:
            if lock is None:
                result = await self.registry.dispatch(action, payload, self.context())
            else:
                async with lock:
                    result = await self.registry.dispatch(action, payload, self.context())
        except BaseException:
            self._record_action(action, started, ok=False)
            raise
        ok = not (isinstance(result, dict) and result.get("status") == "failed")
        # Only successes are cached: a failure is usually "not ready yet" or a transient transport error, and
        # serving that back for the whole TTL would turn one blip into a minute of breakage.
        if ok and cache_key is not None:
            self.cache.set(cache_key, result, _ACTION_CACHE_TTL[action])
        # A successful mutation makes the lookups it touched stale; drop them rather than wait out the TTL.
        if ok:
            for prefix in _CACHE_INVALIDATIONS.get(action, ()):
                self.cache.invalidate(prefix)
        self._record_action(action, started, ok=ok)
        return result

    def _action_cache_key(self, action: str, params: dict[str, Any]) -> str | None:
        """Cache key for a read-only lookup, or None when the action must not be cached.

        `no_cache` is honoured by passing it through to the client *and* by skipping the cache here --
        a caller asking for fresh data must not be handed a stale answer from our own layer.
        """
        ttl = _ACTION_CACHE_TTL.get(action)
        if not ttl:
            return None
        if params.get("no_cache") or params.get("force"):
            return None
        material = {k: v for k, v in params.items() if k not in ("echo", "no_cache", "force")}
        try:
            suffix = json.dumps(material, ensure_ascii=False, sort_keys=True, default=str)
        except (TypeError, ValueError):
            return None
        return f"action:{action}:{suffix}"

    def _send_lock(self, action: str, params: dict[str, Any]) -> asyncio.Lock | None:
        if not self.config.onebot.serialize_sends or action not in SEND_ACTIONS:
            return None
        if params.get("group_id"):
            key = ("group", str(params["group_id"]))
        elif params.get("user_id"):
            key = ("user", str(params["user_id"]))
        else:
            return None
        return self._send_locks.setdefault(key, asyncio.Lock())

    def _record_action(self, action: str, started: float, *, ok: bool) -> None:
        elapsed = (time.perf_counter() - started) * 1000.0
        stat = self._action_stats.setdefault(action, {"calls": 0.0, "errors": 0.0, "total_ms": 0.0, "last_ms": 0.0})
        stat["calls"] += 1
        stat["total_ms"] += elapsed
        stat["last_ms"] = elapsed
        if not ok:
            stat["errors"] += 1

    def action_stats(self) -> dict[str, dict[str, float]]:
        return {name: dict(values) for name, values in self._action_stats.items()}

    async def invoke_for(self, instance: str, action: str,
                         params: dict[str, Any] | None = None) -> dict[str, Any]:
        """由某个实例的处理器执行一个动作。

        这是"网络配置收到动作后交给消息处理器执行"的入口。与 :meth:`invoke`
        的区别只在身份：这里明确指定实例，动作作用在它的账号上，不依赖
        "当前主实例"这种全局状态——多实例并行时那正是最容易串号的地方。
        """
        context = self.instances.instance(str(instance or "C0"))
        payload = dict(params or {})
        account = context.account or (self._logins.get(context.name).uin if context.name in self._logins else "")
        # 这个参数必须留着，而且必须是本实例的账号。
        #
        # 它有两个用途：传输层靠它挑 RPC 目标（headless.py 的 _rpc_target 会读
        # body["account"]），动作上下文也用它判断身份。以前这里 pop 掉它，
        # 结果是 C1 的动作被发到 C0 的宿主上——"看起来执行了，其实作用在另一个号"。
        if account:
            payload["account"] = str(account)
        previous_self_id = self.mapper.self_id
        try:
            if account:
                try:
                    self.mapper.self_id = int(account)
                except (TypeError, ValueError):
                    pass
            result = await self.invoke(action, payload)
        finally:
            self.mapper.self_id = previous_self_id
        return result

    async def send(self, op: str, params: dict[str, Any] | None = None) -> SendResult:
        return await self.transport.send(op, params)

    # --------------------------------------------------------------------- info

    def info(self) -> dict[str, Any]:
        login = self.login
        # 被踢不能只看成"未登录"：顶栏以前只显示"未登录"，用户看不到自己是被
        # QQ 踢下来的、更看不到原因。这里把状态一路透到模板。
        kicked = bool(getattr(login, "kicked", False))
        return {
            "version": __version__,
            "transport": self.transport.name,
            "transport_info": self.transport.info() if hasattr(self.transport, "info") else {},
            "transport_mode": self.config.transport.mode,
            "uin": login.uin,
            "nickname": login.nickname,
            "logged_in": login.logged_in,
            "online": login.online and login.logged_in,
            "kicked": kicked,
            "kick_reason": str(getattr(login, "kick_reason", "") or "") if kicked else "",
            "security_kicked": bool(getattr(login, "security_kicked", False)),
            "uptime": int(time.time() - self._started_at),
            "uptime_human": _human_uptime(int(time.time() - self._started_at)),
            "events_seen": self.events_seen,
            "events_dropped": self.events_dropped,
            "events_duplicate": self.events_duplicate,
            "last_event_at": self.last_event_at,
            "last_error": self._last_error(),
            "network_cards": self.network_cards(),
            "messages_stored": len(self.store),
            "storage": store_info(self.store),
            "actions": len(self.registry._actions),
            "action_categories": self.registry.categories(),
            "plugins": len(self.plugins._loaded),
            "onebot": {
                "host": self.config.onebot.host,
                "port": self.config.onebot.port,
                "ws_path": self.config.onebot.ws_path,
                "http_path": self.config.onebot.http_path,
                "sse_path": self.config.onebot.sse_path,
            },
            "webui": {"host": self.config.webui.host, "port": self.config.webui.port},
            "webui_auth": bool(self.config.webui.access_token),
            "chat_enabled": bool(self.config.webui.chat_enabled),
            "reverse_ws": self.reverse.status(),
        }
