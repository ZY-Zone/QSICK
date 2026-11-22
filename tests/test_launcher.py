from __future__ import annotations

import json
from pathlib import Path

from qsick import launcher


def make_install(root: Path, version: str) -> Path:
    wrapper = root / "versions" / version / "resources" / "app" / "wrapper.node"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_bytes(b"MZ" + b"\0" * 64)
    (root / "versions" / "config.json").write_text(
        json.dumps({"curVersion": version, "baseVersion": version}), encoding="utf-8"
    )
    return wrapper


def test_check_ready_for_pinned_build(tmp_path: Path):
    make_install(tmp_path, launcher.PINNED_VERSION)
    report = launcher.check(tmp_path)
    assert report["status"] == "ready"
    assert report["installs"][0]["pinned"] is True
    assert report["installs"][0]["wrapper_sha256"]


def test_check_reports_version_mismatch(tmp_path: Path):
    make_install(tmp_path, "9.9.37-54395")
    report = launcher.check(tmp_path)
    assert report["status"] == "version_mismatch"
    assert report["installs"][0]["version"] == "9.9.37-54395"


def test_check_not_installed(tmp_path: Path):
    report = launcher.check(tmp_path)
    assert report["status"] == "not_installed"
    assert report["installs"] == []


def test_sha256_file(tmp_path: Path):
    target = tmp_path / "x.bin"
    target.write_bytes(b"abc")
    assert launcher.sha256_file(target) == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
