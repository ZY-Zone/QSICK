"""把 ``protocols/`` 目录挂到 ``sys.path`` 上。

协议层的目录在**项目根**（``{项目根}\\protocols``），不在 ``src`` 下，
因为它要能被用户直接增删——加一个协议就是加一个目录。代价是它不随 ``qsick``
包一起安装，导入方式依赖进程是怎么起来的：

* ``python -m qsick`` —— ``sys.path[0]`` 是项目根，``import protocols`` 能成；
* ``uv run qsick`` / ``qsick.exe``（``start.bat`` 走的这条）—— ``sys.path[0]``
  是 ``.venv\\Scripts``，``import protocols`` 直接 ``ModuleNotFoundError``。

后者曾经让**所有**消息发送都失败（不只是表情：编码入口对每条消息都会调用），
而本地用 ``python -m`` 测又一切正常，所以问题只在真实启动方式下暴露。

这里按包自身位置反推项目根再挂上去，两种启动方式就都成立了。重复调用是安全的。
"""

from __future__ import annotations

import sys
from pathlib import Path

#: ``src/qsick/protocol_bridge.py`` -> 上两层是项目根。
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROTOCOLS_DIR = PROJECT_ROOT / "protocols"


def ensure_importable() -> Path:
    """确保 ``import protocols`` 可用，返回协议目录。

    只有当目录真的存在时才动 ``sys.path``：如果项目被以 wheel 形式安装而没有
    源码树，这里什么都不做，让调用方拿到原本的 ImportError。
    """
    if (PROTOCOLS_DIR / "__init__.py").is_file():
        root = str(PROJECT_ROOT)
        if root not in sys.path:
            sys.path.insert(0, root)
    return PROTOCOLS_DIR


def load_processor(protocol_id: str | None = None, dispatch=None, *, enrich=None):
    """构造一个 :class:`protocols.processor.MessageProcessor`。

    延迟到调用时才导入，避免 ``import qsick`` 就去加载一整层协议实现。
    """
    ensure_importable()
    from protocols.processor import MessageProcessor

    return MessageProcessor(protocol_id, dispatch, enrich=enrich)


def load_protocol(protocol_id: str):
    """按 id 取出协议适配器本身（不是包着它的处理器）。

    分流点需要 :meth:`ChatProtocol.encode_event` 与 :meth:`decode_action`，
    这两个方法在处理器上没有转发——处理器只关心消息段与动作参数。
    """
    ensure_importable()
    from protocols import registry

    return registry.load_protocol(protocol_id)


def protocol_names() -> list[str]:
    """当前登记过的聊天协议 id。注册表读不出来时返回空表而不是抛异常：

    分流点会在每个事件上调用它，一个坏掉的清单不该让消息全部发不出去。
    """
    try:
        ensure_importable()
        from protocols import registry

        return registry.chat_protocol_ids()
    except Exception:  # noqa: BLE001 - 见上面的理由
        return []
