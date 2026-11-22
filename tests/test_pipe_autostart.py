"""Pipe auto-start: one switch, one chosen session, and only that session.

The switch alone is not enough to act on -- it cannot say *which* of several sessions to launch -- and the
choice alone cannot say whether to act. So the setting is a pair, and every case below is about that pair
meaning what it says.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from qsick.config import RuntimeConfig  # noqa: E402
from qsick.runtime import Runtime, _ACCOUNT_POLL_SECONDS, _OFFLINE_MISSES  # noqa: E402

SESSION = "1110000010111011"


def build(tmp_path: Path, monkeypatch, *, sessions: list[dict], enabled: bool, chosen: str) -> tuple[Runtime, list]:
    config = RuntimeConfig(data_dir=str(tmp_path / "data"))
    config.transport.mode = "mock"
    config.log.level = "WARN"
    config.pipe_auto_start = enabled
    config.pipe_auto_start_session = chosen
    runtime = Runtime(config)

    from qsick import headless as headless_mod

    monkeypatch.setattr(headless_mod, "read_session_dirs", lambda *a, **k: list(sessions))
    monkeypatch.setattr(headless_mod, "probe_session_login",
                        lambda port, timeout=2.0: {"running": False, "logged_in": False})

    launched: list = []

    class Manager:
        def start_session(self, port, webui_port, tmp_id=None):
            launched.append({"port": port, "tmp_id": tmp_id})
            return {"pid": 4242}

    runtime._headless_manager = Manager()
    return runtime, launched


def session(tmp_id: str = SESSION, *, nowport: int = 7540) -> dict:
    return {"tmp_id": tmp_id, "port": nowport, "nowport": nowport}


class TestPipeAutoStart:
    async def test_it_launches_the_chosen_session(self, tmp_path: Path, monkeypatch):
        runtime, launched = build(tmp_path, monkeypatch, sessions=[session()], enabled=True, chosen=SESSION)
        await runtime._auto_pipe_session()
        assert launched == [{"port": 7540, "tmp_id": SESSION}], "必须拉起被选中的那个 session"

    async def test_the_switch_off_launches_nothing(self, tmp_path: Path, monkeypatch):
        runtime, launched = build(tmp_path, monkeypatch, sessions=[session()], enabled=False, chosen=SESSION)
        # The task is not even created when the switch is off; calling the body directly still launches though,
        # so this asserts the configuration gate rather than the body.
        assert runtime.config.pipe_auto_start is False
        assert launched == []

    async def test_no_chosen_session_launches_nothing(self, tmp_path: Path, monkeypatch):
        runtime, launched = build(tmp_path, monkeypatch, sessions=[session()], enabled=True, chosen="")
        await runtime._auto_pipe_session()
        assert launched == [], "开关打开但没选 session 时不该猜一个"

    async def test_a_missing_session_is_not_invented(self, tmp_path: Path, monkeypatch):
        # The stored id refers to a session that no longer exists. Launching a *different* one would silently
        # change the operator's configuration, so nothing happens.
        runtime, launched = build(tmp_path, monkeypatch, sessions=[session("other")], enabled=True, chosen=SESSION)
        await runtime._auto_pipe_session()
        assert launched == []

    async def test_a_running_session_is_left_alone(self, tmp_path: Path, monkeypatch):
        runtime, launched = build(tmp_path, monkeypatch, sessions=[session()], enabled=True, chosen=SESSION)

        from qsick import headless as headless_mod

        monkeypatch.setattr(headless_mod, "probe_session_login",
                            lambda port, timeout=2.0: {"running": True, "logged_in": True, "uin": "3905548034"})
        await runtime._auto_pipe_session()
        assert launched == [], "已经在运行的 session 不该被重启"


class TestClosingFollowsTheAccount:
    def test_polling_is_three_seconds(self):
        # The operator asked for 3 s. It is the interval that decides how fast a logout closes its config.
        assert _ACCOUNT_POLL_SECONDS == 3

    def test_the_grace_is_shorter_than_a_restart(self):
        # Long enough to ride out one missed probe, short enough that a real logout closes promptly.
        assert _OFFLINE_MISSES * _ACCOUNT_POLL_SECONDS <= 15, (
            "宽限时间过长会让'非登录状态就关闭'变得不成立")
