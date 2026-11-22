"""The capability-scoped surface a plugin actually receives."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, TYPE_CHECKING

from .. import log as logmod

if TYPE_CHECKING:  # pragma: no cover
    from ..runtime import Runtime
    from .manifest import PluginManifest


class PermissionDenied(PermissionError):
    pass


class DropMessage(Exception):
    """Raised by ``ctx.drop()``: stop this message here, quietly."""

    def __init__(self, reason: str = "") -> None:
        super().__init__(reason or "dropped by plugin")
        self.reason = reason


class PluginContext:
    def __init__(self, runtime: "Runtime", manifest: "PluginManifest") -> None:
        self._runtime = runtime
        self.manifest = manifest
        self.log = logmod.get_logger(f"Plugin.{manifest.id}")

    # ------------------------------------------------------------ permissions

    def has(self, permission: str) -> bool:
        return permission in self.manifest.permissions

    def require(self, permission: str) -> None:
        if not self.has(permission):
            raise PermissionDenied(
                f"plugin {self.manifest.id!r} did not declare {permission!r} in basic.json"
            )

    def drop(self, reason: str = "") -> None:
        self.require("events.drop")
        raise DropMessage(reason)

    # ----------------------------------------------------------------- config

    @property
    def _config_path(self) -> Path:
        return self._runtime.config.path("plugins", self.manifest.id, "config.json")

    def read_config(self, default: dict[str, Any] | None = None) -> dict[str, Any]:
        self.require("config.read")
        if not self._config_path.exists():
            self.write_config(default or {})
            return dict(default or {})
        try:
            data = json.loads(self._config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.log.warn("config is unreadable; using defaults", path=str(self._config_path))
            return dict(default or {})
        return data if isinstance(data, dict) else dict(default or {})

    def write_config(self, data: dict[str, Any]) -> None:
        self.require("config.write")
        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        self._config_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # --------------------------------------------------------------- actions

    async def call(self, action: str, **params: Any) -> dict[str, Any]:
        self.require("actions.invoke")
        return await self._runtime.invoke(action, params)

    async def send_group_msg(self, group_id: int, message: Any) -> dict[str, Any]:
        return await self.call("send_group_msg", group_id=group_id, message=message)

    async def send_private_msg(self, user_id: int, message: Any) -> dict[str, Any]:
        return await self.call("send_private_msg", user_id=user_id, message=message)

    def register_action(self, spec: Any) -> None:
        self.require("actions.register")
        self._runtime.registry.add(spec, replace=True)
        self.log.info("registered action", action=getattr(spec, "name", "?"))

    # ----------------------------------------------------------------- status

    def runtime_info(self) -> dict[str, Any]:
        self.require("runtime.read")
        return self._runtime.info()
