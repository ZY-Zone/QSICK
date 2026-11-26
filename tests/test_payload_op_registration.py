"""Two invariants that a "the branch exists" test cannot see.

Both failures below were real, and both were invisible to a test that only checked the handler was
present and non-empty:

1. `private.file.download_url` had a handler and a line in `NO_GROUP_ID_OPS`, but no entry in
   `REQUIREMENTS`. Every call answered `Unsupported operation` before reaching the handler, because
   `executeInner` refuses any op it cannot look up there.
2. `headless_privatefile.cjs` was not in `PIPE_JS_MODULES`, so the module never reached the injected
   copy -- the handler would have reported "module unavailable" even once dispatch worked.

The first check is scoped to handlers *below* the dispatch gate: ops above it (login identity, the
friend-list family) are answered before `REQUIREMENTS` is consulted, so they are not the same claim.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for entry in (str(ROOT), str(ROOT / "src")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

ACTIONS = ROOT / "src" / "qsick" / "headless_actions.cjs"
GATE = "if (!own(REQUIREMENTS, op)"


def requirements_ops() -> set[str]:
    source = ACTIONS.read_text(encoding="utf-8")
    start = source.index("const REQUIREMENTS = Object.freeze({")
    end = source.index("});", start)
    return set(re.findall(r"'([\w.]+)':", source[start:end]))


def ops_handled_after_the_gate() -> set[str]:
    """Ops with a dispatch branch that only runs once the op is known to REQUIREMENTS.

    Matched as conditions (`if (op === 'x'`, `|| op === 'x'`) rather than any `op ===` in the file:
    helper functions defined after the dispatcher also compare `op` (e.g. `const isSendMsg = op ===
    'send_msg'`), and counting those would report handlers that do not exist in the chain.
    """
    source = ACTIONS.read_text(encoding="utf-8")
    gate = source.index(GATE)
    return set(re.findall(r"(?:if \(|(?:\|\||&&) )op === '([\w.]+)'", source[gate:]))


def gate_exemptions() -> set[str]:
    """Ops the gate itself lets through without a REQUIREMENTS entry.

    Read from the source rather than listed here: an exemption that is removed from the code must
    make this test fail, not silently agree with a stale copy of the list.
    """
    source = ACTIONS.read_text(encoding="utf-8")
    gate_line = next(line for line in source.splitlines() if GATE in line)
    exempt: set[str] = set()
    # `op !== 'x'` disjunctions in the gate expression.
    exempt |= set(re.findall(r"op !== '([\w.]+)'", gate_line))
    # A set membership test built just above the gate, e.g. `[...].includes(op)`.
    for name in re.findall(r"const (\w+Op) = \[([^\]]*)\]\.includes\(op\)", source):
        if name[0] not in gate_line:
            continue
        exempt |= set(re.findall(r"'([\w.]+)'", name[1]))
    return exempt


def test_no_handler_sits_behind_an_unregistered_op():
    """A handler below the gate whose op is not in REQUIREMENTS can never run."""
    declared = requirements_ops()
    after = ops_handled_after_the_gate()
    missing = sorted(after - declared - gate_exemptions())
    assert not missing, (
        "these ops have a handler past the dispatch gate but no REQUIREMENTS entry, so every call "
        f"answers 'Unsupported operation': {missing}")


def test_the_gate_exemptions_are_the_ones_we_think():
    """Pins what the gate lets through, so adding an exemption is a visible decision."""
    assert gate_exemptions() == {
        "message.send",
        "friend.list", "friend.categories", "friend.statuses", "friend.relations",
    }


def test_the_private_file_ops_are_reachable():
    """The specific pair that was broken, asserted by name so a regression is unambiguous."""
    declared = requirements_ops()
    for op in ("private.file.download_url", "upload_private_file", "oidb.call"):
        assert op in declared, f"{op} is missing from REQUIREMENTS"
        assert op in ops_handled_after_the_gate(), f"{op} has no handler past the gate"


@pytest.mark.parametrize("module", ["headless_privatefile.cjs", "headless_groupfile.cjs",
                                    "headless_oidb.cjs", "headless_codec.cjs",
                                    "headless_media.cjs", "headless_sender.cjs",
                                    "headless_notices.cjs", "pipe_notice_bridge.cjs"])
def test_every_module_the_payload_loads_reaches_the_injected_copy(module):
    """A lazily-loaded module must be copied into the session, or the op reports it unavailable."""
    from qsick import headless

    assert module in headless.PIPE_JS_MODULES, (
        f"{module} is loaded by the payload but is not in PIPE_JS_MODULES, "
        "so the injected copy will never have it")
    assert (ROOT / "src" / "qsick" / module).is_file(), f"{module} does not exist in the source tree"


def test_every_module_the_payload_references_is_shipped():
    """Cross-reference the other way: anything the payload loads must be in the copy list."""
    source = ACTIONS.read_text(encoding="utf-8")
    loaded = set(re.findall(r"loadModule\('([\w.]+)'\)", source))
    loaded |= set(re.findall(r"require\('\./([\w.]+)'\)", source))
    from qsick import headless

    missing = sorted(name for name in loaded
                     if name.endswith(".cjs") and name not in headless.PIPE_JS_MODULES)
    assert not missing, f"payload loads these but they are not copied to the session: {missing}"


def test_every_copied_module_exists():
    """A name in the copy list with no file behind it is silently skipped, which reads as a gap
    in the payload rather than a packaging mistake."""
    from qsick import headless

    for module in headless.PIPE_JS_MODULES:
        assert (ROOT / "src" / "qsick" / module).is_file(), f"{module} is listed but missing"
