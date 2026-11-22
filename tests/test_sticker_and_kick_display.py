"""两处真实缺陷的回归测试。

1. **收藏表情发成了图片**：QQ 用 ``picSubType`` 区分"图片"与"表情"，编码器把
   它写死成 ``NORMAL(0)``，所以收藏表情到了接收端只是一张图片，动图也不被当
   表情渲染。

2. **被踢下线看不到**：宿主确实写了 ``phase: "kicked"`` 和原因，但随后的
   ``onLoginDisConnected``（offline）和 ``requestQr``（waiting_qrcode）会把它
   连续覆盖，用户最后只看到"未登录"——而"为什么掉线"恰恰是最该显示的。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MEDIA = ROOT / "src" / "qsick" / "headless_media.cjs"
HOST = ROOT / "src" / "qsick" / "headless_host.js"
CHAT_JS = ROOT / "src" / "qsick" / "webui" / "static" / "chat.js"
CHAT_API = ROOT / "src" / "qsick" / "webui" / "chat_api.py"


def test_pic_sub_type_is_taken_from_the_caller_not_hard_coded():
    """``picSubType`` 必须可以由调用方指定。

    写死 ``NORMAL`` 就等于"所有上传都是图片"，收藏表情永远发不成表情。
    """
    source = MEDIA.read_text(encoding="utf-8")
    body = source.split("async function prepareImage", 1)[1].split("async function prepareFile", 1)[0]
    assert "data.pic_sub_type" in body, "prepareImage must honour an explicit pic_sub_type"
    # 以前是 `const subType = PicSubType.NORMAL;` —— 写死等于"所有上传都是图片"。
    assert "const subType = PicSubType.NORMAL;" not in body


def test_pic_sub_type_is_range_checked():
    """只接受 QQ 认可的取值，不能让调用方塞任意数字。"""
    source = MEDIA.read_text(encoding="utf-8")
    body = source.split("async function prepareImage", 1)[1].split("async function prepareFile", 1)[0]
    # 取值来自枚举而不是裸数字比较：新增子类型时不会漏掉校验。
    assert "PIC_SUB_TYPES.includes(requested)" in body
    assert "PIC_SUB_TYPES = Object.freeze(Object.values(PicSubType))" in source


def test_the_upload_route_forwards_pic_sub_type():
    """HTTP 层要把它转进消息段，否则前端传了也没用。"""
    source = CHAT_API.read_text(encoding="utf-8")
    assert 'form.get("pic_sub_type")' in source
    assert 'payload_data["pic_sub_type"] = value' in source


def test_the_python_element_layer_keeps_the_picture_subtype():
    """真正的断点在协议层：``_media_in`` 曾用白名单把 sub_type 丢掉。

    前端带着 picSubType=1 提交，HTTP 层也写进了消息段，但 ``Element.from_onebot``
    只搬运 6 个固定键，字段在纯 Python 层就消失了——宿主拿到的 data 里没有
    sub_type，于是 ``prepareImage`` 按 NORMAL(0) 发成普通图片。这条测试盯的是
    转换本身，而不是某段源码里的字符串。
    """
    from qsick.onebot.actions.base import message_to_elements
    from qsick.protocol import elements as E

    # WebUI 上传表单的写法
    webui = message_to_elements([{"type": "image", "data": {"path": "C:/t/f.gif", "pic_sub_type": 1}}])
    assert webui[0]["data"]["sub_type"] == 1, webui
    # OneBot 客户端的写法（CQ 码与数组段都是这个键名）
    cq = message_to_elements("[CQ:image,file=a.gif,sub_type=1]")
    assert cq[0]["data"]["sub_type"] == 1, cq
    array = message_to_elements([{"type": "image", "data": {"file": "a.gif", "sub_type": 1}}])
    assert array[0]["data"]["sub_type"] == 1, array
    # 普通图片不能被凭空加上表情语义
    plain = message_to_elements([{"type": "image", "data": {"path": "C:/t/a.png"}}])
    assert "sub_type" not in plain[0]["data"], plain
    # 出站段也要能把它读回来
    assert E.to_onebot(E.from_onebot({"type": "image", "data": {"file": "x", "sub_type": 1}}))["data"]["sub_type"] == 1


def test_the_host_accepts_both_subtype_field_names():
    """宿主的 prepareImage 同时接受 sub_type 与 pic_sub_type。"""
    source = MEDIA.read_text(encoding="utf-8")
    body = source.split("async function prepareImage", 1)[1].split("async function prepareFile", 1)[0]
    assert "data.sub_type" in body and "data.pic_sub_type" in body
    # 1 要有名字（STICKER），否则读代码的人得回去查 QQ 的枚举。
    assert "STICKER: 1" in source, "the sticker subtype must be named, not a bare 1"


def test_an_animated_sticker_is_labelled_as_a_sticker_not_a_picture():
    """聊天栏里动画表情的占位标签不能是 [图片]。

    ``summary`` 的兜底发生在判断 sub_type 之前，于是 QQ 没给 summary 时动画
    表情一律显示成 ``[图片]``。这里直接断言转换结果，而不是某段源码的写法。
    """
    from qsick.protocol.elements import Element
    from qsick.webui.chat_api import _segment

    sticker = _segment(Element("image", {"file_id": "f", "sub_type": 1}))
    assert sticker["summary"] == "动画表情", sticker
    assert sticker["sticker"] is True
    # 上传表单用的字段名同样要识别
    assert _segment(Element("image", {"file_id": "f", "pic_sub_type": 1}))["summary"] == "动画表情"
    # 普通图片保持"图片"
    plain = _segment(Element("image", {"file_id": "f"}))
    assert plain["summary"] == "图片", plain
    assert plain["sticker"] is False


async def test_a_download_that_arrives_before_the_message_is_not_lost(tmp_path):
    """自动下载经常"不生效"，是因为完成事件可能早于消息入库。

    宿主收到消息就并行开始下载，``message_media`` 与 ``message`` 是两条独立的
    请求。先到的那条以前被 ``set_media_path`` 静默拒绝（行还不存在）并就此丢弃，
    而手动点击总是成功——手动路径要求消息已经在库里。这里覆盖补写逻辑。
    """
    from qsick.config import RuntimeConfig
    from qsick.protocol.elements import Element
    from qsick.protocol.message import Message
    from qsick.runtime import Runtime
    from qsick.transport.base import PacketEvent
    import json

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


def test_favorites_are_sent_as_stickers_not_pictures():
    """收藏表情点击后必须带 picSubType=1。"""
    source = CHAT_JS.read_text(encoding="utf-8")
    assert "addFiles([file], { picSubType: 1 })" in source
    # 上传时要真的带上这个字段
    assert 'form.append("pic_sub_type"' in source


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
