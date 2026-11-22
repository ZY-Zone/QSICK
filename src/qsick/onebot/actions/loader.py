"""JSON-driven action loader.

``actions.json`` is the single registry: name, module, class, category, params and
status. The loader imports only the modules it is told about, so a broken or
removed module is a logged error rather than an import-time crash.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any, Callable

from ... import log as logmod
from .. import models
from ..registry import ActionError, ActionRegistry, ActionSpec, Param
from .base import Action, PlannedAction

log = logmod.get_logger("OneBot.Actions")

CATALOG = Path(__file__).with_name("actions.json")
PACKAGE = "qsick.onebot.actions"


def load_catalog() -> dict[str, Any]:
    return json.loads(CATALOG.read_text(encoding="utf-8"))


def build_registry(catalog: dict[str, Any] | None = None) -> ActionRegistry:
    data = catalog or load_catalog()
    registry = ActionRegistry()

    # SnowLuma compiles its registry once and refuses ambiguous names. Do the
    # same here: a duplicate executable name is a catalog bug, not a runtime one.
    claimed: dict[str, str] = {}
    for item in data.get("actions", []):
        name = str(item.get("name", ""))
        if name in claimed:
            raise ValueError(f"duplicate action name {name!r} in actions.json")
        claimed[name] = str(item.get("category", ""))
    for alias in (data.get("aliases") or {}):
        if alias in claimed:
            raise ValueError(f"alias {alias!r} collides with an action name")
        claimed[alias] = "alias"

    for item in data.get("actions", []):
        spec = _build_spec(item)
        if spec is None:
            continue
        registry.add(spec, replace=True)

    for alias, target in (data.get("aliases") or {}).items():
        try:
            registry.alias(alias, target)
        except ValueError as exc:
            log.error("alias rejected", alias=alias, target=target, error=str(exc))

    implemented = sum(1 for spec in registry._actions.values() if spec.status == "implemented")
    log.info(
        "action registry ready",
        registered=len(registry._actions),
        adapter_modules=implemented,
        planned=len(registry._actions) - implemented,
        capability_note="registered adapters are not equivalent to QQ-side support",
        aliases=len(data.get("aliases") or {}),
    )
    return registry


def _build_spec(item: dict[str, Any]) -> ActionSpec | None:
    name = str(item.get("name", "")).strip()
    if not name:
        log.error("catalog entry without a name", entry=item)
        return None

    status = "implemented" if item.get("status") == "implemented" else "planned"
    hint = str(item.get("hint") or "planned: implement this module or route it through send_packet/oidb_call")
    handler: Callable[..., Any]

    if status == "implemented":
        instance = _instantiate(item)
        if instance is None:
            return None
        instance.name = name
        instance.category = str(item.get("category", "extended"))
        instance.status = status
        handler = instance.run
    else:
        handler = _planned_handler(name, hint)

    params = [
        Param(
            name=str(entry.get("name", "")),
            kind=str(entry.get("kind", "str")),
            required=bool(entry.get("required", False)),
            default=entry.get("default"),
            desc=str(entry.get("desc", "")),
        )
        for entry in item.get("params", [])
        if entry.get("name")
    ]
    return ActionSpec(
        name=name,
        summary=str(item.get("summary", "")),
        category=str(item.get("category", "extended")),
        handler=handler,
        params=params,
        status=status,
        hint=hint,
        read_only=bool(item.get("read_only", False)),
    )


def _instantiate(item: dict[str, Any]) -> Action | None:
    module_path = f"{PACKAGE}.{str(item['module']).replace('/', '.')}"
    class_name = str(item["class"])
    try:
        module = importlib.import_module(module_path)
    except Exception as exc:  # noqa: BLE001 - a broken module must not kill startup
        log.error("action module failed to import", action=item.get("name"), module=module_path, error=str(exc))
        return None
    klass = getattr(module, class_name, None)
    if klass is None:
        log.error("action class missing", action=item.get("name"), module=module_path, class_name=class_name)
        return None
    if not issubclass(klass, Action):
        log.error("action class must subclass Action", action=item.get("name"), class_name=class_name)
        return None
    return klass()


def _planned_handler(name: str, hint: str) -> Callable[..., Any]:
    async def handler(ctx: Any, **params: Any) -> Any:
        raise ActionError(hint, models.NOT_IMPLEMENTED)

    handler.__name__ = f"planned_{name}"
    return handler


def verify_catalog() -> list[str]:
    """Return every problem with the catalog -> module mapping (used by tests)."""
    problems: list[str] = []
    data = load_catalog()
    for item in data.get("actions", []):
        module_path = f"{PACKAGE}.{str(item.get('module', '')).replace('/', '.')}"
        try:
            module = importlib.import_module(module_path)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{item.get('name')}: import {module_path} failed: {exc}")
            continue
        klass = getattr(module, str(item.get("class", "")), None)
        if klass is None:
            problems.append(f"{item.get('name')}: {module_path} has no class {item.get('class')}")
            continue
        if not issubclass(klass, Action):
            problems.append(f"{item.get('name')}: {item.get('class')} does not subclass Action")
            continue
        if item.get("status") == "implemented" and klass.run is Action.run:
            problems.append(f"{item.get('name')}: implemented but run() is not overridden")
    for alias, target in (data.get("aliases") or {}).items():
        if not any(entry.get("name") == target for entry in data.get("actions", [])):
            problems.append(f"alias {alias}: target {target} is missing")
    return problems
