"""失败路径的可见性：HTTP 层不许把原因吞掉。

这一组规则来自一串真实故障，每个都让界面"毫无反应"而不是报错：

1. 路由只捕获 ``ChatError``，底层异常变成裸 500；
2. 前端 ``response.json()`` 遇到 HTML 就抛 ``Unexpected token 'I'``；
3. 失败时仍返回 ``ok: true``，前端据此认为成功；
4. 动作名对不上（``media.download`` 是宿主名，不在 OneBot registry 里）。

这些都不会让程序崩溃，只会让用户看见"点了没反应"，所以必须有测试守着。

聊天栏（``chat_api.py`` / ``chat.js``）已移除，第 1、2 条原先盯的是它们。规则本身
没有失效——现在盯的是还在调用传输层的那些路由，以及剩下的前端脚本。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "src" / "qsick" / "webui" / "app.py"
STATIC = ROOT / "src" / "qsick" / "webui" / "static"


def _routes(source: str) -> list[tuple[str, str]]:
    blocks = re.split(r'\n    @app\.(?:get|post|delete)\("([^"]+)"\)', source)
    return [(blocks[i], blocks[i + 1]) for i in range(1, len(blocks), 2)]


def test_every_transport_calling_route_has_a_generic_guard():
    """调用传输层却没兜底异常的路由，出错时只能返回裸 500。

    裸 500 到浏览器那里就是 "Internal Server Error" 这么一行 HTML，前端再当
    JSON 解析，用户看到的是 ``Unexpected token 'I'``——和真实原因毫无关系。
    """
    source = APP.read_text(encoding="utf-8")
    unguarded = [
        name for name, body in _routes(source)
        if ("runtime.invoke" in body or "runtime.send" in body)
        and "except Exception" not in body
    ]
    assert unguarded == [], f"these routes swallow transport failures: {unguarded}"


def test_no_route_returns_ok_true_together_with_an_error():
    """``ok: true`` 加 ``error`` 是自相矛盾，前端只认 ``ok``。

    曾经下载失败返回 ``{"ok": true, "url": "", "error": "..."}``，界面据此判定
    成功、照常替换内容，于是什么都没发生。
    """
    source = APP.read_text(encoding="utf-8")
    offenders = re.findall(r'\{"ok": True[^}]*"error"', source)
    assert offenders == [], f"contradictory success/failure payloads: {offenders}"


def test_the_action_api_does_not_leak_a_bare_500():
    """``/api/actions/{name}`` 同样不能把宿主异常扔成 500。"""
    source = APP.read_text(encoding="utf-8")
    marker = 'async def invoke_api(name: str, request: Request) -> JSONResponse:'
    body = source.split(marker, 1)[1].split("\n    @app.", 1)[0]
    assert "except Exception" in body


def test_the_frontend_never_bare_parses_a_response():
    """前端必须容忍非 JSON 响应。

    服务端出错时可能返回 HTML 错误页；``response.json()`` 会抛
    ``Unexpected token 'I'``，把真正的失败盖掉。允许的形式是先取 text 再试解析，
    或者带 ``.catch()`` 兜底。
    """
    offenders: list[str] = []
    for path in sorted(STATIC.glob("*.js")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if "await response.json()" in line and ".catch(" not in line:
                offenders.append(f"{path.name}: {line.strip()}")
    assert offenders == [], f"bare response.json() at: {offenders}"


def test_host_actions_used_by_the_webui_really_exist():
    """WebUI 直接问宿主的动作名必须真的在宿主里注册过。

    ``media.download`` 一度被交给 OneBot registry，得到
    ``unknown action 'media.download'``——那个名字属于宿主，不属于 OneBot。
    """
    host = (ROOT / "src" / "qsick" / "headless_actions.cjs").read_text(encoding="utf-8")
    declared = set(re.findall(r"^\s*'([a-z][a-z0-9_.]+)':\s*\[", host, re.M))
    aliases = dict(re.findall(r"^\s*'?([a-z][a-z0-9_.]+)'?:\s*'([a-z][a-z0-9_.]+)'", host, re.M))
    known = declared | set(aliases.values())

    source = APP.read_text(encoding="utf-8")
    asked = set(re.findall(r'runtime\.send\("([^"]+)"', source))
    unknown = sorted(name for name in asked if name not in known)
    assert unknown == [], f"these host action names do not exist: {unknown}"
