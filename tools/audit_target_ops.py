"""Find actions that can target a private chat but are forced through the group_id check.

The dispatcher has one shared pre-check: every op not explicitly exempt does

    id(p.group_id, 'group_id')

which throws when the field is absent. That is correct for the group-scoped ops and wrong for anything
that can also address a private chat -- `send_private_forward_msg` was rejected with "group_id must be a
positive canonical decimal ID" even though its handler resolves `user_id` into a private destination.

This lists every op, whether it is exempt, and whether its handler mentions a private-side parameter
(`user_id`, `private`, `friend`). The combination "not exempt" + "mentions private" is the bug class to
look at, so the report puts those first.

Run: python tools/audit_target_ops.py
"""
from __future__ import annotations

import pathlib
import re

SOURCE = pathlib.Path(__file__).resolve().parents[1] / "src/qsick/headless_actions.cjs"


def main() -> int:
    text = SOURCE.read_text(encoding="utf-8")

    # 1. The exemption set, as written in the source.
    match = re.search(r"const NO_GROUP_ID_OPS = new Set\(\[(.*?)\]\);", text, re.S)
    exempt = set(re.findall(r"'([a-z0-9_.]+)'", match.group(1))) if match else set()

    # 2. Every op name the dispatcher knows: aliases (targets) plus requirement keys.
    targets = set(re.findall(r"^\s*[a-zA-Z_][\w]*:\s*'([a-z0-9_.]+)'", text, re.M))
    requires = set(re.findall(r"^\s*'([a-z0-9_.]+)':\s*\[", text, re.M))
    ops = targets | requires

    # 3. Split the handler source per `if (op === '...')` block so each one can be inspected alone.
    blocks: dict[str, str] = {}
    for hit in re.finditer(r"if \(op === '([a-z0-9_.]+)'\)\s*\{", text):
        start = hit.end()
        depth = 1
        cursor = start
        while cursor < len(text) and depth:
            if text[cursor] == "{":
                depth += 1
            elif text[cursor] == "}":
                depth -= 1
            cursor += 1
        blocks[hit.group(1)] = text[start:cursor]

    PRIVATE_HINTS = ("p.user_id", "user_id", "private", "'friend'", "friend.")

    # The check only bites ops that reach it. Anything handled and returned *before* the line that
    # computes `groupId` never sees the requirement, so a mention of `user_id` there is not a bug --
    # `user.status` and `friend.info` both sit earlier and are fine.
    check_line = text[: text.find("const groupId = NO_GROUP_ID_OPS")].count("\n") + 1
    block_line = {
        match.group(1): text[: match.start()].count("\n") + 1
        for match in re.finditer(r"if \(op === '([a-z0-9_.]+)'\)\s*\{", text)
    }

    def mentions_private(op: str) -> bool:
        body = blocks.get(op, "")
        return any(hint in body for hint in PRIVATE_HINTS)

    suspicious: list[tuple[str, str]] = []
    before_check: list[str] = []
    exempt_with_private: list[str] = []
    group_only: list[str] = []

    for op in sorted(ops):
        is_exempt = op in exempt
        private = mentions_private(op)
        line = block_line.get(op)
        reaches_check = line is None or line > check_line
        if not is_exempt and private and reaches_check:
            suspicious.append((op, f"未豁免且在校验之后处理（第 {line} 行 > 第 {check_line} 行）"))
        elif not is_exempt and private and not reaches_check:
            before_check.append(f"{op}(第 {line} 行)")
        elif is_exempt and private:
            exempt_with_private.append(op)
        elif not is_exempt and not private:
            group_only.append(op)

    print(f"  共 {len(ops)} 个 op，豁免 group_id 校验的 {len(exempt)} 个")
    print(f"  group_id 校验在第 {check_line} 行")
    print()
    print("=== 需要检查（未豁免、涉及私聊、且能走到校验）===")
    if suspicious:
        for op, why in suspicious:
            print(f"  {op:34s} {why}")
    else:
        print("  无")

    print()
    print("=== 在校验之前就返回（不受影响）===")
    print("  " + (", ".join(before_check) if before_check else "无"))

    print()
    print("=== 已豁免且涉及私聊（正常）===")
    print("  " + (", ".join(exempt_with_private) if exempt_with_private else "无"))

    print()
    print("=== 未豁免且只涉及群聊（正常）===")
    print(f"  共 {len(group_only)} 个：" + ", ".join(group_only[:24]) + (" …" if len(group_only) > 24 else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
