"""The plugin contract: account scope, send permissions, and declared log categories.

Three things are pinned here, each because the previous behaviour was wrong in a way that looked fine:

  * `messages.send` used to be a promise the API did not keep -- `send_*` went through `call()`, which requires
    `actions.invoke`, so a plugin declaring only `messages.send` was refused.
  * A plugin could not name an account, and `call()` could not tell whether it was allowed to touch one.
  * The log category filter was filled from observed records, so a plugin's category appeared only after it logged
    -- and a plugin that had not logged yet is exactly when you want to select it.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from qsick import log as logmod  # noqa: E402
from qsick.plugins.api import AccountOutOfScope, PermissionDenied, PluginContext  # noqa: E402
from qsick.plugins.manifest import ManifestError, PluginManifest  # noqa: E402


def a_manifest(tmp_path: Path, **overrides) -> PluginManifest:
    payload = {
        "id": "demo", "name": "Demo", "version": "1.0.0", "entry": "plugin.py:Demo",
        "tier": "standard", "permissions": ["messages.send", "actions.invoke", "runtime.read"],
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

    async def invoke(self, action, params):
        self.calls.append((action, dict(params)))
        return {"status": "ok"}

    async def invoke_for(self, instance, action, params):
        self.for_instance.append((instance, action, dict(params)))
        return {"status": "ok"}

    def instances_online(self):
        return [
            {"instance": "C0", "uin": "10001", "nickname": "a", "logged_in": True, "online": True},
            {"instance": "C1", "uin": "20002", "nickname": "b", "logged_in": True, "online": True},
        ]

    def config(self):
        raise AssertionError("not used here")

    def info(self):
        return {"ok": True}


class TestManifest:
    def test_accounts_scope_is_read(self, tmp_path):
        assert a_manifest(tmp_path, accounts=["10001"]).accounts == ["10001"]

    def test_no_accounts_means_all_accounts(self, tmp_path):
        assert a_manifest(tmp_path).accounts == []

    def test_an_instance_id_is_rejected_as_a_scope(self, tmp_path):
        # `C0` looks like a plausible scope but never equals a message's account, so it would silently match nothing.
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

    def test_the_category_name_defaults_to_the_plugin_id(self, tmp_path):
        assert a_manifest(tmp_path).log_category_for_ui == "Plugin.demo"


class TestAccountScope:
    def ctx(self, tmp_path, **overrides) -> tuple[PluginContext, FakeRuntime]:
        runtime = FakeRuntime()
        return PluginContext(runtime, a_manifest(tmp_path, **overrides)), runtime

    def test_an_undeclared_scope_allows_every_account(self, tmp_path):
        ctx, _ = self.ctx(tmp_path)
        assert ctx.account_allowed("10001") and ctx.account_allowed("99999")

    def test_a_declared_scope_excludes_the_others(self, tmp_path):
        ctx, _ = self.ctx(tmp_path, accounts=["10001"])
        assert ctx.account_allowed("10001")
        assert not ctx.account_allowed("20002")

    def test_an_unknown_account_is_allowed(self, tmp_path):
        # Synthetic events and transports that report no account must not vanish on a scope check.
        ctx, _ = self.ctx(tmp_path, accounts=["10001"])
        assert ctx.account_allowed("")

    def test_blocking_narrows_further(self, tmp_path):
        ctx, _ = self.ctx(tmp_path, accounts=["10001", "20002"])
        ctx.block_account("20002")
        assert ctx.account_allowed("10001")
        assert not ctx.account_allowed("20002")
        ctx.unblock_account("20002")
        assert ctx.account_allowed("20002")

    def test_blocking_outside_the_scope_is_refused(self, tmp_path):
        ctx, _ = self.ctx(tmp_path, accounts=["10001"])
        with pytest.raises(AccountOutOfScope):
            ctx.block_account("20002")

    def test_the_roster_shows_accounts_it_cannot_touch(self, tmp_path):
        ctx, _ = self.ctx(tmp_path, accounts=["10001"])
        roster = ctx.accounts()
        assert [entry["account"] for entry in roster] == ["10001", "20002"]
        assert [entry["allowed"] for entry in roster] == [True, False]

    def test_the_roster_needs_runtime_read(self, tmp_path):
        ctx, _ = self.ctx(tmp_path, permissions=[])
        with pytest.raises(PermissionDenied):
            ctx.accounts()


class TestSendPermissions:
    async def test_messages_send_alone_is_enough_to_send(self, tmp_path):
        # The documented contract: `messages.send` grants sending. It used to require `actions.invoke` as well.
        runtime = FakeRuntime()
        ctx = PluginContext(runtime, a_manifest(tmp_path, permissions=["messages.send"]))
        await ctx.send_group_msg(30001, "hi")
        assert runtime.calls[-1][0] == "send_group_msg"

    async def test_sending_without_the_permission_is_refused(self, tmp_path):
        ctx = PluginContext(FakeRuntime(), a_manifest(tmp_path, permissions=[]))
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
        ctx = PluginContext(runtime, a_manifest(tmp_path))
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
        ctx = PluginContext(runtime, a_manifest(tmp_path))
        await ctx.invoke_for("C1", "send_group_msg", group_id=1, message="hi")
        assert runtime.for_instance[-1][0] == "C1"

    async def test_call_threads_and_checks_the_account(self, tmp_path):
        runtime = FakeRuntime()
        ctx = PluginContext(runtime, a_manifest(tmp_path, accounts=["10001"]))
        await ctx.call("get_group_list", account="10001")
        assert runtime.calls[-1][1]["account"] == "10001"
        with pytest.raises(AccountOutOfScope):
            await ctx.call("get_group_list", account="20002")


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

    def test_plugins_sort_after_core(self, tmp_path):
        logmod.register_category("Plugin.zzz", source="plugin")
        try:
            sources = [entry["source"] for entry in logmod.known_categories()]
            assert sources == sorted(sources), "core entries must come before plugin entries"
        finally:
            logmod.unregister_category("Plugin.zzz")


class TestConnectionLogs:
    """WS and HTTP connection events are their own categories, so one can be filtered without the other.

    Both used to log under `OneBot.Server`, which meant "watch the websocket" required reading past every http
    rejection in the same view.
    """

    def test_the_two_categories_are_separate_loggers(self):
        ws = logmod.get_logger("OneBot.WS")
        http = logmod.get_logger("OneBot.HTTP")
        assert ws.category == "OneBot.WS"
        assert http.category == "OneBot.HTTP"

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

    async def test_the_roster_comes_from_the_running_runtime(self, runtime):
        from qsick.plugins.api import PluginContext
        from qsick.plugins.manifest import PluginManifest

        manifest = PluginManifest.load(REPO / "plugins" / "guard" / "basic.json")
        ctx = PluginContext(runtime, manifest)
        roster = ctx.accounts()
        # The mock transport logs in one account, so there is a real entry to read.
        assert roster, "the roster must reflect the runtime's instances, not a placeholder"
        assert all({"instance", "account", "allowed"} <= set(entry) for entry in roster)

    async def test_the_primary_account_is_readable(self, runtime):
        assert isinstance(runtime.primary_account, str)

    async def test_a_declared_scope_filters_the_real_dispatch(self, runtime):
        # guard declares no accounts, so it sees everything; narrowing it must stop the hook from running.
        loaded = runtime.plugins._loaded["guard"]
        loaded.manifest.accounts = ["123456789"]
        original = runtime.plugins._loaded["guard"].ctx._blocked
        try:
            from qsick.protocol.message import Message

            message = Message.from_payload({
                "message_id": 1, "chat_type": "group", "chat_id": 30001,
                "elements": [{"type": "text", "data": {"text": "hi"}}], "account": "10001",
            })
            # Out of scope, so the guard's whitelist rule never runs and the message survives.
            assert await runtime.plugins.dispatch_message(message) is True
        finally:
            loaded.manifest.accounts = []
            loaded.ctx._blocked = original
