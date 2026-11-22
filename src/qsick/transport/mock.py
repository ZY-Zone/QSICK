"""A synthetic QQ used for development, the WebUI demo and the test suite.

The mock speaks the same *payload contract* as the native bridge: high-level ops
in, JSON out, events pushed through :class:`EventFanout`. That keeps every layer
above the transport honest — no test-only shortcuts.
"""

from __future__ import annotations

import asyncio
import base64
import itertools
import json
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from ..log import get_logger
from .base import EventFanout, LoginState, PacketEvent, SendResult, Transport

log = get_logger("Transport.Mock")


@dataclass(slots=True)
class MockMessage:
    message_id: int
    seq: int
    time: int
    chat_type: str  # group | private
    chat_id: int
    sender_uin: int
    sender_name: str
    elements: list[dict[str, Any]]
    recalled: bool = False


@dataclass
class MockWorld:
    uin: str = "10001"
    nickname: str = "QSICK"
    friends: list[dict[str, Any]] = field(
        default_factory=lambda: [
            {"uin": 20001, "uid": "u_20001", "nickname": "Alice", "remark": "alice"},
            {"uin": 20002, "uid": "u_20002", "nickname": "Bob", "remark": ""},
        ]
    )
    groups: list[dict[str, Any]] = field(
        default_factory=lambda: [
            {"group_id": 30001, "group_name": "QSICK 测试群", "member_count": 3, "max_member_count": 200},
        ]
    )
    members: dict[int, list[dict[str, Any]]] = field(
        default_factory=lambda: {
            30001: [
                {"uin": 10001, "uid": "u_10001", "nickname": "QSICK", "card": "", "role": "owner"},
                {"uin": 20001, "uid": "u_20001", "nickname": "Alice", "card": "A", "role": "admin"},
                {"uin": 20002, "uid": "u_20002", "nickname": "Bob", "card": "", "role": "member"},
            ]
        }
    )
    messages: dict[int, MockMessage] = field(default_factory=dict)
    _ids: itertools.count = field(default_factory=lambda: itertools.count(1000))

    def next_id(self) -> tuple[int, int]:
        value = next(self._ids)
        return value, value - 1000 + 1

    def store(self, message: MockMessage) -> None:
        self.messages[message.message_id] = message

    def message_json(self, message: MockMessage) -> dict[str, Any]:
        return {
            "message_id": message.message_id,
            "seq": message.seq,
            "time": message.time,
            "chat": {"type": message.chat_type, "id": message.chat_id},
            "sender": {
                "uin": message.sender_uin,
                "nickname": message.sender_name,
                "card": "",
                "role": "member",
            },
            "elements": message.elements,
            "recalled": message.recalled,
        }


class MockTransport(Transport):
    name = "mock"

    def __init__(self, world: MockWorld | None = None, *, emit_demo: bool = True) -> None:
        self.world = world or MockWorld()
        self._login = LoginState(logged_in=True, uin=self.world.uin, nickname=self.world.nickname)
        self._fanout = EventFanout()
        self._emit_demo = emit_demo
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    @property
    def login(self) -> LoginState:
        return self._login

    async def start(self) -> None:
        self._fanout.bind_loop(asyncio.get_running_loop())
        self._publish("login", {"logged_in": True, "uin": self.world.uin, "nickname": self.world.nickname})
        if self._emit_demo:
            self._task = asyncio.create_task(self._demo_loop(), name="qsick-mock-demo")
        log.info("mock runtime started", uin=self.world.uin, groups=len(self.world.groups))

    async def _demo_loop(self) -> None:
        await asyncio.sleep(0.6)
        await self._emit_group_message(30001, 20001, "Alice", [{"type": "text", "data": {"text": "hello from mock"}}])
        await asyncio.sleep(0.4)
        self._publish_event(
            PacketEvent(
                cmd="noise.typing",
                kind="noise",
                body=json.dumps({"chat": {"type": "group", "id": 30001}, "user_id": 20001}).encode(),
            )
        )
        while not self._stop.is_set():
            await asyncio.sleep(15)
            self._publish_event(PacketEvent(cmd="heartbeat", kind="runtime", body=b"{}"))

    async def _emit_group_message(
        self, group_id: int, sender: int, name: str, elements: list[dict[str, Any]]
    ) -> None:
        message_id, seq = self.world.next_id()
        message = MockMessage(
            message_id=message_id,
            seq=seq,
            time=int(time.time()),
            chat_type="group",
            chat_id=group_id,
            sender_uin=sender,
            sender_name=name,
            elements=elements,
        )
        self.world.store(message)
        self._publish_event(
            PacketEvent(
                cmd="message.group",
                kind="message",
                body=json.dumps(self.world.message_json(message)).encode(),
            )
        )

    def push(self, event: dict[str, Any]) -> None:
        """Accept an event reported from outside this process.

        The synthetic transport has no client of its own, but the WebUI still creates real pipe sessions and
        the runtime polls their `events.jsonl`. Without this the poller found no `push` to call and dropped
        every message -- the listener worked and the file grew, and nothing ever reached the store. This is
        what `start.bat` (no argument, i.e. mock) runs by default.

        Same contract as `HeadlessTransport.push` and `PipeTransport.push`, so all three transports carry an
        externally reported message into the same pipeline.
        """
        kind = str(event.get("kind") or "message")
        payload = event.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("event payload must be an object")
        instance = str(event.get("instance") or "").strip()

        if kind in ("message", "message_sent"):
            chat = payload.get("chat")
            if not isinstance(chat, dict) or chat.get("type") not in ("group", "private"):
                raise ValueError("unsupported message chat type")
            mid = payload.get("message_id")
            if not isinstance(mid, (int, str)) or isinstance(mid, bool) or not str(mid).isdigit():
                raise ValueError("message_id must be a positive decimal integer/string")
            cmd = "message.sent" if kind == "message_sent" else "message." + chat["type"]
            owner = str(payload.get("account") or "").strip() or instance
            if owner == "0":
                owner = ""
            log.debug("mock received external message", message_id=str(mid), account=owner)
            self._publish_event(PacketEvent(
                cmd=cmd, kind=kind, instance=owner, account=owner,
                body=json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")))
            return

        if kind == "login":
            self._publish("login", payload)
            return

        if kind in ("notice", "request", "runtime"):
            cmd = str(event.get("cmd") or "")
            if not cmd.startswith(kind + "."):
                raise ValueError("event kind/cmd mismatch")
            self._publish_event(PacketEvent(
                cmd=cmd, kind=kind, instance=instance,
                body=json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")))
            return

        raise ValueError("unsupported mock event kind")

    def _publish(self, cmd: str, payload: dict[str, Any]) -> None:
        self._publish_event(PacketEvent(cmd=cmd, kind="login", body=json.dumps(payload).encode()))

    def _publish_event(self, event: PacketEvent) -> None:
        self._fanout.publish(event)

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._fanout.close()

    def events(self) -> AsyncIterator[PacketEvent]:
        return self._fanout.stream()

    # ------------------------------------------------------------------ ops

    async def send(
        self, op: str, params: dict[str, Any] | None = None, *, timeout: float | None = None
    ) -> SendResult:
        params = params or {}
        handler = _OPS.get(op)
        if handler is None:
            log.debug("mock: generic adapter response", op=op)
            return SendResult(ok=True, status=0, data={"mock": True, "op": op, "params": params})
        try:
            data = handler(self, params)
        except _MockError as exc:
            return SendResult.failure(str(exc), status=exc.status)
        if isinstance(data, SendResult):
            return data
        return SendResult(ok=True, status=0, data=data or {})


class _MockError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _chat(params: dict[str, Any]) -> tuple[str, int]:
    chat = params.get("chat") or {}
    kind = chat.get("type")
    ident = chat.get("id")
    if kind not in ("group", "private") or not isinstance(ident, int):
        raise _MockError("chat requires {type, id}")
    return kind, ident


def _need(params: dict[str, Any], key: str) -> Any:
    if key not in params:
        raise _MockError(f"missing parameter {key!r}")
    return params[key]


def _op_login_info(t: MockTransport, _: dict[str, Any]) -> dict[str, Any]:
    return {"uin": int(t.world.uin), "nickname": t.world.nickname}


def _op_status(t: MockTransport, _: dict[str, Any]) -> dict[str, Any]:
    return {"online": True, "good": True, "uin": int(t.world.uin)}


def _op_message_send(t: MockTransport, params: dict[str, Any]) -> dict[str, Any]:
    kind, ident = _chat(params)
    elements = params.get("elements")
    if not isinstance(elements, list) or not elements:
        raise _MockError("elements must be a non-empty list")
    message_id, seq = t.world.next_id()
    message = MockMessage(
        message_id=message_id,
        seq=seq,
        time=int(time.time()),
        chat_type=kind,
        chat_id=ident,
        sender_uin=int(t.world.uin),
        sender_name=t.world.nickname,
        elements=elements,
    )
    t.world.store(message)
    t._publish_event(
        PacketEvent(cmd="message.sent", kind="message", body=json.dumps(t.world.message_json(message)).encode())
    )
    return {"message_id": message_id, "seq": seq, "time": message.time}


def _op_message_get(t: MockTransport, params: dict[str, Any]) -> dict[str, Any]:
    message_id = int(_need(params, "message_id"))
    message = t.world.messages.get(message_id)
    if not message:
        raise _MockError(f"message {message_id} not found", status=404)
    return t.world.message_json(message)


def _op_message_recall(t: MockTransport, params: dict[str, Any]) -> dict[str, Any]:
    message = t.world.messages.get(int(_need(params, "message_id")))
    if not message:
        raise _MockError("message not found", status=404)
    message.recalled = True
    t._publish_event(
        PacketEvent(
            cmd="notice.group_recall" if message.chat_type == "group" else "notice.friend_recall",
            kind="notice",
            body=json.dumps({"message_id": message.message_id, "chat": {"type": message.chat_type, "id": message.chat_id}}).encode(),
        )
    )
    return {}


def _op_friend_list(t: MockTransport, _: dict[str, Any]) -> list[dict[str, Any]]:
    return list(t.world.friends)


def _op_group_list(t: MockTransport, _: dict[str, Any]) -> list[dict[str, Any]]:
    return list(t.world.groups)


def _op_group_info(t: MockTransport, params: dict[str, Any]) -> dict[str, Any]:
    group_id = int(_need(params, "group_id"))
    for group in t.world.groups:
        if group["group_id"] == group_id:
            return dict(group)
    raise _MockError("group not found", status=404)


def _op_group_members(t: MockTransport, params: dict[str, Any]) -> list[dict[str, Any]]:
    return list(t.world.members.get(int(_need(params, "group_id")), []))


def _op_group_member(t: MockTransport, params: dict[str, Any]) -> dict[str, Any]:
    group_id = int(_need(params, "group_id"))
    uin = int(_need(params, "user_id"))
    for member in t.world.members.get(group_id, []):
        if member["uin"] == uin:
            return dict(member)
    raise _MockError("member not found", status=404)


def _op_group_mute(t: MockTransport, params: dict[str, Any]) -> dict[str, Any]:
    group_id = int(_need(params, "group_id"))
    uin = int(_need(params, "user_id"))
    duration = int(_need(params, "duration"))
    for member in t.world.members.get(group_id, []):
        if member["uin"] == uin:
            member["mute_until"] = int(time.time()) + duration
            return {}
    raise _MockError("member not found", status=404)


def _op_group_kick(t: MockTransport, params: dict[str, Any]) -> dict[str, Any]:
    group_id = int(_need(params, "group_id"))
    uin = int(_need(params, "user_id"))
    members = t.world.members.get(group_id, [])
    t.world.members[group_id] = [m for m in members if m["uin"] != uin]
    return {}


def _op_group_card(t: MockTransport, params: dict[str, Any]) -> dict[str, Any]:
    group_id = int(_need(params, "group_id"))
    uin = int(_need(params, "user_id"))
    card = str(params.get("card", ""))
    for member in t.world.members.get(group_id, []):
        if member["uin"] == uin:
            member["card"] = card
            return {}
    raise _MockError("member not found", status=404)


def _op_noop(_t: MockTransport, _p: dict[str, Any]) -> dict[str, Any]:
    return {}


def _op_rkey(_t: MockTransport, _p: dict[str, Any]) -> list[dict[str, Any]]:
    now = int(time.time())
    return [
        {"rkey": "MOCK_PRIVATE_RKEY_0123456789", "type": 10, "ttl": 3600, "create_time": now},
        {"rkey": "MOCK_GROUP_RKEY_0123456789", "type": 20, "ttl": 3600, "create_time": now},
    ]


def _op_file_url(t: MockTransport, params: dict[str, Any]) -> dict[str, Any]:
    file_id = str(params.get("file_id", "mock-file"))
    return {"url": f"https://mock.invalid/download?fileid={file_id}&rkey=MOCK_GROUP_RKEY_0123456789"}


def _op_forward_get(t: MockTransport, params: dict[str, Any]) -> dict[str, Any]:
    res_id = str(_need(params, "res_id"))
    return {
        "res_id": res_id,
        "messages": [
            {
                "sender": {"uin": 20001, "nickname": "Alice"},
                "time": int(time.time()),
                "elements": [{"type": "text", "data": {"text": f"forwarded node in {res_id}"}}],
            }
        ],
    }


def _op_raw_call(_t: MockTransport, params: dict[str, Any]) -> dict[str, Any]:
    cmd = str(_need(params, "cmd"))
    body = params.get("body_hex", "")
    try:
        raw = bytes.fromhex(body) if body else b""
    except ValueError as exc:
        raise _MockError(f"body_hex is not hex: {exc}") from exc
    return {
        "cmd": cmd,
        "status": 0,
        "body_hex": raw.hex(),
        "echo_len": len(raw),
        "note": "mock echoes the request body",
    }


def _op_oidb_call(_t: MockTransport, params: dict[str, Any]) -> dict[str, Any]:
    command = int(_need(params, "command"))
    sub = int(params.get("sub_command", 0))
    body = params.get("body_hex", "")
    try:
        raw = bytes.fromhex(body) if body else b""
    except ValueError as exc:
        raise _MockError(f"body_hex is not hex: {exc}") from exc
    return {
        "command": command,
        "sub_command": sub,
        "wire": f"OidbSvcTrpcTcp.0x{command:x}_{sub}",
        "status": 0,
        "body_hex": raw.hex(),
        "note": "mock echoes the OIDB body",
    }


def _op_media_url(_t: MockTransport, params: dict[str, Any]) -> dict[str, Any]:
    kind = str(params.get("kind", "image"))
    return {
        "url": f"https://mock.invalid/{kind}?rkey=MOCK_GROUP_RKEY_0123456789",
        "expires_in": 3600,
        "kind": kind,
        "file_id": base64.b64encode(str(params.get("file_id", "x")).encode()).decode(),
    }



def _op_group_honor(_t: MockTransport, params: dict[str, Any]) -> dict[str, Any]:
    return {"group_id": params.get("group_id", 0), "type": params.get("type", "all"), "users": []}


def _op_at_all_remain(_t: MockTransport, params: dict[str, Any]) -> dict[str, Any]:
    return {
        "group_id": params.get("group_id", 0),
        "can_at_all": True,
        "remain_at_all_count_for_group": 10,
        "remain_at_all_count_for_uin": 10,
    }


def _op_empty_list(_t: MockTransport, _p: dict[str, Any]) -> list[Any]:
    return []


def _op_emoji_likes(_t: MockTransport, params: dict[str, Any]) -> dict[str, Any]:
    return {"message_id": params.get("message_id", 0), "likes": []}


def _op_flash_list(_t: MockTransport, _p: dict[str, Any]) -> dict[str, Any]:
    return {"filesets": []}


def _op_group_files(_t: MockTransport, _p: dict[str, Any]) -> dict[str, Any]:
    return {"files": [], "folders": []}


def _op_create_folder(_t: MockTransport, _p: dict[str, Any]) -> dict[str, Any]:
    return {"folder_id": "mock-folder-1"}


_OPS = {
    "login.info": _op_login_info,
    "status": _op_status,
    "message.send": _op_message_send,
    "message.get": _op_message_get,
    "message.recall": _op_message_recall,
    "friend.list": _op_friend_list,
    "group.list": _op_group_list,
    "group.info": _op_group_info,
    "group.members": _op_group_members,
    "group.member": _op_group_member,
    "group.mute": _op_group_mute,
    "group.kick": _op_group_kick,
    "group.card": _op_group_card,
    "group.name": _op_noop,
    "group.leave": _op_noop,
    "group.whole_ban": _op_noop,
    "friend.like": _op_noop,
    "request.friend": _op_noop,
    "request.group": _op_noop,
    "rkey": _op_rkey,
    "file.url": _op_file_url,
    "forward.get": _op_forward_get,
    "raw.call": _op_raw_call,
    "oidb.call": _op_oidb_call,
    "media.url": _op_media_url,
    "group.honor": _op_group_honor,
    "group.at_all_remain": _op_at_all_remain,
    "group.shut_list": _op_empty_list,
    "friend.unidirectional": _op_empty_list,
    "friend.categories": _op_empty_list,
    "group.remark": _op_noop,
    "input.status": _op_noop,
    "message.emoji_likes": _op_emoji_likes,
    "message.emoji_like.fetch": _op_emoji_likes,
    "flash.list": _op_flash_list,
    "flash.delete": _op_noop,
    "flash.rename": _op_noop,
    "group.files.root": _op_group_files,
    "group.files.folder": _op_group_files,
    "group.files.create_folder": _op_create_folder,
}
