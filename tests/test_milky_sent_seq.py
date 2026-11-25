"""Recall of one's own message must not depend on the message being in the store.

Measured live: `send_group_message` returned seq=1453, the message was visible in
`get_history_messages`, and `get_message` for that seq still answered
`-404 message sequence not found unambiguously`. A history query anchored at a seq does not
return the anchored message, and a self-sent message is not stored at all unless the network
profile reports self messages -- so both of the bridge's lookups missed.

The send receipt already carries the seq and the message ID, so the bridge remembers them.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
for entry in (str(ROOT), str(ROOT / "src")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from qsick.onebot import milky_bridge  # noqa: E402


def fresh():
    milky_bridge._sent_seqs.clear()


def test_a_send_receipt_is_remembered_and_resolves_without_the_store():
    fresh()
    milky_bridge.remember_sent("3905548034", "group", 2169010946, 1453, 7786337763362271981)
    assert milky_bridge.recall_sent("3905548034", "group", 2169010946, 1453) == 7786337763362271981
    # Different conversation or account must not resolve: sequence numbers are local to a chat.
    assert milky_bridge.recall_sent("3905548034", "group", 521860577, 1453) is None
    assert milky_bridge.recall_sent("2208948865", "group", 2169010946, 1453) is None
    assert milky_bridge.recall_sent("3905548034", "private", 2169010946, 1453) is None


def test_incomplete_receipts_are_ignored():
    fresh()
    milky_bridge.remember_sent("1", "group", 2, None, 3)
    milky_bridge.remember_sent("1", "group", 2, 5, None)
    milky_bridge.remember_sent("1", "group", 2, 0, 3)
    milky_bridge.remember_sent("1", "group", 2, "not a number", 3)
    milky_bridge.remember_sent("", "group", 2, 5, 3)
    assert milky_bridge.recall_sent("1", "group", 2, 5) is None


def test_the_table_is_bounded_and_evicts_oldest_first():
    fresh()
    for seq in range(1, milky_bridge._SENT_SEQ_LIMIT + 100):
        milky_bridge.remember_sent("1", "group", 2, seq, 100000 + seq)
    assert len(milky_bridge._sent_seqs) == milky_bridge._SENT_SEQ_LIMIT
    assert milky_bridge.recall_sent("1", "group", 2, 1) is None              # evicted
    assert milky_bridge.recall_sent("1", "group", 2, milky_bridge._SENT_SEQ_LIMIT + 99) is not None


def test_enrich_records_even_when_the_receipt_is_already_complete():
    """The fast path returns early; the receipt must be recorded before that."""
    fresh()
    from qsick.onebot.milky_bridge import MilkyBridge

    runtime = SimpleNamespace(store=None, invoke=None)
    bridge = MilkyBridge(runtime)
    result = {"status": "ok", "data": {"message_id": "900", "message_seq": 77, "time": "1795617672"}}
    asyncio.run(bridge.enrich("send_group_message", {"account": "1", "group_id": 2}, result))
    assert milky_bridge.recall_sent("1", "group", 2, 77) == 900
