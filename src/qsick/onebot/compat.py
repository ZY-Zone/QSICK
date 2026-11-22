"""Field-name compatibility between NapCat and SnowLuma request bodies.

The two implementations agree on OneBot v11's envelope and on most field names. Where they differ, a
request written for one is rejected by the other -- the reference document lists these as "必填冲突
（会直接 400）". The worst case is `move_group_file`, where both sides have *required* fields with
different names, so neither implementation's request works against the other at all.

The rule this module implements, per the operator's direction:

  1. where the two formats agree, use that one shape
  2. where they differ, accept both; if a request carries both spellings, the NapCat one wins and the
     other is ignored silently
  3. be permissive about types and extra fields: accept as much as possible, ignore what is not needed

Point 2's tie-break is not arbitrary -- NapCat is this project's primary target, so when a caller sends
both, the value they most likely mean is NapCat's.

Point 3 has a practical consequence worth stating: nothing here rejects an unknown field. A request
carrying fields this project has never heard of is passed through untouched, because rejecting on
surprise input is how a client that adds a field breaks a server that never needed it.

Scope: only the OneBot v11 and OneBot v11 + EXTemotions protocols. The EXT side keeps its own shapes;
this module only touches the OneBot v11 layer they share.
"""
from __future__ import annotations

from typing import Any

#: Protocols whose params get normalised. EXTemotions rides on OneBot v11, so it is included -- its
#: EXT-specific request shapes are untouched, since they are not OneBot actions at all.
#:
#: Stored lower-cased because the comparison lower-cases the incoming protocol id. The registry writes
#: it as ``onebotv11-EXTemotions``; keeping the camel case here and lower-casing only one side is what
#: made EXTemotions fail to match despite being listed.
ONEBOT_PROTOCOLS = frozenset({"onebotv11", "onebotv11-extemotions"})

#: Per-action alias tables: canonical (NapCat) name -> other spellings SnowLuma accepts.
#:
#: Sourced from `napcat-sl_请求体.md`, which was generated from NapCat's TypeBox schemas and
#: SnowLuma's catalog.json rather than transcribed by hand. Only actions whose request bodies actually
#: differ appear here; an action absent from this table needs no translation.
ALIASES: dict[str, dict[str, tuple[str, ...]]] = {
    # move_group_file is the one action the reference calls "双向都不兼容": NapCat requires
    # current_parent_directory / target_parent_directory, SnowLuma requires parent_directory /
    # target_directory, and neither accepts the other's spelling.
    #
    # The canonical names here are the ones the payload op already reads (folder_id /
    # target_folder_id), so both external spellings fold onto what the implementation expects. Mapping
    # onto NapCat's names instead would mean changing the op as well, for no gain: the wire contract
    # callers care about is that both spellings are accepted.
    "move_group_file": {
        "folder_id": ("current_parent_directory", "parent_directory", "from_folder"),
        "target_folder_id": ("target_parent_directory", "target_directory", "to_folder"),
    },
    "rename_group_file": {
        # Two target names, filled from one set of spellings.
        #
        # The kernel rename takes the parent as `parent_folder_id`; the OIDB variant reads `folder_id`.
        # A plain alias table cannot express "fill both", because folding onto the first canonical
        # removes the alternatives before the second is considered -- so this pair is handled by the
        # explicit mirror in `_MIRRORS` below.
        "parent_folder_id": ("current_parent_directory", "parent_directory", "folder"),
        "file_name": ("new_name", "name", "new_file_name"),
    },
    # Upload: the OneBot field is `file` (a local path or a URL), and the op reads `file_path`.
    # `folder` is the OneBot spelling of the target folder. Folding both means a standard
    # upload_group_file request reaches the op without the caller knowing the internal names.
    "upload_group_file": {
        "file_path": ("file", "path", "source"),
        "name": ("file_name", "fileName"),
        "folder_id": ("folder", "parent_folder_id"),
    },
    # NapCat accepts folder_name or name and both are optional; SnowLuma requires name and adds
    # parent_id. Canonical is folder_name because that is the name the op itself uses.
    "create_group_file_folder": {
        "folder_name": ("name",),
        "parent_id": ("parent_folder_id", "folder_id", "parent"),
    },
    "delete_group_file": {
        "bus_id": ("busid", "busId"),
    },
    "get_group_file_url": {
        "bus_id": ("busid", "busId"),
    },
    "get_group_files_by_folder": {
        "folder_id": ("folder",),
        "file_count": ("count", "limit"),
    },
    "get_group_root_files": {
        "file_count": ("count", "limit"),
    },
    # delete_group_file_folder / rename_group_file_folder: the reference notes NapCat's folder target
    # is optional and SnowLuma's is required, so accept every spelling either side uses.
    "delete_group_file_folder": {
        "folder_id": ("folder", "folder_name", "id"),
    },
    "rename_group_file_folder": {
        "folder_id": ("folder", "id"),
        "new_name": ("name", "folder_name"),
    },
    # Essence: NapCat can address a message by msg_seq + msg_random + group_id instead of message_id.
    # Both spellings are accepted; the canonical stays message_id because that is the OneBot field.
    "delete_essence_msg": {
        "message_id": ("msg_id",),
        "msg_seq": ("message_seq", "message_seq_id"),
        "msg_random": ("message_random",),
    },
    "set_essence_msg": {
        "message_id": ("msg_id",),
    },
    # Mark-as-read family: NapCat makes every field optional, SnowLuma requires message_id.
    "mark_private_msg_as_read": {"message_id": ("msg_id",)},
    "mark_group_msg_as_read": {"message_id": ("msg_id",)},
    "mark_msg_as_read": {"message_id": ("msg_id",)},
    # Todo family: a message_seq alone is a legitimate NapCat address.
    "set_group_todo": {"message_id": ("msg_id",), "message_seq": ("msg_seq",)},
    "complete_group_todo": {"message_id": ("msg_id",), "message_seq": ("msg_seq",)},
    "cancel_group_todo": {"message_id": ("msg_id",), "message_seq": ("msg_seq",)},
    # Custom face deletion: NapCat takes res_id / id / ids / md5, all optional; SnowLuma requires
    # emoji_id. Mapping them all onto emoji_id makes either caller's shape work.
    "delete_custom_face": {
        "emoji_id": ("res_id", "id", "ids", "md5"),
    },
    # Emoji-like: NapCat requires emojiType, SnowLuma has no such field and uses emoji_id.
    "fetch_emoji_like": {
        "emoji_id": ("emojiId", "emoji"),
        "emoji_type": ("emojiType",),
    },
    # Forwarding: the target is optional on one side and required on the other.
    "forward_friend_single_msg": {
        "user_id": ("target_id", "target_user_id"),
    },
    "forward_group_single_msg": {
        "group_id": ("target_id", "target_group_id"),
    },
    # Profile/status: required on one side, optional on the other. Aliases only; nothing is enforced.
    "set_online_status": {
        "status": ("online_status",),
        "ext_status": ("extStatus",),
    },
    "set_input_status": {
        "event_type": ("eventType",),
    },
}

#: Actions whose field names differ only by casing or a common abbreviation. These are cheap to accept
#: and cover the wide tail without needing a table per action.
GENERIC_CASING: dict[str, tuple[str, ...]] = {
    "group_id": ("groupId", "group"),
    "user_id": ("userId", "user", "uin"),
    "message_id": ("messageId", "msg_id", "msgId"),
    "file_id": ("fileId", "id"),
    "folder_id": ("folderId",),
    "file_count": ("fileCount",),
    "bus_id": ("busId",),
}


#: The protocol assumed when none is configured.
#:
#: An instance with no protocol set is not "some other protocol" -- it is one that has not been
#: configured, and OneBot v11 is what this runtime speaks by default. Treating the empty value as
#: "do not normalise" made every field-compatibility rule silently inactive on such instances, which
#: is the failure mode that hides itself.
DEFAULT_PROTOCOL = "onebotv11"


def protocol_supported(protocol: str | None) -> bool:
    """Whether params should be normalised for this protocol.

    An empty or missing value counts as supported: it means the instance has no protocol configured,
    and the default protocol is OneBot v11. Only a *different* protocol opts out.
    """
    value = str(protocol or "").strip().lower()
    if not value:
        return True
    return value in ONEBOT_PROTOCOLS


def _coerce_int(value: Any) -> Any:
    """Return an int if the value is a whole number in any of the shapes clients send.

    OneBot clients send ids as number, decimal string, or occasionally a float from JSON parsing. All
    three mean the same thing, and refusing one of them only because of its spelling breaks callers for
    no benefit. Anything that is not a clean integer is returned unchanged so validation downstream
    still sees the original.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return value
        try:
            return int(text, 10)
        except ValueError:
            return value
    return value


#: Fields that are ids and therefore worth coercing. Everything else keeps its type: a bool sent as the
#: string "true" is a caller bug worth surfacing, not something to silently reinterpret.
ID_FIELDS = frozenset({
    "group_id", "user_id", "message_id", "message_seq", "msg_seq",
    "operator_id", "target_id", "user_uuid_override",
})


def normalize(action: str, params: dict[str, Any], protocol: str | None = None,
              known: set[str] | None = None) -> dict[str, Any]:
    """Return params with every accepted spelling folded onto the canonical field names.

    The canonical name always wins: if a request carries both `current_parent_directory` and
    `parent_directory`, the former is used and the latter is dropped without comment. That is the
    operator's stated tie-break, and it matches which implementation this project targets.

    Unknown fields are preserved rather than stripped. A caller sending extra data is making a mistake
    at most, and dropping it silently would make debugging harder than passing it along.

    ``known`` is the action's own declared parameter names. It is what stops the generic table from
    rewriting a field the action genuinely calls by that name -- see the note on the generic pass below.
    """
    if not protocol_supported(protocol):
        return params
    if not isinstance(params, dict):
        return params

    out = dict(params)

    # Per-action aliases first, so an action-specific spelling beats a generic one.
    table = ALIASES.get(action) or {}
    for canonical, alternatives in table.items():
        _fold(out, canonical, alternatives)

    # Then the generic casing variants, for fields the tables do not mention.
    #
    # A generic spelling is skipped when the action declares that spelling itself. `file_id` accepts
    # `id`, which is right for the file actions -- but `get_forward_msg` has a parameter genuinely named
    # `id` (the merged-forward card id). Folding it renamed the caller's `id` to `file_id`, the required
    # `id` disappeared, and every `get_forward_msg` call answered "missing required parameter 'id'"
    # regardless of how correct the request was.
    declared = known or set()
    for canonical, alternatives in GENERIC_CASING.items():
        accepted = tuple(name for name in alternatives if name not in declared or name == canonical)
        _fold(out, canonical, accepted)

    # Mirror pairs last, so they copy an already-folded value.
    for source, target in _MIRRORS.get(action, ()):
        if out.get(source) is not None and out.get(target) is None:
            out[target] = out[source]

    for field in ID_FIELDS:
        if field in out:
            out[field] = _coerce_int(out[field])

    return out


#: Fields that several implementations name differently but that mean the same thing, where *both*
#: names must end up populated.
#:
#: This cannot be expressed as aliases: folding onto the first canonical removes the alternatives, so
#: the second canonical would never see a value. rename_group_file is the case that forced this -- the
#: kernel implementation reads `parent_folder_id` and the OIDB one reads `folder_id`, and both are
#: plausible entry points for the same request.
_MIRRORS: dict[str, tuple[tuple[str, str], ...]] = {
    "rename_group_file": (("parent_folder_id", "folder_id"),),
    "delete_group_file": (("bus_id", "busid"),),
}


def _fold(params: dict[str, Any], canonical: str, alternatives: tuple[str, ...]) -> None:
    """Move the winning spelling onto ``canonical`` and drop every other one.

    This is the whole tie-break rule in one place, and it has three cases:

    * canonical present -> it wins, and all alternatives are removed
    * canonical absent  -> the first present alternative wins, and the rest are removed
    * none present      -> nothing happens

    The removal of the losing spellings matters. An earlier version only replaced the winner and left
    the others in place, so a request carrying both NapCat's and SnowLuma's names ended up with
    `folder_id` set *and* a stray `parent_directory` still in the dict -- which any op reading the old
    name directly would have silently preferred.
    """
    if _present(params, canonical):
        for alt in alternatives:
            params.pop(alt, None)
        return
    winner = ""
    for alt in alternatives:
        if _present(params, alt):
            winner = alt
            break
    if not winner:
        return
    params[canonical] = params[winner]
    for alt in alternatives:
        if alt != canonical:
            params.pop(alt, None)


def _present(params: dict[str, Any], key: str) -> bool:
    """A field counts as supplied when it exists and is not None.

    Explicit null is treated as absent because clients frequently serialise every schema field and
    leave the unused ones null; honouring a null would overwrite a real value supplied under another
    spelling.
    """
    return key in params and params[key] is not None
