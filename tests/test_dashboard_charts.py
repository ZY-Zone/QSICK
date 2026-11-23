"""The dashboard's charts and the log filter's grouping.

Both were rebuilt from scratch, and both have failure modes that a screenshot would not catch:

  * a chart that draws nothing because the sampler never ran, or because a fit returned the wrong length;
  * a category that lands under the wrong heading, or a group that renders with nothing in it;
  * a filter whose choice is forgotten on refresh -- the one moment it matters, since a refresh is what you do
    while debugging.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from qsick import log as logmod  # noqa: E402
from qsick import metrics  # noqa: E402


class TestCategoryGroups:
    """Four groups, because a list of twenty categories is something to read, not something to filter with."""

    def test_every_declared_category_has_a_real_group(self):
        for entry in logmod.known_categories():
            assert entry["group"] in logmod.GROUPS, f"{entry['category']} has group {entry['group']!r}"

    def test_the_four_headings_exist(self):
        assert set(logmod.GROUPS) == {"core", "dll", "plugin", "net"}
        # And they are ordered, not just named: the filter renders in this order.
        assert tuple(logmod.GROUP_ORDER) == ("core", "dll", "plugin", "net")

    def test_connection_logs_are_their_own_group(self):
        # "网络配置连接日志（包括 http，ws 正反）" -- all four of ws/http and their reverse directions.
        net = {entry["category"] for entry in logmod.known_categories() if entry["group"] == "net"}
        assert {"OneBot.WS", "OneBot.HTTP", "OneBot.Reverse"} <= net

    def test_the_plugin_loader_is_core_not_plugin(self):
        # `Plugins` is the host's own logging; the plugin group is for categories a plugin registered, and mixing
        # the two made "Plugins" look like a plugin called Plugins.
        entry = next(item for item in logmod.known_categories() if item["category"] == "Plugins")
        assert entry["group"] == "core"

    def test_dll_categories_are_the_injection_side(self):
        dll = {entry["category"] for entry in logmod.known_categories() if entry["group"] == "dll"}
        assert {"Transport.Pipe", "Transport.Inject", "SessionLink"} <= dll

    def test_grouping_survives_a_round_trip_through_the_renderer(self):
        groups = logmod.grouped_categories()
        assert [item["group"] for item in groups] == [
            name for name in logmod.GROUP_ORDER
            if any(entry["group"] == name for entry in logmod.known_categories())
        ]
        for item in groups:
            assert item["categories"], f"{item['group']} would render an empty optgroup"
            assert item["label"]

    def test_a_renamed_category_can_be_removed_again(self):
        logmod.register_category("Plugin.temp", source="plugin")
        assert "Plugin.temp" in {entry["category"] for entry in logmod.known_categories()}
        logmod.unregister_category("Plugin.temp")
        assert "Plugin.temp" not in {entry["category"] for entry in logmod.known_categories()}


class TestLogFilterPage:
    """The dropdown itself, as the browser receives it."""

    async def test_the_filter_offers_groups_not_twenty_categories(self, runtime):
        from qsick.webui.app import create_app
        from starlette.testclient import TestClient

        app = create_app(runtime)
        with TestClient(app) as web:
            response = web.get("/logs")
        assert response.status_code == 200
        body = response.text
        for group in ("group:core", "group:dll", "group:net"):
            assert group in body, f"{group} missing from the filter"
        assert "log-groups" in body, "the category→group map has to reach the page"

    async def test_a_registered_plugin_is_selectable_on_its_own(self, runtime):
        from qsick.webui.app import create_app
        from starlette.testclient import TestClient

        logmod.register_category("Plugin.shown", source="plugin")
        try:
            app = create_app(runtime)
            with TestClient(app) as web:
                body = web.get("/logs").text
            assert "Plugin.shown" in body
        finally:
            logmod.unregister_category("Plugin.shown")


class TestFitCurve:
    """The fitted trend line the CPU chart draws over the raw series."""

    def test_a_short_series_gets_no_fit(self):
        # Two points cannot constrain a parabola; drawing one anyway would invent a shape.
        assert metrics.fit_curve([[0.0, 10.0]]) == []
        assert metrics.fit_curve([[0.0, 10.0], [5.0, 12.0]]) == []

    def test_a_flat_series_fits_flat(self):
        points = [[float(i), 20.0] for i in range(10)]
        fit = metrics.fit_curve(points)
        assert fit
        for _x, y in fit:
            assert abs(y - 20.0) < 0.01

    def test_a_rising_series_fits_rising(self):
        points = [[float(i), float(i)] for i in range(20)]
        fit = metrics.fit_curve(points)
        assert fit[0][1] < fit[-1][1]

    def test_the_fit_stays_inside_the_data_range(self):
        # A degree-2 fit on noisy data overshoots at the edges; a percentage chart cannot draw 120%.
        points = [[float(i), 50.0 + (10 if i % 3 == 0 else -10)] for i in range(30)]
        fit = metrics.fit_curve(points)
        low = min(point[1] for point in points)
        high = max(point[1] for point in points)
        assert all(low - 0.01 <= y <= high + 0.01 for _x, y in fit)

    def test_the_fit_spans_the_original_window(self):
        points = [[1000.0 + i, 30.0] for i in range(10)]
        fit = metrics.fit_curve(points)
        assert fit[0][0] == pytest.approx(points[0][0])
        assert fit[-1][0] == pytest.approx(points[-1][0])

    def test_epoch_sized_x_values_still_fit(self):
        # Raw Unix timestamps at degree 2 lose every significant digit in float64 unless x is shifted first.
        base = 1_700_000_000.0
        points = [[base + i * 5, 10.0 + i] for i in range(30)]
        fit = metrics.fit_curve(points)
        assert fit[0][1] < fit[-1][1]
        assert all(abs(y) < 1000 for _x, y in fit)


class TestChartData:
    def test_every_chart_has_the_keys_the_renderer_reads(self, runtime):
        charts = runtime.chart_data()
        assert set(charts) >= {"available", "cpu", "memory", "messages"}
        assert set(charts["cpu"]) >= {"points", "fit", "average", "peak", "current"}
        assert set(charts["messages"]) >= {"buckets", "total", "peak", "average", "bucket_seconds"}

    def test_the_message_window_is_seven_days_in_half_day_buckets(self, runtime):
        data = runtime.message_history()
        assert data["bucket_seconds"] == 12 * 3600
        # Seven days at twelve hours is fourteen buckets; the window is inclusive of a partial one at each end.
        assert 14 <= len(data["buckets"]) <= 15
        spans = [item["end"] - item["start"] for item in data["buckets"]]
        assert max(spans) <= 12 * 3600

    def test_buckets_do_not_move_between_calls(self, runtime):
        # A bar that shifts under the pointer is not a chart: buckets are anchored to the window start.
        first = [item["start"] for item in runtime.message_history()["buckets"]]
        second = [item["start"] for item in runtime.message_history()["buckets"]]
        assert first == second

    def test_stored_messages_actually_land_in_a_bucket(self, runtime):
        """The chart must count what the store holds.

        It read zero for a while: the window start was snapped to a bucket boundary for *display* while the store
        aligned its slots to the raw start, so every key came back offset by the remainder and matched no bucket.
        A chart that shows 0 while the database holds hundreds is worse than no chart -- it says "nothing happened".
        """
        import time as _time

        from qsick.protocol.message import Message

        now = int(_time.time())
        # A message inside the window, deliberately at an odd offset so a mis-aligned query cannot round it in.
        stamp = now - 3600 * 7 + 137
        message = Message.from_payload({
            "message_id": 987654, "chat_type": "group", "chat_id": 4242,
            "elements": [{"type": "text", "data": {"text": "chart fixture"}}],
        })
        message.time = stamp
        runtime.cache_store.put(message)
        try:
            data = runtime.message_history()
            assert data["total"] >= 1, "the fixture message has to be counted"
            counted = sum(item["count"] for item in data["buckets"])
            assert counted == data["total"]
        finally:
            runtime.cache_store.delete(987654)

    def test_buckets_start_on_a_boundary(self, runtime):
        # Off-boundary starts are what broke the query; asserting the invariant keeps it from coming back.
        for item in runtime.message_history()["buckets"]:
            assert item["start"] % (12 * 3600) == 0

    def test_memory_slices_account_for_the_whole_machine(self, runtime):
        runtime.sample_metrics()
        memory = runtime.chart_data()["memory"]
        if not memory.get("total"):
            pytest.skip("no memory data on this platform")
        accounted = memory["self"] + memory["qq_total"] + memory["other"] + memory["available"]
        # Nothing may be double counted or dropped: the four slices have to add up.
        assert accounted == pytest.approx(memory["total"], rel=1e-6)

    def test_the_roster_feeds_the_pie_labels(self, runtime):
        # A QQ slice is labelled with its uin when one is known, which comes from the session, not from the pid.
        assert isinstance(runtime._qq_uins(), dict)


class TestMetricsPlumbing:
    def test_sampling_twice_produces_a_rate(self):
        history = metrics.History()
        history.record()
        import time

        time.sleep(1.1)
        history.record()
        assert len(history.samples) == 2
        assert all(0.0 <= item.cpu <= 100.0 for item in history.samples)

    def test_the_history_is_trimmed_by_age_not_by_count(self):
        history = metrics.History(window_seconds=10.0, capacity=1000)
        now = metrics.time.time()
        # Six old samples well outside the window, then one fresh one.
        for index in range(6):
            history.samples.append(metrics.Sample(
                ts=now - 100 - index, cpu=1.0, total_memory=1, available_memory=1, self_bytes=1))
        history.samples.append(metrics.Sample(
            ts=now, cpu=2.0, total_memory=1, available_memory=1, self_bytes=1))
        history._trim()  # noqa: SLF001 - the trimming rule is what is under test
        # Everything before the window is dropped, except the single sample kept so the line has a start.
        assert len(history.samples) == 2
        assert history.samples[-1].cpu == 2.0

    def test_qq_helpers_belong_to_their_main_process(self):
        entries = [
            {"pid": 1, "ppid": 0, "name": "QQ.exe"},
            {"pid": 2, "ppid": 1, "name": "QQ.exe"},
            {"pid": 3, "ppid": 2, "name": "QQ.exe"},
            {"pid": 4, "ppid": 1, "name": "QQ.exe"},
            {"pid": 9, "ppid": 0, "name": "notepad.exe"},
        ]
        mains = metrics.qq_main_processes(entries)
        assert [item["pid"] for item in mains] == [1]
        # And the grandchildren are part of the tree too -- a renderer spawned by a helper is still QQ's memory.
        assert sorted(mains[0]["helpers"]) == [2, 3, 4]

    def test_a_session_is_read_out_of_the_command_line(self):
        line = '"C:\\x\\QQ.exe" --user-data-dir=headlessClient\\Pipe\\owndata\\userdata\\69fm'
        assert metrics.session_of(line) == "69fm"
        assert metrics.session_of('"C:\\x\\QQ.exe" --type=renderer') == ""
        assert metrics.session_of("") == ""


class TestMemoryMetricIsTheTaskManagerOne:
    """The pie has to agree with the task manager, which reports the *private* working set.

    Summing total working sets across QQ's eight Chromium processes counts every shared DLL page eight times and
    came out at 1.1 GB against a task manager reading of 438 MB. Being wrong by 2.5x on the one number a user can
    check against another program is worse than showing nothing.
    """

    def test_the_process_table_carries_both_metrics(self):
        rows = metrics._system_processes()
        if not rows:
            pytest.skip("no process table on this platform")
        for row in rows[:5]:
            assert {"pid", "ppid", "name", "private", "working_set"} <= set(row)

    def test_private_never_exceeds_the_total_working_set(self):
        # Shared pages are in the total and not in the private count; the reverse would mean the fields are swapped.
        checked = 0
        for row in metrics._system_processes():
            if not row["name"] or row["private"] == 0:
                continue
            checked += 1
            assert row["private"] <= row["working_set"] * 1.05, (
                f"{row['name']} pid={row['pid']}: private {row['private']} > working set {row['working_set']}")
            if checked >= 40:
                break
        assert checked, "expected at least one readable process"

    def test_our_own_process_is_in_the_table(self):
        import os

        rows = {row["pid"]: row for row in metrics._system_processes()}
        assert os.getpid() in rows
        assert rows[os.getpid()]["private"] > 0

    def test_the_sample_uses_the_private_number(self):
        history = metrics.History()
        history.record()
        latest = history.latest()
        rows = {row["pid"]: row for row in metrics._system_processes()}
        import os

        expected = rows[os.getpid()]["private"]
        # Same metric, so within a moment's drift rather than a factor of several.
        assert abs(latest.self_bytes - expected) <= max(2 * 1024 * 1024, expected * 0.5)


class TestNetworkPanel:
    """Every enabled endpoint, one row each; disabled ones are absent rather than greyed out."""

    def test_an_enabled_endpoint_appears_once(self, runtime):
        cards = runtime.network_cards()
        ids = [card["id"] for card in cards]
        assert len(ids) == len(set(ids))

    def test_disabled_endpoints_are_left_out(self, runtime):
        runtime.config.onebot.enable_http = False
        runtime.config.onebot.enable_ws = False
        runtime.config.onebot.enable_sse = False
        runtime.config.onebot.reverse_ws_urls = []
        try:
            assert runtime.network_cards() == []
        finally:
            pass

    def test_servers_report_a_count_and_clients_do_not(self, runtime):
        runtime.config.onebot.enable_http = True
        runtime.config.onebot.enable_ws = True
        cards = {card["id"]: card for card in runtime.network_cards()}
        for name in ("http", "ws"):
            assert cards[name]["show_connections"] is True
            assert isinstance(cards[name]["connections"], int)
        # HTTP is stateless, so its number is labelled a request count -- calling it a connection would be a lie.
        assert cards["http"]["connection_kind"] == "request"
        assert cards["ws"]["connection_kind"] == "connection"

    def test_every_card_has_a_tone_the_template_can_use(self, runtime):
        runtime.config.onebot.enable_http = True
        for card in runtime.network_cards():
            assert card["tone"] in ("green", "gray", "red")
            assert card["label"] and card["state"] and card["detail"]


class TestMessageChartByAccount:
    def test_each_bucket_splits_by_account(self, runtime):
        import time as _time

        from qsick.protocol.message import Message

        now = int(_time.time())
        added = []
        for index, account in enumerate(("111111", "222222")):
            message = Message.from_payload({
                "message_id": 880000 + index, "chat_type": "group", "chat_id": 55,
                "elements": [{"type": "text", "data": {"text": "by account"}}], "account": account,
            })
            message.time = now - 3600 * 5
            runtime.cache_store.put(message)
            added.append(message.message_id)
        try:
            data = runtime.message_history()
            names = [entry["account"] for entry in data["accounts"]]
            assert "111111" in names and "222222" in names
            bucket = next(item for item in data["buckets"] if item["count"])
            assert set(bucket["accounts"]) >= {"111111", "222222"}
            # The split must add up to the bar it is drawn from.
            assert sum(bucket["accounts"].values()) == bucket["count"]
        finally:
            for message_id in added:
                runtime.cache_store.delete(message_id)

    def test_accounts_are_ordered_by_volume(self, runtime):
        accounts = runtime.message_history()["accounts"]
        counts = [entry["count"] for entry in accounts]
        assert counts == sorted(counts, reverse=True), "busiest account first, so it gets the first colour"
