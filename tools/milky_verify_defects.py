"""Verify the three fixes that need a fresh session, in one run, with evidence.

Covers:
  * `create_group_folder` / `rename_group_folder` / `delete_group_folder` -- the service creates
    the folder without returning an ID, so the action resolves it from the listing.
  * `upload_group_file` / `rename_group_file` / `move_group_file` / `delete_group_file` -- an
    upload whose call is uncertain is confirmed from the listing instead of being reported failed.
  * `kick_group_member` -- the service ack has no integer status on this build, so the member list
    decides.

Everything this creates is deleted at the end. The kick is the one exception: removing a member
is not undone by this script, so it needs `--kick` and a target.

    python tools/milky_verify_defects.py --port 5755 --account 3905548034 --group 2169010946
    python tools/milky_verify_defects.py ... --kick 1927676487
"""
from __future__ import annotations

import argparse
import base64
import json
import time
import urllib.error
import urllib.request
from pathlib import Path


def call(port: int, api: str, body: dict, timeout: float = 60.0) -> dict:
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
    except Exception as exc:  # noqa: BLE001
        record.update({"http": None, "error": f"{type(exc).__name__}: {exc}"})
        return record
    record["ms"] = int((time.time() - started) * 1000)
    record["raw"] = raw
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


class Check:
    def __init__(self, port: int, account: str, out: Path) -> None:
        self.port = port
        self.account = account
        self.out = out
        self.out.mkdir(parents=True, exist_ok=True)
        self.n = 0
        self.results: list[dict] = []

    def api(self, api: str, body: dict) -> dict:
        record = call(self.port, api, {**body, "account": self.account})
        self.n += 1
        (self.out / f"{self.n:02d}-{api}.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
        self.results.append({"api": api, "retcode": code(record)})
        print(f"  {'ok  ' if code(record) == 0 else 'FAIL'} {api:28} retcode={code(record)}")
        return record

    def check(self, label: str, condition: bool, detail: str = "") -> bool:
        print(f"       {'PASS' if condition else 'MISMATCH'}  {label}{('  ' + detail) if detail else ''}")
        self.results.append({"check": label, "ok": bool(condition)})
        return condition

    def listing(self, group: int, folder: str = "/") -> list[dict]:
        return data_of(self.api("get_group_files",
                                {"group_id": group, "parent_folder_id": folder})).get("files") or []


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5755)
    parser.add_argument("--account", default="3905548034")
    parser.add_argument("--group", type=int, default=2169010946)
    parser.add_argument("--kick", default="", help="UIN to kick; needs the account to be owner/admin")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    out = Path(args.out) if args.out else Path(r"D:\projects\milky实现") / "实测" / \
        (time.strftime("%Y-%m-%dT%H%M%S") + "-verify") / "writes"
    run = Check(args.port, args.account, out)
    group = args.group
    print(f"verifying against group {group}, evidence -> {out}")
    before = run.listing(group)
    print(f"  root entries before: {len(before)}")

    # ---- folders -----------------------------------------------------------------
    folder = run.api("create_group_folder", {"group_id": group, "folder_name": "QSICK验证"})
    folder_id = str(data_of(folder).get("folder_id") or "")
    run.check("create_group_folder now resolves a real folder id", bool(folder_id), f"id={folder_id!r}")
    if folder_id:
        run.check("the folder is in the listing",
                  any(str(f.get("file_id")) == folder_id for f in run.listing(group)))
        run.api("rename_group_folder", {"group_id": group, "folder_id": folder_id,
                                        "new_folder_name": "QSICK验证2"})
        run.check("rename_group_folder changed it",
                  any(str(f.get("file_id")) == folder_id and str(f.get("file_name")) == "QSICK验证2"
                      for f in run.listing(group)))

    # ---- upload ------------------------------------------------------------------
    blob = base64.b64encode(b"QSICK milky defect verification").decode()
    upload = run.api("upload_group_file", {
        "group_id": group, "parent_folder_id": "/", "file_name": "qsick-verify.txt",
        "file_uri": "base64://" + blob})
    file_id = str(data_of(upload).get("file_id") or "")
    run.check("upload_group_file returns a real file id", bool(file_id), f"id={file_id[:40]!r}")
    if file_id:
        run.api("rename_group_file", {"group_id": group, "file_id": file_id, "parent_folder_id": "/",
                                      "new_file_name": "qsick-verify-renamed.txt"})
        run.check("rename_group_file changed it",
                  any(str(f.get("file_id")) == file_id and str(f.get("file_name")) == "qsick-verify-renamed.txt"
                      for f in run.listing(group)))
        if folder_id:
            run.api("move_group_file", {"group_id": group, "file_id": file_id,
                                        "parent_folder_id": "/", "target_folder_id": folder_id})
            run.check("move_group_file put it inside the folder",
                      any(str(f.get("file_id")) == file_id for f in run.listing(group, folder_id)))

    # ---- cleanup -----------------------------------------------------------------
    if file_id:
        run.api("delete_group_file", {"group_id": group, "file_id": file_id})
    if folder_id:
        run.api("delete_group_folder", {"group_id": group, "folder_id": folder_id})
    after = run.listing(group)
    run.check("every created object is gone again", len(after) == len(before),
              f"entries {len(before)} -> {len(after)}")

    # ---- kick --------------------------------------------------------------------
    if args.kick:
        members = data_of(run.api("get_group_member_list",
                                  {"group_id": group, "no_cache": True})).get("members") or []
        run.check("the target is present before the kick",
                  any(str(m.get("user_id")) == args.kick for m in members), f"members={len(members)}")
        kicked = run.api("kick_group_member", {"group_id": group, "user_id": int(args.kick),
                                               "reject_add_request": False})
        run.check("kick_group_member succeeded", code(kicked) == 0,
                  f"data={json.dumps(data_of(kicked), ensure_ascii=False)[:120]}")
        after_members = data_of(run.api("get_group_member_list",
                                        {"group_id": group, "no_cache": True})).get("members") or []
        run.check("the target is gone afterwards",
                  not any(str(m.get("user_id")) == args.kick for m in after_members),
                  f"members={len(after_members)}")
        # `get_group_share_link` is an internal action, not a Milky API, so it is not part of this
        # verification: a rejoin link is fetched through the WebUI when someone needs it.

    checked = [r for r in run.results if "check" in r]
    passed = sum(1 for r in checked if r["ok"])
    (out / "summary.json").write_text(json.dumps(
        {"out": str(out), "results": run.results}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nverified {passed}/{len(checked)}")
    return 0 if passed == len(checked) else 1


if __name__ == "__main__":
    raise SystemExit(main())
