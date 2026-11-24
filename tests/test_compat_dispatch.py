"""End-to-end check: a request written for either client reaches the op with usable field names.

Field folding runs in ``ActionRegistry.dispatch``, not inside each action -- that placement is what
covers the actions that are not PayloadActions and read params directly. So these tests go through the
registry, which is the path the HTTP and WS servers use.

The assertion is on what the payload op *receives*, captured by a wrapping transport, rather than on
whether the call succeeded. A success would prove the op tolerated whatever it was given; what needs
proving is that it was given the canonical names in the first place.

`move_group_file` is the case worth exercising because it is the one action the reference document says
is incompatible in both directions -- NapCat and SnowLuma each require a field name the other rejects.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from qsick.onebot.actions.base import PayloadAction  # noqa: E402
from qsick.onebot.registry import ActionRegistry, ActionSpec  # noqa: E402

GROUP = 2169010946


class Recorder:
    """A transport stand-in that records the params it was asked to send."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def send(self, op: str, params: dict | None = None, *, timeout: float | None = None):
        self.calls.append((op, dict(params or {})))
        return SimpleNamespace(ok=True, data={}, message="")


def _context(transport: Recorder, protocol: str):
    """A context shaped like the real one for the parts these actions touch."""
    return SimpleNamespace(call=transport.send, protocol=protocol)


class _Move(PayloadAction):
    name = "move_group_file"
    op = "move_group_file"


class _Rename(PayloadAction):
    name = "rename_group_file"
    op = "rename_group_file"


class _Root(PayloadAction):
    name = "get_group_root_files"
    # The real op name. It used to say `group.files.root`, which no layer declares — the action now calls
    # the host's `group.file.list` directly instead of being a PayloadAction at all.
    op = "group.file.list"


def _run(action: PayloadAction, params: dict, protocol: str = "onebotv11") -> dict:
    """Dispatch one action and return the params the payload op received."""
    transport = Recorder()
    registry = ActionRegistry()
    registry.add(ActionSpec(
        name=action.name,
        summary="",
        category="test",
        handler=action.run,
    ))
    reply = asyncio.run(registry.dispatch(action.name, params, _context(transport, protocol)))
    assert transport.calls, f"动作没有到达传输层: {reply}"
    return transport.calls[-1][1]


def test_move_napcat_shape_reaches_op_canonically() -> None:
    received = _run(_Move(), {
        "group_id": str(GROUP), "file_id": "/f",
        "current_parent_directory": "/from", "target_parent_directory": "/to",
    })
    assert received["folder_id"] == "/from"
    assert received["target_folder_id"] == "/to"
    assert "current_parent_directory" not in received
    assert "target_parent_directory" not in received


def test_move_snowluma_shape_reaches_op_canonically() -> None:
    received = _run(_Move(), {
        "group_id": str(GROUP), "file_id": "/f",
        "parent_directory": "/from", "target_directory": "/to",
    })
    assert received["folder_id"] == "/from"
    assert received["target_folder_id"] == "/to"


def test_move_prefers_napcat_when_both_supplied() -> None:
    received = _run(_Move(), {
        "group_id": str(GROUP), "file_id": "/f",
        "current_parent_directory": "/napcat", "target_parent_directory": "/napcat_to",
        "parent_directory": "/sl", "target_directory": "/sl_to",
    })
    assert received["folder_id"] == "/napcat"
    assert received["target_folder_id"] == "/napcat_to"
    # The losing spellings must be gone, not merely shadowed: an op that reads the old name directly
    # would otherwise see the SnowLuma value.
    assert "parent_directory" not in received
    assert "target_directory" not in received


def test_rename_both_shapes() -> None:
    napcat = _run(_Rename(), {
        "group_id": str(GROUP), "file_id": "/f",
        "current_parent_directory": "/d", "new_name": "a.txt",
    })
    # Both target names are populated: the kernel rename reads parent_folder_id, the OIDB one reads
    # folder_id, and either may be the wired path.
    assert napcat["parent_folder_id"] == "/d"
    assert napcat["folder_id"] == "/d"
    assert napcat["file_name"] == "a.txt"

    snowluma = _run(_Rename(), {
        "group_id": str(GROUP), "file_id": "/f", "new_name": "b.txt",
    })
    assert snowluma["file_name"] == "b.txt"


def test_group_id_becomes_int_on_the_way_through() -> None:
    # The op reads ids numerically; a string id reaching it would be a bug that only shows up on the
    # native side, which is the hardest place to debug it.
    received = _run(_Root(), {"group_id": str(GROUP)})
    assert received["group_id"] == GROUP
    assert isinstance(received["group_id"], int)


def test_non_onebot_protocol_passes_through_unchanged() -> None:
    # The objective scopes these rules to OneBot v11 and v11+EXT. Any other protocol must be left
    # alone, so this asserts the op still receives what the caller sent.
    received = _run(_Move(), {
        "group_id": str(GROUP), "file_id": "/f",
        "parent_directory": "/", "target_directory": "/a",
    }, "milky")
    assert "folder_id" not in received
    assert received["parent_directory"] == "/"


def test_context_without_protocol_defaults_to_onebot() -> None:
    # Plugins and tests build minimal contexts. A missing protocol means "unconfigured", and the
    # default protocol is OneBot v11 -- so folding still applies rather than silently switching off.
    transport = Recorder()
    registry = ActionRegistry()
    registry.add(ActionSpec(name="move_group_file", summary="", category="test",
                            handler=_Move().run))
    asyncio.run(registry.dispatch("move_group_file", {
        "group_id": str(GROUP), "file_id": "/f",
        "parent_directory": "/from", "target_directory": "/to",
    }, SimpleNamespace(call=transport.send)))
    assert transport.calls
    received = transport.calls[-1][1]
    assert received["folder_id"] == "/from"
