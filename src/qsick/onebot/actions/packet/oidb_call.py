"""调用任意 OIDB."""

from __future__ import annotations

import re
from typing import Any

from qsick.onebot.actions.base import Action, ActionError, call


async def oidbcall(ctx: Any, command: int = 0, sub_command: int = 0,
                   body_hex: str = "", timeout_ms: int = 0, style: str = "",
                   reserved: int | None = None, **_: Any) -> dict[str, Any]:
    """Send one OIDB command and return QQ's status with the raw response body.

    The body comes back as hex on purpose: this action's job is to reach commands whose layout is
    not yet mapped (a member title, a reaction, a join request), so the caller reads the answer
    itself. Interpreting it here would mean guessing on behalf of every future caller.
    """
    if int(command) <= 0:
        raise ActionError("oidb_call requires command")
    body = str(body_hex or "").strip()
    if body and not re.fullmatch(r"[0-9a-fA-F]*", body):
        raise ActionError("body_hex must be hex")
    if len(body) % 2:
        raise ActionError("body_hex must contain whole bytes")
    params: dict[str, Any] = {"command": int(command), "sub_command": int(sub_command or 0),
                              "body_hex": body}
    if timeout_ms:
        params["timeout_ms"] = int(timeout_ms)
    # Which naming style routes is a property of the command (0x9067 answers to plain, 0x6D6 to
    # trpc), so a caller probing an unmapped command has to be able to name one.
    if style:
        if style not in ("plain", "trpc", "lower"):
            raise ActionError("style must be one of plain / trpc / lower")
        params["style"] = style
    # `isReserved` is per command, not a constant: 0x8FC_2 is built with 0 by NapCat's own builder and
    # a wrong value makes the server reject the packet as unparsable instead of explaining itself.
    if reserved is not None:
        if int(reserved) not in (0, 1):
            raise ActionError("reserved must be 0 or 1")
        params["reserved"] = int(reserved)
    result = await call(ctx, "oidb.call", params, "oidb call")
    return {
        "command": result.get("command", int(command)),
        "sub_command": result.get("sub_command", int(sub_command or 0)),
        "resolved": result.get("resolved", ""),
        "reserved": result.get("reserved", 1),
        "error_code": result.get("error_code", 0),
        "error_message": result.get("error_message", ""),
        "body_hex": result.get("body_hex", ""),
        "body_size": result.get("body_size", 0),
    }


class OidbCallAction(Action):
    async def run(self, ctx: Any, **params: Any) -> Any:
        return await oidbcall(ctx, **params)
