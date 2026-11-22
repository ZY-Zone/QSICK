"""Payload event -> OneBot v11 event JSON.

Suppression policy lives here: noise (typing/input status) is logged at TRACE and
never forwarded unless ``forward_noise`` is on, so clients do not wake up for
something a human never sees.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from .. import log as logmod
from ..config import RuntimeConfig
from ..protocol import elements as E
from ..protocol.media import MediaResolver
from ..protocol.message import Message
from ..storage import MessageStore
from ..transport.base import PacketEvent

log = logmod.get_logger("OneBot.Events")
noise_log = logmod.get_logger("Noise")

NOISE_CMDS = {"noise.typing", "noise.input_status", "noise.typing_state"}


class EventMapper:
    def __init__(
        self,
        config: RuntimeConfig,
        store: MessageStore,
        media: MediaResolver,
        *,
        self_id: int,
    ) -> None:
        self.config = config
        self.store = store
        self.media = media
        self.self_id = self_id
        # Set by the runtime so heartbeat/lifecycle reflect the real login state
        # instead of always reporting a healthy connection.
        self.login: Any = None

    async def map(self, event: PacketEvent) -> list[dict[str, Any]]:
        cmd = event.cmd
        if cmd in NOISE_CMDS:
            return self._noise(event)
        if cmd == "login":
            return [self._lifecycle("enable")]
        if cmd in ("heartbeat", "runtime.heartbeat"):
            return [self._heartbeat()]
        if cmd in ("message.group", "message.private", "message.sent"):
            return [await self.message(Message.from_payload(event.json()), sent=cmd == "message.sent")]
        if cmd.startswith("notice."):
            return [self._notice(cmd, event.json())]
        if cmd in ("trace.call", "trace.send", "trace.recv"):
            payload = event.json()
            log.trace(
                "payload capture",
                kind=payload.get("kind", "?"),
                body_len=len(str(payload.get("body_hex", ""))) // 2,
            )
            return []
        if cmd.startswith("request."):
            return [self._request(cmd, event.json())]
        log.debug("unmapped payload event", cmd=cmd)
        return []

    # ------------------------------------------------------------------ noise

    def _noise(self, event: PacketEvent) -> list[dict[str, Any]]:
        payload = event.json()
        # Always TRACE: searching for "typing" should be possible, but it must
        # never be an INFO line or a client event by default.
        noise_log.trace(
            "input activity",
            chat=payload.get("chat"),
            user_id=payload.get("user_id"),
            cmd=event.cmd,
        )
        if not self.config.forward_noise:
            return []
        return [
            {
                "post_type": "notice",
                "notice_type": "notify",
                "sub_type": "input_status",
                "time": int(time.time()),
                "self_id": self.self_id,
                **payload,
            }
        ]

    # ---------------------------------------------------------------- message

    async def message(self, message: Message, *, sent: bool = False) -> dict[str, Any]:
        if not message.account:
            message.account = str(self.self_id)
        scope = "group" if message.is_group else "private"
        await E.resolve_media(message.elements, lambda el: self.media.url_for(el, scope=scope))
        # 写库挪到线程池：store.put 是同步 SQLite 事务，直接在事件循环里跑会
        # 阻塞随后要处理的每一条消息与动作（发送和接收都会排队等它提交）。
        await asyncio.to_thread(self.store.put, message)

        out: dict[str, Any] = {
            "post_type": "message_sent" if sent else "message",
            "message_type": message.chat_type,
            "sub_type": "normal" if message.is_group else "friend",
            # `message_id` is a number only while a number can hold it exactly.
            #
            # QQ message ids are 64-bit and routinely exceed 2^53, where JavaScript numbers stop being exact.
            # A consumer that received 771069947330992833 as a number quoted back 7710699473099050000, an id
            # that does not exist -- and the reply was rejected. Strings round-trip exactly in every language;
            # numbers only do while they fit. Below the boundary the number is kept, so consumers that insist on
            # a numeric id for ordinary messages still get one.
            "message_id": (message.message_id if message.message_id <= 9007199254740991
                           else str(message.message_id)),
            "message_id_str": str(message.message_id),
            "real_id": str(message.message_id),
            "message_seq": message.seq,
            "user_id": message.sender_uin,
            "time": message.time,
            "self_id": self.self_id,
            "message": [E.to_onebot(element) for element in message.elements],
            "raw_message": E.to_cq_string(message.elements),
            # `message_format` is required in practice, and NapCat's own schema declares it
            # (`packages/napcat-onebot/types/message.ts`: `message_format: 'array' | 'string'`). It tells the
            # consumer whether `message` is a list of segments or a CQ string. Omitting it left clients to
            # guess; this payload always carries segments.
            "message_format": "array",
            # NapCat reports 14 (see its `initializeMessage`). QQ NT has no font concept, so the value only
            # matters to consumers that echo it back -- matching keeps their logs free of a spurious
            # difference.
            "font": 14,
            "sender": {
                "user_id": message.sender_uin,
                "nickname": message.sender_name,
                "card": message.card,
                "role": message.role,
            },
        }
        if message.is_group:
            out["group_id"] = message.chat_id
            # No `group_name`: NapCat fills it from the peer's cached name, but the normalised payload does
            # not carry one -- `chat` is `{type, id}` only. The field is optional in NapCat's own schema, and
            # inventing a value from the raw message would be guessing at a name the client may not have.
            # Adding it needs the codec to capture `peerName` first.
            out["anonymous"] = None
        else:
            out["target_id"] = message.chat_id
        return out

    # ---------------------------------------------------------------- notices

    def _notice(self, cmd: str, payload: dict[str, Any]) -> dict[str, Any]:
        notice_type = cmd.split(".", 1)[1] if "." in cmd else "unknown"
        out: dict[str, Any] = {
            "post_type": "notice",
            "notice_type": notice_type,
            "time": int(time.time()),
            "self_id": self.self_id,
        }
        out.update(payload)
        return out

    def _request(self, cmd: str, payload: dict[str, Any]) -> dict[str, Any]:
        request_type = cmd.split(".", 1)[1] if "." in cmd else "unknown"
        out: dict[str, Any] = {
            "post_type": "request",
            "request_type": request_type,
            "time": int(time.time()),
            "self_id": self.self_id,
        }
        out.update(payload)
        return out

    # ------------------------------------------------------------------- meta

    def _lifecycle(self, sub_type: str) -> dict[str, Any]:
        return {
            "post_type": "meta_event",
            "meta_event_type": "lifecycle",
            "sub_type": sub_type,
            "time": int(time.time()),
            "self_id": self.self_id,
        }

    def _heartbeat(self) -> dict[str, Any]:
        online = bool(getattr(self, "login", None) and self.login.logged_in)
        return {
            "post_type": "meta_event",
            "meta_event_type": "heartbeat",
            "time": int(time.time()),
            "self_id": self.self_id,
            "status": {"online": online, "good": online},
            "interval": 15000,
        }
