"""Action registry with real validation and a single dispatch path.

Every action — implemented, aliased or planned — is registered here, so
`/api/actions` in the WebUI and the OneBot endpoint can never disagree about
what exists.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .. import log as logmod
from . import compat, models

log = logmod.get_logger("OneBot.Actions")


class ActionError(Exception):
    def __init__(self, message: str, retcode: int = models.BAD_REQUEST) -> None:
        super().__init__(message)
        self.retcode = retcode


@dataclass(slots=True)
class Param:
    name: str
    kind: str = "str"  # str | int | float | bool | list | dict | message | any
    required: bool = False
    default: Any = None
    desc: str = ""

    def coerce(self, value: Any) -> Any:
        if self.kind in ("any", "message"):
            return value
        if self.kind == "str":
            if isinstance(value, bool):
                return "true" if value else "false"
            if isinstance(value, (int, float)):
                return str(value)
            if isinstance(value, str):
                return value
            raise ActionError(f"{self.name}: expected string")
        if self.kind == "int":
            if isinstance(value, bool):
                raise ActionError(f"{self.name}: expected integer")
            try:
                return int(value)
            except (TypeError, ValueError):
                raise ActionError(f"{self.name}: expected integer") from None
        if self.kind == "float":
            try:
                return float(value)
            except (TypeError, ValueError):
                raise ActionError(f"{self.name}: expected number") from None
        if self.kind == "bool":
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)):
                return bool(value)
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            raise ActionError(f"{self.name}: expected boolean")
        if self.kind == "list":
            if isinstance(value, list):
                return value
            raise ActionError(f"{self.name}: expected array")
        if self.kind == "dict":
            if isinstance(value, dict):
                return value
            raise ActionError(f"{self.name}: expected object")
        return value


@dataclass(slots=True)
class ActionSpec:
    name: str
    summary: str
    category: str
    handler: Callable[..., Awaitable[Any]] | None = None
    params: list[Param] = field(default_factory=list)
    status: str = "implemented"  # implemented | planned
    hint: str = ""
    alias_of: str | None = None
    read_only: bool = False

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "summary": self.summary,
            "category": self.category,
            "status": self.status,
            "alias_of": self.alias_of,
            "read_only": self.read_only,
            "params": [
                {"name": p.name, "type": p.kind, "required": p.required, "default": p.default, "desc": p.desc}
                for p in self.params
            ],
            "hint": self.hint,
        }


class ActionRegistry:
    def __init__(self) -> None:
        self._actions: dict[str, ActionSpec] = {}
        self._aliases: dict[str, str] = {}

    def add(self, spec: ActionSpec, *, replace: bool = False) -> None:
        if spec.name in self._actions and not replace:
            raise ValueError(f"duplicate action {spec.name!r}")
        self._actions[spec.name] = spec

    def alias(self, name: str, target: str) -> None:
        if target not in self._actions:
            raise ValueError(f"alias {name!r} -> unknown action {target!r}")
        self._aliases[name] = target

    def get(self, name: str) -> ActionSpec | None:
        if name in self._actions:
            return self._actions[name]
        target = self._aliases.get(name)
        return self._actions.get(target) if target else None

    def names(self) -> list[str]:
        return sorted([*self._actions, *self._aliases])

    def specs(self) -> list[dict[str, Any]]:
        out = [spec.describe() for spec in self._actions.values()]
        for alias, target in self._aliases.items():
            base = self._actions[target]
            out.append({**base.describe(), "name": alias, "alias_of": target})
        return sorted(out, key=lambda item: item["name"])

    def categories(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for spec in self._actions.values():
            counts[spec.category] = counts.get(spec.category, 0) + 1
        return dict(sorted(counts.items()))

    async def dispatch(self, name: str, params: dict[str, Any] | None, ctx: Any) -> dict[str, Any]:
        params = params or {}
        echo = params.get("echo")
        spec = self.get(name)
        if spec is None:
            return models.fail(f"unknown action {name!r}", models.NOT_FOUND, echo=echo)
        if spec.status != "implemented" or spec.handler is None:
            hint = spec.hint or "not implemented in QSICK yet"
            return models.fail(f"{name}: {hint}", models.NOT_IMPLEMENTED, echo=echo)

        # Fold client-specific field spellings onto the canonical names before validation.
        #
        # This lives in dispatch rather than in PayloadAction because not every action is a
        # PayloadAction: create_group_file_folder and several others read `params["name"]` directly, and
        # those were left un-normalised when the folding happened per-action. Doing it here means one
        # rule covers every action shape, including plugin-supplied ones.
        #
        # `echo` is dropped before folding and restored after: it is the OneBot correlation id, not
        # action data, and a client is free to spell it however it likes without it being rewritten into
        # something else.
        try:
            params = compat.normalize(name, params, getattr(ctx, "protocol", ""),
                                      known={param.name for param in spec.params})
        except Exception as exc:  # noqa: BLE001 - normalisation must never break dispatch
            log.warn("param normalisation failed", action=name, error=f"{type(exc).__name__}: {exc}")

        try:
            kwargs = _validate(spec, params)
        except ActionError as exc:
            return models.fail(str(exc), exc.retcode, echo=echo)

        try:
            result = spec.handler(ctx, **kwargs)
            if inspect.isawaitable(result):
                result = await result
        except ActionError as exc:
            return models.fail(str(exc), exc.retcode, echo=echo)
        except Exception as exc:  # noqa: BLE001 - action boundary must not crash the server
            log.error("action crashed", action=name, error=f"{type(exc).__name__}: {exc}")
            return models.fail(f"{name}: {exc}", models.INTERNAL, echo=echo)

        if isinstance(result, dict) and "status" in result and "retcode" in result:
            result.setdefault("echo", echo)
            return result
        return models.ok(result, echo=echo)


def _validate(spec: ActionSpec, params: dict[str, Any]) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    known = {param.name for param in spec.params}
    for param in spec.params:
        if param.name in params and params[param.name] is not None:
            kwargs[param.name] = param.coerce(params[param.name])
        elif param.required:
            raise ActionError(f"missing required parameter {param.name!r}")
        else:
            kwargs[param.name] = param.default
    # Unknown keys are passed through: extensions and future fields must not 400.
    for key, value in params.items():
        if key not in known and key != "echo":
            kwargs.setdefault(key, value)
    return kwargs
