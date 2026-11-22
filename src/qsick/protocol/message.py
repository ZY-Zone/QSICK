"""Normalized message model shared by events, the store and the actions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .elements import Element, normalize_all, preview


@dataclass(slots=True)
class Message:
    message_id: int
    seq: int
    time: int
    chat_type: str  # group | private
    chat_id: int
    sender_uin: int
    sender_name: str = ""
    sender_uid: str = ""
    card: str = ""
    role: str = "member"
    account: str = ""
    elements: list[Element] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    native: dict[str, Any] = field(default_factory=dict)

    @property
    def is_group(self) -> bool:
        return self.chat_type == "group"

    @property
    def text(self) -> str:
        return preview(self.elements, limit=10_000)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "Message":
        chat = payload.get("chat") or {}
        sender = payload.get("sender") or {}
        chat_type = str(chat.get("type") or payload.get("chat_type") or "group")
        chat_id = _int(chat.get("id", payload.get("group_id", payload.get("chat_id", 0))))
        return cls(
            message_id=_int(payload.get("message_id")),
            seq=_int(payload.get("seq", payload.get("message_seq"))),
            time=_int(payload.get("time")),
            chat_type=chat_type,
            chat_id=chat_id,
            sender_uin=_int(sender.get("uin", payload.get("user_id", 0))),
            sender_name=str(sender.get("nickname") or sender.get("name") or ""),
            sender_uid=str(sender.get("uid") or ""),
            card=str(sender.get("card") or ""),
            role=str(sender.get("role") or "member"),
            elements=normalize_all(payload.get("elements")),
            account=str(payload.get("account") or ""),
            raw=payload,
            native=payload.get("native") if isinstance(payload.get("native"), dict) else {},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "seq": self.seq,
            "time": self.time,
            "chat": {"type": self.chat_type, "id": self.chat_id},
            "sender": {
                "uin": self.sender_uin,
                "uid": self.sender_uid,
                "nickname": self.sender_name,
                "card": self.card,
                "role": self.role,
            },
            "elements": [element.to_dict() for element in self.elements],
            "account": self.account,
            "native": self.native,
        }


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
