"""maxStore：一个"尽量不丢东西"的存储协议。

包名是 ``maxstore``（Python 导入名不能带大写），目录名是 ``maxStore``，两者不一致是
因为目录由协议加载器按名加载，而导入名要符合 PEP 8。

用法::

    from maxstore import SqliteMaxStore
    store = SqliteMaxStore("data/maxstore.db")
    store.append(raw_event)                       # 原始 dict，原样入库
    store.get(7400000000000000001)                # 完整原始信息
    store.raw_field(7400000000000000001, "/sender/card")
"""

from __future__ import annotations

from .paths import flatten, parse_path, to_pointer
from .schema import SCHEMA_VERSION
from .store import MemoryMaxStore, RawStore, SqliteMaxStore, capture, open_store

__version__ = "1.0.0"

__all__ = [
    "RawStore",
    "SqliteMaxStore",
    "MemoryMaxStore",
    "open_store",
    "capture",
    "flatten",
    "parse_path",
    "to_pointer",
    "SCHEMA_VERSION",
    "__version__",
]
