"""Tests for NapCat / SnowLuma request-body compatibility.

The three rules the operator set, and what each needs to prove:

  1. same format on both sides -> use it unchanged
     Tested implicitly: an action with no alias table must come back byte-identical, otherwise the
     normaliser is silently rewriting requests that were already correct.

  2. different formats -> accept both; if both are present, NapCat's wins and the other is dropped
     The tie-break is the interesting part. It is checked directly, because a normaliser that merged
     the two or preferred the wrong one would still "work" on single-format requests.

  3. permissive about types and extra fields
     ids arrive as int, decimal string, or float; extra fields must survive untouched. Rejecting
     either would break callers that this project has no reason to break.

Scope is also tested: a protocol other than onebotv11 / onebotv11-EXTemotions must not be rewritten at
all, since the objective says these rules apply to those two protocols and other protocols come later.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from qsick.onebot import compat  # noqa: E402

GROUP = "2169010946"


class TestScope:
    """Only the two OneBot protocols are touched."""

    def test_onebotv11_is_normalised(self) -> None:
        params = {"group_id": GROUP, "file_id": "/f", "parent_directory": "/", "target_directory": "/a"}
        out = compat.normalize("move_group_file", params, "onebotv11")
        assert out["folder_id"] == "/"

    def test_extemotions_is_no_longer_a_protocol_it_knows(self) -> None:
        # The EXTemotions chat protocol was removed; its name must now fall outside the table rather
        # than being silently normalised. A stale answer here would rewrite params for a protocol that
        # no longer exists.
        params = {"group_id": GROUP, "file_id": "/f", "parent_directory": "/", "target_directory": "/a"}
        out = compat.normalize("move_group_file", params, "onebotv11-EXTemotions")
        assert out == params
        assert "folder_id" not in out

    def test_other_protocol_is_untouched(self) -> None:
        params = {"group_id": GROUP, "parent_directory": "/", "target_directory": "/a"}
        out = compat.normalize("move_group_file", params, "milky")
        # Returned as-is: a protocol this table does not describe must not be silently rewritten.
        assert out == params
        assert "folder_id" not in out

    def test_missing_protocol_is_treated_as_onebot(self) -> None:
        # An instance with no protocol configured is not "some other protocol" -- it is unconfigured,
        # and OneBot v11 is the default. Treating empty as "opt out" made every rule silently inactive
        # on such instances, which is exactly the kind of failure that never announces itself.
        params = {"group_id": GROUP, "parent_directory": "/", "target_directory": "/a"}
        out = compat.normalize("move_group_file", params, "")
        assert out["folder_id"] == "/"

    def test_non_dict_passes_through(self) -> None:
        # A malformed body should reach the action's own validation, not crash here.
        assert compat.normalize("move_group_file", None, "onebotv11") is None  # type: ignore[arg-type]


class TestMoveGroupFile:
    """The one action the reference calls incompatible in both directions."""

    def test_napcat_spelling(self) -> None:
        out = compat.normalize("move_group_file", {
            "group_id": GROUP, "file_id": "/f",
            "current_parent_directory": "/from", "target_parent_directory": "/to",
        }, "onebotv11")
        assert out["folder_id"] == "/from"
        assert out["target_folder_id"] == "/to"

    def test_snowluma_spelling(self) -> None:
        out = compat.normalize("move_group_file", {
            "group_id": GROUP, "file_id": "/f",
            "parent_directory": "/from", "target_directory": "/to",
        }, "onebotv11")
        assert out["folder_id"] == "/from"
        assert out["target_folder_id"] == "/to"

    def test_both_present_napcat_wins(self) -> None:
        out = compat.normalize("move_group_file", {
            "group_id": GROUP, "file_id": "/f",
            "current_parent_directory": "/napcat", "target_parent_directory": "/napcat_to",
            "parent_directory": "/sl", "target_directory": "/sl_to",
        }, "onebotv11")
        assert out["folder_id"] == "/napcat"
        assert out["target_folder_id"] == "/napcat_to"

    def test_losing_spelling_is_removed(self) -> None:
        # Not merely overridden: the alternative key must be gone, so the op cannot read a stale value
        # if it ever looks the old name up directly.
        out = compat.normalize("move_group_file", {
            "group_id": GROUP, "file_id": "/f",
            "current_parent_directory": "/napcat",
            "parent_directory": "/sl", "target_directory": "/sl_to",
        }, "onebotv11")
        assert "parent_directory" not in out
        # target_parent_directory was absent, so the SnowLuma spelling supplies it.
        assert out["target_folder_id"] == "/sl_to"

    def test_null_alternative_is_ignored(self) -> None:
        # Clients often serialise every schema field and leave unused ones null; a null must not
        # overwrite a real value supplied under the primary spelling.
        out = compat.normalize("move_group_file", {
            "group_id": GROUP, "file_id": "/f",
            "current_parent_directory": "/from", "parent_directory": None,
            "target_parent_directory": "/to", "target_directory": None,
        }, "onebotv11")
        assert out["folder_id"] == "/from"
        assert out["target_folder_id"] == "/to"


class TestOtherActions:
    def test_rename_accepts_both(self) -> None:
        napcat = compat.normalize("rename_group_file", {
            "group_id": GROUP, "file_id": "/f",
            "current_parent_directory": "/d", "new_name": "a.txt",
        }, "onebotv11")
        assert napcat["parent_folder_id"] == "/d"
        assert napcat["file_name"] == "a.txt"

        snowluma = compat.normalize("rename_group_file", {
            "group_id": GROUP, "file_id": "/f", "new_name": "a.txt",
        }, "onebotv11")
        assert snowluma["file_name"] == "a.txt"

    def test_create_folder_accepts_name_or_folder_name(self) -> None:
        with_name = compat.normalize("create_group_file_folder", {
            "group_id": GROUP, "name": "n",
        }, "onebotv11")
        assert with_name["folder_name"] == "n"

        with_folder_name = compat.normalize("create_group_file_folder", {
            "group_id": GROUP, "folder_name": "n",
        }, "onebotv11")
        assert with_folder_name["folder_name"] == "n"


    def test_upload_group_file_folds_onebot_spellings(self) -> None:
        # The OneBot field is `file`; the op reads `file_path`. Without this fold a standard
        # upload_group_file request reaching the op would be told file_path is missing.
        out = compat.normalize("upload_group_file", {
            "group_id": GROUP, "file": "/tmp/a.txt", "name": "a.txt", "folder": "/",
        }, "onebotv11")
        assert out["file_path"] == "/tmp/a.txt"
        assert out["name"] == "a.txt"
        assert out["folder_id"] == "/"
        assert "file" not in out

    def test_delete_group_file_accepts_busid_spellings(self) -> None:
        for spelling in ("bus_id", "busid", "busId"):
            out = compat.normalize("delete_group_file", {
                "group_id": GROUP, "file_id": "/f", spelling: 102,
            }, "onebotv11")
            assert out["bus_id"] == 102, spelling

    def test_unknown_action_is_unchanged(self) -> None:
        # Rule 1: where the two agree there is nothing to do. An action with no table must return the
        # same set of keys, otherwise the normaliser is altering requests it does not understand.
        params = {"group_id": GROUP, "duration": 60, "novel_field": {"x": 1}}
        out = compat.normalize("set_group_ban", params, "onebotv11")
        assert set(out) == set(params)
        assert out["novel_field"] == {"x": 1}


class TestPermissiveness:
    """Rule 3: accept as many types and extra fields as possible."""

    def test_ids_from_int(self) -> None:
        out = compat.normalize("get_group_root_files", {"group_id": 2169010946}, "onebotv11")
        assert out["group_id"] == 2169010946
        assert isinstance(out["group_id"], int)

    def test_ids_from_decimal_string(self) -> None:
        out = compat.normalize("get_group_root_files", {"group_id": "2169010946"}, "onebotv11")
        assert out["group_id"] == 2169010946
        assert isinstance(out["group_id"], int)

    def test_ids_from_float(self) -> None:
        # JSON parsers produce floats for large numeric literals in some clients.
        out = compat.normalize("get_group_root_files", {"group_id": 2169010946.0}, "onebotv11")
        assert out["group_id"] == 2169010946
        assert isinstance(out["group_id"], int)

    def test_non_numeric_string_is_left_alone(self) -> None:
        # Not silently coerced to 0: the downstream validation should see the bad value and say so.
        out = compat.normalize("get_group_root_files", {"group_id": "not-a-number"}, "onebotv11")
        assert out["group_id"] == "not-a-number"

    def test_extra_fields_survive(self) -> None:
        out = compat.normalize("move_group_file", {
            "group_id": GROUP, "file_id": "/f",
            "parent_directory": "/", "target_directory": "/a",
            "some_future_field": True,
        }, "onebotv11")
        assert out["some_future_field"] is True

    def test_file_count_string_accepted(self) -> None:
        out = compat.normalize("get_group_root_files", {
            "group_id": GROUP, "file_count": "50",
        }, "onebotv11")
        assert out["file_count"] == "50" or out["file_count"] == 50

    def test_generic_casing_variants(self) -> None:
        out = compat.normalize("get_group_file_url", {
            "groupId": GROUP, "fileId": "/f",
        }, "onebotv11")
        # Coerced to int, so compare numerically: the point of the test is that `groupId` was
        # recognised as `group_id`, not that it kept its string form.
        assert out["group_id"] == int(GROUP)
        assert out["file_id"] == "/f"


class TestDeclaredNamesAreNotRewritten:
    """A generic spelling must not overwrite a field the action declares by that name.

    `file_id` accepts `id` in the generic table, which is right for the file actions. But
    `get_forward_msg` has a parameter genuinely named `id` -- the merged-forward card id. Folding it
    renamed the caller's `id` to `file_id`, the required `id` vanished, and every call answered
    "missing required parameter 'id'" however correct the request was.

    Measured on a real client: `get_forward_msg` failed every time before this, and the error pointed at
    the caller rather than at the rewriting.
    """

    def test_a_declared_id_survives(self) -> None:
        out = compat.normalize("get_forward_msg", {"id": "123"}, "onebotv11", known={"id"})
        assert out["id"] == "123"
        assert "file_id" not in out

    def test_an_undeclared_id_still_folds_to_file_id(self) -> None:
        # get_image declares file/file_id, not id -- the compatibility fold must keep working there.
        out = compat.normalize("get_image", {"id": "123"}, "onebotv11", known={"file_id", "file"})
        assert out["file_id"] == "123"

    def test_without_known_the_old_behaviour_is_unchanged(self) -> None:
        out = compat.normalize("get_forward_msg", {"id": "123"}, "onebotv11")
        assert out["file_id"] == "123"
