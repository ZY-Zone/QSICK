"""设置页的其余部分：新设置项、配置下载、滚动位置，以及一个曾经污染真实配置的写回路径。

`_write_setting` 的回退分支是这一组里最重要的：它曾经在没有 `source_path` 时写当前工作目录的
`qsick.json`，于是每跑一遍测试就覆盖一次开发者本机的配置。消息保留里的 1234/3 就是这么来的
（`test_webui_ui.py` 往 /settings/storage post 了这两个数字）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from qsick.webui.app import create_app  # noqa: E402

STATIC = REPO / "src" / "qsick" / "webui" / "static"
TEMPLATES = REPO / "src" / "qsick" / "webui" / "templates"


class TestWriteSettingDoesNotTouchTheWorkingTree:
    def test_a_runtime_without_a_source_writes_into_its_own_data_dir(self, runtime, tmp_path):
        """没有 source_path 时必须落到自己的数据目录，而不是仓库根目录的 qsick.json。

        这是回归测试：原来的回退值是 "qsick.json"，测试每跑一次都会改写开发者本机的真实配置。
        """
        stray = Path.cwd() / "qsick.json"
        before = stray.read_bytes() if stray.exists() else None

        with TestClient(create_app(runtime)) as web:
            response = web.post("/settings/storage", data={"max_messages": "77", "max_days": "2"},
                                follow_redirects=False)
            assert response.status_code == 303

        after = stray.read_bytes() if stray.exists() else None
        assert after == before, "测试不该改写仓库根目录的 qsick.json"

        written = runtime.config.path("qsick.json")
        assert written.exists(), "设置应该落在运行时自己的数据目录里"
        data = json.loads(written.read_text(encoding="utf-8"))
        assert data["rotation"]["max_messages"] == 77


class TestNewSettings:
    def test_general_offers_startup_and_forwarding(self, runtime):
        with TestClient(create_app(runtime)) as web:
            body = web.get("/settings?tab=general").text
        assert "启动与转发" in body
        assert "pipe_auto_start" in body
        assert "network_activation_delay" in body
        assert "forward_noise" in body

    def test_saving_them_clamps_the_delay(self, runtime):
        with TestClient(create_app(runtime)) as web:
            web.post("/settings/transport",
                     data={"pipe_auto_start": "1", "network_activation_delay": "999999",
                           "forward_noise": "1"},
                     follow_redirects=False)
        # 一小时以上的等待只会让"网络怎么还没起来"变成一个查不明白的问题。
        assert runtime.config.network_activation_delay == 600.0
        assert runtime.config.pipe_auto_start is True
        assert runtime.config.forward_noise is True

    def test_a_negative_delay_becomes_zero(self, runtime):
        with TestClient(create_app(runtime)) as web:
            web.post("/settings/transport", data={"network_activation_delay": "-30"},
                     follow_redirects=False)
        assert runtime.config.network_activation_delay == 0.0

    def test_display_offers_log_buffer_and_density(self, runtime):
        with TestClient(create_app(runtime)) as web:
            body = web.get("/settings?tab=display").text
        assert "日志缓冲" in body
        assert "log_buffer" in body
        assert 'id="density-picker"' in body

    def test_the_log_buffer_is_clamped(self, runtime):
        with TestClient(create_app(runtime)) as web:
            web.post("/settings/log-buffer", data={"log_buffer": "999999"}, follow_redirects=False)
        assert runtime.config.webui.log_buffer == 20000

    def test_advanced_offers_plugin_policy_and_log_level(self, runtime):
        with TestClient(create_app(runtime)) as web:
            body = web.get("/settings?tab=advanced").text
        assert "allow_remote_install" in body
        assert "max_tier" in body
        assert "日志级别" in body

    def test_an_unknown_tier_is_refused(self, runtime):
        with TestClient(create_app(runtime)) as web:
            response = web.post("/settings/plugins", data={"max_tier": "whatever"},
                                follow_redirects=False)
        assert response.status_code == 303
        assert runtime.config.plugins.max_tier == "core", "非法值不该被写进去"


class TestConfigDownload:
    def test_it_serves_a_real_json_object_as_an_attachment(self, runtime):
        with TestClient(create_app(runtime)) as web:
            response = web.get("/settings/config.json")
        assert response.status_code == 200
        assert "attachment" in response.headers["content-disposition"]
        payload = response.json()
        assert isinstance(payload, dict) and payload

    def test_the_download_is_the_sanitized_form(self, runtime):
        runtime.config.onebot.access_token = "super-secret-token"
        try:
            with TestClient(create_app(runtime)) as web:
                body = web.get("/settings/config.json").text
        finally:
            runtime.config.onebot.access_token = ""
        assert "super-secret-token" not in body

    def test_the_link_points_at_the_endpoint_that_exists(self, runtime):
        with TestClient(create_app(runtime)) as web:
            body = web.get("/settings?tab=advanced").text
        assert 'href="/settings/config.json"' in body
        # `/config` 这个页面从来没有存在过，别再挂回去。
        assert 'href="/config' not in body


class TestScrollPosition:
    """浏览器自带的滚动恢复会把内容较高的设置页放到 scrollY=10，整页看起来跳了一下。"""

    def test_the_first_paint_script_takes_over_scroll_restoration(self):
        base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
        assert 'history.scrollRestoration = "manual"' in base
        assert "window.scrollTo(0, 0)" in base

    def test_anchors_are_left_alone(self):
        # 带 #锚点 的链接要照常跳到目标，不能被"回到顶部"覆盖。
        base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
        assert "if (!location.hash)" in base


class TestDensity:
    def test_the_choice_is_applied_before_first_paint(self):
        base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
        assert "qsick.density" in base
        assert "dataset.density" in base

    def test_compact_only_tightens_spacing(self):
        css = (STATIC / "app.css").read_text(encoding="utf-8")
        assert ':root[data-density="compact"]' in css
        # 字号不动：一变，图表刻度与饼图都要重新校准。
        assert "font-size" not in css[css.index(':root[data-density="compact"]'):]


class TestSecurityPanelsRender:
    """登录记录曾显示"读取失败"、当前会话永远停在"加载中…"。

    原因是内联脚本调用了 app.js 闭包里的 escapeHtml —— ReferenceError 被 catch 掉，
    后面的会话渲染再没执行到。
    """

    def test_the_inline_script_has_its_own_escape(self):
        settings = (TEMPLATES / "settings.html").read_text(encoding="utf-8")
        assert "function escapeHtml(" in settings

    def test_both_panels_have_containers(self, runtime):
        with TestClient(create_app(runtime)) as web:
            body = web.get("/settings?tab=security").text
        assert 'id="login-history"' in body
        assert 'id="session-list"' in body

    def test_the_overview_api_answers(self, runtime):
        with TestClient(create_app(runtime)) as web:
            response = web.get("/api/security/overview")
        assert response.status_code == 200
        data = response.json()
        assert {"has_password", "sessions", "logins"} <= set(data)


class TestTabs:
    @pytest.mark.parametrize("tab", ["general", "display", "security", "cache", "advanced"])
    def test_every_tab_renders_and_shows_the_subnav(self, runtime, tab):
        with TestClient(create_app(runtime)) as web:
            response = web.get(f"/settings?tab={tab}")
        assert response.status_code == 200
        assert "subnav" in response.text

    def test_the_subnav_lists_all_five(self, runtime):
        with TestClient(create_app(runtime)) as web:
            body = web.get("/settings").text
        for label in ("通用", "显示", "安全与隐私", "缓存清理", "高级"):
            assert label in body


class TestLayoutDoesNotShiftBetweenPages:
    """导航在"有滚动条"和"没滚动条"的页面之间会跳 5px。

    实测（1280x1000）：短页无滚动条时 navLeft=296，长页有滚动条时 291 —— 差的正是滚动条宽的一半。
    表现为"总览、消息、安全与隐私、高级 这几项比别的页偏左一点"。预留滚动条位置即可消除。
    """

    def test_the_scrollbar_gutter_is_reserved(self):
        css = (STATIC / "app.css").read_text(encoding="utf-8")
        assert "scrollbar-gutter: stable" in css

    def test_nothing_sets_overflow_hidden_on_the_root(self):
        # html 上出现 overflow:hidden 会让预留失效（没有可滚动的容器就无所谓 gutter）。
        css = (STATIC / "app.css").read_text(encoding="utf-8")
        assert "html { overflow" not in css
