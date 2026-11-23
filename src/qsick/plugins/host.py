"""Plugin discovery, permission enforcement, hot reload and hook dispatch."""

from __future__ import annotations

import importlib.util
import inspect
import json
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TYPE_CHECKING

from .. import log as logmod
from .api import DropMessage, PermissionDenied, PluginContext
from .manifest import MANIFEST_NAME, PluginManifest, TIERS

if TYPE_CHECKING:  # pragma: no cover
    from ..protocol.message import Message
    from ..runtime import Runtime

log = logmod.get_logger("Plugins")


@dataclass(slots=True)
class LoadedPlugin:
    manifest: PluginManifest
    instance: Any
    ctx: PluginContext
    module_name: str
    stamps: dict[str, float]


class PluginHost:
    def __init__(self, runtime: "Runtime", dirs: list[str], *, max_tier: str = "core") -> None:
        self.runtime = runtime
        self.dirs = [Path(item).expanduser() for item in dirs]
        self.max_tier = max_tier if max_tier in TIERS else "core"
        self._loaded: dict[str, LoadedPlugin] = {}
        self._disabled: set[str] = set()
        self._state_path = runtime.config.path("plugins", "state.json")
        self._load_state()

    # ---------------------------------------------------------------- state

    def _load_state(self) -> None:
        if not self._state_path.exists():
            return
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(data, dict):
            self._disabled = {str(item) for item in data.get("disabled", [])}

    def _save_state(self) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(
            json.dumps({"disabled": sorted(self._disabled)}, indent=2) + "\n", encoding="utf-8"
        )

    # -------------------------------------------------------------- discover

    def discover(self) -> list[PluginManifest]:
        found: list[PluginManifest] = []
        seen: set[str] = set()
        for base in self.dirs:
            if not base.exists():
                continue
            for child in sorted(base.iterdir()):
                manifest_path = child / MANIFEST_NAME
                if not child.is_dir() or not manifest_path.exists():
                    continue
                try:
                    manifest = PluginManifest.load(manifest_path)
                except Exception as exc:  # noqa: BLE001 - report and keep scanning
                    log.error("bad manifest", path=str(manifest_path), error=str(exc))
                    continue
                if manifest.id in seen:
                    log.warn("duplicate plugin id; keeping the first", id=manifest.id, path=str(child))
                    continue
                seen.add(manifest.id)
                found.append(manifest)
        return found

    # ------------------------------------------------------------------ load

    async def load_all(self) -> None:
        for manifest in self.discover():
            await self.load(manifest)

    async def load(self, manifest: PluginManifest) -> bool:
        if manifest.id in self._disabled or not manifest.enabled:
            log.info("plugin disabled", id=manifest.id)
            return False
        if manifest.tier_level > TIERS[self.max_tier]:
            log.error(
                "plugin tier exceeds configured ceiling",
                id=manifest.id,
                tier=manifest.tier,
                max_tier=self.max_tier,
            )
            return False
        if manifest.id in self._loaded:
            await self.unload(manifest.id)

        module_file, _, class_name = manifest.entry.partition(":")
        source = manifest.path.parent / module_file
        if not source.exists():
            log.error("plugin entry missing", id=manifest.id, entry=str(source))
            return False

        module_name = f"qsick_plugin_{manifest.id}"
        try:
            spec = importlib.util.spec_from_file_location(module_name, source)
            if spec is None or spec.loader is None:
                raise ImportError(f"cannot load {source}")
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            klass = getattr(module, class_name)
            instance = klass()
        except Exception as exc:  # noqa: BLE001
            log.error("plugin import failed", id=manifest.id, error=f"{type(exc).__name__}: {exc}")
            sys.modules.pop(module_name, None)
            return False

        ctx = PluginContext(self.runtime, manifest)
        try:
            hook = getattr(instance, "on_load", None)
            if hook is not None:
                result = hook(ctx)
                if inspect.isawaitable(result):
                    await result
        except PermissionDenied as exc:
            log.error("plugin asked for an undeclared permission", id=manifest.id, error=str(exc))
            sys.modules.pop(module_name, None)
            return False
        except Exception as exc:  # noqa: BLE001
            log.error("plugin on_load failed", id=manifest.id, error=f"{type(exc).__name__}: {exc}")
            sys.modules.pop(module_name, None)
            return False

        stamps = _stamps(manifest)
        self._loaded[manifest.id] = LoadedPlugin(manifest, instance, ctx, module_name, stamps)
        # The category is declared by the plugin, not inferred from whether it happened to log yet: a plugin that
        # has not logged still has a category worth selecting, and one that will never log should not add noise.
        if manifest.register_log_category:
            logmod.register_category(manifest.log_category_for_ui, source="plugin",
                                     description=f"{manifest.name}（{manifest.id}）")
        # "account-agnostic" and "every account" are different declarations, so the log says which one it read.
        if not manifest.accounts_declared:
            scope = "account-agnostic"
        elif manifest.allows_all_accounts:
            scope = "ALL"
        else:
            scope = ",".join(manifest.account_list)
        # Say it at load time rather than letting the plugin receive nothing and look broken.
        declared = set(manifest.permissions)
        for name in ("on_message", "on_event"):
            if getattr(instance, name, None) is not None and "events.read" not in declared:
                log.warn("plugin implements a receive hook without events.read; it will not be called",
                         id=manifest.id, hook=name, fix="add 'events.read' to permissions")
        log.info(
            "plugin loaded",
            id=manifest.id,
            tier=manifest.tier,
            version=manifest.version,
            priority=manifest.priority,
            hooks=",".join(self._hooks_of(instance)) or "-",
            accounts=scope,
            log_category=manifest.log_category_for_ui if manifest.register_log_category else "-",
        )
        return True

    async def unload(self, plugin_id: str) -> None:
        loaded = self._loaded.pop(plugin_id, None)
        if loaded is None:
            return
        try:
            hook = getattr(loaded.instance, "on_unload", None)
            if hook is not None:
                result = hook(loaded.ctx)
                if inspect.isawaitable(result):
                    await result
        except Exception as exc:  # noqa: BLE001
            log.warn("plugin on_unload failed", id=plugin_id, error=str(exc))
        sys.modules.pop(loaded.module_name, None)
        if loaded.manifest.register_log_category:
            logmod.unregister_category(loaded.manifest.log_category_for_ui)
        log.info("plugin unloaded", id=plugin_id)

    async def reload(self, plugin_id: str) -> bool:
        current = self._loaded.get(plugin_id)
        if current is None:
            return False
        manifest = PluginManifest.load(current.manifest.path)
        await self.unload(plugin_id)
        return await self.load(manifest)

    async def set_enabled(self, plugin_id: str, enabled: bool) -> bool:
        if enabled:
            self._disabled.discard(plugin_id)
        else:
            self._disabled.add(plugin_id)
            await self.unload(plugin_id)
        self._save_state()
        if enabled:
            manifest = next((m for m in self.discover() if m.id == plugin_id), None)
            if manifest is None:
                return False
            return await self.load(manifest)
        return True

    # ------------------------------------------------------------- config ui

    def config_path(self, plugin_id: str) -> Path:
        return self.runtime.config.path("plugins", plugin_id, "config.json")

    def plugin_config(self, plugin_id: str) -> dict[str, Any]:
        loaded = self._loaded.get(plugin_id)
        manifest = loaded.manifest if loaded else next((m for m in self.discover() if m.id == plugin_id), None)
        defaults: dict[str, Any] = {}
        if manifest is not None:
            for item in manifest.config_fields:
                defaults[str(item["key"])] = item.get("default")
        path = self.config_path(plugin_id)
        if path.exists():
            try:
                stored = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(stored, dict):
                    defaults.update(stored)
            except (OSError, json.JSONDecodeError):
                log.warn("plugin config is unreadable", id=plugin_id, path=str(path))
        return defaults

    def save_plugin_config(self, plugin_id: str, values: dict[str, Any]) -> None:
        path = self.config_path(plugin_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(values, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        log.info("plugin config saved", id=plugin_id, keys=len(values))

    async def uninstall(self, plugin_id: str) -> bool:
        loaded = self._loaded.get(plugin_id)
        manifest = loaded.manifest if loaded else next((m for m in self.discover() if m.id == plugin_id), None)
        if manifest is None:
            return False
        await self.unload(plugin_id)
        target = manifest.path.parent
        trash = self.runtime.config.path("plugins", ".trash")
        trash.mkdir(parents=True, exist_ok=True)
        destination = trash / f"{plugin_id}-{int(time.time())}"
        try:
            shutil.move(str(target), str(destination))
        except OSError as exc:
            log.error("plugin uninstall failed", id=plugin_id, error=str(exc))
            return False
        self._disabled.add(plugin_id)
        self._save_state()
        log.warn("plugin moved to trash", id=plugin_id, path=str(destination))
        return True

    async def update(self, plugin_id: str) -> bool:
        """Re-read the plugin from disk (the market has no update feed yet)."""
        return await self.reload(plugin_id)

    # ----------------------------------------------------------- hot reload

    async def watch_once(self) -> None:
        for plugin_id, loaded in list(self._loaded.items()):
            compare = _stamps(loaded.manifest)
            if compare != loaded.stamps:
                log.info("plugin changed on disk; reloading", id=plugin_id)
                try:
                    await self.reload(plugin_id)
                except Exception as exc:  # noqa: BLE001
                    log.error("hot reload failed", id=plugin_id, error=str(exc))

    # -------------------------------------------------------------- dispatch

    @staticmethod
    def _hooks_of(instance: Any) -> list[str]:
        return [name for name in ("on_load", "on_message", "on_event", "on_unload")
                if getattr(instance, name, None) is not None]

    async def dispatch_message(self, message: "Message") -> bool:
        account = str(getattr(message, "account", "") or "")
        for loaded in sorted(self._loaded.values(), key=lambda item: item.manifest.priority):
            hook = getattr(loaded.instance, "on_message", None)
            if hook is None:
                continue
            # `events.read` is what the manifest says grants receiving, so it has to be the thing that does. Before
            # this, any plugin with an `on_message` received messages whether it declared the permission or not --
            # the permission was decoration. Load-time already warns, so nothing breaks silently.
            if not loaded.ctx.has("events.read"):
                continue
            # A manifest that claimed specific accounts does not see messages from the others. Enforced here rather
            # than inside the hook so the plugin cannot forget it, and logged at TRACE because a filtered message is
            # the expected case, not an incident.
            if not loaded.ctx.account_allowed(account):
                loaded.ctx.log.trace("message skipped: account outside this plugin's scope",
                                     account=account, message_id=message.message_id)
                continue
            try:
                result = hook(message, loaded.ctx)
                if inspect.isawaitable(result):
                    result = await result
            except DropMessage as drop:
                loaded.ctx.log.trace(
                    "message dropped", reason=drop.reason, message_id=message.message_id
                )
                return False
            except PermissionDenied as exc:
                loaded.ctx.log.error("plugin used an undeclared permission", error=str(exc))
                continue
            except Exception as exc:  # noqa: BLE001 - a plugin must not kill the pipeline
                loaded.ctx.log.error(
                    "on_message failed", error=f"{type(exc).__name__}: {exc}"
                )
                continue
            if result is False:
                if loaded.ctx.has("events.drop"):
                    loaded.ctx.log.trace("message dropped", reason="hook returned False")
                    return False
                loaded.ctx.log.warn("hook returned False without events.drop; ignoring")
        return True

    async def dispatch_event(self, event: dict[str, Any]) -> dict[str, Any] | None:
        """Run the event hooks. Returns the (possibly replaced) event, or None when a plugin dropped it.

        Dropping used to be an `on_message`-only idea: `ctx.drop()` inside `on_event` fell into the bare
        `except Exception` and was logged as a failure, so the documented control-flow signal silently did nothing
        in half the hooks that could raise it.
        """
        for loaded in sorted(self._loaded.values(), key=lambda item: item.manifest.priority):
            hook = getattr(loaded.instance, "on_event", None)
            if hook is None:
                continue
            if not loaded.ctx.has("events.read"):
                continue
            try:
                result = hook(event, loaded.ctx)
                if inspect.isawaitable(result):
                    result = await result
                if isinstance(result, dict):
                    event = result
            except DropMessage as drop:
                loaded.ctx.log.trace("event dropped", reason=drop.reason)
                return None
            except PermissionDenied as exc:
                loaded.ctx.log.error("plugin used an undeclared permission", error=str(exc))
                continue
            except Exception as exc:  # noqa: BLE001
                loaded.ctx.log.error("on_event failed", error=f"{type(exc).__name__}: {exc}")
        return event

    async def stop(self) -> None:
        for plugin_id in list(self._loaded):
            await self.unload(plugin_id)

    # ------------------------------------------------------------------ view

    def view(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for manifest in self.discover():
            loaded = self._loaded.get(manifest.id)
            item = manifest.to_dict()
            item["loaded"] = loaded is not None
            item["disabled"] = manifest.id in self._disabled
            item["config_ui"] = manifest.config_ui
            item["config_page"] = manifest.config_page
            item["config_fields"] = manifest.config_fields
            item["hooks"] = [
                name
                for name in ("on_load", "on_message", "on_event", "on_unload")
                if loaded is not None and getattr(loaded.instance, name, None) is not None
            ]
            out.append(item)
        return out


def _stamps(manifest: PluginManifest) -> dict[str, float]:
    stamps: dict[str, float] = {}
    base = manifest.path.parent
    for path in sorted(base.rglob("*.py")):
        try:
            stamps[str(path)] = path.stat().st_mtime
        except OSError:
            continue
    try:
        stamps[str(manifest.path)] = manifest.path.stat().st_mtime
    except OSError:
        pass
    return stamps
