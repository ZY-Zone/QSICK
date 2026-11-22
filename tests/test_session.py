from __future__ import annotations

import re
from pathlib import Path

import pytest

from qsick.config import RuntimeConfig
from qsick.transport import session
from qsick.transport.base import TransportError
from qsick.transport.pipe import PipeTransport

TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")


def test_token_roundtrip(tmp_path: Path):
    token = session.new_token()
    assert TOKEN_RE.match(token)
    session.write_token(4242, token, str(tmp_path))
    assert session.read_token(4242, str(tmp_path)) == token
    assert (tmp_path / "4242.token").exists()


def test_pipe_names_include_the_token():
    ctl, rsp, evt = session.pipe_names(r"\\.\pipe\qsick", 123, "abc")
    assert ctl == r"\\.\pipe\qsick.123.abc.ctl"
    assert rsp == r"\\.\pipe\qsick.123.abc.rsp"
    assert evt == r"\\.\pipe\qsick.123.abc.evt"


def test_pipe_names_without_token_are_plain():
    ctl, rsp, evt = session.pipe_names(r"\\.\pipe\qsick", 123, "")
    assert ctl == r"\\.\pipe\qsick.123.ctl"
    assert rsp == r"\\.\pipe\qsick.123.rsp"
    assert evt == r"\\.\pipe\qsick.123.evt"


def _transport(config: RuntimeConfig) -> PipeTransport:
    return PipeTransport(config.transport)


def test_random_mode_generates_and_stores(tmp_path: Path):
    config = RuntimeConfig(data_dir=str(tmp_path))
    config.transport.pipe_auth = "random"
    config.transport.session_dir = str(tmp_path / "sessions")
    token = _transport(config)._resolve_token(999)
    assert TOKEN_RE.match(token)
    assert session.read_token(999, config.transport.session_dir) == token


def test_fixed_mode_requires_a_token(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("QSICK_PIPE_TOKEN", raising=False)
    config = RuntimeConfig(data_dir=str(tmp_path))
    config.transport.pipe_auth = "fixed"
    config.transport.session_dir = str(tmp_path / "sessions")
    with pytest.raises(TransportError):
        _transport(config)._resolve_token(1)


def test_fixed_mode_uses_configured_token(tmp_path: Path):
    config = RuntimeConfig(data_dir=str(tmp_path))
    config.transport.pipe_auth = "fixed"
    config.transport.pipe_token = "deadbeef"
    config.transport.session_dir = str(tmp_path / "sessions")
    assert _transport(config)._resolve_token(1) == "deadbeef"
    assert session.read_token(1, config.transport.session_dir) == "deadbeef"


def test_none_mode_returns_empty_token(tmp_path: Path):
    config = RuntimeConfig(data_dir=str(tmp_path))
    config.transport.pipe_auth = "none"
    assert _transport(config)._resolve_token(1) == ""
