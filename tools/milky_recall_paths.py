"""Measure the two recall paths separately: own message vs someone else's.

The bridge resolves a Milky `message_seq` to a message ID through the store, so what matters is
whether the message is in it. A message this account sent is only stored when the network
profile reports self messages; a message someone else sent is always stored because it arrived
as an event. Measuring them together hides that difference behind one "-404".

    python tools/milky_recall_paths.py --port 5755 --owner 3905548034 --sender 2208948865 \
        --group 2169010946
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from pathlib import Path


def call(port: int, api: str, body: dict, timeout: float = 25.0) -> dict:
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/MILKY/api/{api}", data=payload,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return {"api": api, "request": body, "error": f"{type(exc).__name__}: {exc}"}
    record = {"api": api, "request": body, "raw": raw}
    try:
        record["reply"] = json.loads(raw)
    except ValueError:
        record["reply"] = None
    return record


def code(record: dict) -> object:
    reply = record.get("reply")
    return reply.get("retcode") if isinstance(reply, dict) else record.get("error")


def data_of(record: dict) -> dict:
    reply = record.get("reply")
    return reply["data"] if isinstance(reply, dict) and isinstance(reply.get("data"), dict) else {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=5755)
    ap.add_argument("--owner", default="3905548034", help="account that recalls (group owner)")
    ap.add_argument("--sender", default="2208948865", help="account that sends the message")
    ap.add_argument("--group", type=int, default=2169010946)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    out = Path(args.out) if args.out else Path(r"D:\projects\milky实现") / "实测" / \
        time.strftime("%Y-%m-%dT%H%M%S") / "writes"
    out.mkdir(parents=True, exist_ok=True)
    steps: list[dict] = []

    def note(record: dict, label: str) -> dict:
        steps.append({"label": label, "api": record["api"], "retcode": code(record)})
        print(f"  {label:52} retcode={code(record)}")
        (out / f"{len(steps):02d}-{record['api']}.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
        return record

    text = f"cross-account recall probe {time.strftime('%H:%M:%S')}"
    sent = note(call(args.port, "send_group_message", {
        "account": args.sender, "group_id": args.group,
        "message": [{"type": "text", "data": {"text": text}}]}), "sender posts a group message")
    seq = data_of(sent).get("message_seq")
    print(f"       sender seq={seq}")

    if not isinstance(seq, int) or seq <= 0:
        return 1
    # Give the group push time to reach the owner and be stored.
    for _ in range(10):
        time.sleep(1)
        found = note(call(args.port, "get_message", {
            "account": args.owner, "message_scene": "group", "peer_id": args.group,
            "message_seq": seq}), "owner looks the message up by seq")
        if data_of(found).get("message"):
            break

    message = data_of(found).get("message") or {}
    steps.append({"label": "owner sees a real message", "ok": bool(message),
                  "sender": (message.get("sender") or {}).get("user_id")})
    print(f"       stored? {bool(message)}  sender={(message.get('sender') or {}).get('user_id')}")

    if message:
        recalled = note(call(args.port, "recall_group_message", {
            "account": args.owner, "group_id": args.group, "message_seq": seq}),
            "owner (group owner) recalls someone else's message")
        after = note(call(args.port, "get_message", {
            "account": args.owner, "message_scene": "group", "peer_id": args.group,
            "message_seq": seq}), "owner looks it up after the recall")
        gone = not (data_of(after).get("message"))
        steps.append({"label": "message gone after recall", "ok": gone})
        print(f"       gone after recall? {gone}  (recall retcode={code(recalled)})")

    (out / "summary.json").write_text(json.dumps({"steps": steps}, ensure_ascii=False, indent=1),
                                      encoding="utf-8")
    print(f"\nevidence: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
