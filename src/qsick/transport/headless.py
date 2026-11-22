"""Headless QQ event ingress and explicit, lossless action RPC."""
from __future__ import annotations

import asyncio
import json
import math
import time
from pathlib import Path
from typing import Any, AsyncIterator
import urllib.error
import urllib.request

from ..log import get_logger
from .base import LoginState, PacketEvent, SendResult, Transport

log = get_logger("Transport.Headless")
ERROR_CODES = {"INVALID_PARAMS": 1400, "UNSUPPORTED": 1502, "NOT_READY": 1501,
               "TIMEOUT": 1501, "NOT_FOUND": 1404, "SERVICE_ERROR": 1501, "INVALID_RESULT": 1501}


def _wire_value(value: Any) -> Any:
    """Keep Python uint64 IDs exact when parsed by the JavaScript host."""
    if isinstance(value, int) and not isinstance(value, bool) and abs(value) > (1 << 53) - 1:
        return str(value)
    if isinstance(value, dict):
        return {key: _wire_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_wire_value(item) for item in value]
    return value


class HeadlessTransport(Transport):
    name = "headless"

    def __init__(self, port: int = 9100, maxsize: int = 4096, host_root: str | Path | None = None) -> None:
        # 每个实例各自一份登录状态：消息监听下放到实例后，同时登录 C0 与 C1
        # 是正常状态，共用一个 LoginState 会让后到的登录覆盖前一个。
        self._logins: dict[str, LoginState] = {}
        self._active_instance: str = ""
        self._queue: asyncio.Queue[PacketEvent] = asyncio.Queue(maxsize=maxsize)
        self._stop = asyncio.Event()
        self._port = int(port)
        self._host_root = Path(host_root).resolve() if host_root else None
        # 账号 → (port, monotonic 时间)。见 `_pipe_session_port`。
        self._pipe_ports: dict[str, tuple[int | None, float]] = {}
        #: account -> session tmp_id, published by the runtime from its own state. The port is then read from
        #: that session's `dll/<tmp_id>/qsick_pipe.json`, so a session that binds a new port is followed without
        #: waiting for the next poll.
        self._account_sessions: dict[str, str] = {}

    @property
    def login(self) -> LoginState:
        """当前"主"实例的登录状态。

        保留这个属性是为了不打断既有调用方（顶栏、动作上下文）：它在多实例下
        返回最近一次登录的那个实例。需要精确到实例时用 :meth:`login_for`。
        """
        if self._active_instance and self._active_instance in self._logins:
            return self._logins[self._active_instance]
        if self._logins:
            return next(reversed(self._logins.values()))
        return LoginState(logged_in=False, uin="0")

    def login_for(self, instance: str) -> LoginState:
        return self._logins.get(instance) or LoginState(logged_in=False, uin="0")

    @property
    def _login(self) -> LoginState:
        """兼容旧的单值访问（``transport._login``）。

        多实例改造后真实状态在 ``_logins`` 里；这个属性返回"主实例"的状态，
        让仍然按单账号写法读它的代码（含既有测试与工具脚本）继续工作。
        """
        return self.login

    @_login.setter
    def _login(self, state: LoginState) -> None:
        key = self._active_instance or "C0"
        self._logins[key] = state
        if state.logged_in:
            self._active_instance = key

    def logins(self) -> dict[str, LoginState]:
        return dict(self._logins)

    async def start(self) -> None:
        self._stop.clear()
        log.info("headless bridge ready")

    async def stop(self) -> None:
        self._stop.set()
        try:
            self._queue.put_nowait(PacketEvent(cmd="runtime.stop", kind="runtime", body=b"{}"))
        except asyncio.QueueFull:
            pass

    def push(self, event: dict[str, Any]) -> None:
        kind = str(event.get("kind") or "message")
        payload = event.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("event payload must be an object")
        # 事件来自哪个实例由入口附带（WebUI 的 /api/headless/event 会填上
        # X-QSICK-Instance）。网络配置按实例过滤事件时依赖它。
        instance = str(event.get("instance") or "").strip()
        if kind == "login":
            # 宿主在踢下线事件里带上了 kicked / reason（见 headless_host.js 的
            # onKickedOffline）。早先这里只挑 logged_in/uin/nickname/online，
            # 把"为什么掉线"丢在了传输层，于是顶栏只剩一句"未登录"。
            state = LoginState(logged_in=bool(payload.get("logged_in", False)),
                               uin=str(payload.get("uin") or "0"), nickname=str(payload.get("nickname") or ""),
                               online=bool(payload.get("online", payload.get("logged_in", False))),
                               kicked=bool(payload.get("kicked", False)),
                               kick_reason=str(payload.get("reason") or payload.get("kick_reason") or ""),
                               security_kicked=bool(payload.get("security_kicked_type")
                                                   or payload.get("security_kicked", False)))
            # 登录状态按实例保存：每个实例是独立会话，共用一份会让 C1 的登录
            # 覆盖掉 C0 的状态，界面上"哪个号在线"就无从判断。
            key = instance or "C0"
            self._logins[key] = state
            if state.logged_in:
                self._active_instance = key
            cmd = "login"
        elif kind in ("message", "message_sent"):
            chat = payload.get("chat")
            if not isinstance(chat, dict) or chat.get("type") not in ("group", "private"):
                raise ValueError("unsupported message chat type")
            mid = payload.get("message_id")
            if not isinstance(mid, (int, str)) or isinstance(mid, bool) or not str(mid).isdigit() or int(mid) <= 0:
                raise ValueError("message_id must be a positive decimal integer/string")
            cmd = "message.sent" if kind == "message_sent" else "message." + chat["type"]
            # Per-message tracing lives at DEBUG: a busy group would otherwise fill
            # the log with one INFO line per received message.
            log.debug("headless message received", message_id=str(mid), chat_type=chat["type"], chat_id=chat.get("id"))
        elif kind == "message_media":
            # A late media download completed for an already-stored message.
            cmd = "message.media"
        elif kind in ("notice", "request", "runtime"):
            cmd = str(event.get("cmd") or "")
            if not cmd.startswith(kind + "."):
                raise ValueError("event kind/cmd mismatch")
        else:
            raise ValueError("unsupported headless event kind")
        # 事件归属的 QQ 号：优先载荷自带的 account，其次该实例的登录状态。
        # 多实例并行时，self_id 必须逐条随事件确定，不能读全局单值。
        owner = str(payload.get("account") or "").strip()
        if not owner and instance:
            owner = str(self.login_for(instance).uin or "")
        if owner == "0":
            owner = ""
        packet = PacketEvent(cmd=cmd, kind=kind, instance=instance, account=owner,
                             body=json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"))
        try:
            self._queue.put_nowait(packet)
        except asyncio.QueueFull:
            self._queue.get_nowait()
            self._queue.put_nowait(packet)
            log.warn("headless event queue full; oldest event dropped")

    def _instance_dirs(self) -> list[str]:
        """返回可寻址的实例：C0、C1……以及 pipe 实例。

        原来只认 ``C<数字>``。pipe 实例的目录名是 ``Pipe``（kind 为 pipe，有自己的 QQ.exe 与
        HTTP 端口），被这条过滤条件整个排除在外——于是协议层的动作永远落不到一个有真客户端的
        实例上。这正是"API 只能在无真客户端的 headless 实例上可用"的根因。

        判据改为"目录里有 config.json"：名字不重要，配置文件在才说明它是本模块能寻址的实例。
        编号实例保持原有顺序排在前面，其余按字母序排在后面——不传 account 时取第一个，
        那个回退行为不能从 C0 悄悄变成 Pipe。
        """
        if not self._host_root or not self._host_root.is_dir():
            return []
        try:
            entries = [item for item in self._host_root.iterdir() if item.is_dir()]
        except OSError:
            return []

        def numbered(name: str) -> bool:
            return name.startswith("C") and name[1:].isdigit()

        usable = [item.name for item in entries if (item / "config.json").is_file()]
        first = sorted((name for name in usable if numbered(name)), key=lambda n: int(n[1:]))
        rest = sorted(name for name in usable if not numbered(name))
        return first + rest

    def _host_config(self, instance: str) -> dict[str, Any]:
        if not self._host_root:
            return {}
        try:
            data = json.loads((self._host_root / instance / "config.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _instance_for_account(self, account: str) -> str:
        """把账号（实例名或 QQ 号）解析成实例目录名。

        账号可以是 ``C1``（实例名）或 QQ 号——WebUI 的多数请求带的是后者。
        """
        wanted = str(account or "").strip()
        if not wanted:
            # 未点名时优先 C0（既有的单实例行为），其次任何存在的实例。
            names = self._instance_dirs()
            if "C0" in names:
                return "C0"
            return names[0] if names else "C0"
        if wanted in self._instance_dirs():
            return wanted
        for name in self._instance_dirs():
            if str(self._host_config(name).get("qq") or "") == wanted:
                return name
        raise ValueError(f"unknown instance {wanted!r}")

    def set_account_sessions(self, mapping: dict[str, str]) -> None:
        """Publish which session serves which account, as `uin -> tmp_id`.

        The runtime owns this: each session's state link reports the account it is logged in as, and the session
        list carries every `tmp_id`. Handing the pairing over means an action's destination is resolved by a dict
        lookup plus a file read instead of a request to the session -- which matters because the DLL serves one
        connection at a time, so probing during a send finds the session "not running".
        """
        clean = {str(uin).strip(): str(tmp).strip() for uin, tmp in (mapping or {}).items()
                 if str(uin).strip() and str(tmp).strip()}
        if clean != self._account_sessions:
            self._account_sessions = clean

    def _session_nowport(self, tmp_id: str) -> int:
        """The port the named session actually bound, from its own `qsick_pipe.json`.

        `nowport`, never `port`: the latter is what the operator asked for and only changes when they edit it,
        while a running process keeps listening on the number it bound.
        """
        from .. import headless as headless_mod

        try:
            config = headless_mod.read_session_config(str(tmp_id), self._host_root)
            _asked, bound = headless_mod.session_ports(config or {})
        except Exception as exc:  # noqa: BLE001 - an unreadable config means "cannot resolve", not a crash
            log.debug("could not read the session config", session=str(tmp_id), error=str(exc))
            return 0
        return int(bound or 0)

    def _pipe_session_port(self, account: str, *, recheck: bool = False) -> int | None:
        """Find the HTTP port of the pipe session that owns `account`.

        A pipe instance is one `Pipe/` client launched several times; each launch is a **session** with its own
        tmp_id, its own DLL, and its own port, recorded as `nowport` in
        `Pipe/dll/<tmp_id>/qsick_pipe.json`. The instance-level `Pipe/config.json` port is a different number
        and nothing listens there, so routing an action to it gives "connection refused" -- measured, and it is
        why every send from a pipe account failed while receives worked.

        Which session belongs to `account` is answered by asking each one: `login.info` returns the uin of the
        process that answered. That is a per-session HTTP round trip, so the answer is cached briefly -- long
        enough to keep a burst of actions cheap, short enough that a restart on a new port is picked up.
        """
        from .. import headless as headless_mod

        if not self._host_root:
            return None
        wanted = str(account or "").strip()

        now = time.monotonic()
        if wanted and not recheck:
            # Account -> session -> bound port, and no request to the session at all.
            #
            # The runtime publishes which `tmp_id` serves which account (it knows from the state links); the port
            # then comes from that session's own `dll/<tmp_id>/qsick_pipe.json`, which is authoritative and free
            # to read. Resolving by asking the session instead was measured to fail exactly when it mattered:
            # during a 17.9 s upload, twelve consecutive `login.info` probes all answered "not running", because
            # the DLL served one connection at a time.
            tmp_id = self._account_sessions.get(wanted)
            if tmp_id:
                port = self._session_nowport(tmp_id)
                if port > 0:
                    log.debug("action target resolved", account=wanted, session=tmp_id, port=port)
                    self._pipe_ports[wanted] = (port, now)
                    return port
            log.warn("account is not in the session table", account=wanted,
                     known=sorted(self._account_sessions))
        cached = self._pipe_ports.get(wanted)
        # Kept for two minutes on purpose. Answering costs one full `login.info` round trip through the session's
        # task queue -- measured at 2.3 s, and that queue is serial -- so a short TTL made every action pay it
        # again. A stale answer is handled where it can actually be noticed: a refused connection drops the entry
        # and resolves once more.
        if cached and now - cached[1] < 120.0 and cached[0]:
            return cached[0]

        try:
            sessions = headless_mod.read_session_dirs(self._host_root)
        except Exception as exc:  # noqa: BLE001 - a broken session list must not break dispatch
            log.debug("pipe session enumeration failed", error=str(exc))
            return None
        if not sessions:
            return None

        def match(timeout: float) -> tuple[int | None, int | None]:
            found: int | None = None
            first_live: int | None = None
            for session in sessions:
                port = int(session.get("nowport") or 0)
                if port <= 0:
                    continue
                probe = headless_mod.probe_session_login(port, timeout=timeout)
                if not probe.get("running"):
                    continue
                if first_live is None:
                    first_live = port
                if wanted and probe.get("logged_in") and str(probe.get("uin") or "") == wanted:
                    return port, first_live
            return found, first_live

        resolved, fallback = match(1.5)
        if resolved is None and wanted:
            # A session that is still attaching answers nothing, and one busy uploading can miss a 1.5 s probe.
            # Asking once more before concluding "not here" is the difference between a slow action and a failed
            # one: the caller has already proved the account exists, since it is live enough to have sent us
            # something.
            resolved, fallback = match(3.0)

        if resolved is None and not wanted:
            # No account named. An only-live-session is the right answer -- it is unambiguous. With several
            # running there is nothing to choose by, and answering anyway is how an action came to be executed by
            # an account it was not addressed to. Refusing is the honest answer; the caller reports it.
            live = [int(s.get("nowport") or 0) for s in sessions
                    if headless_mod.probe_session_login(int(s.get("nowport") or 0)).get("running")]
            resolved = live[0] if len(live) == 1 else None

        # Never cache a guess, and never cache a negative: only a port matched to the account (or the single live
        # one) is worth remembering, and a miss must not become sticky.
        if resolved:
            self._pipe_ports[wanted] = (resolved, now)
        else:
            self._pipe_ports.pop(wanted, None)
        return resolved

    def _rpc_target(self, account: str) -> tuple[int, str]:
        # 动作必须落在拥有该账号的实例上。以前这里写死 C0，等于假定只有一个
        # 账号——多实例时 C1 的动作会打到 C0 的宿主上。
        #
        # Pipe sessions come first: their account is a QQ number that no `C<n>` config will match, and they
        # are the instances that actually run a real client.
        pipe_port = self._pipe_session_port(account)
        if pipe_port:
            return pipe_port, ""
        if account.isdigit():
            # A QQ number is never an instance name, so falling through would report "unknown instance" -- which
            # names the wrong problem entirely: the account is fine, its session is not answering. Saying that
            # is what makes the failure actionable.
            raise RuntimeError(f"no running pipe session is serving account {account}")
        if not account and self._host_root:
            # No account at all. With one session running that is unambiguous and `_pipe_session_port` already
            # answered; reaching here means several are running and there is nothing to choose by. Falling
            # through would address a headless port that a pipe-only install does not have, which reads as
            # "connection refused" and hides the real problem: the request never said who should execute it.
            try:
                from .. import headless as headless_mod
                sessions = [s for s in headless_mod.read_session_dirs(self._host_root)
                            if int(s.get("nowport") or 0) > 0]
            except Exception:  # noqa: BLE001 - keep the original path if the list cannot be read
                sessions = []
            if len(sessions) > 1:
                raise RuntimeError(
                    "the request does not say which account should execute it, and several sessions are running")
        instance = self._instance_for_account(account)
        data = self._host_config(instance)
        if not data:
            if not self._host_root:
                return self._port, ""
            raise RuntimeError(f"headless {instance} configuration is unavailable")
        return int(data.get("port") or self._port), str(data.get("bridgeToken") or "")

    def _send_sync(self, body: dict[str, Any], timeout: float) -> dict[str, Any]:
        try:
            return self._send_once(body, timeout)
        except urllib.error.URLError as exc:
            # The port is cached for five seconds to keep a burst of actions cheap. A session that restarted in
            # that window is addressed at a port nothing listens on any more, which surfaces as connection
            # refused rather than as anything that names the cause. Drop the cached answer and resolve again.
            account = str(body.get("account") or "")
            stale = self._pipe_ports.pop(account, None)
            # Drop the published answer too: it is what produced the stale port, so keeping it would make the
            # retry repeat the same mistake. With it gone the retry falls through to a real probe.
            published = self._account_sessions.pop(account, None) is not None
            if not isinstance(exc, urllib.error.HTTPError) and (stale or published):
                return self._send_once(body, timeout)
            raise

    def _send_once(self, body: dict[str, Any], timeout: float) -> dict[str, Any]:
        port, token = self._rpc_target(str(body.get("account") or ""))
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        request = urllib.request.Request(f"http://127.0.0.1:{port}/action",
            data=json.dumps(_wire_value(body), ensure_ascii=False, allow_nan=False).encode("utf-8"), headers=headers, method="POST")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=timeout) as response:  # noqa: S310 - fixed loopback
                result = json.loads(response.read(8 * 1024 * 1024).decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                result = json.loads(exc.read(65536).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise RuntimeError(f"headless host returned HTTP {exc.code}") from exc
            if not isinstance(result, dict) or result.get("ok") is not False:
                raise RuntimeError(f"headless host returned HTTP {exc.code}") from exc
        if not isinstance(result, dict) or not isinstance(result.get("ok"), bool):
            raise RuntimeError("invalid headless RPC response")
        return result

    async def send(self, op: str, params: dict[str, Any] | None = None, *, timeout: float | None = None) -> SendResult:
        if not isinstance(op, str) or not op or (params is not None and not isinstance(params, dict)):
            return SendResult.failure("invalid headless action request", status=1400)
        if op == "rkey":
            # The media URL builder asks for rkey; the host fetches it over OIDB.
            op = "system.rkey"
        payload = dict(params or {})
        account = str(payload.pop("account", "") or "")
        try:
            duration = float(timeout) if timeout is not None else 20.0
            if not math.isfinite(duration) or duration <= 0:
                raise ValueError("timeout must be positive")
            result = await asyncio.wait_for(asyncio.to_thread(self._send_sync,
                {"op": op, "params": payload, "account": account}, duration), timeout=duration + .5)
        except (ValueError, TypeError) as exc:
            return SendResult.failure(str(exc), status=1400)
        except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError, RuntimeError) as exc:
            return SendResult.failure(f"headless action failed: {exc}", status=1501)
        if result["ok"] is not True:
            return SendResult.failure(str(result.get("error") or "headless action failed"),
                                      status=ERROR_CODES.get(str(result.get("code")), 1501))
        data = result.get("data")
        list_fields = {"group.list": "groups", "get_group_list": "groups", "friend.list": "friends",
                       "get_friend_list": "friends", "group.members": "members", "get_group_member_list": "members",
                       "group.shut_list": "members", "get_group_shut_list": "members",
                       "friend.categories": "categories", "get_friends_with_category": "categories",
                       "group.admins": "members", "get_group_admin_list": "members",
                       "friend.statuses": "friends", "get_friend_status_list": "friends",
                       "friend.relations": "friends", "get_friend_relation_list": "friends",
                       "forward.get": "message", "get_forward_msg": "message",
                       "recent.list": "contacts", "get_recent_contact": "contacts",
                       "get_recent_contact_list": "contacts",
                       "message.reactions": "reactions", "get_msg_reaction": "reactions",
                       "get_msg_emoji_likes": "reactions"}
        if isinstance(data, list) and op in list_fields:
            data = {list_fields[op]: data}
        if not isinstance(data, dict):
            return SendResult.failure("headless action returned an invalid payload", status=1501)
        return SendResult(ok=True, data=data)

    async def events(self) -> AsyncIterator[PacketEvent]:
        while not self._stop.is_set():
            event = await self._queue.get()
            if self._stop.is_set():
                break
            yield event
