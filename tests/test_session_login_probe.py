"""`login.info` is the account answer and the running answer at the same time.

Replaces two weaker signals that were tried first:

  * a recorded pid -- survives its process on disk, so a stopped session looks alive;
  * a shared `login-boot.json` -- every session writes the same file, so all of them reported the same
    account and "which account is this session" was unanswerable.

The DLL builds its action dispatcher with a **live** identity source (`qsick_http.cpp` wires
`getIdentity` to the session's own uin), so a successful reply describes that process. Since each session
has its own port, the port *is* the identity -- nothing has to be written anywhere.

The case this pins hardest is the one that is easy to get wrong: a closed session. The operator asked for
"connection refused means closed", and it must come back as an ordinary state rather than an exception,
because a stopped session is normal and a list refresh must not look like a failure.
"""
from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from qsick import headless as headless_module


class _FakeDll(BaseHTTPRequestHandler):
    """A minimal stand-in for the DLL's `/action` endpoint."""

    reply: dict = {}
    status: int = 200
    seen: list[dict] = []

    def do_POST(self):  # noqa: N802 - http.server naming
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b"{}"
        try:
            _FakeDll.seen.append(json.loads(body))
        except json.JSONDecodeError:
            _FakeDll.seen.append({})
        payload = json.dumps(self.reply).encode("utf-8")
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # silence
        pass


@pytest.fixture()
def fake_dll():
    """Run the stand-in on a free port and hand back the port."""
    _FakeDll.seen = []
    server = HTTPServer(("127.0.0.1", 0), _FakeDll)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def closed_port() -> int:
    """A port nothing listens on.

    Bound and released rather than hardcoded: a fixed number could be taken by anything, and then the test
    would be asserting about a stranger's service.
    """
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def test_a_logged_in_session_reports_its_own_uin(fake_dll):
    _FakeDll.reply = {"ok": True, "data": {"uin": "3905548034", "user_id": "3905548034",
                                           "nickname": "号主"}}
    result = headless_module.probe_session_login(fake_dll)

    assert result["running"] is True
    assert result["logged_in"] is True
    assert result["uin"] == "3905548034"
    assert result["nickname"] == "号主"
    # The probe has to be a real action call, not a health ping: only `login.info` carries the identity.
    assert _FakeDll.seen and _FakeDll.seen[0].get("op") == "login.info"


def test_a_refused_connection_is_reported_as_closed(fake_dll_unused=None):
    """The explicit requirement: connection refused -> closed, not an error.

    This is the ordinary state of a session the operator stopped. Raising here would turn every refresh of
    the session list into a failure the UI has to special-case.
    """
    result = headless_module.probe_session_login(closed_port())

    assert result["running"] is False
    assert result["closed"] is True
    assert result["logged_in"] is False
    assert result["uin"] == ""


def test_no_port_means_closed():
    """A session whose config has no port (or `nowport` 0) is closed, without attempting a connection."""
    result = headless_module.probe_session_login(0)
    assert result["running"] is False
    assert result["closed"] is True


def test_a_running_session_without_a_login_is_not_logged_in(fake_dll):
    """The DLL answered, so the process is up -- but `login.info` refuses until a session exists.

    Reporting that as closed would be wrong: the session is running and may be sitting at a QR code.
    """
    _FakeDll.reply = {"ok": False, "error": "NOT_READY: QQNT session is not available"}
    result = headless_module.probe_session_login(fake_dll)

    assert result["running"] is True
    assert result["closed"] is False
    assert result["logged_in"] is False
    assert "NOT_READY" in result["detail"]


def test_a_reply_without_a_usable_uin_is_not_logged_in(fake_dll):
    _FakeDll.reply = {"ok": True, "data": {"uin": "0", "nickname": ""}}
    result = headless_module.probe_session_login(fake_dll)

    assert result["running"] is True
    assert result["logged_in"] is False


def test_garbage_instead_of_json_is_reported_not_raised(fake_dll):
    """Something else may own the port. That is a state to report, not an exception to propagate."""
    _FakeDll.reply = ["not", "an", "object"]
    result = headless_module.probe_session_login(fake_dll)

    assert result["running"] is False
    assert result["logged_in"] is False
    assert result["detail"]
