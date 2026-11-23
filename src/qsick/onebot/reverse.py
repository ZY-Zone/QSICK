"""OneBot reverse WebSocket client.

QSICK dials the configured URLs and behaves like a normal OneBot client: it sends
lifecycle/heartbeat events and accepts actions. This is the deployment shape most
bot frameworks expect behind NAT.
"""

from __future__ import annotations

import asyncio
import json
import random
from typing import Any, TYPE_CHECKING

import websockets

from .. import log as logmod

if TYPE_CHECKING:  # pragma: no cover
    from ..runtime import Runtime

log = logmod.get_logger("OneBot.Reverse")


def is_self_sent_to_others(payload: dict[str, Any]) -> bool:
    """True when this event is a message the account itself sent to somebody else.

    Those are the ones `report_self_message` governs. A private chat with the account itself is excluded: writing
    to your own account is an ordinary conversation, and hiding it would make a working login look broken.

    Both shapes count. Our own outgoing message normally arrives as `message_sent`; a message we sent can also
    come back as a `message` event whose `user_id` is the account itself.
    """
    kind = str(payload.get("post_type") or "")
    if kind not in ("message", "message_sent"):
        return False
    self_id = str(payload.get("self_id") or "").strip()
    if not self_id:
        return False
    private = str(payload.get("message_type") or "") == "private"
    target = str(payload.get("target_id") or "").strip()
    same_account = str(payload.get("user_id") or "").strip() == self_id
    if kind == "message_sent":
        # `target_id` is the peer of a private message. Treat a shape we do not recognise as self-sent, so an
        # unexpected payload cannot slip past a profile that switched reporting off.
        return not (private and target and target == self_id)
    if not same_account:
        return False
    return not private


class ReverseWebSocketClient:
    def __init__(self, runtime: "Runtime", targets: list[Any], reconnect: float = 5.0) -> None:
        """`targets` is `(url, account)` pairs; a bare string means "any account".

        Ownership has to travel with the URL. With a flat list, every connection carried every account's events:
        the consumer registered one bot per connection (from `X-Self-ID`) and then discarded the events whose
        `self_id` it did not expect, reporting "找不到对应Bot，忽略消息". Worse, the reply that followed was built
        against the wrong account, which is how an action for one account ended up querying a group the other
        account is not in.
        """
        self.runtime = runtime
        #: `(url, account, report_self, protocol, profile_id)`.
        #:
        #: `protocol` also travels with the connection, for the same reason ownership does: the fan-out has to know
        #: which protocol this consumer speaks, and the action intake has to know which protocol to decode with.
        #: Before this, every reverse connection received the identical OneBot v11 JSON regardless of what its
        #: profile asked for.
        self.targets: list[tuple[str, str, bool, str, str]] = []
        for item in targets:
            if isinstance(item, (tuple, list)) and len(item) >= 2:
                url = str(item[0] or "").strip()
                account = str(item[1] or "").strip()
                report_self = bool(item[2]) if len(item) >= 3 else True
                protocol = str(item[3] or "").strip() if len(item) >= 4 else ""
                profile_id = str(item[4] or "").strip() if len(item) >= 5 else ""
            else:
                url, account, report_self, protocol, profile_id = str(item or "").strip(), "", True, "", ""
            if url:
                self.targets.append((url, account, report_self, protocol, profile_id))
        self.urls = [url for url, _, _, _, _ in self.targets]
        self.reconnect = max(1.0, reconnect)
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        # Keyed by (url, account), not by URL. One consumer URL serving several bots is the ordinary
        # multi-account setup, and a URL-keyed dict silently kept only one session per URL: the second
        # `_session` overwrote the first one's queue and socket, so one account's events had nowhere to go
        # and its actions ran as the other account.
        self._sockets: dict[tuple[str, str], Any] = {}
        self._queues: dict[tuple[str, str], asyncio.Queue[str]] = {}
        #: Per connection: does this profile want the account's own outgoing messages?
        self._report_self: dict[tuple[str, str], bool] = {}
        #: Per connection: which protocol it speaks. Empty means "the default", resolved at send time.
        self._protocols: dict[tuple[str, str], str] = {}
        self.connected_url = ""

    def alive(self) -> bool:
        """True while the reconnect loop is still running.

        Used to decide whether an unchanged target set needs a rebuild at all. A stopped client has no task and
        answers False, so it is still replaced -- this only preserves a client that is doing its job.
        """
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if not self.urls or self._task is not None:
            return
        self._task = asyncio.create_task(self._run_all(), name="qsick-reverse-ws")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self._queues.clear()
        self._sockets.clear()
        self._report_self.clear()
        self._protocols.clear()
        self.connected_url = ""

    async def broadcast(self, payload: dict[str, Any]) -> None:
        """Push an event to every connected reverse endpoint.

        OneBot v11 reverse WebSocket consumers expect the same event stream as a
        forward client; action replies must never be the only traffic.
        """
        if not self._queues:
            return
        # Route by the event's own account. A connection that handshook as account X must never receive account
        # Y's events: the consumer keys its bot by `X-Self-ID`, so such an event is either discarded or, worse,
        # acted on as if X had received it.
        wanted = str(payload.get("self_id") or "").strip()
        # One decision per connection: two profiles may disagree about self-messages, so a single filtered copy
        # for everyone would make the option unable to differ between them.
        self_sent = is_self_sent_to_others(payload)
        # Encoded once per protocol, not once per connection: N consumers of the same protocol must not be able to
        # receive different JSON for one event.
        frames: dict[str, str | None] = {}
        for (url, owner), queue in list(self._queues.items()):
            if wanted and owner and owner != wanted:
                continue
            if self_sent and not self._report_self.get((url, owner), True):
                continue
            protocol = self._protocols.get((url, owner), "")
            if protocol not in frames:
                frames[protocol] = self._encode(protocol, payload)
            text = frames[protocol]
            if text is None:
                continue          # this protocol could not encode the event; skip its connections only
            try:
                queue.put_nowait(text)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                    queue.put_nowait(text)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    log.debug("reverse ws event dropped: queue full")

    def _encode(self, protocol: str, payload: dict[str, Any]) -> str | None:
        """Serialize one event for one protocol. `None` means "this protocol cannot express it"."""
        pipelines = getattr(self.runtime, "pipelines", None)
        encoded: Any = payload
        if pipelines is not None:
            encoded = pipelines.encode_event(protocol, payload)
            if encoded is None:
                return None
        try:
            return json.dumps(encoded, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            log.warn("reverse ws event not serializable", protocol=protocol, error=str(exc))
            return None

    async def _run_all(self) -> None:
        await asyncio.gather(*(self._run_one(url, account) for url, account, *_ in self.targets))

    async def _run_one(self, url: str, account: str = "") -> None:
        # 固定间隔重连会在对端持续不可达时变成"每 N 秒敲一次"的稳定节拍。对
        # OneBot 反向端来说这是本地行为，但对端若正是转发到 QQ 的中继，这个节拍
        # 会原样传到服务器侧。改成指数退避 + 抖动。
        #
        # 但"连上过、随后断开"不该挨罚：那是正常重连，不是对端不可达。只有连不
        # 上才逐级拉长间隔——否则每次正常断开都要多等一轮，白白拉长恢复时间。
        state = {"failures": 0}
        while not self._stopping.is_set():
            try:
                await self._session(url, account)
                # Reached the end of a session without an exception: the peer accepted us and then closed.
                # A clean close is a working peer, so the next attempt is not a failure.
                state["failures"] = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect on any failure
                state["failures"] = min(state["failures"] + 1, 6)
                log.warn("reverse ws disconnected", url=url, error=f"{type(exc).__name__}: {exc}")
            finally:
                self.connected_url = ""
            if state["failures"] == 0:
                await asyncio.sleep(max(0.2, self.reconnect))
                continue
            # Cap low on purpose. The target is a consumer on this machine (or the LAN), so "come back
            # quickly" matters far more than politeness: the old 300 s ceiling meant a consumer that was
            # restarted could sit disconnected for minutes, which reads as "keeps dropping the connection".
            delay = min(self.reconnect * (2 ** max(0, state["failures"] - 1)), 15.0)
            delay *= 1.0 + random.random() * 0.25
            await asyncio.sleep(max(1.0, delay))

    async def _await_identity(self, timeout: float = 15.0) -> int:
        """Wait briefly for the account to be known before the first handshake.

        `X-Self-ID` is sent once, in the handshake, and the consumer registers the bot under it. A pipe
        session has no login callback into this process -- the account is learned from its first received
        message -- so an app started before any traffic would otherwise announce itself as `0`, and the only
        way to correct that is to drop the connection and reconnect.

        That reconnect is what shows up as "frequently disconnecting": measured in a consumer log as both
        sockets dropping within 3 ms and both reconnecting within 5 ms, minutes after startup. Waiting here
        costs nothing when an account is already known (the usual case, and any restart), and turns the flap
        into a slightly later but correct first connection.
        """
        if self.runtime.self_id:
            return self.runtime.self_id
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if self._stopping.is_set():
                break
            await asyncio.sleep(0.2)
            current = self.runtime.self_id
            if current:
                log.info("reverse ws waited for account before connecting", self_id=current)
                return current
        return self.runtime.self_id

    async def _session(self, url: str, account: str = "") -> None:
        # A connection with a known owner does not wait for one: the account came from the profile, not from
        # traffic, so there is nothing to wait for. Only an unowned (legacy) connection guesses.
        if account:
            connected_self_id = int(account) if account.isdigit() else 0
        else:
            await self._await_identity()
            connected_self_id = self.runtime.self_id
        headers = self._headers(connected_self_id)
        try:
            connector = websockets.connect(url, additional_headers=headers, max_size=16 * 1024 * 1024)
        except TypeError:  # websockets < 12
            connector = websockets.connect(url, extra_headers=headers, max_size=16 * 1024 * 1024)  # type: ignore[call-arg]

        async with connector as socket:
            self.connected_url = url
            queue: asyncio.Queue[str] = asyncio.Queue(maxsize=512)
            self._queues[(url, account)] = queue
            self._sockets[(url, account)] = socket
            self._report_self[(url, account)] = self._report_for(url, account)
            self._protocols[(url, account)] = self._protocol_for(url, account)
            log.info("reverse ws connected", url=url, self_id=connected_self_id, account=account)
            lifecycle = {
                "post_type": "meta_event",
                "meta_event_type": "lifecycle",
                "sub_type": "connect",
                "time": self.runtime.now(),
                "self_id": connected_self_id,
            }
            protocol = self._protocol_for(url, account)
            await self._send(socket, _as_protocol(self.runtime, protocol, lifecycle))
            sender = asyncio.create_task(self._pump(socket, queue), name=f"qsick-reverse-send-{url}")
            # `X-Self-ID` goes out once, in the handshake, and the consumer registers the bot under
            # whatever value was current then. For a pipe session that is "0" until the first message
            # reveals the account -- and a consumer that keyed the bot as 0 then rejects every event
            # carrying the real uin. Reconnecting re-sends the header with the account known.
            # An owned connection already handshook with its account, so there is nothing to correct.
            identity = None if account else asyncio.create_task(
                self._reconnect_when_identity_known(socket, connected_self_id),
                name=f"qsick-reverse-identity-{url}")
            # One task per frame, not one at a time.
            #
            # `await self._on_message(...)` inside the read loop made every action wait for the previous one to
            # finish. A consumer issues its lookups back to back on connect, so one slow action held the rest in
            # the socket buffer until the consumer gave up and closed -- measured in the consumer as "请求超时"
            # for actions that had not even started. Actions are independent; nothing here needs the ordering.
            handlers: set[asyncio.Task[None]] = set()
            try:
                async for raw in socket:
                    task = asyncio.create_task(
                        self._on_message(socket, raw, account),
                        name=f"qsick-reverse-action-{url}")
                    handlers.add(task)
                    task.add_done_callback(handlers.discard)
            finally:
                for task in list(handlers):
                    task.cancel()
                for task in (sender, identity):
                    if task is None:
                        continue
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                self._queues.pop((url, account), None)
                self._sockets.pop((url, account), None)

    async def _reconnect_when_identity_known(self, socket: Any, connected_self_id: int) -> None:
        """Last-resort correction: close the socket once the account is known, so the next handshake names it.

        `_await_identity` already makes the normal case correct, so this only runs when the handshake went
        out as `0` and the account turned up afterwards. It is deliberately bounded in both directions:

        * it never fires when the handshake already carried a real id
        * it gives up after 60 s rather than watching for the rest of the process's life -- an unbounded
          watcher is what produced a reconnect minutes after startup, long after anyone was watching
        * it fires at most once

        Closing on *any* identity change would be worse than useless: with several accounts the primary can
        move, and every close drops whatever actions the consumer had in flight -- which their side reports
        as a 60 s request timeout, not as a disconnect.
        """
        if connected_self_id:
            return
        deadline = asyncio.get_running_loop().time() + 60.0
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(1.0)
            current = self.runtime.self_id
            if current and current != connected_self_id:
                # WARN, not INFO: this costs the consumer a reconnect, so it must be visible in a log
                # configured at WARN. It was silently happening before because nothing recorded it.
                log.warn("reverse ws reconnecting with known self_id; handshake had none",
                         url=self.connected_url, was=connected_self_id, now=current)
                try:
                    await socket.close()
                except Exception as exc:  # noqa: BLE001 - the reconnect loop owns recovery
                    log.debug("reverse ws close failed", error=str(exc))
                return

    async def _pump(self, socket: Any, queue: asyncio.Queue[str]) -> None:
        """Drain queued events onto this reverse socket, independently of replies."""
        while True:
            text = await queue.get()
            try:
                await socket.send(text)
            except Exception as exc:  # noqa: BLE001 - reconnect loop owns recovery
                log.debug("reverse ws event send failed", error=str(exc))
                return

    async def _on_message(self, socket: Any, raw: str | bytes, account: str = "") -> None:
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            log.debug("reverse ws: non-JSON frame ignored")
            return
        if not isinstance(payload, dict):
            return
        action = str(payload.get("action", ""))
        params = payload.get("params") or {}
        if not isinstance(params, dict):
            params = {}
        # The connection's own account decides which session runs the action. Without this, an action that
        # arrived on account X's connection executed as the primary account -- querying groups X is not in,
        # and sending replies as the wrong self_id.
        if account and not str(params.get("account") or "").strip():
            params = {**params, "account": account}
        protocol = self._protocol_of(account)
        pipelines = getattr(self.runtime, "pipelines", None)
        if pipelines is not None:
            try:
                action, params = pipelines.decode_action(protocol, action, params)
            except Exception as exc:  # noqa: BLE001 - 回一个"不认识"，别让对端等到超时
                await self._send(socket, {
                    "status": "failed", "retcode": 1404,
                    "message": f"{protocol}: cannot decode action {action!r}: {exc}",
                    "data": None, "echo": payload.get("echo"),
                })
                return
        result = await self.runtime.invoke(action, {**params, "echo": payload.get("echo")})
        if pipelines is not None:
            result = pipelines.format_reply(protocol, result, payload.get("echo"))
        await self._send(socket, result)

    async def _send(self, socket: Any, payload: dict[str, Any]) -> None:
        try:
            await socket.send(json.dumps(payload, ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001
            # WARN, not debug, when an *answer* is lost. The consumer waits its full timeout for this frame, so
            # losing it silently is what turns one dropped connection into a minute of apparent hangs. Action
            # replies carry `echo`; events never do.
            if payload.get("echo") is not None:
                log.warn("reverse ws answer lost: the connection closed before it could be sent",
                         action=payload.get("action"), echo=payload.get("echo"), error=str(exc))
            else:
                log.debug("reverse ws send failed", error=str(exc))

    def _protocol_of(self, account: str) -> str:
        """Protocol of the connection that delivered an action.

        Keyed by account because `_on_message` carries the account, not the URL: one consumer URL may serve several
        bots, and the account is what tells them apart. Falls back to the default when unknown (a legacy flat URL
        list has no profile, hence no protocol).
        """
        for (url, owner), protocol in self._protocols.items():
            if owner == account and protocol:
                return protocol
        return ""

    def _protocol_for(self, url: str, account: str) -> str:
        """Protocol of the profile behind this connection, or empty for the default."""
        for target_url, target_account, _report, protocol, _profile in self.targets:
            if target_url == url and target_account == account:
                return protocol
        return ""

    def _report_for(self, url: str, account: str) -> bool:
        """Does this profile want the account's own outgoing messages?

        Read from `targets` rather than a second dict built at construction, so the answer cannot drift from the
        connection that is actually being opened.
        """
        for target_url, target_account, report, _protocol, _profile in self.targets:
            if target_url == url and target_account == account:
                return report
        return True

    def _headers(self, self_id: int | None = None) -> dict[str, str]:
        token = self.runtime.config.onebot.access_token
        headers = {
            "X-Self-ID": str(self_id if self_id is not None else self.runtime.self_id),
            "X-Client-Role": "Universal",
            "User-Agent": "QSICK/1",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def protocols(self) -> list[str]:
        """Protocols with at least one live reverse connection."""
        return sorted({item for item in self._protocols.values() if item})

    def status(self) -> dict[str, Any]:
        return {
            "urls": list(self.urls),
            "connected": bool(self.connected_url),
            "connected_url": self.connected_url,
            "protocols": self.protocols(),
        }


def _as_protocol(runtime: Any, protocol: str, event: dict[str, Any]) -> dict[str, Any]:
    """Encode one event for `protocol`; fall back to the internal shape when it cannot be expressed.

    Losing the first frame entirely would leave the consumer waiting for a lifecycle event that never arrives, so a
    failed encode sends the internal shape instead -- structurally wrong, but present and diagnosable.
    """
    pipelines = getattr(runtime, "pipelines", None)
    if pipelines is None:
        return event
    encoded = pipelines.encode_event(protocol, event)
    return encoded if encoded is not None else event
