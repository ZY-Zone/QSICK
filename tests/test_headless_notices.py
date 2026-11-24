"""宿主的系统通知提取：灰条 → `notice.*`。

这些事件以前完全不存在。QQ NT 没有"群成员变更"回调，变更是以**灰条消息**的形式到达的
（`elementType` 8），所以提取器读的是同一条原始消息。

测试喂的是按 NapCat 解出并写进 `headless_notices.cjs` 注释里的那些结构。它们是**形状**测试，
不是真实抓包：能证明的是"字段这么放，就会得到这个通知"，证明不了"QQ 一定这么放"。抓包之后
应当补一组真实报文。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXTRACTOR = ROOT / "src" / "qsick" / "headless_notices.cjs"

NODE = shutil.which("node")


def _run(payload: dict) -> list[dict]:
    """Call the extractor in Node and return what it produced."""
    script = f"""
const {{ createNoticeExtractor }} = require({json.dumps(str(EXTRACTOR))});
const input = {json.dumps(payload)};
const extractor = createNoticeExtractor({{
  resolveUin: uid => ({{ "u_admin": "200", "u_member": "100", "u_me": "999" }})[uid] || "",
  debug: () => {{}},
  selfUin: () => "999",
}});
const found = extractor.inspect(input.raw, input.mapped);
console.log(JSON.stringify({{ notices: found, systemOnly: extractor.isSystemOnly(input.raw) }}));
"""
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True,
                            # 中文群名必须按 UTF-8 读回来：不写这一行，Windows 会用区域编码
                            # 解码，读到一半就在读线程里抛异常，而断言只会说"输出是 None"。
                            encoding="utf-8", errors="replace", timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip(), "the extractor printed nothing"
    return json.loads(result.stdout.strip())


def message(*elements: dict, group: int | str = 2169010946, sender: str = "300") -> dict:
    """A raw native message carrying the given elements."""
    return {
        "raw": {
            "chatType": "2", "peerUid": "2169010946", "peerUin": str(group),
            "senderUid": "u_sender", "senderUin": sender, "msgId": "7001", "msgSeq": "10", "msgTime": "1",
            "elements": list(elements),
        },
        "mapped": {"chat": {"type": "group", "id": int(group)}},
    }


def group_tip(body: dict) -> dict:
    return {"elementType": 8, "grayTipElement": {"subElementType": 4, "groupElement": body}}


def json_tip(busi: str, items: list[dict]) -> dict:
    return {"elementType": 8, "grayTipElement": {
        "subElementType": 17,
        "jsonGrayTipElement": {"busiId": busi, "jsonStr": json.dumps({"items": items})},
    }}


pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


class TestGroupNameChange:
    def test_it_becomes_a_group_name_notice(self):
        out = _run(message(group_tip({"type": 5, "groupName": "新群名", "memberUid": "u_admin"})))
        assert len(out["notices"]) == 1
        notice = out["notices"][0]
        assert notice["cmd"] == "notice.group_name"
        assert notice["payload"]["group_name"] == "新群名"
        assert notice["payload"]["group_id"] == "2169010946"
        assert notice["payload"]["operator_id"] == "200"

    def test_a_tip_without_a_name_reports_nothing(self):
        """没有名字就没有事件。发一个空名字只会让客户端把群名刷成空白。"""
        out = _run(message(group_tip({"type": 5, "memberUid": "u_admin"})))
        assert out["notices"] == []


class TestMute:
    def test_a_member_mute(self):
        out = _run(message(group_tip({"type": 8, "memberUid": "u_admin", "shutUp": {
            "duration": "60", "member": {"uid": "u_member"}, "admin": {"uid": "u_admin"}}})))
        payload = out["notices"][0]["payload"]
        assert out["notices"][0]["cmd"] == "notice.group_ban"
        assert payload["sub_type"] == "ban"
        assert payload["user_id"] == "100"
        assert payload["operator_id"] == "200"
        assert payload["duration"] == 60

    def test_zero_duration_is_a_lift(self):
        out = _run(message(group_tip({"type": 8, "shutUp": {
            "duration": "0", "member": {"uid": "u_member"}, "admin": {"uid": "u_admin"}}})))
        assert out["notices"][0]["payload"]["sub_type"] == "lift_ban"

    def test_no_member_means_a_whole_group_mute(self):
        """全员禁言的灰条不带 member —— 这正是它与单个禁言的区别，不用猜。"""
        out = _run(message(group_tip({"type": 8, "shutUp": {
            "duration": "600", "admin": {"uid": "u_admin"}}})))
        notice = out["notices"][0]
        assert notice["cmd"] == "notice.group_whole_ban"
        assert notice["payload"]["sub_type"] == "ban"
        assert "user_id" not in notice["payload"]

    def test_an_unresolvable_admin_still_produces_the_notice(self):
        """操作人解析不出来时退回消息的发送者：灰条就是那个人发的。"""
        out = _run(message(group_tip({"type": 8, "shutUp": {
            "duration": "60", "member": {"uid": "u_member"}, "admin": {"uid": "u_unknown"}}})))
        assert out["notices"][0]["payload"]["operator_id"] == "300"


class TestMemberChange:
    def test_self_being_added_is_a_group_increase(self):
        out = _run(message(group_tip({"type": 1, "memberUid": "u_me", "adminUid": "u_admin"})))
        notice = out["notices"][0]
        assert notice["cmd"] == "notice.group_increase"
        assert notice["payload"]["user_id"] == "999"
        assert notice["payload"]["sub_type"] == "approve"

    def test_being_added_without_an_admin_is_an_invite(self):
        out = _run(message(group_tip({"type": 1, "memberUid": "u_me"})))
        assert out["notices"][0]["payload"]["sub_type"] == "invite"

    def test_somebody_else_being_added_is_not_reported(self):
        """QQ 的这条灰条不说邀请人，也不说是不是入群申请通过。

        如实说"不知道"就只能不发——发一个猜的 sub_type 会让客户端显示错的原因。
        """
        out = _run(message(group_tip({"type": 1, "memberUid": "u_member", "adminUid": "u_admin"})))
        assert out["notices"] == []

    def test_a_quit_is_a_group_decrease(self):
        out = _run(message(group_tip({"type": 3, "memberUid": "u_member"})))
        notice = out["notices"][0]
        assert notice["cmd"] == "notice.group_decrease"
        assert notice["payload"]["sub_type"] == "leave"
        assert notice["payload"]["user_id"] == "100"

    def test_a_dissolved_group(self):
        for kind in (2, 10):
            out = _run(message(group_tip({"type": kind, "memberUid": "u_admin"})))
            assert out["notices"][0]["cmd"] == "notice.group_disband", kind


class TestPoke:
    def _items(self, first: str, second: str) -> list[dict]:
        return [{"uid": first, "txt": "拍了拍"}, {"uid": second, "txt": "的肩膀"}]

    def test_a_group_poke_carries_the_group(self):
        out = _run(message(json_tip("1061", self._items("u_member", "u_admin"))))
        notice = out["notices"][0]
        assert notice["cmd"] == "notice.notify"
        assert notice["payload"]["sub_type"] == "poke"
        assert notice["payload"]["user_id"] == "100"
        assert notice["payload"]["target_id"] == "200"
        assert notice["payload"]["group_id"] == "2169010946"

    def test_a_poke_with_three_parties_is_skipped(self):
        """方向不可知时不发：戳一戳是有方向的，方向错了比没有更糟。"""
        items = self._items("u_member", "u_admin") + [{"uid": "u_me", "txt": "x"}]
        out = _run(message(json_tip("1061", items)))
        assert out["notices"] == []

    def test_an_unknown_business_id_is_ignored(self):
        out = _run(message(json_tip("99999", self._items("u_member", "u_admin"))))
        assert out["notices"] == []


class TestWhatIsNotClaimed:
    def test_a_reaction_xml_tip_produces_nothing(self):
        """表情回应要读 XML 里的回应人与表情 id，离线无法确认其形状，所以不产出事件。"""
        element = {"elementType": 8, "grayTipElement": {
            "subElementType": 12, "xmlElement": {"templId": "10382", "content": "<gtip/>"}}}
        out = _run(message(element))
        assert out["notices"] == []

    def test_a_member_join_json_tip_produces_nothing(self):
        """busiId 51 的灰条不写谁入群，只能靠成员列表差分。猜一个就是造假。"""
        element = json_tip("51", [{"txt": "某人加入群"}])
        out = _run(message(element))
        assert out["notices"] == []


class TestSystemOnlyDetection:
    def test_a_pure_gray_tip_message_is_not_a_chat_message(self):
        out = _run(message(group_tip({"type": 5, "groupName": "x", "memberUid": "u_admin"})))
        assert out["systemOnly"] is True

    def test_a_message_with_a_text_element_is_a_chat_message(self):
        text = {"elementType": 1, "textElement": {"content": "hi", "atType": 0}}
        out = _run(message(text, group_tip({"type": 5, "groupName": "x", "memberUid": "u_admin"})))
        assert out["systemOnly"] is False

    def test_a_message_with_no_elements_is_not_treated_as_system(self):
        out = _run({"raw": {"chatType": "2", "peerUid": "1", "peerUin": "1", "elements": []},
                    "mapped": {"chat": {"type": "group", "id": 1}}})
        assert out["systemOnly"] is False


class TestFileUpload:
    def _file(self, **body) -> dict:
        return {"elementType": 3, "fileElement": dict(body)}

    def test_a_file_message_becomes_a_group_upload_notice(self):
        """群文件上传没有独立通知，它就是一条带 fileElement 的消息。

        转译它才让 `notice.group_upload` 存在；不转译的话客户端只看到一条"[文件]"，永远不知道
        这是一个可下载的上传事件。
        """
        out = _run(message(self._file(fileName="a.zip", fileSize="1234",
                                      fileUuid="uuid-1", fileBizId="102")))
        notice = out["notices"][0]
        assert notice["cmd"] == "notice.group_upload"
        assert notice["payload"]["group_id"] == "2169010946"
        assert notice["payload"]["user_id"] == "300"
        assert notice["payload"]["file"] == {
            "id": "uuid-1", "name": "a.zip", "size": 1234, "busid": 102,
        }

    def test_a_file_without_a_name_is_skipped(self):
        out = _run(message(self._file(fileSize="10")))
        assert out["notices"] == []

    def test_a_file_in_a_private_chat_is_not_a_group_upload(self):
        payload = {
            "raw": {"chatType": "1", "peerUid": "u_friend", "senderUid": "u_sender",
                    "senderUin": "300", "msgId": "1", "elements": [self._file(fileName="a.zip")]},
            "mapped": {"chat": {"type": "private", "id": 300}},
        }
        out = _run(payload)
        assert out["notices"] == []

    def test_a_file_message_is_still_a_chat_message(self):
        """上传事件与消息本身并存，不是二选一。"""
        out = _run(message(self._file(fileName="a.zip")))
        assert out["systemOnly"] is False


class TestTitleChange:
    def _tip(self, items: list[dict]) -> dict:
        return {"elementType": 8, "grayTipElement": {
            "subElementType": 17,
            "jsonGrayTipElement": {"busiId": "0", "jsonStr": json.dumps({"items": items})},
        }}

    def test_a_title_change_is_read_from_the_fixed_slots(self):
        # The tail item's text is what identifies the tip -- it carries no busiId worth matching on.
        out = _run(message(self._tip([
            {"txt": "x"}, {"txt": "", "param": ["u_member"]}, {"txt": "x"},
            {"txt": "大佬"}, {"txt": "头衔"}])))
        notice = out["notices"][0]
        assert notice["cmd"] == "notice.group_title"
        assert notice["payload"]["user_id"] == "100"
        assert notice["payload"]["title"] == "大佬"

    def test_an_empty_title_is_a_real_value(self):
        """清空头衔是一条真实的事件，不是"没有标题"——丢掉它会让客户端一直显示旧头衔。"""
        out = _run(message(self._tip([
            {"txt": "x"}, {"txt": "", "param": ["u_member"]}, {"txt": "x"},
            {"txt": ""}, {"txt": "头衔"}])))
        assert out["notices"][0]["payload"]["title"] == ""

    def test_a_tip_whose_tail_is_not_the_title_marker_is_not_a_title_change(self):
        out = _run(message(self._tip([
            {"txt": "x"}, {"txt": "", "param": ["u_member"]}, {"txt": "x"}, {"txt": "大佬"}])))
        assert out["notices"] == []


class TestReaction:
    def _xml(self, uin: str, seq: str, face: str) -> dict:
        content = (f'<gtip><qq jp="{uin}"/><url msgseq="{seq}"/><face id="{face}"/></gtip>')
        return {"elementType": 8, "grayTipElement": {
            "subElementType": 12, "xmlElement": {"templId": "10382", "content": content},
        }}

    def test_a_reaction_is_read_out_of_the_xml_attributes(self):
        out = _run(message(self._xml("100", "555", "14")))
        notice = out["notices"][0]
        assert notice["cmd"] == "notice.group_msg_emoji_like"
        assert notice["payload"]["user_id"] == "100"
        assert notice["payload"]["message_seq"] == "555"
        assert notice["payload"]["likes"] == [{"emoji_id": "14", "count": 1}]

    def test_single_quoted_attributes_also_work(self):
        content = "<gtip><qq jp='100'/><url msgseq='555'/><face id='14'/></gtip>"
        element = {"elementType": 8, "grayTipElement": {
            "subElementType": 12, "xmlElement": {"templId": "10382", "content": content}}}
        out = _run(message(element))
        assert out["notices"][0]["payload"]["message_seq"] == "555"

    def test_a_tip_without_a_sequence_is_skipped(self):
        element = {"elementType": 8, "grayTipElement": {
            "subElementType": 12,
            "xmlElement": {"templId": "10382", "content": '<gtip><qq jp="100"/></gtip>'}}}
        out = _run(message(element))
        assert out["notices"] == []

    def test_a_different_template_produces_nothing(self):
        element = {"elementType": 8, "grayTipElement": {
            "subElementType": 12, "xmlElement": {"templId": "9999", "content": "<gtip/>"}}}
        out = _run(message(element))
        assert out["notices"] == []


class TestEssence:
    def _tip(self, url: str) -> dict:
        return {"elementType": 8, "grayTipElement": {
            "subElementType": 17,
            "jsonGrayTipElement": {"busiId": "0", "jsonStr": json.dumps({"items": [{"jp": url}]})},
        }}

    def test_an_essence_change_reads_the_sequence_out_of_the_url(self):
        out = _run(message(self._tip(
            "https://x/y?groupCode=2169010946&msgSeq=555")))
        notice = out["notices"][0]
        assert notice["cmd"] == "notice.group_essence"
        assert notice["payload"]["message_seq"] == "555"
        assert notice["payload"]["group_id"] == "2169010946"

    def test_an_essence_url_for_another_group_is_ignored(self):
        out = _run(message(self._tip("https://x/y?groupCode=999&msgSeq=555")))
        assert out["notices"] == []

    def test_a_url_without_a_sequence_produces_nothing(self):
        out = _run(message(self._tip("https://x/y?groupCode=2169010946")))
        assert out["notices"] == []
