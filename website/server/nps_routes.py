#!/usr/bin/env python3
"""给 NPS 增加 /QSICK 路由（www + apex），指向本地 8790 的静态站。

NPS 0.34.7 只在启动时读取 /etc/nps/conf/hosts.json，所以流程是：
备份 → 停 Nps → 修改 hosts.json → 校验 → 启动 → 探测新旧路由 → 失败则回滚。
匹配规则是最长 Location 前缀优先（见 lib/file/db.go GetInfoByHost），
因此 /QSICK 会先于 "/" 命中，且现有的 /QSICK/plugins-market 仍然优先。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from copy import deepcopy
from datetime import datetime
from pathlib import Path

STORE = Path("/etc/nps/conf/hosts.json")
TARGET = "127.0.0.1:8790"
ROUTES = [
    ("www.sickcc.online", "/QSICK", "QSICK official site"),
    ("sickcc.online", "/QSICK", "QSICK official site"),
]


def nps(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", *args], check=check, text=True, capture_output=True, timeout=40)


def is_active() -> bool:
    return nps("is-active", "Nps", check=False).stdout.strip() == "active"


def wait_inactive(timeout: float = 30.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if not is_active():
            return True
        time.sleep(0.4)
    return False


def start_nps() -> bool:
    for _ in range(4):
        nps("start", check=False)
        for _ in range(20):
            time.sleep(0.4)
            if is_active():
                return True
        time.sleep(1.0)
    return False


def http_code(host: str, path: str) -> str:
    result = subprocess.run(
        ["curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "8",
         "-H", f"Host: {host}", f"http://127.0.0.1{path}"],
        text=True, capture_output=True, timeout=15,
    )
    return result.stdout.strip()


def http_body(host: str, path: str) -> str:
    result = subprocess.run(
        ["curl", "-fsS", "--max-time", "8", "-H", f"Host: {host}", f"http://127.0.0.1{path}"],
        text=True, capture_output=True, timeout=15,
    )
    return result.stdout


def main() -> int:
    if not STORE.exists():
        print(f"missing {STORE}", file=sys.stderr)
        return 1

    backup = STORE.with_suffix(f".json.bak-qsick-{datetime.now():%Y%m%d-%H%M%S}")
    was_active = is_active()
    if was_active:
        print("stopping Nps...")
        nps("stop", check=False)
        if not wait_inactive(30):
            print("Nps did not stop in time", file=sys.stderr)
            nps("start", check=False)
            return 1
        time.sleep(0.8)
        print("Nps stopped.")

    original = STORE.read_text(encoding="utf-8")
    backup.write_text(original, encoding="utf-8")
    print("backup:", backup)
    started = False
    try:
        data = json.loads(original)
        if not isinstance(data, list) or not data:
            raise RuntimeError("unexpected NPS host store")
        template = next((e for e in data if (e.get("Target") or {}).get("LocalProxy")), data[0])
        next_id = max(int(e.get("Id", 0)) for e in data) + 1
        changed = False
        for host, location, remark in ROUTES:
            existing = next(
                (e for e in data if e.get("Host") == host and e.get("Location") == location),
                None,
            )
            target = {
                "TargetStr": TARGET,
                "TargetArr": [TARGET],
                "LocalProxy": True,
                "ProxyProtocol": 0,
            }
            if existing is not None:
                existing["Target"] = target
                existing["TargetIsHttps"] = False
                existing["IsClose"] = False
                print("updated:", host, location)
                changed = True
                continue
            entry = deepcopy(template)
            entry["Id"] = next_id
            next_id += 1
            entry["Host"] = host
            entry["Location"] = location
            entry["Remark"] = remark
            entry["Scheme"] = "all"
            entry["TargetIsHttps"] = False
            entry["Target"] = target
            entry["AutoSSL"] = False
            entry["AutoHttps"] = False
            entry["CertType"] = "none"
            entry["CertFile"] = ""
            entry["KeyFile"] = ""
            entry["RedirectURL"] = ""
            entry["PathRewrite"] = ""
            entry["NowConn"] = 0
            entry["Flow"] = {
                "ExportFlow": 0,
                "InletFlow": 0,
                "FlowLimit": 0,
                "TimeLimit": "0001-01-01T00:00:00Z",
            }
            data.append(entry)
            changed = True
            print("added:", host, location, "->", TARGET)

        if changed:
            STORE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        json.loads(STORE.read_text(encoding="utf-8"))
        print("hosts.json valid; entries:", len(data))

        if not start_nps():
            raise RuntimeError("Nps failed to start")
        started = True

        checks = []
        for host in ("www.sickcc.online", "sickcc.online"):
            body = http_body(host, "/QSICK/")
            checks.append((f"{host} /QSICK/", "QSICK" in body and "sickcc" in body))
            code = http_code(host, "/QSICK/healthz")
            checks.append((f"{host} /QSICK/healthz", code == "200"))
        # 确认既有路由没有被破坏
        checks.append(("www /blog/", http_code("www.sickcc.online", "/blog/") == "200"))
        checks.append(("www /blog/api/health", '"ok":true' in http_body("www.sickcc.online", "/blog/api/health").replace(" ", "")))
        checks.append(("apex /cw", http_code("sickcc.online", "/cw") in ("200", "301", "302", "304")))

        failed = [name for name, ok in checks if not ok]
        for name, ok in checks:
            print(("PASS " if ok else "FAIL ") + name)
        if failed:
            raise RuntimeError("failed checks: " + ", ".join(failed))
        print("NPS routes ready.")
        return 0
    except Exception as exc:
        print("ERROR:", exc, file=sys.stderr)
        shutil.copy2(backup, STORE)
        if is_active():
            nps("stop", check=False)
            wait_inactive(30)
        started = start_nps()
        print("restored backup; Nps active=" + str(started), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
