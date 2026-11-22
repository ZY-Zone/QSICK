from __future__ import annotations

from qsick.protocol.message import Message


def message(group_id: int = 30001, user_id: int = 20001, text: str = "hello") -> Message:
    return Message.from_payload(
        {
            "message_id": 1,
            "seq": 1,
            "time": 1,
            "chat": {"type": "group", "id": group_id},
            "sender": {"uin": user_id, "nickname": "Alice", "role": "member"},
            "elements": [{"type": "text", "data": {"text": text}}],
        }
    )


def set_guard(rt, **overrides):
    plugin = rt.plugins._loaded["guard"].instance
    plugin.config.update(overrides)
    return plugin


async def test_guard_passes_everything_by_default(runtime):
    assert await runtime.plugins.dispatch_message(message()) is True


async def test_guard_blacklists_a_group(runtime):
    set_guard(runtime, mode="blacklist", groups={"whitelist": [], "blacklist": [30001]})
    assert await runtime.plugins.dispatch_message(message(group_id=30001)) is False
    assert await runtime.plugins.dispatch_message(message(group_id=30002)) is True


async def test_guard_blacklists_a_user(runtime):
    set_guard(runtime, mode="blacklist", users={"whitelist": [], "blacklist": [20001]})
    assert await runtime.plugins.dispatch_message(message(user_id=20001)) is False
    assert await runtime.plugins.dispatch_message(message(user_id=20002)) is True


async def test_guard_whitelist_drops_unlisted(runtime):
    set_guard(runtime, mode="whitelist", groups={"whitelist": [30001], "blacklist": []})
    assert await runtime.plugins.dispatch_message(message(group_id=30001)) is True
    assert await runtime.plugins.dispatch_message(message(group_id=30099)) is False


async def test_guard_whitelist_with_empty_lists_drops_all(runtime):
    set_guard(runtime, mode="whitelist", groups={"whitelist": [], "blacklist": []})
    assert await runtime.plugins.dispatch_message(message()) is False


async def test_guard_always_allow_bypasses_blacklist(runtime):
    set_guard(
        runtime,
        mode="blacklist",
        groups={"whitelist": [], "blacklist": [30001]},
        always_allow_users=[20001],
    )
    assert await runtime.plugins.dispatch_message(message(user_id=20001)) is True


async def test_guard_scope_private_only_ignores_group(runtime):
    set_guard(
        runtime,
        mode="blacklist",
        scope="private",
        groups={"whitelist": [], "blacklist": [30001]},
    )
    assert await runtime.plugins.dispatch_message(message(group_id=30001)) is True


async def test_drop_reason_is_logged(runtime):
    from qsick import log as logmod

    set_guard(runtime, mode="blacklist", groups={"whitelist": [], "blacklist": [30001]})
    await runtime.plugins.dispatch_message(message(group_id=30001))
    categories = {record.category for record in logmod.BUFFER.tail(200)}
    assert "Plugin.guard" in categories
