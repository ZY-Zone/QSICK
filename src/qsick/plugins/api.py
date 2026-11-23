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


class AccountScopeUndeclared(PermissionDenied):
    """Raised when an account-scoped API is used without declaring `accounts` at all.

    Distinct from :class:`AccountOutOfScope` on purpose: one says "you named an account you did not claim", the
    other says "you never said anything about accounts". The fix is different -- add the uin to the list, versus add
    the list (or ``["ALL"]``) -- so the errors should not read the same.
    """


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
        """The accounts this plugin declared; ``["ALL"]`` means every one of them."""
        return list(self.manifest.accounts)

    def _require_accounts(self, what: str) -> None:
        """Refuse an account-scoped call from a manifest that never said which accounts it uses.

        This is the whole point of declaring ``["ALL"]`` instead of leaving the field out: an omission and a
        deliberate "all accounts" have to look different, or the manifest stops being an audit trail.
        """
        if not self.manifest.accounts_declared:
            raise AccountScopeUndeclared(
                f"plugin {self.manifest.id!r} called {what} without declaring `accounts` in basic.json; "
                f'add the QQ numbers it may use, or "accounts": ["ALL"]')

    def account_allowed(self, account: Any) -> bool:
        """Whether this plugin may act on, or receive from, `account`.

        An undeclared manifest is *account-agnostic*, not account-blind: it sees every account, the way a spam
        filter that never mentions accounts should. Declaring accounts is what turns the plugin account-aware, and
        the account-scoped APIs demand it -- via :meth:`_require_accounts` -- because those are the calls that
        actually target one account on purpose.

        An empty account means "unknown", which is allowed: messages that arrive without one (a synthetic event, a
        transport that does not report it) must not vanish over a scope check they cannot satisfy.
        """
        wanted = str(account or "").strip()
        if not wanted:
            return True
        if wanted in self._blocked:
            return False
        if self.manifest.allows_all_accounts:
            return True
        if not self.manifest.accounts_declared:
            return True
        return wanted in self.manifest.account_list

    def block_account(self, account: Any) -> None:
        """Mute one account for this plugin. Narrowing only -- the host re-checks the declared scope."""
        self._require_accounts("block_account")
        wanted = str(account or "").strip()
        if not wanted:
            raise ValueError("block_account needs an account")
        if not self.account_allowed(wanted):
            raise AccountOutOfScope(
                f"plugin {self.manifest.id!r} may not block {wanted!r}: not in its declared accounts")
        self._blocked.add(wanted)
        self.log.info("account muted for this plugin", account=wanted)

    def unblock_account(self, account: Any) -> None:
        self._blocked.discard(str(account or "").strip())

    def blocked_accounts(self) -> list[str]:
        return sorted(self._blocked)

    # --------------------------------------------------- kernel-level restrictions

    def _require_core(self, what: str) -> None:
        """A restriction that reaches past this plugin is a core-tier privilege.

        Tier, not permission: the manifest's `tier` already says how much of the kernel the plugin is trusted with,
        and a separate permission for the same decision would let a `standard` plugin ask for the ability the tier
        was there to withhold.
        """
        if not self.manifest.is_core:
            raise PermissionDenied(
                f"plugin {self.manifest.id!r} called {what}, which only core-tier plugins may use "
                f"(its tier is {self.manifest.tier!r})")

    def restrict_account(self, account: Any, *, direction: str = "both") -> str:
        """Block an account's traffic for the whole runtime. Core tier only.

        `direction` is ``in``, ``out`` or ``both``. Inbound means the message never reaches storage, a plugin or a
        client -- the same consequence `events.drop` has. Outbound means the send action is refused for every
        caller: this plugin, another plugin, a OneBot client, the WebUI.
        """
        self._require_core("restrict_account")
        state = self._runtime.restrict_account(account, direction=direction)
        self.log.warn("account restricted for the whole runtime", account=str(account), direction=state)
        return state

    def unrestrict_account(self, account: Any, *, direction: str = "both") -> str:
        self._require_core("unrestrict_account")
        state = self._runtime.unrestrict_account(account, direction=direction)
        self.log.info("runtime account restriction lifted", account=str(account), direction=direction, left=state or "-")
        return state

    def restrictions(self) -> dict[str, str]:
        self._require_core("restrictions")
        return self._runtime.account_restrictions()

    def accounts(self) -> list[dict[str, Any]]:
        """Every account the runtime currently knows, with the plugin's own verdict attached.

        Reads like a roster rather than a permission check: a plugin deciding what to do needs to see the accounts
        it cannot touch, otherwise "nothing happened" is indistinguishable from "not logged in".
        """
        self.require("runtime.read")
        self._require_accounts("accounts")
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
            self._require_accounts("call(account=...)")
            params["account"] = self._resolve_account(params["account"])
        return await self._runtime.invoke(action, params)

    async def invoke_for(self, instance: str, action: str, **params: Any) -> dict[str, Any]:
        """Run an action as a specific instance.

        Unlike :meth:`call`, this does not depend on whoever is "primary" this second -- which is the whole reason
        it exists: with several sessions live, primary changes as they log in and out.
        """
        self.require("actions.invoke")
        self._require_accounts("invoke_for")
        account = str(params.pop("account", "") or "")
        if account and not self.account_allowed(account):
            raise AccountOutOfScope(
                f"plugin {self.manifest.id!r} did not claim account {account!r} in basic.json")
        return await self._runtime.invoke_for(instance, action, params)

    async def send_group_msg(self, group_id: int, message: Any, *, account: Any = "") -> dict[str, Any]:
        self.require("messages.send")
        self._require_accounts("send_group_msg")
        payload: dict[str, Any] = {"group_id": int(group_id), "message": message}
        target = self._resolve_account(account)
        if target:
            payload["account"] = target
        return await self._runtime.invoke("send_group_msg", payload)

    async def send_private_msg(self, user_id: int, message: Any, *, account: Any = "") -> dict[str, Any]:
        self.require("messages.send")
        self._require_accounts("send_private_msg")
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
