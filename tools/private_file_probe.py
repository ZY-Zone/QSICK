"""Probe the private-file download command against a live session, and keep the raw answer.

Report 15 documents OIDB `0xE37_800` as the private-file command (group files use `0x6D6_2`) and
lists its full protocol as not covered. Two things are therefore unknown: whether the command is
routed on this build at all, and what the response body looks like. This script answers the first
and records the evidence for the second -- it prints the raw hex of whatever came back.

    python tools/private_file_probe.py --port 5755 --account 3905548034 --peer 1927676487

`--file-id` / `--file-hash` come from a real received file message (the payload caches the element's
`file_id` and `md5`). Without one the probe still runs: a routed command answers with an error body,
which already tells us whether the number is right.
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from pathlib import Path


def action(port: int, name: str, params: dict, timeout: float = 40.0) -> dict:
    payload = json.dumps({"action": name, "params": params}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/", data=payload,
        headers={"Content-Type": "application/json"}, method="POST")
    started = time.time()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}", "ms": int((time.time() - started) * 1000)}
    record = {"ms": int((time.time() - started) * 1000), "raw": raw}
    try:
        record["reply"] = json.loads(raw)
    except ValueError:
        record["reply"] = None
    return record


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5755)
    parser.add_argument("--account", default="3905548034")
    parser.add_argument("--peer", type=int, required=True, help="the friend's QQ number")
    parser.add_argument("--file-id", default="", help="file_id from a received file message")
    parser.add_argument("--file-hash", default="", help="md5/triSHA1 from the same element")
    parser.add_argument("--self-send", action="store_true", help="the file was sent by this account")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    out = Path(args.out) if args.out else Path(__file__).resolve().parents[1] / "temp" / "private-file-probe"
    out.mkdir(parents=True, exist_ok=True)

    params = {"account": args.account, "user_id": args.peer, "file_id": args.file_id,
              "file_hash": args.file_hash, "is_self_send": bool(args.self_send)}
    record = action(args.port, "get_private_file_url", params)
    (out / "probe.json").write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")

    reply = record.get("reply") or {}
    print(f"action=get_private_file_url  retcode={reply.get('retcode')}  ms={record.get('ms')}")
    print(f"message: {str(reply.get('message') or record.get('error') or '')[:400]}")
    details = reply.get("details") or (reply.get("data") or {})
    if isinstance(details, dict) and details.get("attempts"):
        print("\nattempts:")
        for attempt in details["attempts"]:
            print(f"  {attempt.get('command')}")
            print(f"     routed={attempt.get('routed')} status={attempt.get('status')} "
                  f"msg={str(attempt.get('message') or '')[:120]}")
            if attempt.get("raw_hex"):
                hex_text = attempt["raw_hex"]
                print(f"     raw[{len(hex_text) // 2}B]: {hex_text[:220]}")
            for field in (attempt.get("fields") or [])[:12]:
                print(f"       field {field.get('field')} wire {field.get('wire')} "
                      f"{field.get('value', field.get('text', field.get('hex', '')))}")
    print(f"\nevidence: {out / 'probe.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
