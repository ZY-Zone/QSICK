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
    """The keys a caller sees, which are NapCat's `native` names -- not the response's own names."""

    def test_the_item_carries_exactly_the_schema_keys(self):
        feed = qzone.normalize_feed(a_feed())
        assert set(feed) == {"tid", "user_id", "content", "time", "comment_num", "like_num",
                             "images", "feed_ref", "feed"}

    def test_the_author_is_user_id(self):
        feed = qzone.normalize_feed(a_feed())
        assert feed["user_id"] == "1927676487"

    def test_the_time_is_common_time(self):
        assert qzone.normalize_feed(a_feed())["time"] == 1795368979

    def test_the_body_is_the_joined_fragments(self):
        # The summary is a list of rich segments, not a string.
        assert qzone.normalize_feed(a_feed())["content"] == "test。"

    def test_the_counts_are_comment_num_and_like_num(self):
        feed = qzone.normalize_feed(a_feed())
        assert feed["like_num"] == 52
        assert feed["comment_num"] == 19

    def test_images_are_bare_urls(self):
        # NapCat's schema is `string[]`, not objects.
        images = qzone.normalize_feed(a_feed())["images"]
        assert images == ["https://m.qpic.cn/pic"]

    def test_the_feed_id_surfaces_as_tid(self):
        assert qzone.normalize_feed(a_feed())["tid"] == "4702e6721328036b35b90100"

    def test_the_raw_feed_is_kept_untouched(self):
        raw = a_feed()
        assert qzone.normalize_feed(raw)["feed"] is raw["feed"]

    def test_the_action_handle_is_exposed(self):
        # Writes and detail need this, not the tid: a bare tid is refused with retcode 10023.
        ref = qzone.normalize_feed(a_feed())["feed_ref"]
        assert ref["union_id"]["feed_id"] == "4702e6721328036b35b90100"
        assert ref["like_key"] == "like-key-1"
        assert ref["feed_action_domain_type"] == 1

    def test_a_feed_without_a_union_id_reports_no_handle(self):
        bare = {"feed": {"cell_common": {"time": 1}}}
        assert qzone.normalize_feed(bare)["feed_ref"] is None

    def test_unknown_values_are_null_rather_than_absent(self):
        # NapCat's schema is all-nullable; a missing key and a null mean different things to a caller.
        feed = qzone.normalize_feed({"feed": {"cell_common": {"time": 0}}})
        assert feed["tid"] is None and feed["content"] is None and feed["images"] is None

    def test_a_broken_feed_does_not_raise(self):
        assert qzone.normalize_feed(None)["feed"] is None
        assert qzone.normalize_feed({"feed": {"cell_media": {"media_items": [None, 3]}}})["images"] is None


class TestWrappers:
    def test_a_friend_feed_page_speaks_next_cursor(self):
        page = qzone._feed_page({"feeds": [a_feed()], "has_more": True, "attach_info": "abc"}, count=10)
        assert set(page) == {"feeds", "has_more", "next_cursor"}
        assert page["next_cursor"] == "abc"

    def test_a_msg_list_page_is_named_msglist(self):
        page = qzone._feed_page({"feeds": [a_feed()]}, count=10, key="msglist")
        assert set(page) == {"msglist", "has_more", "next_cursor"}

    def test_an_empty_page_still_names_its_list(self):
        page = qzone._feed_page(None, count=10, key="msglist")
        assert page == {"msglist": [], "has_more": False, "next_cursor": None}

    def test_a_comment_inside_a_detail_stays_raw(self):
        # The detail action's `comments` are raw in NapCat (`QzoneComment`), not normalized.
        comment = {"id": "5", "user": {"uin": 1}, "content": [{"content": "hi"}], "time": "9"}
        made = qzone._normalize_comment(comment)
        assert made["id"] == "5"
        assert made["content"] == [{"content": "hi"}]
        assert made["time"] == "9"

    def test_a_comment_listing_uses_the_item_shape(self):
        # `get_qzone_comment_list` returns `QzoneCommentItem`, which flattens content and keeps the raw object.
        comment = {"id": "5", "user": {"uin": 7, "nick": "n"}, "content": [{"content": "a"}, {"content": "b"}],
                   "time": "9", "like": {"count": 3, "status": 1}, "reply_num": "2"}
        made = qzone.comment_item(comment)
        assert set(made) == {"comment_id", "user_id", "uid", "nickname", "time", "content", "images",
                             "like_count", "liked", "reply_num", "has_more_replies", "replies", "comment"}
        assert made["content"] == "ab"
        assert made["user_id"] == "7"
        assert made["like_count"] == 3 and made["liked"] is True
        assert made["comment"] is comment

    def test_a_reply_adds_who_it_answers(self):
        reply = {"id": "9", "user": {"uin": 1}, "content": [{"content": "x"}],
                 "target": {"uin": 2, "nick": "target"}}
        made = qzone.reply_item(reply)
        assert made["reply_id"] == "9"
        assert made["target_user_id"] == "2"
        assert made["target_nickname"] == "target"
        assert "comment_id" not in made


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


class TestRightRewrite:
    """`set_msg_right` resubmits the whole post, so the picture has to be rebuilt into `richval`.

    Measured the hard way: sending only `tid`/`con`/`ugc_right` answers ok and silently drops the attached image.
    """

    def a_detail(self, *, pictures=True) -> dict:
        detail = {
            "code": 0, "tid": "tid-1", "uin": "3905548034", "ugcright_id": "tid-1",
            "content": "test", "conlist": [{"con": "test"}], "t1_subtype": 1, "ugc_right": 1,
        }
        if pictures:
            detail["pic"] = [{
                "pic_id": ",album-1,lloc-1,extra", "pictype": 22, "height": 240, "width": 240,
                "url1": "https://m.qpic.cn/x?bo=8ADwAPAA8AADACU!&ek=1",
            }]
        return detail

    async def _body(self, detail) -> dict:
        client = FakeClient([(200, '{"code":0}')])
        client.own_msg_detail = lambda tid: _resolved(detail)
        await client.set_msg_right("tid-1", 1)
        _method, _url, _headers, body = client.requests[0]
        import urllib.parse
        # `keep_blank_values` matters: a text-only post sends `richval=` and dropping it would look like the
        # field was never written at all.
        return dict(urllib.parse.parse_qsl(body.decode("utf-8"), keep_blank_values=True))

    async def test_the_picture_becomes_richval(self):
        body = await self._body(self.a_detail())
        # `,albumid,lloc,lloc,type,height,width,,0,0` -- lloc appears twice, as NapCat writes it.
        assert body["richval"] == ",album-1,lloc-1,lloc-1,22,240,240,,0,0"
        assert body["richtype"] == "1"

    async def test_the_text_and_identity_are_carried_back(self):
        body = await self._body(self.a_detail())
        assert body["con"] == "test"
        assert body["tid"] == "tid-1"
        assert body["ugcright_id"] == "tid-1"
        assert body["hostuin"] == "3905548034"
        assert body["ugc_right"] == "1"

    async def test_the_picture_bo_metadata_is_echoed(self):
        body = await self._body(self.a_detail())
        assert body["pic_bo"] == "8ADwAPAA8AADACU!\t8ADwAPAA8AADACU!"

    async def test_a_text_only_post_sends_no_richval(self):
        body = await self._body(self.a_detail(pictures=False))
        assert body["richval"] == ""
        assert body["richtype"] == ""

    async def test_a_forwarded_post_is_refused_rather_than_rewritten(self):
        detail = self.a_detail()
        detail["rt_tid"] = "something"
        client = FakeClient([])
        client.own_msg_detail = lambda tid: _resolved(detail)
        with pytest.raises(qzone.QzoneError) as caught:
            await client.set_msg_right("tid-1", 1)
        assert "cannot be rebuilt" in str(caught.value)

    async def test_another_accounts_post_is_refused(self):
        detail = self.a_detail()
        detail["uin"] = "1927676487"
        client = FakeClient([])
        client.own_msg_detail = lambda tid: _resolved(detail)
        with pytest.raises(qzone.QzoneError) as caught:
            await client.set_msg_right("tid-1", 1)
        assert "own posts" in str(caught.value)


async def _resolved(value):
    return value


class TestProfileRename:
    """The profile CGI takes a bitmap of which fields are being set, and ignores anything unflagged.

    Measured: sending `spacename` without `mb` answers `code 0` and changes nothing -- indistinguishable from
    success until the profile is read back.
    """

    async def _body(self, **kwargs) -> dict:
        client = FakeClient([(200, '{"code":0}')])
        await client.set_profile(**kwargs)
        _method, _url, _headers, body = client.requests[0]
        import urllib.parse
        return dict(urllib.parse.parse_qsl(body.decode("utf-8"), keep_blank_values=True))

    async def test_the_name_sets_its_bit(self):
        body = await self._body(name="SICKCC")
        assert body["spacename"] == "SICKCC"
        assert body["mb"] == "2048"

    async def test_the_description_sets_its_own_bit(self):
        body = await self._body(description="hi")
        assert body["desc"] == "hi"
        assert body["mb"] == "4096"

    async def test_both_fields_combine_into_one_bitmap(self):
        body = await self._body(name="a", description="b")
        assert body["mb"] == str(2048 | 4096)

    async def test_the_form_carries_what_the_cgi_expects(self):
        body = await self._body(name="a")
        assert body["pageindex"] == "3"
        assert body["fupdate"] == "1"
        assert body["uin"] == "3905548034"

    async def test_nothing_to_set_is_refused(self):
        client = FakeClient([])
        with pytest.raises(qzone.QzoneError):
            await client.set_profile()
