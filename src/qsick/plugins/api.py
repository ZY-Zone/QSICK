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


class AccountNotOnline(PermissionDenied):
    """Raised when a plugin names an account that is not logged in right now.

    The cause is availability, not permissions -- but it subclasses `PermissionDenied` because from the plugin's
    side it is the same situation: the call was refused, nothing happened, and the next event will get the same
    answer until the account comes back. Handling it as a crash would be wrong.
    """


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
        """Whether this manifest declared `permission`. Answers False rather than raising -- use it to branch."""
        return permission in self.manifest.permissions

    def require(self, permission: str) -> None:
        """Raise :class:`PermissionDenied` unless `permission` was declared.

        Every capability method calls this first, so a plugin cannot reach an ability by forgetting to check.
        """
        if not self.has(permission):
            raise PermissionDenied(
                f"plugin {self.manifest.id!r} did not declare {permission!r} in basic.json"
            )

    def drop(self, reason: str = "") -> None:
        """Stop the current message or event from going any further. Needs ``events.drop``.

        Works in ``on_message`` (the message is never stored or published) and in ``on_event`` (the event is not
        published). It raises instead of returning, so it must not be caught: it is control flow, not an error.
        """
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
        """Undo :meth:`block_account`. Harmless when the account was not blocked."""
        self._blocked.discard(str(account or "").strip())

    def blocked_accounts(self) -> list[str]:
        """The accounts this plugin has muted at runtime -- a subset of what it declared."""
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
        """Lift a kernel restriction, wholly or for one direction. Returns what is left (``""`` when nothing is).

        Core tier only, like :meth:`restrict_account`.
        """
        self._require_core("unrestrict_account")
        state = self._runtime.unrestrict_account(account, direction=direction)
        self.log.info("runtime account restriction lifted", account=str(account), direction=direction, left=state or "-")
        return state

    def restrictions(self) -> dict[str, str]:
        """The kernel restrictions in force: ``{uin: "in" | "out" | "both"}``. Core tier only."""
        self._require_core("restrictions")
        return self._runtime.account_restrictions()

    def accounts(self) -> list[dict[str, Any]]:
        """The accounts that are logged in right now, each with this plugin's verdict attached.

        The same set the 账户管理 page lists as usable, and the same set the account-scoped calls accept: a stopped
        instance, one that never signed in, and a network profile with no live login are all absent. Handing those
        out would give a plugin a target list it cannot use.

        `allowed` is the *scope* answer, not an availability one -- everything in the list is already reachable, so
        a `False` there means "declared accounts do not include this one".
        """
        self.require("runtime.read")
        self._require_accounts("accounts")
        # Online accounts only -- the same set the 账户管理 page lists, and the same set the account-scoped calls
        # will accept. Returning signed-out instances here would invite a plugin to build a target list it cannot
        # use; `allowed` then only has to answer the scope question.
        return [{**entry, "allowed": self.account_allowed(entry["account"])}
                for entry in self._runtime.online_accounts()]

    def _resolve_account(self, account: Any) -> str:
        """Turn the caller's `account` into one this plugin may use, or raise.

        Two separate questions, asked in this order: may this plugin touch the account at all (its declared scope),
        and is the account actually reachable right now (logged in). Only online accounts can be acted on -- naming
        a registered-but-signed-out one is a mistake worth reporting, not a call to buffer until it returns.
        """
        wanted = str(account or "").strip()
        if wanted:
            if not self.account_allowed(wanted):
                raise AccountOutOfScope(
                    f"plugin {self.manifest.id!r} did not claim account {wanted!r} in basic.json")
            if not self._runtime.is_account_online(wanted):
                raise AccountNotOnline(self._online_hint(wanted))
            return wanted
        # No account named: fall back to the primary one, but only if this plugin may use it and it is online.
        primary = str(getattr(self._runtime, "primary_account", "") or "")
        if not primary or not self.account_allowed(primary):
            return ""
        return primary if self._runtime.is_account_online(primary) else ""

    def _online_hint(self, account: str) -> str:
        """Say which accounts *are* available, because "not online" alone leaves the caller guessing."""
        live = [item["account"] for item in self._runtime.online_accounts()]
        available = ", ".join(live) if live else "none"
        return (f"plugin {self.manifest.id!r} cannot use account {account!r}: it is not logged in "
                f"(online now: {available})")

    # ----------------------------------------------------------------- config

    @property
    def _config_path(self) -> Path:
        return self._runtime.config.path("plugins", self.manifest.id, "config.json")

    def _write_config_file(self, data: dict[str, Any]) -> None:
        """Write the file with no permission check -- callers decide whether one is owed."""
        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        self._config_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def read_config(self, default: dict[str, Any] | None = None) -> dict[str, Any]:
        """This plugin's own config. Needs ``config.read``; on first use the file is seeded from `default`.

        Seeding does not need ``config.write`` -- it is how reading a config that has never been written works.
        """
        self.require("config.read")
        if not self._config_path.exists():
            # Seeding the file from the declared defaults is part of reading it, not a write the plugin asked for.
            # Going through `write_config` here made `config.read` alone unusable on a first run, which is the one
            # time it matters.
            self._write_config_file(default or {})
            return dict(default or {})
        try:
            data = json.loads(self._config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.log.warn("config is unreadable; using defaults", path=str(self._config_path))
            return dict(default or {})
        return data if isinstance(data, dict) else dict(default or {})

    def write_config(self, data: dict[str, Any]) -> None:
        """Replace this plugin's config file. Needs ``config.write``."""
        self.require("config.write")
        self._write_config_file(data)

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
        # The account comes from the instance, not from the caller -- that is the point of naming one. A caller that
        # also passes `account` is either repeating itself or confused, and silently dropping the value (the old
        # behaviour) hid the second case until a send landed on the wrong number.
        account = str(params.pop("account", "") or "")
        if account:
            if not self.account_allowed(account):
                raise AccountOutOfScope(
                    f"plugin {self.manifest.id!r} did not claim account {account!r} in basic.json")
            owner = next((entry["account"] for entry in self._runtime.online_accounts()
                          if entry["instance"] == str(instance)), "")
            if owner and owner != account:
                raise AccountOutOfScope(
                    f"instance {instance!r} belongs to account {owner!r}, not {account!r}")
        if not self._runtime.is_instance_online(instance):
            raise AccountNotOnline(
                f"plugin {self.manifest.id!r} cannot act as instance {instance!r}: it is not logged in")
        return await self._runtime.invoke_for(instance, action, params)

    @staticmethod
    def _send_payload(key: str, target: int, message: Any, account: str,
                      auto_escape: bool | None) -> dict[str, Any]:
        payload: dict[str, Any] = {key: int(target), "message": message}
        if account:
            payload["account"] = account
        # Passed through only when asked for: the action's default is not ours to restate, and sending
        # `auto_escape=None` would read as "explicitly no" rather than "unset".
        if auto_escape is not None:
            payload["auto_escape"] = bool(auto_escape)
        return payload

    async def send_group_msg(self, group_id: int, message: Any, *, account: Any = "",
                             auto_escape: bool | None = None) -> dict[str, Any]:
        """Send to a group. `auto_escape` is the OneBot v11 CQ-string escape flag; leave it unset for segments."""
        self.require("messages.send")
        self._require_accounts("send_group_msg")
        payload = self._send_payload("group_id", group_id, message, self._resolve_account(account), auto_escape)
        return await self._runtime.invoke("send_group_msg", payload)

    async def send_private_msg(self, user_id: int, message: Any, *, account: Any = "",
                               auto_escape: bool | None = None) -> dict[str, Any]:
        """Send to one person. `auto_escape` behaves as in :meth:`send_group_msg`."""
        self.require("messages.send")
        self._require_accounts("send_private_msg")
        payload = self._send_payload("user_id", user_id, message, self._resolve_account(account), auto_escape)
        return await self._runtime.invoke("send_private_msg", payload)

    def register_action(self, spec: Any) -> None:
        """Add or replace an action on the OneBot surface. Needs ``actions.register``.

        A name that already exists is replaced rather than refused, which is what a hot reload needs; the registry's
        own duplicate check is bypassed deliberately.
        """
        self.require("actions.register")
        self._runtime.registry.add(spec, replace=True)
        self.log.info("registered action", action=getattr(spec, "name", "?"))

    # ----------------------------------------------------------------- status

    def runtime_info(self) -> dict[str, Any]:
        """The runtime's status snapshot -- the same dict the WebUI renders. Needs ``runtime.read``."""
        self.require("runtime.read")
        return self._runtime.info()
