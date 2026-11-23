"""光标：类型表、光标包格式、以及"不叠加"的保证。

不叠加是这次重构的全部理由，所以它必须被真的测到。最容易失效的方式是"看起来对，实际不一致"——
类型表说支持 22 种而 JS 只画 20 种、包缺一个类型就退回系统光标——都不会报错，只会让人在某个角落
发现两个指针叠着。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from qsick.webui.app import create_app  # noqa: E402
from qsick.webui.cursor_types import CURSOR_TYPES, TYPE_NAMES  # noqa: E402

STATIC = REPO / "src" / "qsick" / "webui" / "static"
TEMPLATES = REPO / "src" / "qsick" / "webui" / "templates"


def cursor_js() -> str:
    return (STATIC / "cursor.js").read_text(encoding="utf-8")


def cursor_css() -> str:
    return (STATIC / "cursor.css").read_text(encoding="utf-8")


class TestTypesAreInSync:
    def test_the_python_list_and_the_js_list_are_the_same(self):
        js = cursor_js()
        block = re.search(r"var TYPES = \[(.*?)\];", js, re.S)
        assert block, "cursor.js 里找不到 TYPES"
        declared = set(re.findall(r'"([a-z-]+)"', block.group(1)))
        assert declared == set(TYPE_NAMES)

    def test_every_type_but_default_has_a_selector(self):
        js = cursor_js()
        block = re.search(r"var TYPE_SELECTORS = \{(.*?)\n  \};", js, re.S)
        assert block, "cursor.js 里找不到 TYPE_SELECTORS"
        handled = set(re.findall(r'\n    "?([a-z-]+)"?:', block.group(1)))
        # `default` 是通配兜底，不需要选择器。
        assert handled == set(TYPE_NAMES) - {"default"}

    def test_no_type_appears_twice(self):
        assert len(TYPE_NAMES) == len(set(TYPE_NAMES))

    def test_the_page_shows_every_type(self, runtime):
        with TestClient(create_app(runtime)) as web:
            body = web.get("/settings?tab=display").text
        for name in TYPE_NAMES:
            assert f'data-type="{name}"' in body, name

    def test_the_count_matches_the_heading(self, runtime):
        with TestClient(create_app(runtime)) as web:
            body = web.get("/settings?tab=display").text
        assert f"光标状态（{len(CURSOR_TYPES)} 种）" in body


class TestNoOverlap:
    """包生效时，每个元素都必须拿到包的 cursor，不能有任何元素落回系统光标。

    落回系统光标 = 系统画一个、包画一个，也就是两个指针叠着。
    """

    def test_a_universal_rule_is_written_before_the_type_rules(self):
        """通配兜底必须在类型规则之前 —— CSS 里后写的赢，顺序反了类型就全被盖掉。"""
        js = cursor_js()
        apply_pack = js[js.index("function applyPack("):js.index("function renderPackCard(")]
        assert "body, body *" in apply_pack
        # 通配是 rules 数组的第一项，类型规则在后面 push 进去。
        assert apply_pack.index("body, body *") < apply_pack.index("TYPE_SELECTORS).forEach")

    def test_a_missing_type_inherits_the_pack_arrow_rather_than_the_system_cursor(self):
        js = cursor_js()
        # 没定义的类型直接 continue：它继承通配规则里的初始箭头。
        assert "if (!source) return;" in js
        # 回退关键字写 default，不写 pointer/text 这些系统值。
        assert ', default !important; }"' in js

    def test_the_builtin_follow_element_is_gone(self):
        # 跟随元素是叠加的根源：它画在指针位置，而系统光标同时也在。
        js = cursor_js()
        # 跟随元素曾用 `.qsick-cursor` 这个类；现在只剩注入样式用的 #qsick-cursor-pack。
        assert "class = \"qsick-cursor\"" not in js
        assert "'qsick-cursor'" not in js
        css = cursor_css()
        assert "data-cursor-builtin" not in css
        assert "cursor: none !important" not in css

    def test_an_undecodable_image_is_rejected_not_silently_broken(self):
        # cursor: url() 指向坏图时浏览器静默退回系统箭头，那就是叠加。
        js = cursor_js()
        assert "canDecode" in js
        assert "image.onerror" in js


class TestPackFormat:
    def test_description_is_required(self):
        js = cursor_js()
        assert "缺少 description" in js

    def test_description_is_limited(self):
        js = cursor_js()
        assert "MAX_DESCRIPTION = 12" in js
        assert "不能超过" in js

    def test_name_is_required(self):
        assert "缺少 name" in cursor_js()

    def test_the_documented_example_carries_both_fields(self, runtime):
        with TestClient(create_app(runtime)) as web:
            body = web.get("/settings?tab=display").text
        assert '"name":' in body
        assert '"description":' in body

    def test_the_example_description_fits_the_limit(self, runtime):
        with TestClient(create_app(runtime)) as web:
            body = web.get("/settings?tab=display").text
        found = re.search(r'"description":\s*"([^"]+)"', body)
        assert found, "格式示例里没有 description"
        assert len(found.group(1)) <= 12


class TestTheCard:
    def test_the_pack_card_reuses_the_system_default_structure(self):
        js = cursor_js()
        # 名称 → strong，描述 → small，预览 → img，与「系统默认」那张卡片同构。
        assert "createElement(\"strong\")" in js
        assert "createElement(\"small\")" in js
        assert "cursor-pack-preview" in js

    def test_the_pack_card_is_inserted_into_the_picker(self):
        js = cursor_js()
        assert 'document.getElementById("cursor-picker")' in js
        assert 'data-cursor="custom"' in js

    def test_the_system_default_card_explains_itself(self, runtime):
        with TestClient(create_app(runtime)) as web:
            body = web.get("/settings?tab=display").text
        assert "系统默认" in body
        assert "不接管指针，用系统的箭头" in body

    def test_no_builtin_cursor_templates_are_offered(self, runtime):
        with TestClient(create_app(runtime)) as web:
            body = web.get("/settings?tab=display").text
        for template in ("线描", "实心", "柔边"):
            assert template not in body, template


class TestFolders:
    def test_the_type_table_and_the_format_are_collapsible(self, runtime):
        with TestClient(create_app(runtime)) as web:
            body = web.get("/settings?tab=display").text
        assert 'id="cursor-types-fold"' in body
        assert 'id="cursor-format-fold"' in body
        # 默认收起：设置页的主要任务是选光标，参考信息展开会把选择器挤下去。
        assert re.search(r'<details class="cursor-fold" id="cursor-types-fold">', body)
        assert 'open' not in re.search(r'<details[^>]*id="cursor-types-fold"[^>]*>', body).group(0)

    def test_they_use_native_details_not_a_hand_rolled_toggle(self):
        css = cursor_css()
        assert ".cursor-fold > summary::before" in css
        assert "rotate(90deg)" in css, "展开时三角要朝下"


class TestMotionSettingIsGone:
    def test_the_display_tab_no_longer_offers_a_motion_switch(self, runtime):
        with TestClient(create_app(runtime)) as web:
            body = web.get("/settings?tab=display").text
        assert "motion-toggle" not in body
        assert "减少动态效果" not in body

    def test_no_code_still_reads_the_old_key(self):
        offenders = []
        for path in list(STATIC.glob("*.js")) + list(TEMPLATES.rglob("*.html")):
            text = path.read_text(encoding="utf-8")
            if "qsick.motion" in text or "data-motion" in text:
                offenders.append(path.name)
        assert offenders == []

    def test_the_os_preference_is_still_honoured(self):
        assert "prefers-reduced-motion" in (STATIC / "app.css").read_text(encoding="utf-8")


class TestNavDoesNotReloadWhenAlreadyThere:
    def test_the_active_link_hijacks_the_click(self):
        js = (STATIC / "app.js").read_text(encoding="utf-8")
        assert "initNavNoop" in js
        assert "event.defaultPrevented" in js
        assert "event.metaKey" in js and "event.ctrlKey" in js
        assert ".nav" in js and ".subnav" in js

    def test_the_active_marker_exists_on_both_navigations(self, runtime):
        with TestClient(create_app(runtime)) as web:
            home = web.get("/").text
            settings = web.get("/settings?tab=display").text
        assert 'class="active"' in home
        assert 'class="active"' in settings
