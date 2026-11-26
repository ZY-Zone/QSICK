"""Survey what the client's own service objects expose, for the four missing capabilities.

    python tools/survey_service_methods.py --port 5754 --account 3905548034

Runs one call per keyword rather than one per service: each `service.methods` call with
`service="*"` walks every `getXxxService` on the session, so the cost is in the number of keywords,
not the number of services.
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

#: Capability -> substrings worth looking for. A method may be named for the noun ("Title") or the
#: action ("Poke"); both are searched, because the naming is not consistent in this client.
KEYWORDS: dict[str, list[str]] = {
    "special_title": ["title", "specialtitle", "membertitle"],
    "reaction": ["emoji", "like", "reaction", "emojilike"],
    "poke": ["poke", "nudge", "facebubble", "tickle"],
    "group_request": ["request", "approve", "accept", "invit", "join", "apply"],
}


def action(port: int, name: str, params: dict, account: str, timeout: float = 90.0) -> dict:
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
        record["raw"] = raw[:600]
    return record


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5754)
    parser.add_argument("--account", default="3905548034")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    out = Path(args.out) if args.out else Path(__file__).resolve().parents[1] / "temp" / "service-survey"
    out.mkdir(parents=True, exist_ok=True)

    report: dict[str, object] = {"account": args.account, "queries": {}}

    for capability, needles in KEYWORDS.items():
        hits: dict[str, list[str]] = {}
        errors: dict[str, str] = {}
        for needle in needles:
            reply = action(args.port, "service_methods",
                           {"service": "*", "contains": needle}, args.account)
            data = reply.get("data") or {}
            services = data.get("services") or {}
            if reply.get("retcode") != 0:
                errors[needle] = str(reply.get("message") or reply.get("error") or "")[:200]
                continue
            for service, info in services.items():
                methods = (info or {}).get("methods") or []
                if not methods:
                    continue
                bucket = hits.setdefault(service, [])
                for method in methods:
                    if method not in bucket:
                        bucket.append(method)
        report["queries"][capability] = {"hits": hits, "errors": errors}
        print(f"\n=== {capability} ===")
        if errors:
            for needle, message in errors.items():
                print(f"  [{needle}] 失败: {message}")
        if not hits:
            print("  没有任何服务的方法名命中这些关键字")
        for service in sorted(hits):
            print(f"  {service:26} {', '.join(sorted(hits[service]))}")

    (out / "survey.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nevidence: {out / 'survey.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
