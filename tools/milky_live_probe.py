"""Run the Milky read-only APIs against a live QSICK listener and record what came back.

Purpose: the capability CSV in `milky实现` currently says "是 / 部分 / 否" from *offline* tests.
This turns those into measured statements by calling the real endpoint and keeping the raw
request/response, so a later reader can check the claim instead of trusting it.

What it will not do: send anything that changes state. Only `get_*` APIs are called, plus the
few "否" ones whose failure is safe to observe. Write actions live in the layered plan and need
a disposable group; see `docs/milky-live-test.md`.

    python tools/milky_live_probe.py --port 5755 --account 3905548034 \
        --group 2169010946 --peer 1927676487

Evidence goes to `milky实现/实测/<timestamp>/`: one JSON per API plus `summary.json`.
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def call(port: int, api: str, body: dict, timeout: float = 20.0) -> dict:
    """POST one API and return a record of what happened, never raising on a bad reply."""
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/MILKY/api/{api}", data=payload,
        headers={"Content-Type": "application/json"}, method="POST")
    started = time.time()
    record = {"api": api, "request": body, "at": int(started)}
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
            record["http"] = response.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        record["http"] = exc.code
    except Exception as exc:  # noqa: BLE001 - a timeout is a result, not a crash
        record.update({"http": None, "error": f"{type(exc).__name__}: {exc}",
                       "ms": int((time.time() - started) * 1000)})
        return record
    record["ms"] = int((time.time() - started) * 1000)
    record["raw"] = raw
    try:
        record["reply"] = json.loads(raw)
    except ValueError:
        record["reply"] = None
    return record


def verdict(record: dict) -> str:
    """Turn one record into a short measured status.

    `ok`           retcode 0 and a data object
    `retcode:N`    the endpoint answered with a non-zero Milky retcode
    `http:N`       the request never reached the API layer
    `timeout`      no answer within the budget -- distinct from a refusal, and worse
    """
    if record.get("http") is None:
        return "timeout" if "timed out" in str(record.get("error", "")).lower() else "transport"
    reply = record.get("reply")
    if not isinstance(reply, dict):
        return f"http:{record['http']}"
    code = reply.get("retcode")
    if code == 0:
        return "ok"
    return f"retcode:{code}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5755)
    parser.add_argument("--account", default="3905548034")
    parser.add_argument("--group", type=int, default=2169010946, help="group this account owns")
    parser.add_argument("--member-group", type=int, default=521860577, help="group where it is a member")
    parser.add_argument("--peer", type=int, default=1927676487)
    parser.add_argument("--other", type=int, default=2208948865)
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    stamp = time.strftime("%Y-%m-%dT%H%M%S")
    out = Path(args.out) if args.out else Path(r"D:\projects\milky实现") / "实测" / stamp
    out.mkdir(parents=True, exist_ok=True)

    account = args.account
    base = {"account": account}
    results: list[dict] = []

    def run(api: str, body: dict, timeout: float = 20.0) -> dict:
        record = call(args.port, api, {**body, **base}, timeout)
        record["verdict"] = verdict(record)
        results.append(record)
        return record

    # ---- discovery: a real (seq, message_id, file_id) so the message and file APIs get real input
    history = run("get_history_messages", {"message_scene": "group", "peer_id": args.group, "limit": 5})
    seq = 0
    for row in ((history.get("reply") or {}).get("data") or {}).get("messages") or []:
        if isinstance(row, dict) and str(row.get("message_seq", "")).isdigit():
            seq = int(row["message_seq"])
            break

    files = run("get_group_files", {"group_id": args.group, "parent_folder_id": "/"})
    file_id = ""
    for row in ((files.get("reply") or {}).get("data") or {}).get("files") or []:
        if isinstance(row, dict) and row.get("file_id"):
            file_id = str(row["file_id"])
            break

    # ---- the rest of the read-only surface
    run("get_login_info", {})
    run("get_impl_info", {})
    run("get_user_profile", {"user_id": args.peer})
    run("get_friend_list", {"no_cache": True})
    run("get_friend_info", {"user_id": args.peer, "no_cache": True})
    run("get_group_list", {"no_cache": True})
    run("get_group_info", {"group_id": args.group, "no_cache": True})
    run("get_group_info", {"group_id": args.member_group, "no_cache": True})
    run("get_group_member_list", {"group_id": args.group, "no_cache": True})
    run("get_group_member_info", {"group_id": args.group, "user_id": args.peer, "no_cache": True})
    run("get_group_essence_messages", {"group_id": args.group, "page_index": 0, "page_size": 10})
    if seq:
        run("get_message", {"message_scene": "group", "peer_id": args.group, "message_seq": seq})
        run("get_history_messages", {"message_scene": "group", "peer_id": args.group,
                                     "start_message_seq": seq, "limit": 5})
    if file_id:
        run("get_group_file_download_url", {"group_id": args.group, "file_id": file_id})

    # ---- "否" entries: the useful measurement is that they fail cleanly, not that they fail
    run("get_peer_pins", {})
    run("get_custom_face_url_list", {})
    run("get_cookies", {"domain": "qun.qq.com"})
    run("get_csrf_token", {})
    run("get_friend_requests", {"limit": 10, "is_filtered": False})
    run("get_group_announcements", {"group_id": args.group})
    run("get_group_notifications", {"start_notification_seq": 0, "is_filtered": False, "limit": 10})
    run("get_resource_temp_url", {"resource_id": file_id or "none"})

    # Two calls of the same API are both worth keeping -- a cursor taken from the tail answers
    # differently from the discovery call, and overwriting the first one hides that.
    seen: dict[str, int] = {}
    for record in results:
        name = record["api"]
        seen[name] = seen.get(name, 0) + 1
        suffix = "" if seen[name] == 1 else f"__{seen[name]}"
        (out / f"{name}{suffix}.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    summary = {
        "at": stamp, "port": args.port, "account": account,
        "context": {"group": args.group, "member_group": args.member_group,
                    "peer": args.peer, "other": args.other,
                    "discovered_seq": seq, "discovered_file_id": file_id},
        "results": [{k: r.get(k) for k in ("api", "verdict", "http", "ms", "request")} for r in results],
    }
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"evidence: {out}")
    width = max(len(r["api"]) for r in results)
    for record in results:
        print(f"  {record['api']:<{width}}  {record['verdict']:<12} {record.get('ms', '')}ms")
    ok = sum(1 for r in results if r["verdict"] == "ok")
    print(f"\nok={ok} other={len(results) - ok} total={len(results)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
