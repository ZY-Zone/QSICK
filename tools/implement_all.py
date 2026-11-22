"""Batch-complete the action surface: every planned entry becomes a payload adapter.

    uv run python tools/implement_all.py

What "implemented" means here: the core validates the OneBot parameters and calls a
payload op with the same name and parameter object. The wire behaviour for that op is
still the payload's job; ``docs/payload-ops.md`` lists what remains.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ACTIONS = ROOT / "src" / "qsick" / "onebot" / "actions"
CATALOG = ACTIONS / "actions.json"

TEMPLATE = '''"""OneBot adapter for `{name}`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `{name}` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class {class_name}(PayloadAction):
    op = "{name}"
'''


def main() -> None:
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    converted = 0
    for item in catalog["actions"]:
        if item.get("status") == "implemented":
            item.setdefault("wire", "core" if not item.get("op") else "pending")
            continue
        path = ACTIONS / f"{item['module']}.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            TEMPLATE.format(name=item["name"], class_name=item["class"]),
            encoding="utf-8",
        )
        item["status"] = "implemented"
        item["op"] = item["name"]
        item["wire"] = "pending"
        converted += 1

    CATALOG.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    implemented = sum(1 for item in catalog["actions"] if item["status"] == "implemented")
    pending = sum(1 for item in catalog["actions"] if item.get("wire") == "pending")
    print(f"converted={converted} implemented={implemented} wire_pending={pending}")


if __name__ == "__main__":
    main()
