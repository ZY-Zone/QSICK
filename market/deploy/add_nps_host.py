#!/usr/bin/env python3
"""Add the QSICK market path route to the NPS host table.

NPS maps ``Host + Location`` to a local target. This script:
  1. backs up /etc/nps/conf/hosts.json,
  2. removes any previous route for the same location,
  3. appends a route to 127.0.0.1:8787,
  4. prints the resulting entry.

Run it while NPS is stopped; start NPS afterwards.
"""

from __future__ import annotations

import json
import shutil
import time
from copy import deepcopy
from pathlib import Path

HOSTS = Path("/etc/nps/conf/hosts.json")
HOST = "sickcc.online"
LOCATION = "/QSICK/plugins-market"
TARGET = "127.0.0.1:8787"
REMARK = "QSICK plugin market"


def main() -> int:
    entries = json.loads(HOSTS.read_text(encoding="utf-8") or "[]")
    if not isinstance(entries, list) or not entries:
        raise SystemExit("hosts.json is empty; refusing to guess a template")

    template = next((entry for entry in entries if entry.get("Location") == "/"), entries[0])
    entry = deepcopy(template)
    entry["Id"] = max(int(item.get("Id", 0)) for item in entries) + 1
    entry["Host"] = HOST
    entry["Location"] = LOCATION
    entry["PathRewrite"] = ""
    entry["Remark"] = REMARK
    entry["Target"]["TargetStr"] = TARGET
    entry["Target"]["TargetArr"] = [TARGET]
    entry["Target"]["LocalProxy"] = True
    entry["TargetIsHttps"] = False
    entry["Client"]["Id"] = -1
    entry["Client"]["Mode"] = "local"
    entry["Client"]["Addr"] = "127.0.0.1"
    entry["Client"]["VerifyKey"] = "localproxy"
    entry["Flow"] = {
        "ExportFlow": 0,
        "InletFlow": 0,
        "FlowLimit": 0,
        "TimeLimit": "0001-01-01T00:00:00Z",
    }

    entries = [
        item
        for item in entries
        if not (item.get("Host") == HOST and item.get("Location") == LOCATION)
    ]
    entries.append(entry)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    shutil.copy2(HOSTS, f"/root/nps-hosts-backup-{stamp}.json")
    HOSTS.write_text(json.dumps(entries, ensure_ascii=False, indent=0), encoding="utf-8")
    print(json.dumps(entry, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
