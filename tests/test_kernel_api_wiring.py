"""HeadlessManager.kernel_api() wiring.

The method itself is thin, so what is worth testing is its contract: it refuses
non-pipe instances, and it reports "not ready" clearly instead of letting the caller
hit a connection error later.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from qsick import headless
from qsick.headless import HeadlessManager


@pytest.fixture
def manager(tmp_path: Path, monkeypatch):
    """A manager with one instance directory of each kind.

    Instance ids must match ``C<number>`` -- that is the naming scheme the manager
    enforces, so the fixture uses C0/C1 rather than arbitrary names.
    """
    root = tmp_path / "headlessClient"
    for name, kind in (("C0", "pipe"), ("C1", "headless")):
        data = root / name / "data"
        data.mkdir(parents=True)
        (root / name / "instance.json").write_text(
            json.dumps({"id": name, "kind": kind}), encoding="utf-8")
    monkeypatch.setattr(HeadlessManager, "_dirs",
                        lambda self: sorted(p for p in root.iterdir() if p.is_dir()))
    return HeadlessManager(root)


def test_pipe_instance_returns_an_api(manager, monkeypatch):
    ready = {"called": False}

    class FakeApi:
        client = type("C", (), {"pipe_path": r"\\.\pipe\qsick-C0"})()

        def ready(self, timeout=5):
            ready["called"] = True
            return True

    monkeypatch.setattr("qsick.transport.kernel_api.api_for_instance",
                        lambda path: FakeApi())

    api = manager.kernel_api("C0")
    assert isinstance(api, FakeApi)
    assert ready["called"], "必须先确认管道可应答再返回"


def test_headless_instance_is_rejected(manager):
    """Headless reaches the kernel directly; a pipe would be a hop for nothing."""
    with pytest.raises(RuntimeError, match="只适用于 pipe"):
        manager.kernel_api("C1")


def test_unready_pipe_reports_the_pipe_name(manager, monkeypatch):
    class FakeApi:
        client = type("C", (), {"pipe_path": r"\\.\pipe\qsick-C0"})()

        def ready(self, timeout=5):
            return False

    monkeypatch.setattr("qsick.transport.kernel_api.api_for_instance",
                        lambda path: FakeApi())

    with pytest.raises(RuntimeError) as exc:
        manager.kernel_api("C0")
    message = str(exc.value)
    # The path is the first thing someone needs when debugging why it is not up.
    assert r"\\.\pipe\qsick-C0" in message
    assert "未就绪" in message
