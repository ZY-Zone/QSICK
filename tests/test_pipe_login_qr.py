"""Pipe instances must surface a login QR, merged from the injected DLL's output.

The bug this covers: a pipe instance wrote `"qr": ""` once and never updated it, so the WebUI
showed a blank panel forever. Root cause was two-layered:

  1. The QR comes from NodeIKernelLoginService, which needs a full startup chain
     (StartupSessionWrapper -> WrapperSession -> initWithDeskTopConfig -> login.initConfig
     -> addKernelLoginListener -> connect). Without it the service answers every call
     successfully and never calls back.
  2. Even with the chain in place, the callback only arrives in a context whose event loop
     turns. Tasks provably run on one whose loop does not (timers and microtasks stayed at
     0 forever); the injected bridge script's context does (a 60 ms timer fired and wrote
     its marker).

So the bootstrap lives in the bridge script and writes `login-boot.json` plus `qrcode.txt`
into owndata, and `status()` merges them. These tests pin the merge, which is the part that
can regress silently.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from qsick.headless import Instance


def make_instance(root: Path, kind: str = "pipe") -> Instance:
    """An instance whose data/status.json exists, as start() would leave it."""
    (root / "data").mkdir(parents=True, exist_ok=True)
    instance = Instance(path=root, id="C1", kind=kind)
    (root / "data" / "status.json").write_text(
        json.dumps({"id": "C1", "phase": "starting", "pid": 4242, "qr": "",
                    "uin": "", "uid": "", "nickname": "", "error": "",
                    "transport": "pipe"}),
        encoding="utf-8")
    (root / "owndata").mkdir(parents=True, exist_ok=True)
    return instance


def write_boot(root: Path, **fields) -> None:
    (root / "owndata" / "login-boot.json").write_text(
        json.dumps(fields), encoding="utf-8")


def write_qr(root: Path, text: str) -> None:
    (root / "owndata" / "qrcode.txt").write_text(text, encoding="utf-8")


def test_qr_from_owndata_reaches_status(tmp_path: Path):
    instance = make_instance(tmp_path)
    write_qr(tmp_path, "data:image/png;base64,AAAA")

    assert instance.status()["qr"] == "data:image/png;base64,AAAA"
    assert instance.to_dict()["qr"] == "data:image/png;base64,AAAA"


def test_waiting_qrcode_stage_becomes_the_phase(tmp_path: Path):
    instance = make_instance(tmp_path)
    write_boot(tmp_path, stage="waiting_qrcode", qrLen=900)

    assert instance.status()["phase"] == "waiting_qrcode"


@pytest.mark.parametrize("stage,expected", [
    ("scanned", "scanned"),
    ("qrcode_failed", "qrcode_failed"),
    ("login_failed", "login_failed"),
    ("logged_in", "ready"),
])
def test_stages_map_to_phases(tmp_path: Path, stage: str, expected: str):
    instance = make_instance(tmp_path)
    write_boot(tmp_path, stage=stage)

    assert instance.status()["phase"] == expected


def test_login_succeed_means_initializing_not_ready(tmp_path: Path):
    """A scanned code is not a live session; ready only after logged_in."""
    instance = make_instance(tmp_path)
    write_boot(tmp_path, stage="login_succeed", uin="10001")

    status = instance.status()
    assert status["phase"] == "initializing"
    assert status["uin"] == "10001"


def test_session_ready_makes_the_instance_ready(tmp_path: Path):
    """A created session is the pipe equivalent of ready.

    This is the state the DLL reports once init() completed. It has to become "ready" here or
    the instance would sit in "initializing" forever even though actions and messages are
    possible -- which is exactly the gap this session closed.
    """
    instance = make_instance(tmp_path)
    write_boot(tmp_path, stage="session_ready", uin="10001", sessionId="nt_1")

    data = instance.to_dict()
    assert data["phase"] == "ready"
    # to_dict exposes the account number as "qq", not "uin".
    assert data["qq"] == "10001"
    assert data["running"] is True


def test_session_ready_records_the_session_id(tmp_path: Path):
    instance = make_instance(tmp_path)
    write_boot(tmp_path, stage="session_ready", uin="10001", sessionId="nt_1")

    status = instance.status()
    assert status["session"] == "nt_1"


def test_session_failure_is_reported_not_hidden(tmp_path: Path):
    """A failed session must surface.

    Reporting it as "initializing" would be worse than an error: the UI would wait forever and
    the user could not tell a broken login from a slow one.
    """
    instance = make_instance(tmp_path)
    write_boot(tmp_path, stage="session_failed",
               sessionErr="getNTWrapperSession returned nothing")

    data = instance.to_dict()
    assert data["phase"] == "error"
    assert "getNTWrapperSession" in data["error"]


def test_session_failure_clears_a_stale_uin(tmp_path: Path):
    """The uin may be known while the session is not; don't imply a usable account."""
    instance = make_instance(tmp_path)
    write_boot(tmp_path, stage="session_failed", uin="10001", sessionErr="boom")

    data = instance.to_dict()
    assert data["phase"] == "error"


def test_logged_in_marks_the_instance_ready_and_alive(tmp_path: Path):
    """The activity refresh matters: without it to_dict() calls the instance dead."""
    instance = make_instance(tmp_path)
    # An old timestamp in status.json would normally make it look stopped.
    stale = json.loads((tmp_path / "data" / "status.json").read_text(encoding="utf-8"))
    stale["updated_at"] = 1
    (tmp_path / "data" / "status.json").write_text(json.dumps(stale), encoding="utf-8")

    write_boot(tmp_path, stage="logged_in", uin="10001")

    data = instance.to_dict()
    assert data["phase"] == "ready"
    assert data["running"] is True
    assert data["logged_in"] is True


def test_headless_instances_are_not_merged(tmp_path: Path):
    """The merge is pipe-only; headless writes its own status directly."""
    instance = make_instance(tmp_path, kind="headless")
    write_qr(tmp_path, "data:image/png;base64,AAAA")
    write_boot(tmp_path, stage="waiting_qrcode")

    status = instance.status()
    assert status["qr"] == ""
    assert status["phase"] == "starting"


def test_no_boot_files_leaves_status_untouched(tmp_path: Path):
    instance = make_instance(tmp_path)

    status = instance.status()
    assert status["phase"] == "starting"
    assert status["qr"] == ""


def test_partial_boot_file_is_tolerated(tmp_path: Path):
    """A half-written JSON must not break the accounts page."""
    instance = make_instance(tmp_path)
    (tmp_path / "owndata" / "login-boot.json").write_text("{oops", encoding="utf-8")
    write_qr(tmp_path, "data:image/png;base64,BBBB")

    status = instance.status()
    assert status["phase"] == "starting"
    assert status["qr"] == "data:image/png;base64,BBBB"


def test_error_from_boot_file_is_surfaced(tmp_path: Path):
    instance = make_instance(tmp_path)
    write_boot(tmp_path, stage="qrcode_failed", err="超时/1")

    assert instance.status()["error"] == "超时/1"
