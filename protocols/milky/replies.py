"""API-aware Milky success data conversion, without inventing upstream facts.

Extra upstream fields are retained for forward compatibility. Missing optional
upstream facts are not replaced with made-up entities, IDs or timestamps.
"""
from __future__ import annotations

from typing import Any, Callable

from ..base import PartError


class ReplyConversionError(PartError):
    """A successful internal action did not supply the required output."""


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReplyConversionError("expected an object in action result")
    return dict(value)


def _aliases(value: Any, aliases: dict[str, tuple[str, ...]]) -> dict[str, Any]:
    out = _object(value)
    for target, sources in aliases.items():
        if out.get(target) is not None:
            continue
        for source in sources:
            if out.get(source) is not None:
                out[target] = out[source]
                break
    return out


def _require(out: dict[str, Any], *keys: str) -> dict[str, Any]:
    missing = [key for key in keys if key not in out or out[key] is None]
    if missing:
        raise ReplyConversionError("action result missing " + ", ".join(missing))
    return out


def _friend(value: Any) -> dict[str, Any]:
    out = _aliases(value, {"user_id": ("uin",)})
    # A category is only available if the host actually returned both fields.
    if "category" not in out and all(k in out for k in ("category_id", "category_name")):
        out["category"] = {k: out[k] for k in ("category_id", "category_name")}
    return _require(out, "user_id", "nickname")


def _group(value: Any) -> dict[str, Any]:
    return _require(_aliases(value, {"created_time": ("group_create_time", "create_time")}),
                    "group_id", "group_name")


def _member(value: Any) -> dict[str, Any]:
    out = _aliases(value, {"user_id": ("uin",), "title": ("special_title",)})
    if "level" in out:
        try:
            out["level"] = int(out["level"])
        except (TypeError, ValueError) as exc:
            raise ReplyConversionError("member level is not an integer") from exc
    return _require(out, "user_id", "group_id", "nickname")


def _list(value: Any, key: str, convert: Callable[[Any], Any]) -> dict[str, Any]:
    out = dict(value) if isinstance(value, dict) else {}
    items = out.get(key) if isinstance(value, dict) else value
    if not isinstance(items, list):
        raise ReplyConversionError(f"action result missing {key} array")
    out[key] = [convert(item) for item in items]
    return out


def _single(value: Any, key: str, convert: Callable[[Any], Any]) -> dict[str, Any]:
    out = _object(value)
    if key in out:
        out[key] = convert(out[key])
        return out
    return {key: convert(out)}


def _file(value: Any) -> dict[str, Any]:
    out = _aliases(value, {
        "uploaded_time": ("upload_time",), "uploader_id": ("uploader",),
        "downloaded_times": ("download_times",), "expire_time": ("dead_time",),
    })
    # Zero is the host's permanent-file sentinel, not the Unix epoch.
    if out.get("expire_time") in (0, "0"):
        out["expire_time"] = None
    return _require(out, "file_id", "file_name")


def _folder(value: Any) -> dict[str, Any]:
    return _require(_aliases(value, {
        "folder_id": ("file_id",), "folder_name": ("file_name",),
        "created_time": ("upload_time",), "last_modified_time": ("modify_time",),
        "creator_id": ("uploader",), "file_count": ("total_file_count",),
    }), "folder_id", "folder_name")


def _segments(out: dict[str, Any]) -> None:
    if "segments" in out:
        return
    from .events import _segments as encode_segments
    source = out.get("message")
    if not isinstance(source, list):
        raise ReplyConversionError("action result missing message segments")
    out["segments"] = encode_segments(source)


def _message(value: Any) -> dict[str, Any]:
    out = _aliases(value, {"message_seq": ("seq",)})
    _require(out, "message_seq", "time")
    for key in ("message_seq", "time"):
        if isinstance(out[key], bool) or not isinstance(out[key], int) or out[key] <= 0:
            raise ReplyConversionError(f"action result missing real {key}")
    scene = out.get("message_scene")
    if scene is None:
        kind = out.get("message_type")
        scene = {"group": "group", "private": "friend", "temp": "temp"}.get(kind)
        if kind == "private" and out.get("sub_type") in ("group", "temp"):
            scene = "temp"
    if scene not in ("group", "friend", "temp"):
        raise ReplyConversionError("action result missing message_scene")
    out["message_scene"] = scene
    if "peer_id" not in out:
        key = "group_id" if scene == "group" else "user_id"
        _require(out, key)
        out["peer_id"] = out[key]
    sender = out.get("sender")
    if "sender_id" not in out and isinstance(sender, dict):
        if sender.get("user_id") is not None:
            out["sender_id"] = sender["user_id"]
    _require(out, "sender_id")
    _segments(out)
    # Use only entities actually provided by upstream, never event placeholders.
    for key, fn in (("friend", _friend), ("group", _group), ("group_member", _member)):
        if out.get(key) is not None:
            out[key] = fn(out[key])
    return out


def _forward(value: Any) -> dict[str, Any]:
    out = _aliases(value, {"message_seq": ("seq",)})
    sender = out.get("sender")
    if "sender_name" not in out and isinstance(sender, dict) and "nickname" in sender:
        out["sender_name"] = sender["nickname"]
    _require(out, "message_seq", "time", "sender_name", "avatar_url")
    _segments(out)
    return out


_LISTS = {
    "get_friend_list": ("friends", _friend),
    "get_group_list": ("groups", _group),
    "get_group_member_list": ("members", _member),
}
_SINGLES = {
    "get_friend_info": ("friend", _friend),
    "get_group_info": ("group", _group),
    "get_group_member_info": ("member", _member),
}


def convert(api: str, data: Any) -> Any:
    """Convert data for the original Milky API (never the canonical action name)."""
    if api == "get_message":
        if isinstance(data, dict) and isinstance(data.get("message"), dict):
            return {**data, "message": _message(data["message"])}
        return {"message": _message(data)}
    if api == "get_history_messages":
        out = _list(data, "messages", _message)
        out["messages"].sort(key=lambda item: item["message_seq"])
        # No pagination cursor can be inferred from an arbitrary/filtered batch.
        out.setdefault("next_message_seq", None)
        return out
    if api == "get_forwarded_messages":
        return _list(data, "messages", _forward)
    if api == "get_group_essence_messages":
        out = _list(data, "messages", _object)
        for item in out["messages"]:
            _require(item, "message_seq", "message_time")
            _segments(item)
        return _require(out, "is_end")
    if api in _LISTS:
        return _list(data, *_LISTS[api])
    if api in _SINGLES:
        return _single(data, *_SINGLES[api])
    if api == "get_login_info":
        return _require(_aliases(data, {"uin": ("user_id",)}), "uin", "nickname")
    if api == "get_user_profile":
        return _require(_aliases(data, {"bio": ("personal_note",)}), "nickname")
    if api == "get_custom_face_url_list":
        out = _list(data, "urls", lambda value: value)
        if not all(isinstance(url, str) for url in out["urls"]):
            raise ReplyConversionError("custom face URLs must be strings")
        return out
    if api == "get_csrf_token":
        out = _require(_aliases(data, {"csrf_token": ("token",)}), "csrf_token")
        out["csrf_token"] = str(out["csrf_token"])
        return out
    if api in ("get_private_file_download_url", "get_group_file_download_url", "get_resource_temp_url"):
        key = "url" if api == "get_resource_temp_url" else "download_url"
        return _require(_aliases(data, {key: ("url", "download_url")}), key)
    if api == "get_group_files":
        out = _list(data, "files", _file)
        return _list(out, "folders", _folder)
    if api in ("upload_private_file", "upload_group_file", "create_group_folder"):
        key = "folder_id" if api == "create_group_folder" else "file_id"
        out = _require(_object(data), key)
        if not out[key]:
            raise ReplyConversionError(f"action result missing real {key}")
        return out
    if api in ("send_private_message", "send_group_message"):
        out = _require(_aliases(data, {"message_seq": ("seq",)}), "message_seq", "time")
        for key in ("message_seq", "time"):
            value = out[key]
            if isinstance(value, str) and value.isascii() and value.isdecimal():
                value = int(value)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ReplyConversionError(f"action result missing real {key}")
            out[key] = value
        return out
    # No-output successes and already compatible / future APIs retain old behavior.
    return {} if data is None else data
