"""QSICK 协议层。

``protocols/`` 下的每个目录就是一份协议适配器实现：``onebotv11`` 负责 OneBot v11
聊天格式，``milky`` 负责 Milky 格式，``maxStore`` 负责存储。这里只做格式转换，
动作执行仍在 ``src/qsick/onebot/``。

关于 import：目录名可能含非标识符字符（例如连字符），不是合法模块名，
只能按目录路径加载；能按名字 import 的实现（如 ``protocols.onebotv11``）统一
从这里出去。项目根不在 ``src`` 下、也不会被打包进 sys.path，所以 ``import
protocols`` 要求项目根在 sys.path 上：应用从项目根启动，测试文件自带一段
sys.path 引导（见 ``tests/test_protocol_onebotv11.py``）。
"""

from __future__ import annotations

from .base import ChatProtocol, PartError, normalize_part

__all__ = ["ChatProtocol", "PartError", "normalize_part"]
