"""OneBot v11 response envelope and retcodes."""

from __future__ import annotations

from typing import Any

OK = 0
BAD_REQUEST = 1400
UNAUTHORIZED = 1401
FORBIDDEN = 1403
NOT_FOUND = 1404
NOT_IMPLEMENTED = 1405
CONFLICT = 1409
INTERNAL = 1500
UPSTREAM = 1502


def ok(data: Any = None, echo: Any = None) -> dict[str, Any]:
    return {"status": "ok", "retcode": OK, "data": data, "message": "", "wording": "", "echo": echo}


def fail(
    message: str,
    retcode: int = BAD_REQUEST,
    *,
    data: Any = None,
    echo: Any = None,
) -> dict[str, Any]:
    return {
        "status": "failed",
        "retcode": retcode,
        "data": data,
        "message": message,
        "wording": message,
        "echo": echo,
    }
