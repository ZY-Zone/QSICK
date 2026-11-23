"""Milky 协议包入口。注册表按目录名加载这里，再取 ``create()``。

目录名 `milky` 是合法的 Python 标识符，理论上可以按名字 import；但注册表统一按**文件路径**
加载（见 `protocols/registry.py`），因为别的协议可能带连字符。两边都支持，保持一条路径。
"""

from __future__ import annotations

from .adapter import MilkyProtocol, create, from_runtime

__all__ = ["MilkyProtocol", "create", "from_runtime"]
