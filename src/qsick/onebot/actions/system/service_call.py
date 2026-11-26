"""OneBot adapter for `service_call`: invoke a service method whose signature is not published.

Diagnostic, and built for a specific job. Four capabilities have no op of their own -- a group
member's title, a message reaction, a nudge, a join request -- and the reason was never that the
client lacks them: `Group.setIdentityTitleInfo`, `Msg.setMsgEmojiLikes`, `Msg.sendNudge` and
`Group.operateSysNotify` all exist. What was missing was the argument shapes, and those are not in
any documentation this project has.

Calling with no arguments and reading the complaint is how the shapes get found. Guessing a struct
and shipping an op around the guess produces a failure that reads like a broken feature instead of an
unknown signature.

Same class of tool as `oidb_call`: a transport, not a capability.
"""
from __future__ import annotations

from typing import Any

from qsick.onebot import models
from qsick.onebot.actions.base import ActionContext, PayloadAction, call
from qsick.onebot.registry import ActionError


class ServiceCallAction(PayloadAction):
    op = "service.call"

    async def run(self, ctx: ActionContext, **params: Any) -> Any:
        service = str(params.get("service") or "").strip()
        method = str(params.get("method") or "").strip()
        if not service:
            raise ActionError("service_call: service is required (e.g. Group)", models.BAD_REQUEST)
        if not service.isalpha():
            raise ActionError("service_call: service must be a bare name such as Group",
                              models.BAD_REQUEST)
        if not method:
            raise ActionError("service_call: method is required", models.BAD_REQUEST)
        raw_args = params.get("args")
        if raw_args is None:
            args: list[Any] = []
        elif isinstance(raw_args, list):
            args = list(raw_args)
        else:
            raise ActionError("service_call: args must be an array", models.BAD_REQUEST)
        # The payload caps this too; refusing here keeps the failure on the side that can explain it.
        if len(args) > 24:
            raise ActionError("service_call: args may hold at most 24 entries", models.BAD_REQUEST)
        result = await call(ctx, self.op,
                            {"service": service, "method": method, "args": args},
                            self.name or self.op)
        return {
            "service": result.get("service", service),
            "method": result.get("method", method),
            "arg_count": int(result.get("arg_count") or 0),
            "returned": result.get("returned", ""),
            "result": result.get("result"),
        }
