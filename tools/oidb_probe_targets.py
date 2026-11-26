"""Probe OIDB command numbers for the four capabilities that have no kernel service method.

Nothing is interpreted here: for each candidate the script records which name style was accepted,
the error code QQ returned and the first bytes of the body. A command that answers 0 is usable; 253
means "routed but the packet did not decode"; a rejection means the number or the style is wrong.

    python tools/oidb_probe_targets.py --port 5754 --account 3905548034
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

#: Candidates worth trying, with the reason each number is on the list.
#:
#: These are hypotheses, not facts: the point of the run is to find out which ones QQ routes. The
#: four capabilities have no `NodeIKernel*` method, so an OIDB command is the only route -- but the
#: numbers below come from other clients' published tables, not from this build.
CANDIDATES: list[tuple[str, int, int, str]] = [
    # 设置群成员专属头衔
    ("special_title", 0x8FC, 2, "OidbSvc.0x8fc_2 -- set member special title (common table)"),
    ("special_title", 0x8FC, 3, "OidbSvc.0x8fc_3 -- alternate sub"),
    ("special_title", 0x8FC, 1, "OidbSvc.0x8fc_1 -- alternate sub"),
    ("special_title", 0x89A, 1, "OidbSvc.0x89a_1 -- alternate command family"),
    # 发送群消息表情回应
    ("reaction", 0x9082, 1, "OidbSvcTrpcTcp.0x9082_1 -- set emoji like"),
    ("reaction", 0x9082, 0, "OidbSvc.0x9082_0 -- alternate sub"),
    ("reaction", 0x9083, 0, "OidbSvc.0x9083_0 -- get emoji like list family"),
    # 群/好友戳一戳
    ("poke", 0xED3, 1, "OidbSvc.0xed3_1 -- send poke"),
    ("poke", 0xED3, 0, "OidbSvc.0xed3_0 -- alternate sub"),
    ("poke", 0xE0A, 1, "OidbSvc.0xe0a_1 -- alternate poke family"),
    # 入群 / 邀请请求的同意与拒绝
    ("group_request", 0x10C8, 1, "OidbSvc.0x10c8_1 -- operate group request"),
    ("group_request", 0x10C8, 0, "OidbSvc.0x10c8_0 -- alternate sub"),
    ("group_request", 0xE9E, 1, "OidbSvc.0xe9e_1 -- alternate request family"),
]


def action(port: int, name: str, params: dict, account: str, timeout: float = 60.0) -> dict:
    payload = json.dumps({"account": account, **params}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/actions/{name}", data=payload,
        headers={"Content-Type": "application/json"}, method="POST")
    started = time.time()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}", "ms": int((time.time() - started) * 1000)}
    record = {"ms": int((time.time() - started) * 1000)}
    try:
        record.update(json.loads(raw))
    except ValueError:
        record["raw"] = raw[:400]
    return record


def probe(port: int, account: str, command: int, sub: int, style: str, body_hex: str) -> dict:
    params: dict = {"command": command, "sub_command": sub, "body_hex": body_hex}
    if style:
        params["style"] = style
    reply = action(port, "oidb_call", params, account)
    data = reply.get("data") or {}
    return {
        "style": style or "default(plain)",
        "retcode": reply.get("retcode"),
        "resolved": data.get("resolved"),
        "error_code": data.get("error_code"),
        "error_message": str(data.get("error_message") or reply.get("message") or "")[:200],
        "body_size": data.get("body_size"),
        "body_hex": str(data.get("body_hex") or "")[:160],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5754)
    parser.add_argument("--account", default="3905548034")
    parser.add_argument("--out", default="")
    parser.add_argument("--body-hex", default="", help="request body to send with every probe")
    args = parser.parse_args()

    out = Path(args.out) if args.out else Path(__file__).resolve().parents[1] / "temp" / "oidb-probe"
    out.mkdir(parents=True, exist_ok=True)

    report: dict[str, list] = {}
    for group, command, sub, why in CANDIDATES:
        for style in ("", "trpc"):
            label = f"0x{command:X}_{sub}"
            result = probe(args.port, args.account, command, sub, style, args.body_hex)
            result["why"] = why
            report.setdefault(group, []).append({"command": label, **result})
            verdict = "ROUTED" if result["error_code"] == 0 else f"error {result['error_code']}"
            print(f"{group:14} {label:12} {result['style']:16} "
                  f"retcode={result['retcode']!s:5} {verdict:10} {result['error_message'][:70]}")

    (out / "probe.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nevidence: {out / 'probe.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
