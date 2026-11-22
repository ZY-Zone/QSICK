"""Keep ``actions.json`` in sync with the capability survey.

- implemented entries are preserved as they are (they are the source of truth);
- missing planned actions from the survey are added, and their module is created
  from the ``PlannedAction`` template;
- test actions and the internal raw actions are skipped.

    uv run python tools/sync_actions.py
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "src" / "qsick" / "onebot" / "actions"
CATALOG = OUT / "actions.json"
SURVEY = ROOT.parent / "_analysis" / "action-diff.csv"

EXCLUDE = {".handle_quick_operation", ".handle_quick_operation_async"}

DOMAIN = {
    "空间": "qzone", "群管理": "group_admin", "消息/转发": "message", "群文件": "group_file",
    "群信息/成员": "group", "好友/资料": "friend", "AI/工具": "extended", "互动": "interaction",
    "群相册": "group_album", "表情": "face", "闪传/收藏": "flash", "系统表情": "face",
    "Ark/卡片": "ark", "在线文件": "online_file", "系统/账号": "system", "流式传输": "stream",
    "包透传": "packet", "频道 Guild": "guild", "其他/杂项": "extended", "扩展": "extended",
    "extends": "extended", "file": "group_file", "go-cqhttp": "message", "guild": "guild",
    "group": "group_admin", "msg": "message", "new": "extended", "packet": "packet",
    "stream": "stream", "system": "system", "user": "friend",
}


def pascal(name: str) -> str:
    parts = [part for part in re.split(r"[^A-Za-z0-9]+", name) if part]
    return "".join(part[:1].upper() + part[1:] for part in parts) or "Action"


def module_name(name: str) -> str:
    cleaned = re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_")
    if not cleaned or cleaned[0].isdigit():
        cleaned = f"a_{cleaned}"
    return cleaned


def guess_category(name: str) -> str:
    rules = [
        (r"qzone|feed", "qzone"), (r"album", "group_album"), (r"flash", "flash"),
        (r"online_file|online_folder", "online_file"), (r"guild", "guild"), (r"stream", "stream"),
        (r"packet|oidb|decrypt", "packet"),
        (r"group_(file|fileset)|upload_.*file|download_file", "group_file"),
        (r"group_(ban|kick|admin|name|card|leave|whole|special|search|add_option|member_|anonymous|tag|robot)", "group_admin"),
        (r"group", "group"), (r"friend|stranger|profile|dress|like|longnick", "friend"),
        (r"poke|emoji|reaction|input_status|online", "interaction"),
        (r"^send_|^delete_msg|^get_msg|forward|mark_.*read|history", "message"),
        (r"face|mface", "face"),
        (r"system|login|version|restart|cache|credential|cookie|rkey", "system"),
        (r"ark|share", "ark"),
    ]
    for pattern, category in rules:
        if re.search(pattern, name):
            return category
    return "extended"


def main() -> None:
    catalog = json.loads(CATALOG.read_text(encoding="utf-8")) if CATALOG.exists() else {"actions": [], "aliases": {}}
    known = {item["name"] for item in catalog["actions"]}

    rows: dict[str, str] = {}
    if SURVEY.exists():
        with SURVEY.open(encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                name = (row.get("name") or "").strip()
                if name and not name.startswith("test_") and name not in EXCLUDE:
                    rows.setdefault(name, row.get("category") or "")

    added = 0
    for name, raw_category in sorted(rows.items()):
        if name in known:
            continue
        category = DOMAIN.get(raw_category) or guess_category(name)
        class_name = f"{pascal(name)}Action"
        module = f"{category}/{module_name(name)}"
        directory = OUT / category
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{module_name(name)}.py"
        if not path.exists():
            path.write_text(
                f'"""Planned action: {name}.\n\n'
                "Registered from actions.json; implement `run` here (or provide the payload op)\n"
                'and flip the status to "implemented" in the catalog.\n'
                '"""\n\n'
                "from __future__ import annotations\n\n"
                "from qsick.onebot.actions.base import PlannedAction\n\n\n"
                f"class {class_name}(PlannedAction):\n"
                "    pass\n",
                encoding="utf-8",
            )
        catalog["actions"].append(
            {
                "name": name,
                "module": module,
                "class": class_name,
                "category": category,
                "summary": "",
                "status": "planned",
                "read_only": False,
                "params": [],
            }
        )
        known.add(name)
        added += 1

    catalog["actions"].sort(key=lambda item: item["name"])
    catalog.setdefault("aliases", {})
    CATALOG.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    implemented = sum(1 for item in catalog["actions"] if item.get("status") == "implemented")
    print(f"actions={len(catalog['actions'])} implemented={implemented} planned_added={added}")


if __name__ == "__main__":
    main()
