"""Generate docs/payload-ops.md from the action catalog.

    uv run python tools/gen_payload_ops.py
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "src" / "qsick" / "onebot" / "actions" / "actions.json"
OUT = ROOT / "docs" / "payload-ops.md"


def fmt_params(params: list[dict]) -> str:
    if not params:
        return "—"
    parts = []
    for item in params:
        name = item.get("name", "?")
        kind = item.get("kind", "str")
        required = "!" if item.get("required") else "?"
        parts.append(f"{name}:{kind}{required}")
    return ", ".join(parts)


def main() -> None:
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    pending = [item for item in catalog["actions"] if item.get("wire") == "pending"]
    core = [item for item in catalog["actions"] if item.get("wire") == "core"]

    lines = [
        "# Payload op checklist",
        "",
        "Action names double as payload op names, and parameters are forwarded unchanged.",
        "An op marked *pending* is fully wired in the core (validation, retcodes, WebUI) but",
        "still needs its QQ-side implementation in the native payload.",
        "",
        f"- core-backed actions: **{len(core)}**",
        f"- payload ops pending: **{len(pending)}**",
        "",
        "## Pending ops",
        "",
        "| Category | Op | Params |",
        "| --- | --- | --- |",
    ]
    for item in sorted(pending, key=lambda entry: (entry["category"], entry["name"])):
        lines.append(f"| {item['category']} | `{item['name']}` | {fmt_params(item.get('params', []))} |")

    lines += [
        "",
        "## Core-backed actions (no payload work)",
        "",
        ", ".join(f"`{item['name']}`" for item in sorted(core, key=lambda entry: entry["name"])),
        "",
        "## Contract",
        "",
        "1. The core validates parameters from `actions.json` and calls `op(params)`.",
        "2. The payload replies with OneBot-shaped `data` for that action (the core forwards",
        "   it unchanged), or a non-zero status with a readable message.",
        "3. A missing op must fail loudly (`status != 0`); silent empty success is a bug.",
        "",
    ]
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {OUT} ({len(pending)} pending ops)")


if __name__ == "__main__":
    main()
