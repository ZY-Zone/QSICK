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
        self.targets: list[tuple[str, str]] = []
        for item in targets:
            if isinstance(item, (tuple, list)) and len(item) >= 2:
                url, account = str(item[0] or "").strip(), str(item[1] or "").strip()
            else:
                url, account = str(item or "").strip(), ""
            if url:
                self.targets.append((url, account))
        self.urls = [url for url, _ in self.targets]
        self._owner = {url: account for url, account in self.targets}
        self.reconnect = max(1.0, reconnect)
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._sockets: dict[str, Any] = {}
        self._queues: dict[str, asyncio.Queue[str]] = {}
        self.connected_url = ""

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
        self.connected_url = ""

    async def broadcast(self, payload: dict[str, Any]) -> None:
        """Push an event to every connected reverse endpoint.

        OneBot v11 reverse WebSocket consumers expect the same event stream as a
        forward client; action replies must never be the only traffic.
        """
        if not self._queues:
            return
        text = json.dumps(payload, ensure_ascii=False)
        # Route by the event's own account. A connection that handshook as account X must never receive account
        # Y's events: the consumer keys its bot by `X-Self-ID`, so such an event is either discarded or, worse,
        # acted on as if X had received it.
        wanted = str(payload.get("self_id") or "").strip()
        for url, queue in list(self._queues.items()):
            owner = self._owner.get(url, "")
            if wanted and owner and owner != wanted:
                continue
            try:
                queue.put_nowait(text)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                    queue.put_nowait(text)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    log.debug("reverse ws event dropped: queue full")

    async def _run_all(self) -> None:
        await asyncio.gather(*(self._run_one(url, account) for url, account in self.targets))

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
            self._queues[url] = queue
            self._sockets[url] = socket
            log.info("reverse ws connected", url=url, self_id=connected_self_id, account=account)
            await self._send(
                socket,
                {
                    "post_type": "meta_event",
                    "meta_event_type": "lifecycle",
                    "sub_type": "connect",
                    "time": self.runtime.now(),
                    "self_id": connected_self_id,
                },
            )
            sender = asyncio.create_task(self._pump(socket, queue), name=f"qsick-reverse-send-{url}")
            # `X-Self-ID` goes out once, in the handshake, and the consumer registers the bot under
            # whatever value was current then. For a pipe session that is "0" until the first message
            # reveals the account -- and a consumer that keyed the bot as 0 then rejects every event
            # carrying the real uin. Reconnecting re-sends the header with the account known.
            # An owned connection already handshook with its account, so there is nothing to correct.
            identity = None if account else asyncio.create_task(
                self._reconnect_when_identity_known(socket, connected_self_id),
                name=f"qsick-reverse-identity-{url}")
            try:
                async for raw in socket:
                    await self._on_message(socket, raw, account)
            finally:
                for task in (sender, identity):
                    if task is None:
                        continue
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                self._queues.pop(url, None)
                self._sockets.pop(url, None)

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
        result = await self.runtime.invoke(action, {**params, "echo": payload.get("echo")})
        await self._send(socket, result)

    async def _send(self, socket: Any, payload: dict[str, Any]) -> None:
        try:
            await socket.send(json.dumps(payload, ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001
            log.debug("reverse ws send failed", error=str(exc))

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

    def status(self) -> dict[str, Any]:
        return {
            "urls": list(self.urls),
            "connected": bool(self.connected_url),
            "connected_url": self.connected_url,
        }
