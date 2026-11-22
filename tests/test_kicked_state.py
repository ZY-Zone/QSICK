"""被踢下线后，界面必须一直显示"为什么掉线"。

这里锁住两个曾经真实存在的缺陷：

1. ``stop()`` 把 ``status.json`` **整份重写**成 ``{"phase": "stopped"}``，被踢的
   原因（``kicked_type`` / ``security_kicked_type`` / ``error``）随之消失；
2. 被踢后 host 进程会退出，10 秒心跳窗口一过 ``alive`` 就是 False，只按
   ``phase`` 判断的实现于是把界面显示成"未登录"，用户根本看不出自己被踢了。

"为什么掉线"恰恰是最该显示的信息，所以它不能依赖进程还活着。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qsick.headless import Instance


def _instance(tmp_path: Path, status: dict) -> Instance:
    path = tmp_path / "C0"
    (path / "data").mkdir(parents=True)
    # host.js marks the directory as a headless instance; without it the manager does not
    # recognise the directory and every assertion about it fails with an empty list.
    (path / "host.js").write_bytes(b"// fake host")
    (path / "data" / "status.json").write_text(json.dumps(status), encoding="utf-8")
    return Instance(id="C0", path=path)


def test_a_live_kicked_instance_is_reported_as_kicked(tmp_path):
    instance = _instance(tmp_path, {
        "id": "C0", "phase": "kicked", "pid": 4321,
        "updated_at": time.time(), "uin": "10001",
        "security_kicked_type": 3, "error": "登录被安全策略强制下线",
    })
    described = instance.to_dict()
    assert described["kicked"] is True
    assert described["security_kicked"] is True
    assert described["kick_reason"] == "登录被安全策略强制下线"
    assert described["logged_in"] is False


def test_a_kicked_instance_stays_kicked_after_its_host_exits(tmp_path):
    """被踢后进程退出，心跳早已过期——状态仍然必须是"被踢"。

    只按 ``updated_at`` 判定会把它降级成"未登录"，把原因彻底藏起来。
    """
    instance = _instance(tmp_path, {
        "id": "C0", "phase": "kicked", "pid": 0,
        "updated_at": time.time() - 600, "uin": "10001",
        "error": "账号已在其他设备登录",
    })
    described = instance.to_dict()
    assert described["running"] is False        # 进程确实没了
    assert described["kicked"] is True          # 但"被踢"这件事还在
    assert described["phase"] == "kicked"
    assert described["kick_reason"] == "账号已在其他设备登录"


def test_stopping_keeps_the_reason_it_was_kicked(tmp_path):
    """停止实例只该改变"是否运行"，不该抹掉"为什么停下来"。"""
    from qsick.headless import HeadlessManager

    path = tmp_path / "C0"
    (path / "data").mkdir(parents=True)
    # host.js marks the directory as a headless instance; without it the manager does not
    # recognise the directory and every assertion about it fails with an empty list.
    (path / "host.js").write_bytes(b"// fake host")
    (path / "data" / "status.json").write_text(json.dumps({
        "id": "C0", "phase": "kicked", "pid": 0, "updated_at": time.time(),
        "security_kicked_type": 2, "kicked_type": 1, "error": "登录被安全策略强制下线",
    }), encoding="utf-8")

    manager = HeadlessManager(root=tmp_path)
    manager.stop("C0")

    status = json.loads((path / "data" / "status.json").read_text(encoding="utf-8"))
    assert status["phase"] == "stopped"
    assert status["previous_phase"] == "kicked"
    assert status["security_kicked_type"] == 2
    assert status["error"] == "登录被安全策略强制下线"

    # 且对外仍然报成"被踢"，而不是一句普通的"已停止"。
    described = manager.instances()[0]
    assert described["kicked"] is True
    assert described["phase"] == "kicked"


def test_stopping_a_healthy_instance_leaves_no_kick_trace(tmp_path):
    """正常停止不该伪造出"被踢"。"""
    from qsick.headless import HeadlessManager

    path = tmp_path / "C0"
    (path / "data").mkdir(parents=True)
    # host.js marks the directory as a headless instance; without it the manager does not
    # recognise the directory and every assertion about it fails with an empty list.
    (path / "host.js").write_bytes(b"// fake host")
    (path / "data" / "status.json").write_text(json.dumps({
        "id": "C0", "phase": "ready", "pid": 0, "updated_at": time.time(),
    }), encoding="utf-8")

    manager = HeadlessManager(root=tmp_path)
    manager.stop("C0")

    status = json.loads((path / "data" / "status.json").read_text(encoding="utf-8"))
    assert status["phase"] == "stopped"
    assert "previous_phase" not in status
    assert manager.instances()[0]["kicked"] is False

def test_the_transport_keeps_the_kick_reason_from_the_host():
    """宿主在踢下线事件里带了 ``kicked`` / ``reason``，传输层不能挑掉。

    早先 ``push`` 只取 logged_in/uin/nickname/online 四个字段，把原因丢在传输层，
    于是到了界面只剩一句无从查起的"未登录"。
    """
    from qsick.transport.headless import HeadlessTransport

    transport = HeadlessTransport()
    transport.push({"kind": "login", "payload": {
        "logged_in": False, "online": False, "uin": "10001",
        "nickname": "", "kicked": True, "reason": "账号已在其他设备登录",
    }})
    login = transport._login
    assert login.kicked is True
    assert login.kick_reason == "账号已在其他设备登录"
    assert login.logged_in is False


def test_a_normal_login_does_not_look_kicked():
    from qsick.transport.headless import HeadlessTransport

    transport = HeadlessTransport()
    transport.push({"kind": "login", "payload": {
        "logged_in": True, "online": True, "uin": "10001", "nickname": "me",
    }})
    assert transport._login.kicked is False
    assert transport._login.kick_reason == ""


def test_the_footer_shows_the_kick_rather_than_a_bare_logged_out():
    """页脚必须把"被踢"和"未登录"区分开，并给出原因。

    顶栏那个账号状态控件已经按操作者的要求删掉（它重复了页脚和账户管理页），所以被踢状态现在由页脚承载。
    这个区分本身必须留着：被踢需要处理（重新登录/检查安全策略），未登录只是还没登录。
    """
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    env = Environment(loader=FileSystemLoader(str(ROOT / "src" / "qsick" / "webui" / "templates")),
                      autoescape=select_autoescape())
    template = env.get_template("base.html")
    base_info = {"version": "1", "transport": "headless", "webui_auth": False,
                 "uin": "10001", "nickname": "", "logged_in": False,
                 "onebot": {"port": 3000}, "webui": {"port": 5754}}

    kicked = template.render(info=dict(base_info, kicked=True,
                                       kick_reason="登录被安全策略强制下线"), page="")
    assert "已被踢下线" in kicked
    assert "登录被安全策略强制下线" in kicked
    assert "is-kicked" in kicked

    plain = template.render(info=dict(base_info, kicked=False, kick_reason=""), page="")
    assert "未登录" in plain and "已被踢下线" not in plain
    assert "is-kicked" not in plain

    # The old top-bar pill is gone; nothing may put it back.
    assert 'class="pill' not in kicked and 'class="pill' not in plain
