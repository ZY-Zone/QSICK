"""Tests for the instance lifecycle rules changed this session.

Four behaviours, each of which is easy to regress and expensive to notice:

  1. pipe is a singleton. Two pipe instances would share one data root (reports/06) and
     fight over the same login state.
  2. nothing starts automatically unless the instance opted in via auto_start.
  3. on a device with no instances at all, first launch creates exactly one pipe instance
     and turns its auto_start on, so a fresh clone is usable.
  4. the account field survives the task protocol, since one DLL can serve several sessions.

These are unit-level: no QQ process is launched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from qsick.headless import HeadlessManager
from qsick.transport.tasks import TaskBatch, TaskResult


def make_manager(tmp_path: Path) -> HeadlessManager:
    root = tmp_path / "headlessClient"
    root.mkdir(parents=True, exist_ok=True)
    return HeadlessManager(root)


def write_instance(root: Path, instance_id: str, kind: str, **meta) -> Path:
    """Creates an instance directory with its metadata, without launching anything.

    A headless instance also gets a `host.js`, because that file is what marks the directory as an
    instance at all: a directory with the right name but no host cannot start, and listing it would offer
    the operator something that only fails later.
    """
    path = root / instance_id
    (path / "data").mkdir(parents=True, exist_ok=True)
    payload = {"id": instance_id, "kind": kind, **meta}
    (path / "instance.json").write_text(json.dumps(payload), encoding="utf-8")
    if kind != "pipe":
        (path / "host.js").write_bytes(b"// fake host")
    return path


# -- one pipe client, many sessions ------------------------------------------

def test_only_one_pipe_client_exists(tmp_path: Path):
    """Sessions are launches of one client, not copies of it.

    Copying the client per session cost ~1.3 GB each time and is not what "repeat-launch QQ.exe" means.
    So `create(kind="pipe")` when a client already exists must fail rather than quietly produce `Pipe2`.
    """
    manager = make_manager(tmp_path)
    write_instance(manager.root, "Pipe", "pipe", name="本机 QQ")

    with pytest.raises(ValueError):
        manager.create(name="第二个", kind="pipe")

    pipe_dirs = [item.name for item in manager.root.iterdir()
                 if item.is_dir() and item.name.startswith("Pipe")]
    assert pipe_dirs == ["Pipe"], f"多出了客户端副本: {pipe_dirs}"


def test_sessions_live_under_the_pipe_client(tmp_path: Path):
    """Each session gets `Pipe/dll/<tmp_id>/` with its own DLL and port.

    The directory is the session: it holds the DLL copy that launch loaded and the config naming its port.
    Nothing is written outside it, which is what makes the list readable from disk.
    """
    from qsick import headless as headless_module

    manager = make_manager(tmp_path)
    write_instance(manager.root, "Pipe", "pipe", name="本机 QQ")
    scratch = manager.session_dll_dir()
    assert scratch == manager.root / "Pipe" / "dll"

    tmp_id = headless_module.new_session_tmp_id()
    (scratch / tmp_id).mkdir(parents=True)
    (scratch / tmp_id / headless_module.PIPE_DLL_NAME).write_bytes(b"MZ")
    (scratch / tmp_id / headless_module.PIPE_DLL_CONFIG).write_text(
        json.dumps({"session": {"tmp_id": tmp_id, "port": 7540}, "port": 7540}), encoding="utf-8")

    sessions = headless_module.read_session_dirs(manager.root)
    assert [item["tmp_id"] for item in sessions] == [tmp_id]
    assert sessions[0]["port"] == 7540
    assert sessions[0]["has_dll"] is True


def test_session_dirs_ignore_anything_not_ours(tmp_path: Path):
    """Only 16-character binary names count.

    Anything else under `dll/` is not a session, and treating it as one would aim the close button at
    something unrelated.
    """
    from qsick import headless as headless_module

    manager = make_manager(tmp_path)
    write_instance(manager.root, "Pipe", "pipe", name="本机 QQ")
    scratch = manager.session_dll_dir()
    scratch.mkdir(parents=True)
    (scratch / "not-a-session").mkdir()
    (scratch / "0123456789abcdef").mkdir()
    (scratch / "0").mkdir()
    good = headless_module.new_session_tmp_id()
    (scratch / good).mkdir()
    # A session also needs a config now: without one there is no port, so nothing can be started, probed or
    # closed, and such a directory is skipped rather than shown as a row reading "配置缺失".
    (scratch / good / headless_module.PIPE_DLL_CONFIG).write_text(
        json.dumps({"session": {"tmp_id": good, "port": 7540, "nowport": 0}}), encoding="utf-8")

    names = [item["tmp_id"] for item in headless_module.read_session_dirs(manager.root)]
    assert names == [good]


def test_a_directory_without_a_config_is_not_a_session(tmp_path: Path):
    """What a delete that could not remove a locked DLL leaves behind.

    Such a directory has no port, so it cannot be started, probed or meaningfully closed. Showing it produced
    a row reading "配置缺失" that survived a delete attempt -- indistinguishable from a UI bug.
    """
    from qsick import headless as headless_module

    manager = make_manager(tmp_path)
    write_instance(manager.root, "Pipe", "pipe", name="本机 QQ")
    scratch = manager.session_dll_dir()
    orphan = scratch / headless_module.new_session_tmp_id()
    orphan.mkdir(parents=True)

    assert headless_module.read_session_dirs(manager.root) == []


def test_headless_instances_are_unlimited(tmp_path: Path):
    """Headless instances were never limited by the pipe rule and still are not."""
    manager = make_manager(tmp_path)
    write_instance(manager.root, "Pipe", "pipe", name="本机 QQ")

    first = manager.create(name="账号一", kind="headless")
    second = manager.create(name="账号二", kind="headless")

    assert first["id"] != second["id"]
    assert {first["kind"], second["kind"]} == {"headless"}


# -- auto_start ------------------------------------------------------------

def test_auto_start_defaults_to_off(tmp_path: Path):
    manager = make_manager(tmp_path)
    path = write_instance(manager.root, "C0", "headless")

    data = manager._get("C0").to_dict()
    assert data["auto_start"] is False
    # And it must not be written into the metadata just by being read.
    meta = json.loads((path / "instance.json").read_text(encoding="utf-8"))
    assert "autoStart" not in meta


def test_auto_start_can_be_switched_on_and_persists(tmp_path: Path):
    manager = make_manager(tmp_path)
    path = write_instance(manager.root, "C0", "headless")

    result = manager.set_auto_start("C0", True)

    assert result["auto_start"] is True
    meta = json.loads((path / "instance.json").read_text(encoding="utf-8"))
    assert meta["autoStart"] is True

    # A fresh manager (as after a restart) must see the same value.
    assert make_manager(tmp_path)._get("C0").to_dict()["auto_start"] is True


def test_auto_start_switch_is_per_instance(tmp_path: Path):
    manager = make_manager(tmp_path)
    write_instance(manager.root, "C0", "headless")
    write_instance(manager.root, "C1", "headless")

    manager.set_auto_start("C0", True)

    assert manager._get("C0").to_dict()["auto_start"] is True
    assert manager._get("C1").to_dict()["auto_start"] is False


# -- first-run bootstrap ---------------------------------------------------

def test_first_run_creates_one_pipe_instance_but_does_not_autostart_it(tmp_path: Path):
    """A fresh device gets a pipe instance, and nothing more.

    It is created so the UI is usable immediately, but auto_start stays off: QSICK must not
    launch any instance on startup, not even one it just created. Starting it is the user's
    decision, made with the per-instance switch.
    """
    manager = make_manager(tmp_path)
    assert manager.instances() == []
    assert not (manager.root / "Pipe").exists()

    from qsick.runtime import Runtime

    # The helper only touches metadata when no instance exists.
    Runtime._ensure_first_instance(manager)

    # The pipe client now lives outside the instance list, so assert on the directory it creates rather
    # than on `instances()`, which reports headless instances only.
    assert (manager.root / "Pipe").is_dir(), "首次运行应当在没有任何实例时建好 pipe 客户端"
    meta = json.loads((manager.root / "Pipe" / "instance.json").read_text(encoding="utf-8"))
    assert meta["kind"] == "pipe"
    assert meta.get("autoStart") is not True, "首次创建的客户端不该被设为自动启动"


def test_first_run_does_nothing_when_an_instance_exists(tmp_path: Path):
    """A user who only wants headless instances must not get a pipe one forced on them."""
    manager = make_manager(tmp_path)
    write_instance(manager.root, "C0", "headless", name="我的账号")

    from qsick.runtime import Runtime

    Runtime._ensure_first_instance(manager)

    assert [item["id"] for item in manager.instances()] == ["C0"]


# -- account field ---------------------------------------------------------

def test_account_is_written_into_the_task_row(tmp_path: Path):
    batch = TaskBatch(tmp_path)
    batch.add("1+1", "a", account="10001")

    assert batch.tasks[0]["account"] == "10001"


def test_batch_default_account_applies_to_every_task(tmp_path: Path):
    batch = TaskBatch(tmp_path, default_account="20002")
    batch.add("1+1", "a")
    batch.add("2+2", "b")

    assert [t["account"] for t in batch.tasks] == ["20002", "20002"]


def test_a_task_can_override_the_batch_default(tmp_path: Path):
    batch = TaskBatch(tmp_path, default_account="20002")
    batch.add("1+1", "a", account="30003")

    assert batch.tasks[0]["account"] == "30003"


def test_no_account_leaves_the_field_out_entirely(tmp_path: Path):
    """Absent means "whichever session exists"; an empty string would also be a name."""
    batch = TaskBatch(tmp_path)
    batch.add("1+1", "a")

    assert "account" not in batch.tasks[0]


def test_results_carry_the_account_back(tmp_path: Path):
    batch = TaskBatch(tmp_path)
    batch.owndata.mkdir(parents=True, exist_ok=True)
    (batch.owndata / "results.jsonl").write_text(
        json.dumps({"id": "a", "account": "10001", "ok": True, "result": "2"}) + "\n",
        encoding="utf-8")

    result = batch.results()[0]
    assert result.account == "10001"
    assert result.value == "2"


def test_old_results_without_account_still_parse(tmp_path: Path):
    """Backwards compatibility: results written before this field existed."""
    batch = TaskBatch(tmp_path)
    batch.owndata.mkdir(parents=True, exist_ok=True)
    (batch.owndata / "results.jsonl").write_text(
        json.dumps({"id": "a", "ok": True, "result": "2"}) + "\n", encoding="utf-8")

    result = batch.results()[0]
    assert result.account == ""
    assert result.ok is True


def test_task_result_default_account_is_empty():
    assert TaskResult(id="a", ok=True).account == ""


# -- pipe directory naming -------------------------------------------------
# The pipe instance's directory is the fixed name "Pipe". Older layouts used C<n>, which
# shared headless's numbering and made the singleton hard to spot.

def test_pipe_instance_uses_the_fixed_directory_name(tmp_path: Path):
    from qsick.headless import PIPE_INSTANCE_ID

    assert PIPE_INSTANCE_ID == "Pipe"

    manager = make_manager(tmp_path)
    write_instance(manager.root, "C0", "headless")
    manager._migrate_pipe_dir_name()  # no pipe yet: nothing to do
    assert (manager.root / "C0").is_dir()

    # A pipe instance created under the old naming scheme gets renamed.
    old = write_instance(manager.root, "C1", "pipe", name="老管道")
    manager.instances()

    assert (manager.root / "Pipe").is_dir()
    assert not old.exists()
    meta = json.loads((manager.root / "Pipe" / "instance.json").read_text(encoding="utf-8"))
    assert meta["id"] == "Pipe"


def test_migration_never_overwrites_an_existing_pipe_directory(tmp_path: Path):
    """Two directories must never be merged: that would mix account data."""
    manager = make_manager(tmp_path)
    write_instance(manager.root, "Pipe", "pipe", name="已存在")
    write_instance(manager.root, "C1", "pipe", name="旧的")

    manager.instances()

    # Both stay put; the existing Pipe is untouched.
    assert (manager.root / "Pipe").is_dir()
    assert (manager.root / "C1").is_dir()
    meta = json.loads((manager.root / "Pipe" / "instance.json").read_text(encoding="utf-8"))
    assert meta["name"] == "已存在"


def test_migration_leaves_headless_instances_alone(tmp_path: Path):
    manager = make_manager(tmp_path)
    write_instance(manager.root, "C0", "headless")
    write_instance(manager.root, "C1", "headless")

    manager.instances()

    assert (manager.root / "C0").is_dir()
    assert (manager.root / "C1").is_dir()
    assert not (manager.root / "Pipe").exists()


def test_instances_excludes_the_pipe_client(tmp_path: Path):
    """The instance list is headless only.

    `Pipe/` is the single client that sessions are launched from, not an instance. Listing it here is what
    put a Pipe row in the instance table -- a thing with a different lifecycle (start/close per session,
    rather than delete an instance directory), shown as if the two were interchangeable.
    """
    manager = make_manager(tmp_path)
    write_instance(manager.root, "C0", "headless")
    write_instance(manager.root, "C1", "headless")
    write_instance(manager.root, "Pipe", "pipe")

    assert [item["id"] for item in manager.instances()] == ["C0", "C1"]
    # The client directory still exists and is still reachable by id; it is only absent from the list.
    assert (manager.root / "Pipe").is_dir()
    assert manager.pipe_instance_id() == "Pipe"
