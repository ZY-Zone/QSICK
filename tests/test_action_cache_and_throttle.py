"""Read-only lookups must not make the caller wait, and must not go stale silently.

Two behaviours here were both wrong in the same direction, and both showed up as "actions are slow":

  * `throttle.SENSITIVE_INTERVALS` spaced calls out by making the *caller* sleep -- 30 s for
    `get_group_member_list`, 10 s for `get_group_member_info`. Measured in a real session: 24-46 s per
    `get_group_member_list` call, while the underlying request, once the client had the list, took 0.2 s. A bot
    consumer asks for members on every message, so every command took tens of seconds.
  * nothing cached the answer, so "space the requests out" was paid on every single call

The fix caches read-only results (`runtime._ACTION_CACHE_TTL`) and lets the throttle apply only to the real
outbound request. These tests pin the properties that matter: a repeat is served without touching the client,
a mutation is never cached, and asking for fresh data bypasses the cache.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from qsick import runtime as runtime_mod  # noqa: E402
from qsick.throttle import SENSITIVE_INTERVALS, Throttle  # noqa: E402

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
        assert cache_key("get_group_member_list", {"group_id": 1}) != \
            cache_key("get_group_member_list", {"group_id": 2}), "different groups must not share an entry"

    def test_echo_does_not_change_the_key(self):
        assert cache_key("get_group_list", {}) == cache_key("get_group_list", {"echo": "abc"}), \
            "echo is the caller's correlation id and must not affect caching"

    def test_no_cache_bypasses_the_cache(self):
        assert cache_key("get_group_list", {"no_cache": True}) is None, \
            "a caller asking for fresh data must not be handed a cached answer"

    def test_mutations_are_never_cached(self):
        for action in ("send_group_msg", "set_group_card", "delete_msg", "get_msg"):
            assert cache_key(action, {"group_id": GROUP}) is None, action

    def test_every_cached_action_is_also_throttled(self):
        # A cached action that is not throttled would be rate-limited by nothing on a cache miss.
        for action in runtime_mod._ACTION_CACHE_TTL:
            assert action in SENSITIVE_INTERVALS, \
                f"{action} is cached but not throttled; a cache miss would hit the client unthrottled"


class TestThrottleNoLongerBlocksReads:
    def test_burst_refill_is_shorter_than_the_cache_ttl(self):
        # `Throttle.record` only refills the burst allowance after `base * 4` seconds of silence. That has to be
        # shorter than the cache TTL, or every cache expiry pays another full wait.
        for action, ttl in runtime_mod._ACTION_CACHE_TTL.items():
            base = SENSITIVE_INTERVALS.get(action)
            if base is None:
                continue
            assert base * 4 < ttl, (
                f"{action}: burst refill needs {base * 4}s but the cache only lives {ttl}s, "
                "so the call after each expiry waits again")

    def test_the_first_three_lookups_are_free(self):
        throttle = Throttle(enabled=True)
        for _ in range(3):
            assert throttle.wait_for("get_group_member_list") == 0.0

    def test_sending_still_keeps_a_human_pace(self):
        throttle = Throttle(enabled=True)
        for _ in range(3):
            throttle.wait_for("send_group_msg")
            throttle.record("send_group_msg")
        # After the burst is spent the next send must wait: a bot that answers instantly to everything is the
        # pattern this protects against, and the fix deliberately did not touch send pacing.
        assert throttle.wait_for("send_group_msg") > 0.0
