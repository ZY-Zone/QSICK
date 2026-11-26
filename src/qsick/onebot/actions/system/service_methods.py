"""OneBot adapter for `service_methods`: ask a QQNT service what it can do.

This exists because "the client cannot do X" and "nobody found where X lives" look identical from
the outside, and only one of them is true. Four capabilities reported as missing -- a member's
special title, a message reaction, a nudge, approving a join request -- had no entry in the payload
because no service method was found for them; guessing OIDB command numbers is expensive and the
empty-body answer is worthless (an empty packet cannot decode, so every command comes back 253).

Read-only: it reads property names off one getter and calls nothing.
"""
from __future__ import annotations

from typing import Any

from qsick.onebot import models
from qsick.onebot.actions.base import ActionContext, PayloadAction
from qsick.onebot.registry import ActionError


class ServiceMethodsAction(PayloadAction):
    op = "service.methods"

    async def run(self, ctx: ActionContext, **params: Any) -> Any:
        service = str(params.get("service") or "").strip()
        if not service:
            raise ActionError("service_methods: service is required (a name such as Msg, or \"*\")",
                              models.BAD_REQUEST)
        # `*` is the survey form: every service reachable from the session in one call. Asking one
        # service at a time costs a session rebuild per guess, which is the whole problem this
        # action exists to avoid.
        if service != "*" and not service.isalpha():
            raise ActionError("service_methods: service must be a bare name such as Msg, or \"*\"",
                              models.BAD_REQUEST)
        payload: dict[str, Any] = {"service": service}
        contains = str(params.get("contains") or "").strip()
        if contains:
            payload["contains"] = contains
        result = await self._call(ctx, payload)
        out: dict[str, Any] = {
            "service": result.get("service", service),
            "filtered": bool(result.get("filtered")),
            "total": int(result.get("total") or 0),
        }
        if service == "*":
            services = result.get("services") or {}
            out["services"] = services
            out["matched"] = sum(len((info or {}).get("methods") or []) for info in services.values())
        else:
            methods = list(result.get("methods") or [])
            out["methods"] = methods
            out["matched"] = len(methods)
        return out

    async def _call(self, ctx: ActionContext, payload: dict[str, Any]) -> dict[str, Any]:
        from qsick.onebot.actions.base import call

        return await call(ctx, self.op, payload, self.name or self.op)
