"""缓存清理：测量、单项清理、一键清理。

这个功能的失效方式很难看：清完显示"已释放 234 MB"而磁盘没变、或者一键清理顺手删掉了密码。
所以测试盯的是"报告的数字是否真实"和"没被列为缓存的东西是否被碰过"。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from qsick.webui import caches  # noqa: E402
from qsick.webui.app import create_app  # noqa: E402


class TestHumanSize:
    @pytest.mark.parametrize("value,expected", [
        (0, "0 B"),
        (512, "512 B"),
        (1024, "1 KB"),
        (1536, "2 KB"),                 # KB 不带小数：这个量级上小数是噪音
        (1024 * 1024, "1.0 MB"),
        (int(1.5 * 1024 * 1024), "1.5 MB"),
        (int(2.25 * 1024 ** 3), "2.2 GB"),
    ])
    def test_sizes_read_like_sizes(self, value, expected):
        assert caches.human_size(value) == expected

    def test_negative_is_clamped_rather_than_rendered(self):
        assert caches.human_size(-5) == "0 B"


class TestSqliteFamily:
    def test_the_wal_is_counted_with_the_database(self):
        # 实测过 13MB 的 -wal 配 106MB 的库：只报 .db 会把缓存量说小一大截。
        family = caches.sqlite_family(Path("x/messages.db"))
        names = [path.name for path in family]
        assert "messages.db" in names
        assert "messages.db-wal" in names
        assert "messages.db-shm" in names


class TestCollect:
    def test_every_item_has_an_id_label_and_place(self, runtime):
        for item in caches.collect(runtime):
            assert item.id and item.label and item.detail
            assert item.where in ("disk", "memory")

    def test_ids_are_unique(self, runtime):
        ids = [item.id for item in caches.collect(runtime)]
        assert len(ids) == len(set(ids))

    def test_the_password_is_not_a_cache(self, runtime):
        """一键清理不能把密码清掉 —— 那会把人锁在自己的控制台外面。"""
        ids = [item.id for item in caches.collect(runtime)]
        assert "password" not in ids
        assert "auth" not in ids
        # 登录记录可以清，但它明确写着"不动密码本身"。
        for item in caches.collect(runtime):
            if item.id == "logins":
                assert "密码" in item.detail

    def test_config_and_plugins_are_not_caches(self, runtime):
        ids = [item.id for item in caches.collect(runtime)]
        for forbidden in ("config", "plugins", "network", "sentences"):
            assert forbidden not in ids


class TestMeasure:
    def test_rows_are_sorted_by_size(self, runtime):
        rows = caches.measure(caches.collect(runtime))
        sizes = [row["bytes"] for row in rows]
        assert sizes == sorted(sizes, reverse=True)

    def test_every_row_carries_a_human_readable_size(self, runtime):
        for row in caches.measure(caches.collect(runtime)):
            assert row["human"]
            assert row["where_label"] in ("磁盘", "内存")

    def test_a_missing_path_measures_zero_rather_than_raising(self, tmp_path):
        item = caches.CacheItem(id="ghost", label="x", detail="x", where="disk",
                                paths=(tmp_path / "does-not-exist",))
        assert caches.size_of(item) == 0

    def test_a_directory_is_summed_recursively(self, tmp_path):
        root = tmp_path / "media"
        (root / "a").mkdir(parents=True)
        (root / "a" / "one.bin").write_bytes(b"x" * 100)
        (root / "two.bin").write_bytes(b"x" * 50)
        item = caches.CacheItem(id="media", label="x", detail="x", where="disk", paths=(root,))
        assert caches.size_of(item) == 150


class TestClear:
    def test_clearing_one_item_removes_only_that_item(self, tmp_path):
        first = tmp_path / "one.bin"
        second = tmp_path / "two.bin"
        first.write_bytes(b"x" * 200)
        second.write_bytes(b"x" * 300)
        items = [
            caches.CacheItem(id="one", label="1", detail="", where="disk", paths=(first,)),
            caches.CacheItem(id="two", label="2", detail="", where="disk", paths=(second,)),
        ]
        result = caches.clear(items, "one")
        assert result["ok"] is True
        assert not first.exists()
        assert second.exists()
        assert result["freed"] == 200

    def test_all_clears_everything(self, tmp_path):
        paths = []
        for index in range(3):
            path = tmp_path / f"f{index}.bin"
            path.write_bytes(b"x" * 100)
            paths.append(path)
        items = [caches.CacheItem(id=f"f{index}", label=str(index), detail="", where="disk", paths=(path,))
                 for index, path in enumerate(paths)]
        result = caches.clear(items, "all")
        assert result["ok"] is True
        assert all(not path.exists() for path in paths)
        assert result["freed"] == 300

    def test_an_unknown_id_is_refused(self, runtime):
        result = caches.clear(caches.collect(runtime), "nope")
        assert result["ok"] is False
        assert "nope" in result["error"]

    def test_the_freed_figure_is_measured_not_promised(self, tmp_path):
        """清不掉的东西不能被算成已释放。

        模拟一个"声称清了但文件还在"的清理函数：报告必须是 0，因为磁盘没变。
        """
        path = tmp_path / "stubborn.bin"
        path.write_bytes(b"x" * 500)
        item = caches.CacheItem(id="stubborn", label="x", detail="", where="disk",
                                paths=(path,), clear=lambda: None)
        result = caches.clear([item], "stubborn")
        assert result["ok"] is True
        assert result["freed"] == 0, "文件还在就不该报告释放"

    def test_one_failing_item_does_not_abort_clear_all(self, tmp_path):
        good = tmp_path / "good.bin"
        good.write_bytes(b"x" * 120)

        def explode() -> None:
            raise RuntimeError("boom")

        items = [
            caches.CacheItem(id="bad", label="x", detail="", where="disk", paths=(), clear=explode),
            caches.CacheItem(id="good", label="y", detail="", where="disk", paths=(good,)),
        ]
        result = caches.clear(items, "all")
        assert result["ok"] is True
        assert not good.exists(), "一项失败不该让后面的一起不执行"
        assert set(result["cleared"]) == {"bad", "good"}


class TestThePageAndApi:
    def test_the_tab_exists_and_lists_every_item(self, runtime):
        with TestClient(create_app(runtime)) as web:
            body = web.get("/settings?tab=cache").text
        assert "缓存清理" in body
        for item in caches.collect(runtime):
            assert f'data-id="{item.id}"' in body, item.id

    def test_there_is_a_clear_button_per_item_and_one_for_all(self, runtime):
        with TestClient(create_app(runtime)) as web:
            body = web.get("/settings?tab=cache").text
        assert "cache-clear-all" in body
        assert "一键清理" in body
        assert body.count("cache-clear\"") >= len(caches.collect(runtime))

    def test_the_pie_chart_is_present(self, runtime):
        with TestClient(create_app(runtime)) as web:
            body = web.get("/settings?tab=cache").text
        assert 'id="cache-chart"' in body
        assert 'id="cache-legend"' in body

    def test_the_seed_json_parses(self, runtime):
        import re

        with TestClient(create_app(runtime)) as web:
            body = web.get("/settings?tab=cache").text
        seed = re.search(r'<script type="application/json" id="cache-seed">(.*?)</script>', body, re.S)
        assert seed, "页面里没有缓存数据"
        rows = json.loads(seed.group(1))
        assert rows and all("human" in row for row in rows)

    def test_the_api_lists_and_clears(self, runtime):
        with TestClient(create_app(runtime)) as web:
            listing = web.get("/api/cache")
            assert listing.status_code == 200
            ids = [row["id"] for row in listing.json()["items"]]
            assert ids

            # 清一个不存在的项要被拒绝，而不是静默成功。
            bad = web.post("/api/cache/clear", json={"id": "nope"})
            assert bad.status_code == 400

            good = web.post("/api/cache/clear", json={"id": "logs"})
            assert good.status_code == 200
            payload = good.json()
            assert payload["ok"] is True
            assert "items" in payload, "清理后要回传新的尺寸，否则页面还显示旧数字"

    def test_a_missing_id_is_a_bad_request(self, runtime):
        with TestClient(create_app(runtime)) as web:
            response = web.post("/api/cache/clear", json={})
        assert response.status_code == 400


class TestLogBufferAccounting:
    def test_buffer_size_grows_with_logs_and_resets_on_clear(self):
        from qsick import log as logmod

        logmod.clear_buffer()
        assert logmod.buffer_bytes() == 0
        logmod.get_logger("Test").info("x" * 200)
        assert logmod.buffer_bytes() > 0
        freed = logmod.clear_buffer()
        assert freed > 0
        assert logmod.buffer_bytes() == 0


class TestRawStoreClear:
    """实例库的清空必须真的清空。

    走过一个坑：原先按记录里的 message_id 逐条 delete，而 message_id 缺失或不可用的记录会被底层
    存成合成键（raw:...），`delete(0)` 返回 False —— "清空"之后库里还留着行。
    """

    def test_every_row_goes_including_the_ones_without_a_usable_id(self, tmp_path):
        from qsick.raw_store import RawMessageStore
        from qsick.storage import Message

        path = tmp_path / "messages.db"
        store = RawMessageStore(path)
        for index in range(40):
            store.put(Message.from_payload({
                "message_id": index, "seq": index, "time": 1795000000 + index,
                "chat": {"type": "group", "id": 1},
                "sender": {"uin": 100, "nickname": "n"},
                "elements": [{"type": "text", "data": {"text": "x" * 200}}],
                "account": "1",
            }))
        assert store.count() == 40
        assert store.clear() == 40
        assert store.count() == 0

    def test_the_frames_table_is_cleared_too(self, tmp_path):
        import sqlite3

        from qsick.raw_store import RawMessageStore
        from qsick.storage import Message

        path = tmp_path / "messages.db"
        store = RawMessageStore(path)
        store.put(Message.from_payload({
            "message_id": 7, "seq": 1, "time": 1795000000,
            "chat": {"type": "group", "id": 1}, "sender": {"uin": 100, "nickname": "n"},
            "elements": [{"type": "text", "data": {"text": "hello"}}], "account": "1",
        }))
        store.clear()
        # 留着摊平行等于把每条消息的每个元素继续存在盘上。
        remaining = sqlite3.connect(path).execute("SELECT COUNT(*) FROM raw_frames").fetchone()[0]
        assert remaining == 0

    def test_the_file_actually_shrinks(self, tmp_path):
        from qsick.raw_store import RawMessageStore
        from qsick.storage import Message

        path = tmp_path / "messages.db"
        store = RawMessageStore(path)

        def size() -> int:
            wal = path.with_name(path.name + "-wal")
            return path.stat().st_size + (wal.stat().st_size if wal.exists() else 0)

        for index in range(300):
            store.put(Message.from_payload({
                "message_id": index, "seq": index, "time": 1795000000 + index,
                "chat": {"type": "group", "id": 1}, "sender": {"uin": 100, "nickname": "n"},
                "elements": [{"type": "text", "data": {"text": "x" * 400}}], "account": "1",
            }))
        before = size()
        store.clear()
        # 只有 DELETE 不 VACUUM 的话文件不会变小，缓存面板就会报"释放 0"，看起来像没生效。
        assert size() < before
