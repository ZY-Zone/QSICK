"""The plugin contract: declared account scope, send permissions, kernel restrictions, log categories.

Four things are pinned here, each because the previous behaviour was wrong in a way that looked fine:

  * `messages.send` used to be a promise the API did not keep -- `send_*` went through `call()`, which requires
    `actions.invoke`, so a plugin declaring only `messages.send` was refused.
  * A plugin could not name an account, so it could only ever act as "whoever is primary this second".
  * Account-scoped calls have to say which accounts they use. Leaving `accounts` out means "account-agnostic
    observer", and `["ALL"]` is how a plugin asks for every account -- the two must not look alike.
  * A restriction that reaches past the plugin that set it is a core-tier privilege, because `tier` is where the
    trust decision already lives.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from qsick import log as logmod  # noqa: E402
from qsick.plugins.api import (  # noqa: E402
    AccountNotOnline,
    AccountOutOfScope,
    AccountScopeUndeclared,
    PermissionDenied,
    PluginContext,
)
from qsick.plugins.manifest import ALL_ACCOUNTS, ManifestError, PluginManifest  # noqa: E402


def a_manifest(tmp_path: Path, **overrides) -> PluginManifest:
    payload = {
        "id": "demo", "name": "Demo", "version": "1.0.0", "entry": "plugin.py:Demo",
        "tier": "standard",
        "permissions": ["messages.send", "actions.invoke", "runtime.read", "events.read"],
    }
    payload.update(overrides)
    path = tmp_path / "demo" / "basic.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return PluginManifest.load(path)


class FakeRuntime:
    """Records what a plugin asked for, and answers like the real runtime would."""

    def __init__(self, *, primary: str = "10001"):
        self.primary_account = primary
        self.calls: list[tuple[str, dict]] = []
        self.for_instance: list[tuple[str, str, dict]] = []
        self.registry = type("R", (), {"add": lambda self, spec, replace=False: None})()
        self.restrictions: dict[str, str] = {}

    async def invoke(self, action, params):
        self.calls.append((action, dict(params)))
        return {"status": "ok"}

    async def invoke_for(self, instance, action, params):
        self.for_instance.append((instance, action, dict(params)))
        return {"status": "ok"}

    #: Accounts the fake runtime considers logged in. Tests flip this to exercise the offline path.
    online: list[str] = ["10001", "20002"]

    def online_accounts(self):
        return [
            {"account": uin, "uin": uin, "nickname": uin, "instance": f"C{index}", "session": f"C{index}",
             "kind": "headless", "logged_in": True, "online": True, "kicked": False, "reason": ""}
            for index, uin in enumerate(self.online)
        ]

    def is_account_online(self, account):
        return str(account or "").strip() in self.online

    def is_instance_online(self, instance):
        return str(instance or "").strip() in [f"C{i}" for i in range(len(self.online))]

    def restrict_account(self, account, *, direction="both"):
        self.restrictions[str(account)] = direction
        return direction

    def unrestrict_account(self, account, *, direction="both"):
        return self.restrictions.pop(str(account), "")

    def account_restrictions(self):
        return dict(self.restrictions)

    def info(self):
        return {"ok": True}


class TestManifest:
    def test_an_account_list_is_read(self, tmp_path):
        assert a_manifest(tmp_path, accounts=["10001"]).accounts == ["10001"]

    def test_no_accounts_field_means_undeclared(self, tmp_path):
        manifest = a_manifest(tmp_path)
        assert manifest.accounts == []
        assert manifest.accounts_declared is False

    def test_all_is_the_explicit_catch_all(self, tmp_path):
        manifest = a_manifest(tmp_path, accounts=[ALL_ACCOUNTS])
        assert manifest.accounts_declared is True
        assert manifest.allows_all_accounts is True

    def test_all_is_accepted_in_any_case(self, tmp_path):
        assert a_manifest(tmp_path, accounts=["all"]).allows_all_accounts is True

    def test_all_cannot_be_mixed_with_numbers(self, tmp_path):
        # "everything and also 10001" has no meaning; silently picking one reading would be worse.
        with pytest.raises(ManifestError):
            a_manifest(tmp_path, accounts=[ALL_ACCOUNTS, "10001"])

    def test_an_instance_id_is_rejected_as_a_scope(self, tmp_path):
        # `C0` looks like a plausible scope but never equals a message's account, so it would match nothing.
        with pytest.raises(ManifestError):
            a_manifest(tmp_path, accounts=["C0"])

    def test_log_registration_is_opt_in(self, tmp_path):
        assert a_manifest(tmp_path).register_log_category is False

    def test_log_accepts_an_object(self, tmp_path):
        manifest = a_manifest(tmp_path, log={"register": True})
        assert manifest.register_log_category is True
        assert manifest.log_category_for_ui == "Plugin.demo"

    def test_log_accepts_a_bare_bool(self, tmp_path):
        assert a_manifest(tmp_path, log=True).register_log_category is True

    def test_a_named_category_is_used(self, tmp_path):
        manifest = a_manifest(tmp_path, log={"register": True, "category": "Guard"})
        assert manifest.log_category_for_ui == "Guard"

    def test_core_tier_is_recognised(self, tmp_path):
        assert a_manifest(tmp_path, tier="core").is_core is True
        assert a_manifest(tmp_path, tier="standard").is_core is False


class TestAccountScope:
    def ctx(self, tmp_path, **overrides) -> tuple[PluginContext, FakeRuntime]:
        runtime = FakeRuntime()
        return PluginContext(runtime, a_manifest(tmp_path, **overrides)), runtime

    def test_an_undeclared_plugin_observes_every_account(self, tmp_path):
        # Account-agnostic, not account-blind: a filter that never mentions accounts still sees messages.
        ctx, _ = self.ctx(tmp_path)
        assert ctx.account_allowed("10001") and ctx.account_allowed("99999")

    def test_all_allows_every_account(self, tmp_path):
        ctx, _ = self.ctx(tmp_path, accounts=[ALL_ACCOUNTS])
        assert ctx.account_allowed("10001") and ctx.account_allowed("99999")

    def test_a_declared_list_excludes_the_others(self, tmp_path):
        ctx, _ = self.ctx(tmp_path, accounts=["10001"])
        assert ctx.account_allowed("10001")
        assert not ctx.account_allowed("20002")

    def test_an_unknown_account_is_allowed(self, tmp_path):
        ctx, _ = self.ctx(tmp_path, accounts=["10001"])
        assert ctx.account_allowed("")

    def test_blocking_narrows_further(self, tmp_path):
        ctx, _ = self.ctx(tmp_path, accounts=[ALL_ACCOUNTS])
        ctx.block_account("20002")
        assert ctx.account_allowed("10001")
        assert not ctx.account_allowed("20002")
        ctx.unblock_account("20002")
        assert ctx.account_allowed("20002")

    def test_blocking_outside_the_scope_is_refused(self, tmp_path):
        ctx, _ = self.ctx(tmp_path, accounts=["10001"])
        with pytest.raises(AccountOutOfScope):
            ctx.block_account("20002")


class TestAccountsFieldIsRequired:
    """The account-scoped calls refuse when the manifest never said which accounts it uses."""

    def ctx(self, tmp_path, **overrides) -> tuple[PluginContext, FakeRuntime]:
        runtime = FakeRuntime()
        return PluginContext(runtime, a_manifest(tmp_path, **overrides)), runtime

    async def test_sending_requires_the_field(self, tmp_path):
        ctx, runtime = self.ctx(tmp_path)
        with pytest.raises(AccountScopeUndeclared):
            await ctx.send_group_msg(30001, "hi")
        with pytest.raises(AccountScopeUndeclared):
            await ctx.send_private_msg(20002, "hi")
        assert runtime.calls == []

    async def test_the_roster_requires_the_field(self, tmp_path):
        ctx, _ = self.ctx(tmp_path)
        with pytest.raises(AccountScopeUndeclared):
            ctx.accounts()

    async def test_invoke_for_requires_the_field(self, tmp_path):
        ctx, _ = self.ctx(tmp_path)
        with pytest.raises(AccountScopeUndeclared):
            await ctx.invoke_for("C1", "get_group_list")

    async def test_calling_with_an_account_requires_the_field(self, tmp_path):
        ctx, _ = self.ctx(tmp_path)
        with pytest.raises(AccountScopeUndeclared):
            await ctx.call("get_group_list", account="10001")

    async def test_blocking_requires_the_field(self, tmp_path):
        ctx, _ = self.ctx(tmp_path)
        with pytest.raises(AccountScopeUndeclared):
            ctx.block_account("10001")

    async def test_calling_without_an_account_does_not(self, tmp_path):
        # `call("get_group_list")` names no account, so it is not an account-scoped call.
        ctx, runtime = self.ctx(tmp_path)
        await ctx.call("get_group_list")
        assert runtime.calls[-1][0] == "get_group_list"

    async def test_the_error_says_how_to_fix_it(self, tmp_path):
        ctx, _ = self.ctx(tmp_path)
        with pytest.raises(AccountScopeUndeclared) as caught:
            await ctx.send_group_msg(30001, "hi")
        assert '"ALL"' in str(caught.value)


class TestSendPermissions:
    async def test_messages_send_alone_is_enough_to_send(self, tmp_path):
        # The documented contract: `messages.send` grants sending. It used to require `actions.invoke` as well.
        runtime = FakeRuntime()
        manifest = a_manifest(tmp_path, permissions=["messages.send"], accounts=[ALL_ACCOUNTS])
        await PluginContext(runtime, manifest).send_group_msg(30001, "hi")
        assert runtime.calls[-1][0] == "send_group_msg"

    async def test_sending_without_the_permission_is_refused(self, tmp_path):
        ctx = PluginContext(FakeRuntime(), a_manifest(tmp_path, permissions=[], accounts=[ALL_ACCOUNTS]))
        with pytest.raises(PermissionDenied):
            await ctx.send_group_msg(30001, "hi")

    async def test_an_account_is_carried_into_the_payload(self, tmp_path):
        runtime = FakeRuntime()
        ctx = PluginContext(runtime, a_manifest(tmp_path, accounts=["10001", "20002"]))
        await ctx.send_group_msg(30001, "hi", account="20002")
        assert runtime.calls[-1][1]["account"] == "20002"

    async def test_sending_through_an_unclaimed_account_is_refused(self, tmp_path):
        runtime = FakeRuntime()
        ctx = PluginContext(runtime, a_manifest(tmp_path, accounts=["10001"]))
        with pytest.raises(AccountOutOfScope):
            await ctx.send_group_msg(30001, "hi", account="20002")
        assert runtime.calls == []

    async def test_no_account_falls_back_to_the_primary(self, tmp_path):
        runtime = FakeRuntime(primary="10001")
        ctx = PluginContext(runtime, a_manifest(tmp_path, accounts=[ALL_ACCOUNTS]))
        await ctx.send_private_msg(20002, "hi")
        assert runtime.calls[-1][1]["account"] == "10001"

    async def test_the_primary_is_dropped_when_out_of_scope(self, tmp_path):
        runtime = FakeRuntime(primary="20002")
        ctx = PluginContext(runtime, a_manifest(tmp_path, accounts=["10001"]))
        await ctx.send_private_msg(20002, "hi")
        # Nothing is named rather than naming an account the plugin may not use.
        assert "account" not in runtime.calls[-1][1]

    async def test_invoke_for_uses_the_instance_not_the_primary(self, tmp_path):
        runtime = FakeRuntime(primary="10001")
        ctx = PluginContext(runtime, a_manifest(tmp_path, accounts=[ALL_ACCOUNTS]))
        await ctx.invoke_for("C1", "send_group_msg", group_id=1, message="hi")
        assert runtime.for_instance[-1][0] == "C1"

    async def test_call_threads_and_checks_the_account(self, tmp_path):
        runtime = FakeRuntime()
        ctx = PluginContext(runtime, a_manifest(tmp_path, accounts=["10001"]))
        await ctx.call("get_group_list", account="10001")
        assert runtime.calls[-1][1]["account"] == "10001"
        with pytest.raises(AccountOutOfScope):
            await ctx.call("get_group_list", account="20002")


class TestOnlineOnly:
    """The roster and the gate both use the accounts that are logged in right now.

    Not every registered instance: a stopped one, one that never signed in, and a network profile with no live
    login all look like accounts on disk and none of them can be reached. Handing them to a plugin would give it a
    target list it cannot use, and accepting them would defer the failure to a send nobody answers.
    """

    def ctx(self, tmp_path, online, **overrides):
        runtime = FakeRuntime()
        runtime.online = list(online)
        return PluginContext(runtime, a_manifest(tmp_path, accounts=[ALL_ACCOUNTS], **overrides)), runtime

    def test_the_roster_lists_only_online_accounts(self, tmp_path):
        ctx, _ = self.ctx(tmp_path, online=["10001"])
        assert [entry["account"] for entry in ctx.accounts()] == ["10001"]

    def test_an_empty_roster_is_a_valid_answer(self, tmp_path):
        ctx, _ = self.ctx(tmp_path, online=[])
        assert ctx.accounts() == []

    async def test_sending_to_a_signed_out_account_is_refused(self, tmp_path):
        ctx, runtime = self.ctx(tmp_path, online=["10001"])
        with pytest.raises(AccountNotOnline):
            await ctx.send_group_msg(30001, "hi", account="20002")
        assert runtime.calls == []

    async def test_the_refusal_names_what_is_available(self, tmp_path):
        ctx, _ = self.ctx(tmp_path, online=["10001"])
        with pytest.raises(AccountNotOnline) as caught:
            await ctx.send_private_msg(20002, "hi", account="20002")
        assert "10001" in str(caught.value)

    async def test_the_primary_is_dropped_when_it_is_signed_out(self, tmp_path):
        runtime = FakeRuntime(primary="10001")
        runtime.online = []
        ctx = PluginContext(runtime, a_manifest(tmp_path, accounts=[ALL_ACCOUNTS]))
        await ctx.send_group_msg(30001, "hi")
        # Nothing is named rather than naming an account that cannot answer.
        assert "account" not in runtime.calls[-1][1]

    async def test_invoke_for_a_signed_out_instance_is_refused(self, tmp_path):
        ctx, runtime = self.ctx(tmp_path, online=["10001"])
        with pytest.raises(AccountNotOnline):
            await ctx.invoke_for("C1", "get_group_list")
        assert runtime.for_instance == []

    async def test_invoke_for_an_online_instance_works(self, tmp_path):
        ctx, runtime = self.ctx(tmp_path, online=["10001", "20002"])
        await ctx.invoke_for("C1", "get_group_list")
        assert runtime.for_instance[-1][0] == "C1"

    async def test_scope_still_comes_first(self, tmp_path):
        # Two ways to be refused, and they must not be confused: an account outside the declaration is a scope
        # mistake, an account inside it that is signed out is an availability one.
        runtime = FakeRuntime()
        runtime.online = ["10001"]
        ctx = PluginContext(runtime, a_manifest(tmp_path, accounts=["10001", "99999"]))
        with pytest.raises(AccountOutOfScope):
            await ctx.send_group_msg(30001, "hi", account="20002")
        with pytest.raises(AccountNotOnline):
            await ctx.send_group_msg(30001, "hi", account="99999")


class TestKernelRestrictions:
    """A restriction that reaches past the plugin itself is reserved for core tier."""

    def ctx(self, tmp_path, **overrides) -> tuple[PluginContext, FakeRuntime]:
        runtime = FakeRuntime()
        return PluginContext(runtime, a_manifest(tmp_path, **overrides)), runtime

    def test_a_standard_plugin_cannot_restrict(self, tmp_path):
        ctx, runtime = self.ctx(tmp_path, tier="standard")
        with pytest.raises(PermissionDenied) as caught:
            ctx.restrict_account("10001")
        assert "core" in str(caught.value)
        assert runtime.restrictions == {}

    def test_a_sandbox_plugin_cannot_restrict(self, tmp_path):
        ctx, _ = self.ctx(tmp_path, tier="sandbox")
        with pytest.raises(PermissionDenied):
            ctx.restrict_account("10001")

    def test_a_core_plugin_can_restrict(self, tmp_path):
        ctx, runtime = self.ctx(tmp_path, tier="core")
        assert ctx.restrict_account("10001", direction="out") == "out"
        assert runtime.restrictions == {"10001": "out"}

    def test_reading_restrictions_is_also_core_only(self, tmp_path):
        ctx, _ = self.ctx(tmp_path, tier="standard")
        with pytest.raises(PermissionDenied):
            ctx.restrictions()
        core, runtime = self.ctx(tmp_path, tier="core")
        core.restrict_account("10001")
        assert core.restrictions() == runtime.restrictions

    def test_lifting_a_restriction_is_core_only(self, tmp_path):
        ctx, _ = self.ctx(tmp_path, tier="standard")
        with pytest.raises(PermissionDenied):
            ctx.unrestrict_account("10001")

    def test_restrictions_do_not_need_the_accounts_field(self, tmp_path):
        # A kernel restriction is about other callers, not about which accounts this plugin may use itself.
        ctx, runtime = self.ctx(tmp_path, tier="core")
        ctx.restrict_account("99999", direction="in")
        assert runtime.restrictions == {"99999": "in"}


class TestRuntimeRestrictionState:
    """The state machine behind the kernel gate, exercised on the real runtime object."""

    def a_runtime(self, tmp_path):
        from qsick.config import RuntimeConfig
        from qsick.runtime import Runtime

        config = RuntimeConfig(data_dir=str(tmp_path / "data"))
        config.transport.mode = "mock"
        config.plugins.dirs = []
        return Runtime(config)

    def test_directions_merge_rather_than_replace(self, tmp_path):
        runtime = self.a_runtime(tmp_path)
        runtime.restrict_account("10001", direction="in")
        assert runtime.restrict_account("10001", direction="out") == "both"
        assert runtime.account_restricted("10001", "in")
        assert runtime.account_restricted("10001", "out")

    def test_one_direction_can_be_lifted(self, tmp_path):
        runtime = self.a_runtime(tmp_path)
        runtime.restrict_account("10001", direction="both")
        assert runtime.unrestrict_account("10001", direction="in") == "out"
        assert not runtime.account_restricted("10001", "in")
        assert runtime.account_restricted("10001", "out")

    def test_lifting_everything_clears_the_entry(self, tmp_path):
        runtime = self.a_runtime(tmp_path)
        runtime.restrict_account("10001", direction="out")
        assert runtime.unrestrict_account("10001") == ""
        assert runtime.account_restrictions() == {}
        assert not runtime.account_restricted("10001", "out")

    def test_an_unknown_account_is_not_restricted(self, tmp_path):
        runtime = self.a_runtime(tmp_path)
        assert not runtime.account_restricted("", "out")
        assert not runtime.account_restricted("10001", "out")

    def test_a_bad_direction_is_refused(self, tmp_path):
        runtime = self.a_runtime(tmp_path)
        with pytest.raises(ValueError):
            runtime.restrict_account("10001", direction="sideways")

    def test_a_non_numeric_account_is_refused(self, tmp_path):
        runtime = self.a_runtime(tmp_path)
        with pytest.raises(ValueError):
            runtime.restrict_account("C0")


class TestLogCategories:
    def test_core_categories_are_present_before_anything_logs(self):
        names = {entry["category"] for entry in logmod.known_categories()}
        assert {"Plugins", "OneBot.WS", "OneBot.HTTP"} <= names

    def test_the_plugin_category_is_registered_when_declared(self, tmp_path):
        manifest = a_manifest(tmp_path, log={"register": True})
        logmod.register_category(manifest.log_category_for_ui, source="plugin")
        try:
            entries = {entry["category"]: entry for entry in logmod.known_categories()}
            assert entries["Plugin.demo"]["source"] == "plugin"
        finally:
            logmod.unregister_category("Plugin.demo")
        assert "Plugin.demo" not in {entry["category"] for entry in logmod.known_categories()}

    def test_core_categories_cannot_be_removed(self):
        logmod.unregister_category("Plugins")
        assert "Plugins" in {entry["category"] for entry in logmod.known_categories()}

    def test_ordering_follows_the_group_order(self):
        # The filter renders by group, so the list has to arrive in that order: core, dll, plugin, net. A plugin
        # category landed among the core ones would sit under the wrong heading.
        logmod.register_category("Plugin.zzz", source="plugin")
        try:
            order = [entry["group"] for entry in logmod.known_categories()]
            ranks = {name: index for index, name in enumerate(logmod.GROUP_ORDER)}
            assert order == sorted(order, key=lambda name: ranks.get(name, 99))
        finally:
            logmod.unregister_category("Plugin.zzz")

    def test_a_plugin_category_is_always_filed_under_plugins(self):
        # A plugin does not get to file itself under "core" whatever it asks for.
        logmod.register_category("Plugin.sneaky", source="plugin", group="core")
        try:
            entry = next(item for item in logmod.known_categories()
                         if item["category"] == "Plugin.sneaky")
            assert entry["group"] == "plugin"
        finally:
            logmod.unregister_category("Plugin.sneaky")

    def test_grouped_categories_drops_empty_groups(self):
        # An <optgroup> with nothing under it is a dead heading.
        for item in logmod.grouped_categories():
            assert item["categories"], f"{item['group']} rendered with no entries"

    def test_the_four_groups_are_the_documented_ones(self):
        assert set(logmod.GROUPS) == {"core", "dll", "plugin", "net"}
        assert set(logmod.GROUP_ORDER) == set(logmod.GROUPS)


class TestConnectionLogs:
    """WS and HTTP connection events are their own categories, so one can be filtered without the other."""

    def test_the_two_categories_are_separate_loggers(self):
        assert logmod.get_logger("OneBot.WS").category == "OneBot.WS"
        assert logmod.get_logger("OneBot.HTTP").category == "OneBot.HTTP"

    async def test_http_listening_is_logged_when_the_listener_binds(self, tmp_path):
        from qsick.config import RuntimeConfig
        from qsick.runtime import Runtime

        config = RuntimeConfig(data_dir=str(tmp_path / "data"))
        config.transport.mode = "mock"
        config.plugins.dirs = []
        config.forward_noise = False
        config.rkey_url = ""
        # Every listener on port 0: the suite may run beside a real QSICK, and a fixed port collides with it.
        config.onebot.port = 0
        config.onebot.enable_sse = True
        config.webui.port = 0
        config.message_cache.port = 0
        runtime = Runtime(config)
        await runtime.start(serve=True)
        try:
            await runtime.restart_onebot()
            seen = [record for record in logmod.BUFFER.tail(400)
                    if record.category == "OneBot.HTTP" and record.message == "onebot listener ready"]
            assert seen, "binding the OneBot listener must leave a OneBot.HTTP line"
            assert "endpoints" in seen[-1].fields
        finally:
            await runtime.stop()


class TestAgainstTheRealRuntime:
    """The fake runtime answers what it is told; these check the wiring it stands in for."""

    async def test_the_roster_comes_from_the_running_runtime(self, runtime, tmp_path):
        manifest = a_manifest(tmp_path, accounts=[ALL_ACCOUNTS])
        roster = PluginContext(runtime, manifest).accounts()
        assert roster, "the roster must reflect the runtime's instances, not a placeholder"
        assert all({"instance", "account", "allowed"} <= set(entry) for entry in roster)

    async def test_the_primary_account_is_readable(self, runtime):
        assert isinstance(runtime.primary_account, str)

    async def test_an_inbound_restriction_stops_the_message_before_the_plugins(self, runtime):
        from qsick.protocol.message import Message

        message = Message.from_payload({
            "message_id": 7, "chat_type": "group", "chat_id": 30001,
            "elements": [{"type": "text", "data": {"text": "hi"}}], "account": "10001",
        })

        class FakeEvent:
            cmd = "message.group"

            @staticmethod
            def json():
                return message.to_dict()

        runtime.restrict_account("10001", direction="in")
        try:
            before = runtime.events_dropped
            await runtime._dispatch_event(FakeEvent())
            assert runtime.events_dropped == before + 1
        finally:
            runtime.unrestrict_account("10001")
