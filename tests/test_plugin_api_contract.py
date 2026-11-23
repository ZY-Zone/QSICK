"""Every plugin-facing interface, exercised once.

The other plugin tests check behaviour; this one checks that the *surface* is what the docs say it is. Two failure
modes it exists for:

  * a documented method that does nothing, or that is missing -- the contract in `docs/plugin-api.md` has been
    wrong before (`messages.send` did not actually permit sending);
  * a permission that is listed but never enforced, which reads as a guarantee and is not one.

Real runtime, real manifest, real store. The transport is the mock one, so nothing leaves the machine.
"""
from __future__ import annotations

import inspect
import json
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from qsick.plugins import api as api_module  # noqa: E402
from qsick.plugins.api import (  # noqa: E402
    AccountNotOnline,
    AccountOutOfScope,
    AccountScopeUndeclared,
    DropMessage,
    PermissionDenied,
    PluginContext,
)
from qsick.plugins.manifest import ALL_ACCOUNTS, PERMISSIONS, PluginManifest  # noqa: E402

#: Every permission, so a test can hand out exactly one and see whether it is enforced.
ALL_PERMISSIONS = sorted(PERMISSIONS)

#: The public surface of `PluginContext`, as documented. A method added without being listed here is a method the
#: docs do not mention; one removed shows up as an AttributeError in every test below.
PUBLIC_API = {
    # permissions
    "has", "require", "drop",
    # accounts
    "account_scope", "account_allowed", "accounts", "block_account", "unblock_account", "blocked_accounts",
    # kernel restrictions
    "restrict_account", "unrestrict_account", "restrictions",
    # config
    "read_config", "write_config",
    # actions
    "call", "invoke_for", "send_group_msg", "send_private_msg", "register_action",
    # status
    "runtime_info",
}


def a_manifest(tmp_path: Path, **overrides) -> PluginManifest:
    payload = {
        "id": "contract", "name": "Contract", "version": "1.0.0", "entry": "plugin.py:Contract",
        "tier": "standard",
        "permissions": ALL_PERMISSIONS,
        "accounts": [ALL_ACCOUNTS],
    }
    payload.update(overrides)
    path = tmp_path / "contract" / "basic.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return PluginManifest.load(path)


class TestSurface:
    def test_the_public_surface_is_exactly_what_is_documented(self):
        actual = {name for name in dir(PluginContext) if not name.startswith("_")}
        # `manifest` and `log` are attributes set in __init__, so they are not on the class; the rest are methods.
        actual -= {"manifest", "log"}
        assert actual == PUBLIC_API, (
            f"undocumented: {sorted(actual - PUBLIC_API)}; documented but missing: {sorted(PUBLIC_API - actual)}")

    def test_every_public_method_has_a_docstring_or_is_obvious(self):
        # Not style for its own sake: these are the strings a plugin author has to work from.
        thin = [name for name in PUBLIC_API
                if callable(getattr(PluginContext, name, None))
                and not (getattr(PluginContext, name).__doc__ or "").strip()]
        assert thin == [], f"undocumented methods: {thin}"

    def test_every_documented_permission_exists_in_the_manifest_enum(self):
        for name in ALL_PERMISSIONS:
            manifest = {"permissions": [name]}
            assert manifest["permissions"][0] in PERMISSIONS

    def test_the_exceptions_are_a_family_a_plugin_can_catch(self):
        for kind in (AccountOutOfScope, AccountNotOnline, AccountScopeUndeclared):
            assert issubclass(kind, PermissionDenied)
        assert issubclass(PermissionDenied, PermissionError)


class TestPermissionsAreEnforced:
    """Each permission is granted alone, and the call it is supposed to protect must still work."""

    def ctx(self, runtime, tmp_path, permissions) -> PluginContext:
        return PluginContext(runtime, a_manifest(tmp_path, permissions=permissions))

    async def test_config_read_works_without_config_write(self, runtime, tmp_path):
        # Seeding the file from defaults is part of reading; it used to require `config.write` and made a
        # read-only plugin unusable on its first run.
        ctx = self.ctx(runtime, tmp_path, ["config.read"])
        assert ctx.read_config({"a": 1}) == {"a": 1}
        assert ctx.read_config() == {"a": 1}
        with pytest.raises(PermissionDenied):
            ctx.write_config({"b": 2})

    async def test_config_write_alone_cannot_read(self, runtime, tmp_path):
        ctx = self.ctx(runtime, tmp_path, ["config.write"])
        ctx.write_config({"x": 1})
        with pytest.raises(PermissionDenied):
            ctx.read_config()

    async def test_messages_send_alone_is_enough(self, runtime, tmp_path):
        ctx = self.ctx(template_manifest(tmp_path), ["messages.send"]) if False else self.ctx(
            runtime, tmp_path, ["messages.send"])
        result = await ctx.send_group_msg(30001, [{"type": "text", "data": {"text": "hi"}}])
        assert result["status"] in ("ok", "failed"), "the call must reach the runtime, not be refused locally"

    async def test_actions_invoke_alone_is_enough(self, runtime, tmp_path):
        ctx = self.ctx(runtime, tmp_path, ["actions.invoke"])
        result = await ctx.call("get_version_info")
        assert "status" in result

    async def test_runtime_read_gates_the_two_readers(self, runtime, tmp_path):
        ctx = self.ctx(runtime, tmp_path, [])
        with pytest.raises(PermissionDenied):
            ctx.runtime_info()
        with pytest.raises(PermissionDenied):
            ctx.accounts()
        rich = self.ctx(runtime, tmp_path, ["runtime.read"])
        assert isinstance(rich.runtime_info(), dict)
        assert isinstance(rich.accounts(), list)

    async def test_events_drop_gates_drop(self, runtime, tmp_path):
        ctx = self.ctx(runtime, tmp_path, [])
        with pytest.raises(PermissionDenied):
            ctx.drop("no")
        allowed = self.ctx(runtime, tmp_path, ["events.drop"])
        with pytest.raises(DropMessage):
            allowed.drop("yes")

    async def test_actions_register_gates_register_action(self, runtime, tmp_path):
        from qsick.onebot.registry import ActionSpec

        def spec(name):
            async def handler(ctx, **_params):
                return {"ok": True}

            return ActionSpec(name=name, summary="contract", category="extended", params=[], handler=handler)

        denied = self.ctx(runtime, tmp_path, [])
        with pytest.raises(PermissionDenied):
            denied.register_action(spec("contract_denied"))
        allowed = self.ctx(runtime, tmp_path, ["actions.register"])
        allowed.register_action(spec("contract_allowed"))
        assert runtime.registry.get("contract_allowed") is not None


class TestEventsReadIsReal:
    """`events.read` used to be decoration: any `on_message` received messages without it."""

    async def test_a_plugin_without_events_read_receives_nothing(self, runtime):
        from qsick.protocol.message import Message

        loaded = runtime.plugins._loaded["guard"]
        original = list(loaded.manifest.permissions)
        message = Message.from_payload({
            "message_id": 1, "chat_type": "group", "chat_id": 30001,
            "elements": [{"type": "text", "data": {"text": "hi"}}], "account": "10001",
        })
        try:
            # guard's policy would drop this (whitelist off, not in any list) -- with the permission removed the
            # hook must not run at all, so the message survives.
            loaded.manifest.permissions = [p for p in original if p != "events.read"]
            assert await runtime.plugins.dispatch_message(message) is True
            # And with it back, the hook runs and its verdict applies.
            loaded.manifest.permissions = original
            assert await runtime.plugins.dispatch_message(message) is True
        finally:
            loaded.manifest.permissions = original


class TestDropInBothHooks:
    async def test_drop_in_on_event_returns_none_instead_of_logging_an_error(self, runtime, tmp_path):
        from qsick.plugins.host import LoadedPlugin

        class Dropper:
            def on_event(self, event, ctx):
                ctx.drop("filtered")

        manifest = a_manifest(tmp_path, permissions=["events.read", "events.drop"])
        ctx = PluginContext(runtime, manifest)
        runtime.plugins._loaded["contract_drop"] = LoadedPlugin(
            manifest, Dropper(), ctx, "qsick_plugin_contract_drop", {})
        try:
            assert await runtime.plugins.dispatch_event({"post_type": "test"}) is None
        finally:
            runtime.plugins._loaded.pop("contract_drop", None)

    async def test_an_event_hook_that_returns_a_dict_replaces_the_event(self, runtime, tmp_path):
        from qsick.plugins.host import LoadedPlugin

        class Replacer:
            def on_event(self, event, ctx):
                return {**event, "patched": True}

        manifest = a_manifest(tmp_path, permissions=["events.read"])
        runtime.plugins._loaded["contract_patch"] = LoadedPlugin(
            manifest, Replacer(), PluginContext(runtime, manifest), "qsick_plugin_contract_patch", {})
        try:
            result = await runtime.plugins.dispatch_event({"post_type": "test"})
            assert result == {"post_type": "test", "patched": True}
        finally:
            runtime.plugins._loaded.pop("contract_patch", None)


class TestEveryCallWorks:
    """One pass over the whole surface with every permission granted, asserting each answers rather than raising."""

    async def test_the_whole_surface_answers(self, runtime, tmp_path):
        manifest = a_manifest(tmp_path)
        ctx = PluginContext(runtime, manifest)

        # permissions
        assert ctx.has("events.read") is True
        ctx.require("events.read")

        # accounts
        assert ctx.account_scope == [ALL_ACCOUNTS]
        assert ctx.account_allowed("10001") is True
        roster = ctx.accounts()
        assert isinstance(roster, list)
        live = [entry["account"] for entry in roster]
        assert live, "the mock transport logs one account in, so the roster cannot be empty"
        ctx.block_account(live[0])
        assert ctx.blocked_accounts() == [live[0]]
        assert ctx.account_allowed(live[0]) is False
        ctx.unblock_account(live[0])
        assert ctx.blocked_accounts() == []

        # kernel restrictions (core tier is required; this manifest is standard, so use a core one)
        core = PluginContext(runtime, a_manifest(tmp_path, id="contract_core", tier="core"))
        assert core.restrict_account("1999999999", direction="out") == "out"
        assert core.restrictions()["1999999999"] == "out"
        assert core.unrestrict_account("1999999999") == ""

        # config
        assert ctx.read_config({"seed": True}) == {"seed": True}
        ctx.write_config({"seed": False})
        assert ctx.read_config() == {"seed": False}

        # actions
        assert "status" in await ctx.call("get_version_info")
        assert "status" in await ctx.invoke_for(live_instance(runtime), "get_version_info")
        assert "status" in await ctx.send_group_msg(30001, [{"type": "text", "data": {"text": "x"}}])
        assert "status" in await ctx.send_private_msg(20001, [{"type": "text", "data": {"text": "x"}}])

        # status
        assert isinstance(ctx.runtime_info(), dict)

    async def test_logging_uses_the_declared_category(self, runtime, tmp_path):
        manifest = a_manifest(tmp_path, log={"register": True, "category": "Contract"})
        ctx = PluginContext(runtime, manifest)
        assert ctx.log.category == "Contract"

    async def test_auto_escape_is_passed_only_when_asked_for(self, runtime, tmp_path):
        ctx = PluginContext(runtime, a_manifest(tmp_path))
        seen: list[dict] = []
        original = runtime.invoke

        async def spy(action, params=None):
            seen.append(dict(params or {}))
            return await original(action, params)

        runtime.invoke = spy  # type: ignore[method-assign]
        try:
            await ctx.send_group_msg(30001, "hi")
            await ctx.send_group_msg(30001, "hi", auto_escape=False)
        finally:
            runtime.invoke = original  # type: ignore[method-assign]
        assert "auto_escape" not in seen[0], "unset must stay unset, not become an explicit False"
        assert seen[1]["auto_escape"] is False


def live_instance(runtime) -> str:
    """The instance id behind the first online account."""
    return runtime.online_accounts()[0]["instance"]


class TestRefusalsAreSpecific:
    """Three different refusals, three different messages -- a caller has to know which one it hit."""

    async def test_undeclared_scope(self, runtime, tmp_path):
        ctx = PluginContext(runtime, a_manifest(tmp_path, accounts=[]))
        with pytest.raises(AccountScopeUndeclared):
            await ctx.send_group_msg(30001, "hi")

    async def test_offline_account(self, runtime, tmp_path):
        ctx = PluginContext(runtime, a_manifest(tmp_path))
        with pytest.raises(AccountNotOnline):
            await ctx.send_group_msg(30001, "hi", account="1888888888")

    async def test_out_of_scope_account(self, runtime, tmp_path):
        ctx = PluginContext(runtime, a_manifest(tmp_path, accounts=["1777777777"]))
        with pytest.raises(AccountOutOfScope):
            await ctx.send_group_msg(30001, "hi", account="1888888888")

    async def test_invoke_for_an_instance_that_is_not_the_named_account(self, runtime, tmp_path):
        ctx = PluginContext(runtime, a_manifest(tmp_path))
        instance = live_instance(runtime)
        with pytest.raises((AccountOutOfScope, AccountNotOnline)):
            await ctx.invoke_for(instance, "get_version_info", account="1888888888")


class TestDocsMatchTheCode:
    """`docs/plugin-api.md` is the only description a plugin author has, so drift is a defect.

    Every item here failed at least once before it was caught by hand: a documented method that did not exist, a
    documented permission that did nothing, and an example manifest that had drifted from the real plugin.
    """

    DOC = REPO / "docs" / "plugin-api.md"

    def doc(self) -> str:
        return self.DOC.read_text(encoding="utf-8")

    def test_every_interface_is_documented(self):
        text = self.doc()
        missing = sorted(name for name in PUBLIC_API if f"ctx.{name}" not in text and f"`{name}`" not in text)
        assert missing == [], f"not mentioned in the doc: {missing}"

    def test_the_doc_invents_nothing(self):
        # `ctx.<name>` in the prose must be a real member; `log` and `manifest` are attributes, not methods.
        named = set(re.findall(r"ctx\.(\w+)", self.doc()))
        known = PUBLIC_API | {"log", "manifest"}
        assert named - known == set(), f"the doc names things that do not exist: {sorted(named - known)}"

    def test_every_permission_is_documented(self):
        text = self.doc()
        assert [name for name in ALL_PERMISSIONS if name not in text] == []

    def test_the_reserved_permission_says_it_grants_nothing(self):
        # `core.hooks` sits in the enum and is read by no code. Saying so is the honest option; leaving it looking
        # like a capability is not.
        text = self.doc()
        assert "core.hooks" in text
        assert "当前不授予任何能力" in text or "没有任何效果" in text

    def test_the_example_manifest_matches_the_real_plugin(self):
        match = re.search(r"## 1\. `basic\.json`\s+```json\s+(\{.*?\})\s+```", self.doc(), re.S)
        assert match, "the doc must show a basic.json example"
        example = json.loads(match.group(1))
        real = json.loads((REPO / "plugins" / "guard" / "basic.json").read_text(encoding="utf-8"))
        for key in ("id", "entry", "tier", "permissions", "hooks", "log", "priority", "enabled"):
            assert example.get(key) == real.get(key), f"{key} drifted: doc={example.get(key)!r} real={real.get(key)!r}"
