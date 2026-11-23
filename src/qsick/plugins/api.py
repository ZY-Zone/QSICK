"""The capability-scoped surface a plugin actually receives.

## Why accounts are part of the contract

QSICK drives several QQ accounts at once, and a plugin that cannot name one is stuck with "whichever account the
runtime happens to think is primary" -- which changes as sessions log in and out. So every send takes an `account`,
and a manifest can narrow which accounts a plugin ever sees.

The narrowing is declared, not requested: `accounts` in `basic.json` is enforced by the host, both when dispatching
a received message and on every send. A plugin cannot widen its own scope at runtime -- it can only narrow it
further (`ctx.block_account`), which is what a filter plugin wants.

## Permissions are the real gate

`messages.send` covers sending, `actions.invoke` covers arbitrary actions. They used to be conflated: `send_*`
went through `call()`, so a plugin that declared only `messages.send` got `PermissionDenied` and the documented
contract was wrong. They are separate now, and `send_*` requires `messages.send`.
"""

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


class AccountOutOfScope(PermissionDenied):
    """Raised when a plugin reaches for an account its manifest did not claim."""


class DropMessage(Exception):
    """Raised by ``ctx.drop()``: stop this message here, quietly."""

    def __init__(self, reason: str = "") -> None:
        super().__init__(reason or "dropped by plugin")
        self.reason = reason


class PluginContext:
    def __init__(self, runtime: "Runtime", manifest: "PluginManifest") -> None:
        self._runtime = runtime
        self.manifest = manifest
        self.log = logmod.get_logger(manifest.log_category_for_ui)
        #: Accounts this plugin has muted at runtime. A subset of the manifest's scope, never a superset.
        self._blocked: set[str] = set()

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

    # --------------------------------------------------------------- accounts

    @property
    def account_scope(self) -> list[str]:
        """The accounts this plugin declared, empty meaning "all of them"."""
        return list(self.manifest.accounts)

    def account_allowed(self, account: Any) -> bool:
        """Whether this plugin may act on, or receive from, `account`.

        Two filters, in order: the manifest's declared scope, then whatever the plugin has muted at runtime. An
        empty account means "unknown", which is allowed -- messages that arrive without one (a synthetic event, a
        transport that does not report it) must not vanish because of a scope check they cannot satisfy.
        """
        wanted = str(account or "").strip()
        if not wanted:
            return True
        if self.manifest.accounts and wanted not in self.manifest.accounts:
            return False
        return wanted not in self._blocked

    def block_account(self, account: Any) -> None:
        """Mute one account for this plugin. Narrowing only -- the host re-checks the declared scope."""
        wanted = str(account or "").strip()
        if not wanted:
            raise ValueError("block_account needs an account")
        if self.manifest.accounts and wanted not in self.manifest.accounts:
            raise AccountOutOfScope(
                f"plugin {self.manifest.id!r} may not block {wanted!r}: not in its declared accounts")
        self._blocked.add(wanted)
        self.log.info("account muted for this plugin", account=wanted)

    def unblock_account(self, account: Any) -> None:
        self._blocked.discard(str(account or "").strip())

    def blocked_accounts(self) -> list[str]:
        return sorted(self._blocked)

    def accounts(self) -> list[dict[str, Any]]:
        """Every account the runtime currently knows, with the plugin's own verdict attached.

        Reads like a roster rather than a permission check: a plugin deciding what to do needs to see the accounts
        it cannot touch, otherwise "nothing happened" is indistinguishable from "not logged in".
        """
        self.require("runtime.read")
        out: list[dict[str, Any]] = []
        for entry in self._runtime.instances_online():
            account = str(entry.get("uin") or "")
            out.append({
                **entry,
                "account": account,
                "allowed": self.account_allowed(account),
            })
        return out

    def _resolve_account(self, account: Any) -> str:
        """Turn the caller's `account` into one this plugin may use, or raise."""
        wanted = str(account or "").strip()
        if wanted and not self.account_allowed(wanted):
            raise AccountOutOfScope(
                f"plugin {self.manifest.id!r} did not claim account {wanted!r} in basic.json")
        if wanted:
            return wanted
        # No account named: fall back to the primary one, but only if this plugin is allowed to use it.
        primary = str(getattr(self._runtime, "primary_account", "") or "")
        return primary if self.account_allowed(primary) else ""

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
        """Invoke any registered action.

        If the caller names an `account`, it must be one this plugin claimed; if it names none, the plugin's own
        scope decides whether the call may proceed with the primary account.
        """
        self.require("actions.invoke")
        if "account" in params:
            params["account"] = self._resolve_account(params["account"])
        return await self._runtime.invoke(action, params)

    async def invoke_for(self, instance: str, action: str, **params: Any) -> dict[str, Any]:
        """Run an action as a specific instance.

        Unlike :meth:`call`, this does not depend on whoever is "primary" this second -- which is the whole reason
        it exists: with several sessions live, primary changes as they log in and out.
        """
        self.require("actions.invoke")
        account = str(params.pop("account", "") or "")
        if account and not self.account_allowed(account):
            raise AccountOutOfScope(
                f"plugin {self.manifest.id!r} did not claim account {account!r} in basic.json")
        return await self._runtime.invoke_for(instance, action, params)

    async def send_group_msg(self, group_id: int, message: Any, *, account: Any = "") -> dict[str, Any]:
        self.require("messages.send")
        payload: dict[str, Any] = {"group_id": int(group_id), "message": message}
        target = self._resolve_account(account)
        if target:
            payload["account"] = target
        return await self._runtime.invoke("send_group_msg", payload)

    async def send_private_msg(self, user_id: int, message: Any, *, account: Any = "") -> dict[str, Any]:
        self.require("messages.send")
        payload: dict[str, Any] = {"user_id": int(user_id), "message": message}
        target = self._resolve_account(account)
        if target:
            payload["account"] = target
        return await self._runtime.invoke("send_private_msg", payload)

    def register_action(self, spec: Any) -> None:
        self.require("actions.register")
        self._runtime.registry.add(spec, replace=True)
        self.log.info("registered action", action=getattr(spec, "name", "?"))

    # ----------------------------------------------------------------- status

    def runtime_info(self) -> dict[str, Any]:
        self.require("runtime.read")
        return self._runtime.info()
