"""Read-only lookups must not make the caller wait, and must not go stale silently.

Both behaviours here were wrong in the same direction, and both showed up as "actions are slow":

  * an outbound throttle spaced calls out by making the *caller* sleep -- 30 s for `get_group_member_list`,
    10 s for `get_group_member_info`. Measured in a real session: 24-46 s per `get_group_member_list` call, while
    the underlying request, once the client had the list, took 0.2 s. A bot consumer asks for members on every
    message, so every command took tens of seconds.
  * nothing cached the answer, so "space the requests out" was paid on every single call

The throttle is gone entirely now; the cache stays, and it is what keeps repeats from reaching the client at all.
These tests pin the properties that matter: a repeat is served from the cache, a mutation is never cached, and
asking for fresh data bypasses it.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from qsick import runtime as runtime_mod  # noqa: E402

GROUP = 2169010946


def cache_key(action: str, params: dict):
    # Bound to a throwaway instance: the method only reads module-level tables.
    helper = object.__new__(runtime_mod.Runtime)
    return runtime_mod.Runtime._action_cache_key(helper, action, params)


class TestActionCache:
    def test_a_repeat_is_served_from_cache(self):
        first = cache_key("get_group_member_list", {"group_id": GROUP})
        second = cache_key("get_group_member_list", {"group_id": GROUP})
        assert first is not None
        assert first == second, "the same request must hit the same entry"

    def test_the_key_separates_targets(self):
        first = cache_key("get_group_member_list", {"group_id": GROUP})
        second = cache_key("get_group_member_list", {"group_id": GROUP + 1})
        assert first != second, "two groups must not share an answer"

    def test_a_mutation_is_never_cached(self):
        # Caching a send would report the previous message id as if it were a new send.
        assert cache_key("send_group_msg", {"group_id": GROUP}) is None

    def test_fresh_data_bypasses_the_cache(self):
        assert cache_key("get_group_member_list", {"group_id": GROUP, "no_cache": True}) is None
        assert cache_key("get_group_member_list", {"group_id": GROUP, "force": True}) is None

    def test_the_echo_does_not_fragment_the_cache(self):
        # `echo` identifies a request, not its content: including it would make every consumer call a miss.
        first = cache_key("get_group_member_list", {"group_id": GROUP, "echo": "a"})
        second = cache_key("get_group_member_list", {"group_id": GROUP, "echo": "b"})
        assert first == second


class TestCacheCoverage:
    def test_cached_actions_are_the_expensive_ones(self):
        # Every entry must be a read-only lookup. A mutation in here would serve stale confirmations.
        for action in runtime_mod._ACTION_CACHE_TTL:
            assert "send" not in action and "set" not in action and "delete" not in action, action
