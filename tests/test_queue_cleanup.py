"""Every start begins with empty queues and empty event streams.

Not housekeeping for its own sake. The event streams are ingested from offset zero on each run, so a stream left
from the previous run is **replayed**: every message in it is written to the store again as if it had just
arrived. Measured on a real tree before this existed: `events.jsonl` 8.4 MB, `events.jsonl.1` 17 MB, and
`tasks.jsonl` 17 MB of work that no longer had a session.

The line the cleanup must not cross is the session's identity: `qsick_pipe.json` holds the port the caller
resolves, and the DLL copy beside it is what the running client loads. Both survive.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from qsick import headless as headless_module  # noqa: E402

TMP_ID = "a1b2"


def session_dir(root: Path) -> Path:
    path = headless_module.session_dll_dir(root) / TMP_ID
    path.mkdir(parents=True, exist_ok=True)
    return path


def own_dir(root: Path) -> Path:
    path = root / headless_module.PIPE_INSTANCE_ID / "owndata"
    path.mkdir(parents=True, exist_ok=True)
    return path


def seed(directory: Path) -> None:
    (directory / "tasks.jsonl").write_text('{"id":"t1"}\n', encoding="utf-8")
    (directory / "results.jsonl").write_text('{"id":"t1"}\n', encoding="utf-8")
    (directory / "events.jsonl").write_text('{"type":"message"}\n', encoding="utf-8")
    (directory / "events.jsonl.1").write_text('{"type":"message"}\n', encoding="utf-8")
    (directory / "claims").mkdir(exist_ok=True)
    (directory / "claims" / "t1").write_text("1", encoding="utf-8")
    (directory / "q-login").mkdir(exist_ok=True)
    (directory / "q-login" / "tasks.jsonl").write_text('{"id":"l1"}\n', encoding="utf-8")
    (directory / "q-groupfile").mkdir(exist_ok=True)


def test_every_queue_file_of_a_session_is_cleared(tmp_path: Path):
    root = tmp_path / "headlessClient"
    directory = session_dir(root)
    seed(directory)

    removed = headless_module.reset_queue_files(root)

    assert removed["files"] >= 4
    assert removed["dirs"] >= 3
    for name in headless_module.QUEUE_FILES:
        assert not (directory / name).exists(), name
    assert not (directory / "events.jsonl.1").exists()
    for name in headless_module.QUEUE_DIRS:
        assert not (directory / name).exists(), name


def test_the_session_identity_survives(tmp_path: Path):
    root = tmp_path / "headlessClient"
    directory = session_dir(root)
    seed(directory)
    config = directory / "qsick_pipe.json"
    config.write_text('{"port":7540}', encoding="utf-8")
    dll = directory / "qsick_pipe.dll"
    dll.write_bytes(b"MZ")

    headless_module.reset_queue_files(root)

    assert config.is_file(), "端口配置是会话的身份，不能被清理掉"
    assert dll.is_file(), "会话要加载的 DLL 副本必须留下"


def test_the_pre_migration_shared_files_are_cleared_too(tmp_path: Path):
    """The old shared location is still read as a legacy stream, so leaving it replays it."""
    root = tmp_path / "headlessClient"
    own = own_dir(root)
    seed(own)

    headless_module.reset_queue_files(root)

    assert not (own / "events.jsonl").exists()
    assert not (own / "tasks.jsonl").exists()
    assert not (own / "q-login").exists()


def test_an_empty_tree_is_not_an_error(tmp_path: Path):
    root = tmp_path / "headlessClient"
    removed = headless_module.reset_queue_files(root)
    assert removed == {"files": 0, "dirs": 0}
    assert not headless_module.session_dll_dir(root).exists(), "清理不该顺手建出空目录"


def test_a_file_that_cannot_be_removed_does_not_stop_the_rest(tmp_path: Path, monkeypatch):
    """Windows keeps a queue file open while a dying process still holds it."""
    root = tmp_path / "headlessClient"
    directory = session_dir(root)
    seed(directory)

    original = Path.unlink

    def refuse(self, *args, **kwargs):  # noqa: ANN001, ANN003
        if self.name == "tasks.jsonl":
            raise PermissionError("in use")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", refuse)
    headless_module.reset_queue_files(root)

    assert (directory / "tasks.jsonl").exists(), "拒绝删除的那个应当留在原处"
    assert not (directory / "events.jsonl").exists(), "其余的仍应被清理"
