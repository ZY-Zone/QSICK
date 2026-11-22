"""The QZone client, tested against the shapes a live session actually returned.

Every constant in here came from a real response on 2026-11-23, and several of them are the *second* guess: the
author is `cell_user_info.user.uin` rather than `cell_user_info.uin`, the timestamp is `cell_common.time` and there
is no `abstime`, a like count is `cell_like.num` rather than `like_num`, and a picture's address is
`image.default_url.url`. Guessing these wrong does not raise -- it produces a feed with empty fields -- so they are
pinned here.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from qsick.protocol import qzone  # noqa: E402


def a_feed() -> dict:
    """One feed, shaped like the real ones (a `feed` wrapper, `cell_*` keys, nested identifiers)."""
    return {
        "feed": {
            "cell_common": {
                "time": 1795368979,
                "union_id": {"feed_id": "4702e6721328036b35b90100", "ugc_id": "ugc-1", "uf_key": "k"},
                "cur_like_key": "like-key-1",
                "domain_type": 1,
                "feed_type": 1,
                # Measured: `cell_common.app_id` (the key is not `appid`).
                "app_id": 311,
            },
            "cell_user_info": {"user": {"uin": 1927676487, "nick": "。"}},
            "cell_summary": {"summary": [
                {"content": "test", "media_items": []},
                {"content": "。", "media_items": []},
            ]},
            "cell_like": {"num": 52, "liked": True},
            "cell_comment": {"num": 19},
            "cell_media": {"media_items": [
                {"image": {"default_url": {"url": "https://m.qpic.cn/pic", "width": 240, "height": 240}},
                 "video": None},
                {"image": None, "video": None},
            ]},
        }
    }


class TestShape:
    def test_the_author_and_time_come_from_the_places_they_actually_live(self):
        feed = qzone.normalize_feed(a_feed())
        assert feed["uin"] == "1927676487"
        assert feed["nickname"] == "。"
        assert feed["create_time"] == 1795368979

    def test_the_body_is_the_joined_fragments(self):
        # The summary is a list of rich segments, not a string.
        assert qzone.normalize_feed(a_feed())["content"] == "test。"

    def test_counts_come_from_num(self):
        feed = qzone.normalize_feed(a_feed())
        assert feed["like_count"] == 52
        assert feed["liked"] is True
        assert feed["comment_count"] == 19

    def test_a_picture_url_is_nested_under_default_url(self):
        images = qzone.normalize_feed(a_feed())["images"]
        assert len(images) == 1, "an entry with no image must not become an empty picture"
        assert images[0]["url"] == "https://m.qpic.cn/pic"
        assert (images[0]["width"], images[0]["height"]) == (240, 240)

    def test_the_feed_id_is_found_one_level_down(self):
        feed = qzone.normalize_feed(a_feed())
        assert feed["feed_id"] == "4702e6721328036b35b90100"
        assert feed["app_id"]

    def test_the_action_handle_is_exposed(self):
        # Writes and detail need this, not the tid: a bare tid is refused with retcode 10023.
        ref = qzone.normalize_feed(a_feed())["feed_ref"]
        assert ref["union_id"]["feed_id"] == "4702e6721328036b35b90100"
        assert ref["like_key"] == "like-key-1"
        assert ref["feed_action_domain_type"] == 1

    def test_a_feed_without_a_union_id_yields_no_handle(self):
        bare = {"feed": {"cell_common": {"time": 1}}}
        assert qzone.normalize_feed(bare)["feed_ref"] == {}

    def test_a_broken_feed_does_not_raise(self):
        assert qzone.normalize_feed(None) == {"raw": None}
        assert qzone.normalize_feed({"feed": {"cell_media": {"media_items": [None, 3]}}})["images"] == []


class TestTransports:
    def test_g_tk_is_hash33(self):
        assert qzone.hash33("abc") == 193485963

    def test_a_plain_jsonp_wrapper_is_stripped(self):
        assert qzone.unwrap_jsonp('_Callback({"code":0,"data":{"a":1}});') == {"code": 0, "data": {"a": 1}}

    def test_the_upload_answer_is_an_html_page(self):
        # `jsonhtml` format: a whole document whose script calls `frameElement.callback({...})`.
        page = ('<html><script>document.domain="h5.qzone.qq.com";'
                'frameElement.callback({"code":0,"data":{"albumid":"a","lloc":"l"}});</script></html>')
        assert qzone.unwrap_upload_response(page)["data"]["albumid"] == "a"

    def test_richval_matches_the_shape_publish_embeds(self):
        made = qzone.richval_of({"albumid": "A", "lloc": "L", "type": "3", "height": "240", "width": "240"})
        assert made == ",A,L,L,3,240,240,,240,240"


class FakeClient(qzone.QzoneClient):
    """A client whose HTTP is replaced by a queue of canned answers, so the request itself can be asserted."""

    def __init__(self, answers, **kwargs):
        super().__init__(self._caller, account="3905548034", **kwargs)
        self.answers = list(answers)
        self.requests: list[tuple[str, str, dict, bytes | None]] = []

    async def _caller(self, op, params):
        return {"uin": "3905548034", "pskey": "P" * 44, "g_tk": zzone_g_tk(), "available": {}}

    async def _request(self, method, url, *, headers, body=None):
        self.requests.append((method, url, headers, body))
        return self.answers.pop(0)


def zzone_g_tk() -> int:
    return qzone.hash33("P" * 44)


class TestRequests:
    async def test_trpc_sends_the_oidb_header_that_makes_it_work(self):
        client = FakeClient([(200, json.dumps({"retcode": 0, "data": {"feeds": []}}))])
        await client.trpc("friend_feeds", {})
        method, url, headers, _body = client.requests[0]
        assert method == "POST"
        assert "/trpc.qzone.feeds_reader.FeedsReader/GetFriendFeeds" in url
        assert f"g_tk={zzone_g_tk()}" in url
        # Without this header the gateway answers 4001 for every request -- the original wrong conclusion.
        assert json.loads(headers["x-oidb"]) == {"uint32_command": "0x97e5", "uint32_service_type": "0"}
        assert headers["x-web-qua"] == qzone.WEB_QUA
        assert headers["Cookie"].startswith("uin=o3905548034; p_uin=o3905548034; p_skey=")

    async def test_the_old_cgi_sends_no_oidb_header(self):
        client = FakeClient([(200, '_Callback({"code":0,"data":{"uin":1}});')])
        await client.profile()
        _method, url, headers, _body = client.requests[0]
        assert url.startswith(qzone.WEB_ROOT)
        assert "x-oidb" not in headers

    async def test_a_write_uses_the_user_gateway(self):
        # Measured: the publish CGI answers 403 on the h5 root and works on the user root.
        client = FakeClient([(200, '{"code":0,"tid":"t1"}')])
        await client.publish("test")
        _method, url, _headers, _body = client.requests[0]
        assert url.startswith(qzone.USER_ROOT)

    async def test_publish_reads_the_tid_from_the_top_level(self):
        # The publish answer has `data: null` and the tid beside it.
        client = FakeClient([(200, '{"code":0,"tid":"the-tid","message":"ok"}')])
        result = await client.publish("test")
        assert result["tid"] == "the-tid"

    async def test_a_non_zero_retcode_raises_with_the_number(self):
        client = FakeClient([(200, '{"retcode":10023,"message":"[backend] 无权限访问"}')])
        with pytest.raises(qzone.QzoneError) as caught:
            await client.trpc("feed_detail", {})
        assert caught.value.retcode == 10023
        assert "无权限" in str(caught.value)

    async def test_a_cgi_error_code_raises(self):
        client = FakeClient([(200, '_Callback({"code":-3000,"message":"nope"});')])
        with pytest.raises(qzone.QzoneError) as caught:
            await client.profile()
        assert caught.value.retcode == -3000

    async def test_liking_without_a_like_key_says_so_instead_of_guessing(self):
        client = FakeClient([])
        with pytest.raises(qzone.QzoneError) as caught:
            await client.like({"union_id": {"feed_id": "x"}})
        assert "like_key" in str(caught.value)


class TestCredentials:
    async def test_a_ticket_is_reused_between_calls(self):
        client = FakeClient([(200, '{"retcode":0,"data":{"feeds":[]}}'),
                             (200, '{"retcode":0,"data":{"feeds":[]}}')])
        await client.trpc("friend_feeds", {})
        await client.trpc("friend_feeds", {})
        # Two HTTP calls, one credential fetch.
        assert len(client.requests) == 2
        assert client._credentials is not None

    async def test_a_session_without_a_ticket_is_reported(self):
        class Empty(qzone.QzoneClient):
            async def ticket(self, op, params):
                return {"uin": "1", "pskey": ""}

        instance = Empty(None)
        instance._caller = instance.ticket
        with pytest.raises(qzone.QzoneError) as caught:
            await instance.credentials()
        assert "p_skey" in str(caught.value)
