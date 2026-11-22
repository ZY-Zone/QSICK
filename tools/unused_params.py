"""Find action parameters that are declared but never read by the handler.

This is the shape of two real defects found by hand: `reverse_order` was in the OneBot contract, accepted by
the signature, and dropped before it reached the transport; and that class of mistake is invisible at runtime
because a wrong answer is still a valid answer.

The check is textual and deliberately loose -- it reports candidates, not verdicts. A parameter can be
legitimately unused (accepted for compatibility and ignored on purpose), so each hit needs reading.

Usage: python tools/unused_params.py [--limit N]
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "src/qsick/onebot/actions/actions.json"
MODULES = ROOT / "src/qsick/onebot/actions"

#: Parameters that are allowed to be accepted and ignored, with the reason.
ALLOWED = {
    "echo": "the OneBot correlation id; handled by the registry, never by a handler",
    "self_id": "unused by request handlers",
}


def body_of(module: str) -> str | None:
    """The module source with its `def` lines removed.

    The signature mentions every parameter name by definition, so including it would hide exactly the cases
    this looks for.
    """
    path = MODULES / f"{module}.py"
    if not path.is_file():
        return None
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(line for line in lines if not line.lstrip().startswith(("async def ", "def ")))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=60)
    args = parser.parse_args()

    raw = json.loads(CATALOG.read_text(encoding="utf-8"))
    entries = raw["actions"] if isinstance(raw, dict) else raw

    hits: list[tuple[str, str, str]] = []
    missing = 0
    for entry in entries:
        module = str(entry.get("module") or "")
        if not module:
            continue
        body = body_of(module)
        if body is None:
            missing += 1
            continue
        name = str(entry.get("name") or module)
        for param in entry.get("params") or []:
            key = str(param.get("name") or "")
            if not key or key in ALLOWED:
                continue
            if re.search(rf"\b{re.escape(key)}\b", body):
                continue
            hits.append((name, key, module))

    print(f"  动作 {len(entries)} 个；缺模块 {missing} 个")
    print(f"  声明了但处理函数里没出现的参数：{len(hits)} 处")
    for name, key, module in hits[: args.limit]:
        print(f"    {name:<34} {key:<18} {module}")
    if len(hits) > args.limit:
        print(f"    …… 还有 {len(hits) - args.limit} 处")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
