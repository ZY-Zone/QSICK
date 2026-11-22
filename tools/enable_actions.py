"""One-off: flip a batch of planned actions to implemented, with real modules.

Kept as a record of the migration; afterwards the modules are normal sources and
``actions.json`` is the status authority.

    uv run python tools/enable_actions.py
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ACTIONS = ROOT / "src" / "qsick" / "onebot" / "actions"
CATALOG = ACTIONS / "actions.json"

TEMPLATE = '''"""{summary}"""

from __future__ import annotations

from typing import Any

from qsick.onebot.actions.base import Action, call


class {class_name}(Action):
    async def run(self, ctx: Any, **params: Any) -> Any:
{body}
'''

PLAN: dict[str, dict] = {
    "get_group_honor_info": {
        "summary": "获取群荣誉信息",
        "params": [
            {"name": "group_id", "kind": "int", "required": True, "default": None, "desc": ""},
            {"name": "type", "kind": "str", "required": False, "default": "all", "desc": ""},
        ],
        "body": """        data = await call(ctx, "group.honor", {"group_id": params["group_id"], "type": params.get("type", "all")}, "group honor")
        return data""",
    },
    "get_group_at_all_remain": {
        "summary": "获取群 @全体成员 剩余次数",
        "params": [{"name": "group_id", "kind": "int", "required": True, "default": None, "desc": ""}],
        "body": """        data = await call(ctx, "group.at_all_remain", {"group_id": params["group_id"]}, "at-all remain")
        return {
            "can_at_all": bool(data.get("can_at_all", True)),
            "remain_at_all_count_for_group": int(data.get("remain_at_all_count_for_group", 0)),
            "remain_at_all_count_for_uin": int(data.get("remain_at_all_count_for_uin", 0)),
        }""",
    },
    "get_group_shut_list": {
        "summary": "获取群禁言列表",
        "params": [{"name": "group_id", "kind": "int", "required": True, "default": None, "desc": ""}],
        "body": """        data = await call(ctx, "group.shut_list", {"group_id": params["group_id"]}, "mute list")
        return data if isinstance(data, list) else data.get("members", [])""",
    },
    "get_unidirectional_friend_list": {
        "summary": "获取单向好友列表",
        "params": [],
        "body": """        data = await call(ctx, "friend.unidirectional", {}, "unidirectional friends")
        return data if isinstance(data, list) else data.get("friends", [])""",
    },
    "get_friends_with_category": {
        "summary": "获取好友分组",
        "params": [],
        "body": """        data = await call(ctx, "friend.categories", {}, "friend categories")
        return data if isinstance(data, list) else data.get("categories", [])""",
    },
    "set_group_remark": {
        "summary": "设置群备注",
        "params": [
            {"name": "group_id", "kind": "int", "required": True, "default": None, "desc": ""},
            {"name": "remark", "kind": "str", "required": False, "default": "", "desc": ""},
        ],
        "body": """        await call(ctx, "group.remark", {"group_id": params["group_id"], "remark": params.get("remark", "")}, "group remark")
        return None""",
    },
    "set_input_status": {
        "summary": "设置输入状态（正在输入）",
        "params": [
            {"name": "user_id", "kind": "int", "required": True, "default": None, "desc": ""},
            {"name": "event_type", "kind": "int", "required": False, "default": 1, "desc": ""},
        ],
        "body": """        await call(ctx, "input.status", {"user_id": params["user_id"], "event_type": params.get("event_type", 1)}, "input status")
        return None""",
    },
    "get_msg_emoji_likes": {
        "summary": "获取消息表情回应列表",
        "params": [
            {"name": "message_id", "kind": "int", "required": True, "default": None, "desc": ""},
            {"name": "count", "kind": "int", "required": False, "default": 20, "desc": ""},
        ],
        "body": """        data = await call(ctx, "message.emoji_likes", {"message_id": params["message_id"], "count": params.get("count", 20)}, "emoji likes")
        return data if isinstance(data, list) else data.get("likes", [])""",
    },
    "fetch_emoji_like": {
        "summary": "拉取消息表情回应（增量）",
        "params": [{"name": "message_id", "kind": "int", "required": True, "default": None, "desc": ""}],
        "body": """        data = await call(ctx, "message.emoji_like.fetch", {"message_id": params["message_id"]}, "fetch emoji likes")
        return {"emoji_likes": data.get("likes", [])}""",
    },
    "get_flash_file_list": {
        "summary": "获取闪传文件集列表",
        "params": [{"name": "count", "kind": "int", "required": False, "default": 20, "desc": ""}],
        "body": """        data = await call(ctx, "flash.list", {"count": params.get("count", 20)}, "flash list")
        return {"filesets": data.get("filesets", [])}""",
    },
    "delete_flash_file": {
        "summary": "删除闪传文件",
        "params": [
            {"name": "fileset_id", "kind": "str", "required": True, "default": None, "desc": ""},
            {"name": "file_id", "kind": "str", "required": False, "default": "", "desc": ""},
        ],
        "body": """        await call(ctx, "flash.delete", {"fileset_id": params["fileset_id"], "file_id": params.get("file_id", "")}, "flash delete")
        return None""",
    },
    "rename_flash_file": {
        "summary": "重命名闪传文件",
        "params": [
            {"name": "fileset_id", "kind": "str", "required": True, "default": None, "desc": ""},
            {"name": "file_id", "kind": "str", "required": True, "default": None, "desc": ""},
            {"name": "name", "kind": "str", "required": True, "default": None, "desc": ""},
        ],
        "body": """        await call(ctx, "flash.rename", {"fileset_id": params["fileset_id"], "file_id": params["file_id"], "name": params["name"]}, "flash rename")
        return None""",
    },
    "get_group_root_files": {
        "summary": "获取群文件根目录",
        "params": [{"name": "group_id", "kind": "int", "required": True, "default": None, "desc": ""}],
        "body": """        data = await call(ctx, "group.files.root", {"group_id": params["group_id"]}, "group files")
        return {"files": data.get("files", []), "folders": data.get("folders", [])}""",
    },
    "get_group_files_by_folder": {
        "summary": "获取群文件夹内容",
        "params": [
            {"name": "group_id", "kind": "int", "required": True, "default": None, "desc": ""},
            {"name": "folder_id", "kind": "str", "required": True, "default": None, "desc": ""},
        ],
        "body": """        data = await call(ctx, "group.files.folder", {"group_id": params["group_id"], "folder_id": params["folder_id"]}, "folder files")
        return {"files": data.get("files", []), "folders": data.get("folders", [])}""",
    },
    "create_group_file_folder": {
        "summary": "创建群文件夹",
        "params": [
            {"name": "group_id", "kind": "int", "required": True, "default": None, "desc": ""},
            {"name": "name", "kind": "str", "required": True, "default": None, "desc": ""},
        ],
        "body": """        data = await call(ctx, "group.files.create_folder", {"group_id": params["group_id"], "name": params["name"]}, "create folder")
        return {"folder_id": str(data.get("folder_id", ""))}""",
    },
}


def main() -> None:
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    by_name = {item["name"]: item for item in catalog["actions"]}
    enabled = 0
    for name, spec in PLAN.items():
        entry = by_name.get(name)
        if entry is None:
            print(f"skip {name}: not in catalog")
            continue
        path = ACTIONS / f"{entry['module']}.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            TEMPLATE.format(summary=spec["summary"], class_name=entry["class"], body=spec["body"]),
            encoding="utf-8",
        )
        entry["status"] = "implemented"
        entry["summary"] = spec["summary"]
        entry["params"] = spec["params"]
        enabled += 1
    CATALOG.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    implemented = sum(1 for item in catalog["actions"] if item.get("status") == "implemented")
    print(f"enabled {enabled}; implemented={implemented}; total={len(catalog['actions'])}")


if __name__ == "__main__":
    main()
