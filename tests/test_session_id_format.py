"""Session ids: short, lowercase, and never a reason to lose an existing session.

The id names the session's directory and is the session's only lasting identity -- it is what the list shows,
what the start/close endpoints take, and what the auto-start setting stores. Two shapes must be accepted at
once, because changing the format cannot orphan what is already on disk: a session the list refuses to show has
no way to be started, closed or deleted.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from qsick import headless as headless_mod  # noqa: E402


class TestNewIdFormat:
    def test_it_is_four_characters(self):
        for _ in range(50):
            assert len(headless_mod.new_session_tmp_id()) == 4

    def test_it_is_lowercase_alphanumeric(self):
        for _ in range(50):
            value = headless_mod.new_session_tmp_id()
            assert value == value.lower()
            assert all(ch in "abcdefghijklmnopqrstuvwxyz0123456789" for ch in value)

    def test_it_avoids_the_characters_people_misread(self):
        # Read off the screen and typed back into a form; these pairs are the ones that get confused.
        for ch in "ilo01":
            assert ch not in headless_mod.SESSION_ID_ALPHABET, ch

    def test_ids_are_not_obviously_repeating(self):
        generated = {headless_mod.new_session_tmp_id() for _ in range(200)}
        assert len(generated) > 190, "随机性太差会导致频繁碰撞"


class TestValidation:
    def test_a_new_id_is_a_session_id(self):
        assert headless_mod.is_session_id(headless_mod.new_session_tmp_id())

    def test_the_previous_format_still_lists(self):
        # Sessions created before the change keep their directory, their port and their config. If they stopped
        # being recognised they would vanish from the list and become unmanageable.
        assert headless_mod.is_session_id("1110000010111011")

    def test_anything_else_is_not_a_session(self):
        for value in ("", "ab", "abc", "abcde", "ABCD", "ab/d", "../x", "k3m9x"):
            assert not headless_mod.is_session_id(value), value

    def test_the_generators_reduced_alphabet_is_enforced(self):
        # `0` and `1` are deliberately outside the alphabet (they read as `o` and `l`). A directory named with
        # them was never produced by this code, so it is not a session.
        assert headless_mod.is_session_id("a0b1") is False
        assert headless_mod.is_session_id("0101") is False
