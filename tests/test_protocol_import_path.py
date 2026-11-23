"""协议层的导入路径。

``protocols/`` 在项目根、不在 ``src`` 下，因为它要能被用户直接增删（加一个协议
就是加一个目录）。代价是它不随 ``qsick`` 包安装，能否导入取决于进程怎么起来：

* ``python -m qsick`` —— ``sys.path[0]`` 是项目根，能导入；
* ``uv run qsick`` / ``qsick.exe``（``start.bat`` 走这条）—— ``sys.path`` 里
  没有项目根，``import protocols`` 直接失败。

后者曾经让**所有**消息都发不出去：编码入口对每条消息都会调用，纯文字也不例外。
而本地用 ``python -m`` 测一切正常，所以问题只在真实启动方式下暴露。

聊天栏（``chat_api.py``）已移除，原先那条"在子进程里发一条消息"的测试没有消费方了，
改成直接在同样的导入条件下加载处理器并编码——覆盖的是同一件事：桥必须把
``protocols`` 找回来。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qsick import protocol_bridge


def test_bridge_points_at_the_real_protocols_directory():
    assert protocol_bridge.PROTOCOLS_DIR == ROOT / "protocols"
    assert (protocol_bridge.PROTOCOLS_DIR / "__init__.py").is_file()


def test_bridge_makes_protocols_importable():
    protocol_bridge.ensure_importable()
    import importlib.util

    assert importlib.util.find_spec("protocols") is not None


def test_bridge_loads_a_working_processor():
    processor = protocol_bridge.load_processor("onebotv11")
    segments = processor.encode_outbound([{"kind": "text", "text": "hi"}])
    assert segments == [{"type": "text", "data": {"text": "hi"}}]


def test_encoding_works_without_the_project_root_on_sys_path():
    """复现 ``qsick.exe`` 的导入条件：项目根不在 ``sys.path``。

    这条测试在子进程里跑，因为要真的把项目根从 ``sys.path`` 拿掉——在当前进程
    里做会污染其它测试的导入状态。
    """
    script = """
import sys
sys.path = [p for p in sys.path if p.rstrip("\\\\") != r"__ROOT__" and p != ""]
from qsick.protocol_bridge import load_processor
segments = load_processor("onebotv11").encode_outbound([{"kind": "text", "text": "hello"}])
assert segments == [{"type": "text", "data": {"text": "hello"}}], segments
# 表情在纯 v11 里只剩 id —— 编码路径本身必须仍然可用。
face = load_processor("onebotv11").encode_outbound(
    [{"kind": "emotion", "emotion": {"id": "14", "moveable": True}}])
assert face == [{"type": "face", "data": {"id": "14"}}], face
import importlib.util
assert importlib.util.find_spec("protocols") is not None, "bridge must restore it"
print("ok")
""".replace("__ROOT__", str(ROOT))
    result = subprocess.run([sys.executable, "-c", script], cwd=str(ROOT),
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout
    assert "ModuleNotFoundError" not in result.stderr
