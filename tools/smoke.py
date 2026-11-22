"""End-to-end smoke test against real HTTP ports (mock transport, no QQ needed).

    uv run python tools/smoke.py [onebot_port] [webui_port]
"""

from __future__ import annotations

import asyncio
import sys

import httpx

from qsick.config import RuntimeConfig
from qsick.runtime import Runtime


async def main(onebot_port: int, webui_port: int) -> int:
    cfg = RuntimeConfig(data_dir="data/smoke")
    cfg.transport.mode = "mock"
    cfg.onebot.port = onebot_port
    cfg.webui.port = webui_port
    runtime = Runtime(cfg)
    await runtime.start(serve=True)
    failures = 0

    async def check(name: str, condition: bool, detail: str = "") -> None:
        nonlocal failures
        mark = "PASS" if condition else "FAIL"
        if not condition:
            failures += 1
        print(f"[{mark}] {name} {detail}".rstrip())

    try:
        await asyncio.sleep(1.0)  # let the mock post its demo message
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{webui_port}") as web:
            page = await web.get("/")
            await check("webui dashboard", page.status_code == 200 and "QSICK" in page.text, str(page.status_code))
            logs = await web.get("/api/logs?cursor=0")
            await check("webui logs api", logs.status_code == 200 and "records" in logs.json())
            plugins = await web.get("/api/plugins")
            await check("webui plugins api", any(p["id"] == "guard" and p["loaded"] for p in plugins.json()["plugins"]))
            actions = await web.get("/api/actions?q=send_group_msg")
            await check("webui actions api", any(a["name"] == "send_group_msg" for a in actions.json()["actions"]))
            run = await web.post("/api/actions/get_status", json={})
            await check("webui action invoke", run.json().get("status") == "ok", run.text[:80])
            partial = await web.get("/partials/status")
            await check("webui status partial", partial.status_code == 200 and "连接" in partial.text)
            plugin_page = await web.get("/plugins")
            await check(
                "webui plugin manager",
                plugin_page.status_code == 200 and "配置" in plugin_page.text and "卸载" in plugin_page.text,
            )
            plugin_config = await web.get("/plugins/guard/config")
            await check(
                "webui plugin config page",
                plugin_config.status_code == 200 and "cfg_mode" in plugin_config.text,
            )
            market = await web.get("/plugins/market")
            await check(
                "webui plugin market",
                market.status_code == 200 and runtime.config.plugins.market_url in market.text,
            )

        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{onebot_port}") as ob:
            health = await ob.get("/health")
            await check("onebot health", health.json().get("status") == "ok")
            login = await ob.post("/", json={"action": "get_login_info", "params": {}, "echo": "smoke"})
            body = login.json()
            await check("onebot get_login_info", body["status"] == "ok" and body["data"]["user_id"] == 10001, str(body))
            send = await ob.post(
                "/",
                json={
                    "action": "send_group_msg",
                    "params": {"group_id": 30001, "message": [{"type": "text", "data": {"text": "smoke"}}]},
                },
            )
            await check("onebot send_group_msg", send.json()["status"] == "ok", str(send.json())[:120])
            unknown = await ob.post("/", json={"action": "no_such_action", "params": {}})
            await check("onebot unknown action -> 1404", unknown.json()["retcode"] == 1404)
            stored = await ob.post("/", json={"action": "get_group_list", "params": {}})
            await check("onebot get_group_list", len(stored.json()["data"]) >= 1)
    finally:
        await runtime.stop()

    print(f"\n{failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    onebot = int(sys.argv[1]) if len(sys.argv) > 1 else 39111
    webui = int(sys.argv[2]) if len(sys.argv) > 2 else 39112
    raise SystemExit(asyncio.run(main(onebot, webui)))
