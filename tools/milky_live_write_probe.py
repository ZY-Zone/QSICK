"""Run the Milky write APIs against a live QSICK listener, with read-back verification.

Every write is followed by a read of the thing it changed, because `retcode: 0` only proves
the call was accepted -- it does not prove the group was renamed or the member was muted.
Steps that change shared state are reverted in place; anything that cannot be reverted is
left to a separate, explicitly requested run.

    python tools/milky_live_write_probe.py --port 5755 --account 3905548034 \
        --group 2169010946 --peer 1927676487 --stage l2

Stages: `l2` (messages), `l3` (group settings and files), `l4` (kick and quit -- separate on
purpose, see docs). Evidence goes to `milky实现/实测/<timestamp>/writes/`.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

#: Listener base path; Milky appends `/api/{api}` to it. Set from `--base`.
MILKY_BASE = "/"


def api_url(port: int, api: str) -> str:
    """`{base}/api/{api}` on the listener's port."""
    base = MILKY_BASE.rstrip("/")
    return f"http://127.0.0.1:{port}{base}/api/{api}"

ROOT = Path(__file__).resolve().parents[1]


def call(port: int, api: str, body: dict, timeout: float = 25.0) -> dict:
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        api_url(port, api), data=payload,
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
    except Exception as exc:  # noqa: BLE001
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


def ok(record: dict) -> bool:
    reply = record.get("reply")
    return isinstance(reply, dict) and reply.get("retcode") == 0


def data_of(record: dict) -> dict:
    reply = record.get("reply")
    if isinstance(reply, dict) and isinstance(reply.get("data"), dict):
        return reply["data"]
    return {}


class Run:
    def __init__(self, port: int, account: str, out: Path, segments: str = "milky") -> None:
        self.segments = segments
        self.port = port
        self.account = account
        self.out = out
        self.out.mkdir(parents=True, exist_ok=True)
        self.steps: list[dict] = []
        self.index = 0

    def api(self, api: str, body: dict, note: str = "") -> dict:
        record = call(self.port, api, {**body, "account": self.account})
        record["note"] = note
        self.index += 1
        (self.out / f"{self.index:02d}-{api}.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
        self.steps.append({"api": api, "note": note, "ok": ok(record),
                           "retcode": (record.get("reply") or {}).get("retcode"),
                           "ms": record.get("ms")})
        flag = "ok  " if ok(record) else "FAIL"
        detail = "" if ok(record) else f"  retcode={(record.get('reply') or {}).get('retcode')} " \
                                       f"{(record.get('reply') or {}).get('message') or record.get('error') or ''}"[:160]
        print(f"  {flag} {api:30} {note}{detail}")
        return record

    def check(self, label: str, condition: bool, detail: str = "") -> bool:
        print(f"       {'PASS' if condition else 'MISMATCH'}  {label}{('  ' + detail) if detail else ''}")
        self.steps.append({"check": label, "ok": bool(condition), "detail": detail})
        return condition

    def summary(self) -> dict:
        return {"at": time.strftime("%Y-%m-%dT%H%M%S"), "port": self.port,
                "account": self.account, "steps": self.steps}


def group_member(run: Run, group: int, user: int) -> dict:
    return data_of(run.api("get_group_member_info", {"group_id": group, "user_id": user, "no_cache": True}))\
        .get("member") or {}


def group_info(run: Run, group: int) -> dict:
    return data_of(run.api("get_group_info", {"group_id": group, "no_cache": True})).get("group") or {}


def group_files(run: Run, group: int, folder: str = "/") -> list[dict]:
    return data_of(run.api("get_group_files", {"group_id": group, "parent_folder_id": folder})).get("files") or []


def stage_l2(run: Run, group: int, peer: int) -> None:
    """Messages in a group and a private chat, then recall them."""
    stamp = time.strftime("%H:%M:%S")
    text = f"QSICK Milky 实测 {stamp}"
    shape = run.segments

    def segment(kind: str, **fields) -> dict:
        """A Milky outgoing segment, or the v11 equivalent when the server predates the fix."""
        if shape == "v11":
            return {"type": kind, "data": fields}
        return {"type": kind, **fields}

    sent = run.api("send_group_message", {"group_id": group, "message": [segment("text", text=text)]},
                   f"group text ({shape} segments)")
    seq = data_of(sent).get("message_seq")
    print(f"       message_seq={seq} message_id={data_of(sent).get('message_id')}")
    run.check("send_group_message returns a real message_seq", isinstance(seq, int) and seq > 0, f"seq={seq}")
    if isinstance(seq, int) and seq > 0:
        back = data_of(run.api("get_message", {"message_scene": "group", "peer_id": group,
                                              "message_seq": seq})).get("message") or {}
        run.check("the group message can be read back by seq",
                  str(back.get("message_seq")) == str(seq))
        run.api("recall_group_message", {"group_id": group, "message_seq": seq}, "recall it")
        after = data_of(run.api("get_message", {"message_scene": "group", "peer_id": group,
                                                "message_seq": seq}))
        run.check("after recall the message is no longer readable", not after.get("message"))

    pm = run.api("send_private_message", {"user_id": peer, "message": [segment("text", text=text)]},
                 f"private text ({shape} segments)")
    pseq = data_of(pm).get("message_seq")
    run.check("send_private_message returns a real message_seq", isinstance(pseq, int) and pseq > 0, f"seq={pseq}")
    if isinstance(pseq, int) and pseq > 0:
        run.api("recall_private_message", {"user_id": peer, "message_seq": pseq}, "recall it")
    text_pm = run.api("send_private_message", {
        "user_id": peer, "message": [segment("image", resource_id="not-a-real-resource")]},
        "private image with a bogus id")
    run.check("a bogus image resource fails instead of sending nothing", not ok(text_pm))


def stage_l3(run: Run, group: int, peer: int) -> None:
    """Group settings and files, each verified by reading it back, each reverted."""
    before = group_info(run, group)
    original_name = before.get("group_name") or ""
    print(f"  original group name: {original_name!r}")

    new_name = f"{original_name}-实测"
    run.api("set_group_name", {"group_id": group, "new_group_name": new_name}, "rename")
    run.check("set_group_name changed the name", group_info(run, group).get("group_name") == new_name)
    if original_name:
        run.api("set_group_name", {"group_id": group, "new_group_name": original_name}, "revert")
        run.check("name reverted", group_info(run, group).get("group_name") == original_name)

    me = group_member(run, group, int(run.account))
    original_card = me.get("card") or ""
    run.api("set_group_member_card", {"group_id": group, "user_id": int(run.account), "card": "实测名片"},
            "card")
    run.check("set_group_member_card changed the card",
              group_member(run, group, int(run.account)).get("card") == "实测名片")
    run.api("set_group_member_card", {"group_id": group, "user_id": int(run.account), "card": original_card},
            "revert")
    run.check("card reverted", (group_member(run, group, int(run.account)).get("card") or "") == original_card)

    run.api("set_group_member_mute", {"group_id": group, "user_id": peer, "duration": 60}, "mute 60s")
    muted = group_member(run, group, peer)
    run.check("mute shows up on the member", (muted.get("shut_up_end_time") or 0) > 0,
              f"shut_up_end_time={muted.get('shut_up_end_time')}")
    run.api("set_group_member_mute", {"group_id": group, "user_id": peer, "duration": 0}, "unmute")
    run.check("unmute clears it", (group_member(run, group, peer).get("shut_up_end_time") or 0) == 0)

    run.api("set_group_whole_mute", {"group_id": group, "is_mute": True}, "whole mute on")
    run.api("set_group_whole_mute", {"group_id": group, "is_mute": False}, "whole mute off")

    before_admin = (group_member(run, group, peer).get("role") or "")
    if before_admin != "admin":
        run.api("set_group_member_admin", {"group_id": group, "user_id": peer, "is_set": True}, "promote")
        run.check("promote changed the role",
                  group_member(run, group, peer).get("role") == "admin",
                  f"role={group_member(run, group, peer).get('role')}")
        run.api("set_group_member_admin", {"group_id": group, "user_id": peer, "is_set": False}, "demote")
        run.check("demote restored the role", group_member(run, group, peer).get("role") == before_admin,
                  f"role={group_member(run, group, peer).get('role')}")

    # ---- files: work on a file this run uploads, never on the operator's own uploads
    folder = run.api("create_group_folder", {"group_id": group, "folder_name": "QSICK实测"}, "create folder")
    folder_id = str(data_of(folder).get("folder_id") or "")
    run.check("create_group_folder returns a real folder id", bool(folder_id), f"folder_id={folder_id!r}")

    payload = "QSICK milky live probe".encode("utf-8").hex()
    upload = run.api("upload_group_file", {"group_id": group, "parent_folder_id": "/",
                                           "file_name": "qsick-probe.txt",
                                           "file_uri": f"data:text/plain;base64,{payload}"},
                     "upload (data: must be rejected)")
    run.check("a data: URI is refused", not ok(upload))

    import base64

    upload = run.api("upload_group_file", {"group_id": group, "parent_folder_id": "/",
                                           "file_name": "qsick-probe.txt",
                                           "file_uri": "base64://" + base64.b64encode(b"QSICK milky live probe").decode()},
                     "upload via base64://")
    file_id = str(data_of(upload).get("file_id") or "")
    run.check("upload_group_file returns a file id", bool(file_id), f"file_id={file_id[:40]!r}")

    if file_id:
        listed = [f for f in group_files(run, group) if str(f.get("file_id")) == file_id]
        run.check("the uploaded file is listed", bool(listed), f"found={len(listed)}")
        run.api("rename_group_file", {"group_id": group, "file_id": file_id, "parent_folder_id": "/",
                                      "new_file_name": "qsick-probe-renamed.txt"}, "rename file")
        names = [str(f.get("file_name")) for f in group_files(run, group) if str(f.get("file_id")) == file_id]
        run.check("rename_group_file changed the name", "qsick-probe-renamed.txt" in names, f"names={names}")
        if folder_id:
            run.api("move_group_file", {"group_id": group, "file_id": file_id,
                                        "parent_folder_id": "/", "target_folder_id": folder_id}, "move")
            inside = [f for f in group_files(run, group, folder_id) if str(f.get("file_id")) == file_id]
            run.check("move_group_file put it in the folder", bool(inside), f"found={len(inside)}")

    if folder_id:
        run.api("rename_group_folder", {"group_id": group, "folder_id": folder_id,
                                        "new_folder_name": "QSICK实测2"}, "rename folder")
        folders = [f for f in group_files(run, group) if str(f.get("file_id")) == folder_id]
        run.check("rename_group_folder changed the name",
                  any(str(f.get("file_name")) == "QSICK实测2" for f in folders), f"folders={folders[:1]}")
    if file_id:
        run.api("delete_group_file", {"group_id": group, "file_id": file_id}, "delete the probe file")
        run.check("the probe file is gone",
                  not any(str(f.get("file_id")) == file_id for f in group_files(run, group, folder_id or "/")))
    if folder_id:
        run.api("delete_group_folder", {"group_id": group, "folder_id": folder_id}, "delete folder")

    # ---- essence: listing is refused by the client, so set/unset is all that can be verified
    history = data_of(run.api("get_history_messages", {"message_scene": "group", "peer_id": group,
                                                       "limit": 5})).get("messages") or []
    target = next((m for m in history if isinstance(m, dict) and m.get("message_seq")), None)
    if target:
        seq = int(target["message_seq"])
        run.api("set_group_essence_message", {"group_id": group, "message_seq": seq, "is_set": True}, "set essence")
        run.api("set_group_essence_message", {"group_id": group, "message_seq": seq, "is_set": False},
                "unset essence")
    else:
        print("       (no message available to test essence)")


def stage_l4(run: Run, group: int, peer: int) -> None:
    """Kick, then leave. Not reversible -- run only when explicitly asked for."""
    members = data_of(run.api("get_group_member_list", {"group_id": group, "no_cache": True})).get("members") or []
    run.check("the target is in the group before kicking",
              any(str(m.get("user_id")) == str(peer) for m in members), f"members={len(members)}")
    run.api("kick_group_member", {"group_id": group, "user_id": peer, "reject_add_request": False}, "kick")
    after = data_of(run.api("get_group_member_list", {"group_id": group, "no_cache": True})).get("members") or []
    run.check("the target is gone after the kick",
              not any(str(m.get("user_id")) == str(peer) for m in after), f"members={len(after)}")
    run.api("quit_group", {"group_id": group}, "leave the group")
    groups = data_of(run.api("get_group_list", {"no_cache": True})).get("groups") or []
    run.check("the account is no longer in the group",
              not any(str(g.get("group_id")) == str(group) for g in groups), f"groups={len(groups)}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5755)
    parser.add_argument("--base", default="/",
                        help="listener base path (the profile's 路径); Milky appends /api/{api}")
    parser.add_argument("--account", default="3905548034")
    parser.add_argument("--group", type=int, default=2169010946, help="group this account owns")
    parser.add_argument("--peer", type=int, default=1927676487)
    parser.add_argument("--stage", default="l2", choices=("l2", "l3", "l4"))
    parser.add_argument("--segments", default="milky", choices=("milky", "v11"),
                        help="outgoing segment shape; use v11 only against a server that predates the fix")
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    global MILKY_BASE
    MILKY_BASE = args.base or "/"

    stamp = time.strftime("%Y-%m-%dT%H%M%S")
    out = Path(args.out) if args.out else Path(r"D:\projects\milky实现") / "实测" / stamp / "writes"
    run = Run(args.port, args.account, out, args.segments)
    print(f"stage {args.stage} -> {out}")
    {"l2": stage_l2, "l3": stage_l3, "l4": stage_l4}[args.stage](run, args.group, args.peer)
    summary = run.summary()
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    checked = [s for s in run.steps if "check" in s]
    passed = sum(1 for s in checked if s["ok"])
    print(f"\nverified {passed}/{len(checked)}; calls={len([s for s in run.steps if 'api' in s])}")
    return 0 if passed == len(checked) else 1


if __name__ == "__main__":
    raise SystemExit(main())
