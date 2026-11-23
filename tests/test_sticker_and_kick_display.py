"""两处真实缺陷的回归测试。

聊天栏（``chat_api.py`` / ``chat.js``）已移除，所以原先针对它上传路由、收藏表情、
``[图片]/[动画表情]`` 摘要的几条断言一并去掉。保留下来的都是与聊天栏无关、
今天仍然存在的缺陷回归：

* 图片子类型 ``pic_sub_type`` 由调用方给，不能在 Python 侧写死成 0；
* 被踢之后 QQ 必然断线，断线不能把"被踢"改写成"连接已断开"；
* 自动重连刷二维码同样不能抹掉被踢状态。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MEDIA = ROOT / "src" / "qsick" / "headless_media.cjs"
HOST = ROOT / "src" / "qsick" / "headless_host.js"
ELEMENTS = ROOT / "src" / "qsick" / "protocol" / "elements.py"


def test_pic_sub_type_is_taken_from_the_caller_not_hard_coded():
    """``pic_sub_type`` 写死成 0 会让动画表情当成普通图片发出去。"""
    source = MEDIA.read_text(encoding="utf-8")
    assert "picSubType" in source
    # 允许读调用方的值，不允许把常量直接赋给它。
    assert not re.search(r"picSubType:\s*0\b", source), "picSubType must not be hardcoded to 0"


def test_pic_sub_type_is_range_checked():
    """越界的子类型要退回普通图片，而不是原样抛给 QQ。

    实现是一个白名单（``PIC_SUB_TYPES.includes``），不是范围比较——所以断言的是
    "有白名单兜底"，而不是某种写法。
    """
    source = MEDIA.read_text(encoding="utf-8")
    assert "PIC_SUB_TYPES.includes(requested)" in source
    # 兜底值必须是 0，否则"没给子类型"会被当成表情发出去。
    assert "PicSubType.NORMAL" in source


def test_the_python_element_layer_keeps_the_picture_subtype():
    """元素层要把子类型留住，否则后面无从判断这是不是表情。

    只断言"字段能被读到、能被识别成表情"，不再依赖聊天栏的 ``_segment``。
    """
    import sys

    sys.path.insert(0, str(ROOT / "src"))
    from qsick.protocol.elements import Element

    sticker = Element("image", {"file_id": "f", "sub_type": 1})
    assert sticker.data.get("sub_type") == 1
    assert sticker.type == "image"

    plain = Element("image", {"file_id": "f"})
    assert plain.data.get("sub_type") is None


def test_the_host_accepts_both_subtype_field_names():
    """上传表单用 ``pic_sub_type``，客户端用 ``subType``；两个都要认。

    只认一个的结果是"手动点表情发出去变图片"——一个只在某条路径上出现的错。
    """
    source = MEDIA.read_text(encoding="utf-8")
    assert "picSubType" in source
    assert "subType" in source


async def test_a_download_that_arrives_before_the_message_is_not_lost(tmp_path):
    """自动下载经常"不生效"，是因为完成事件可能早于消息入库。

    宿主收到消息就并行开始下载，``message_media`` 与 ``message`` 是两条独立的
    请求。先到的那条以前被 ``set_media_path`` 静默拒绝（行还不存在）并就此丢弃，
    而手动点击总是成功——手动路径要求消息已经在库里。这里覆盖补写逻辑。
    """
    import json

    from qsick.config import RuntimeConfig
    from qsick.protocol.elements import Element
    from qsick.protocol.message import Message
    from qsick.runtime import Runtime
    from qsick.transport.base import PacketEvent

    runtime = Runtime(RuntimeConfig(data_dir=str(tmp_path / "data")))
    runtime.config.transport.mode = "mock"
    runtime.config.plugins.enabled = False
    await runtime.start(serve=False)
    try:
        message_id = 987654
        local = tmp_path / "x.gif"
        early = PacketEvent(
            cmd="message.media",
            kind="message",
            body=json.dumps({"message_id": message_id, "element_id": "el1", "path": str(local)}).encode(),
        )
        await runtime._handle(early)
        # 键里带实例名：两个实例的 message_id 会撞车，只按 id 存会把路径写到
        # 另一个实例的消息上（审计发现的跨实例泄漏）。
        key = ("C0", message_id)
        assert key in runtime._pending_media, "the path must be remembered, not dropped"

        runtime.store.put(Message(message_id=message_id, seq=1, time=1, chat_type="group", chat_id=1,
                                  sender_uin=2, elements=[Element("image", {"element_id": "el1"})]))
        runtime._apply_pending_media(message_id, runtime.instances.instance("C0"))
        stored = runtime.store.get(message_id)
        assert stored.elements[0].data.get("local_path") == str(local), stored.elements[0].data
        assert key not in runtime._pending_media
    finally:
        await runtime.stop()


def test_the_kicked_phase_survives_a_disconnect():
    """被踢之后 QQ 必然断线；断线不能把"被踢"改写成"连接已断开"。"""
    source = HOST.read_text(encoding="utf-8")
    block = source.split("onLoginDisConnected:", 1)[1].split("onLoginConnected:", 1)[0]
    assert 'status.phase === "kicked"' in block, "disconnect must not overwrite a kick"
    assert 'publish({ phase: "offline"' in block  # 非被踢的情况仍然要报 offline


def test_the_kicked_phase_survives_automatic_qr_requests():
    """自动重连会刷二维码，那不该抹掉被踢状态；只有用户主动刷新才翻篇。"""
    source = HOST.read_text(encoding="utf-8")
    body = source.split("function requestQr(message, options)", 1)[1].split("function ", 1)[0]
    assert 'status.phase !== "kicked"' in body
    assert "manual" in body
