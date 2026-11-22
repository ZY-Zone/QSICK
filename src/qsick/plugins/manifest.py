"""Plugin manifests: ``basic.json`` is the contract, not a suggestion.

A plugin declares what it *is* (tier) and what it *may do* (permissions). The
host refuses to load anything that asks for more than the configured ceiling, so
a third-party plugin cannot silently take over the message pipeline.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

TIERS = {"sandbox": 0, "standard": 1, "core": 2}

MANIFEST_NAME = "basic.json"

PERMISSIONS = {
    "events.read",       # observe messages and events
    "events.drop",       # stop a message from reaching clients/actions
    "messages.send",     # send through the runtime
    "actions.invoke",    # call any registered action
    "actions.register",  # add new actions to the OneBot surface
    "config.read",       # read own config
    "config.write",      # write own config
    "runtime.read",      # read status, metrics, account info
    "core.hooks",        # install hooks outside the declared ones
}

ID_RE = r"^[a-z][a-z0-9_-]{1,31}$"


class ManifestError(ValueError):
    pass


@dataclass(slots=True)
class PluginManifest:
    id: str
    name: str
    version: str
    entry: str
    path: Path
    api: int = 1
    tier: str = "standard"
    permissions: list[str] = field(default_factory=list)
    hooks: list[str] = field(default_factory=list)
    priority: int = 100
    enabled: bool = True
    description: str = ""
    # "default": QSICK renders the field list; "custom": the plugin ships its own page.
    config_ui: str = "default"
    config_page: str = ""
    config_fields: list[dict] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "PluginManifest":
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ManifestError(f"{path}: top level must be an object")

        missing = [key for key in ("id", "name", "version", "entry") if not raw.get(key)]
        if missing:
            raise ManifestError(f"{path}: missing {', '.join(missing)}")

        manifest = cls(
            id=str(raw["id"]),
            name=str(raw["name"]),
            version=str(raw["version"]),
            entry=str(raw["entry"]),
            path=path,
            api=int(raw.get("api", 1)),
            tier=str(raw.get("tier", "standard")),
            permissions=[str(item) for item in raw.get("permissions", [])],
            hooks=[str(item) for item in raw.get("hooks", [])],
            priority=int(raw.get("priority", 100)),
            enabled=bool(raw.get("enabled", True)),
            description=str(raw.get("description", "")),
            config_ui=str(raw.get("config_ui", "default")),
            config_page=str(raw.get("config_page", "")),
            config_fields=[dict(item) for item in raw.get("config_fields", []) if isinstance(item, dict)],
        )
        manifest.validate()
        return manifest

    def validate(self) -> None:
        import re

        if not re.fullmatch(ID_RE, self.id):
            raise ManifestError(f"{self.id}: id must match {ID_RE}")
        if self.api != 1:
            raise ManifestError(f"{self.id}: unsupported api version {self.api}")
        if self.tier not in TIERS:
            raise ManifestError(f"{self.id}: unknown tier {self.tier!r}")
        unknown = set(self.permissions) - PERMISSIONS
        if unknown:
            raise ManifestError(f"{self.id}: unknown permissions {sorted(unknown)}")
        if ":" not in self.entry:
            raise ManifestError(f"{self.id}: entry must be 'module.py:ClassName'")
        if self.config_ui not in ("default", "custom"):
            raise ManifestError(f"{self.id}: config_ui must be 'default' or 'custom'")
        if self.config_ui == "custom" and not self.config_page:
            raise ManifestError(f"{self.id}: config_ui=custom requires config_page")
        for item in self.config_fields:
            if not item.get("key"):
                raise ManifestError(f"{self.id}: every config_fields entry needs a key")

    @property
    def tier_level(self) -> int:
        return TIERS[self.tier]

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "tier": self.tier,
            "permissions": list(self.permissions),
            "priority": self.priority,
            "enabled": self.enabled,
            "description": self.description,
            "config_ui": self.config_ui,
            "config_page": self.config_page,
            "config_fields": self.config_fields,
            "path": str(self.path.parent),
        }
