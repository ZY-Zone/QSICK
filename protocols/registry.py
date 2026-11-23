"""协议注册表：按名字加载 ``protocols/`` 下的适配器。

目录名就是协议名。名字里允许出现连字符之类不是合法 Python 标识符的字符，所以
这里按**文件路径**动态加载，而不是 ``import``。这也让"加一个协议"变成"加一个
目录"，不需要改动任何 import 语句。

``enabled_protocol.json`` 是这份注册表的清单文件，但注册表**以磁盘为准**：
清单里列了目录却没有实现，加载时明确报错；目录存在却没登记，则不暴露给用户。
两者不一致时宁可吵起来，也不要静默地少一个协议。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

ROOT = Path(__file__).resolve().parent
MANIFEST = ROOT / "enabled_protocol.json"

#: 存储协议不是聊天协议，不能出现在协议下拉框里。
STORE_PROTOCOL = "maxStore"


class ProtocolError(RuntimeError):
    """注册表与磁盘不一致，或适配器不符合契约。"""


def _load_manifest() -> dict[str, Any]:
    try:
        payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProtocolError(f"无法读取协议清单 {MANIFEST.name}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ProtocolError("协议清单必须是 JSON 对象")
    return payload


def chat_protocol_ids() -> list[str]:
    """清单里登记的聊天协议 id，按顺序返回。"""
    manifest = _load_manifest()
    entries = manifest.get("chat")
    if not isinstance(entries, list):
        raise ProtocolError("协议清单缺少 chat 数组")
    ids: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        identifier = str(entry.get("id") or "").strip()
        if identifier:
            ids.append(identifier)
    if not ids:
        raise ProtocolError("协议清单没有登记任何聊天协议")
    return ids


def default_protocol_id() -> str:
    """清单里标了 ``default`` 的协议；没有标记时取第一个。"""
    manifest = _load_manifest()
    for entry in manifest.get("chat") or []:
        if isinstance(entry, dict) and entry.get("default"):
            identifier = str(entry.get("id") or "").strip()
            if identifier:
                return identifier
    return chat_protocol_ids()[0]


def describe_protocols() -> list[dict[str, Any]]:
    """给网络配置下拉框用的协议说明。"""
    manifest = _load_manifest()
    described: list[dict[str, Any]] = []
    for entry in manifest.get("chat") or []:
        if not isinstance(entry, dict):
            continue
        identifier = str(entry.get("id") or "").strip()
        if not identifier:
            continue
        described.append({
            "id": identifier,
            "name": str(entry.get("name") or identifier),
            "description": str(entry.get("description") or ""),
            "emotions": str(entry.get("emotions") or ""),
            "default": bool(entry.get("default")),
            "available": (ROOT / identifier / "adapter.py").is_file(),
        })
    return described


def _load_package() -> None:
    """确保 ``protocols`` 包本身已按正常路径注册。

    各适配器内部用相对 import（``from ..base import ...``），这要求父包在
    ``sys.modules`` 里且带 ``__path__``。带连字符的目录没法按名字 import，
    但父包可以——所以先按路径把 ``protocols`` 注册好，再加载子目录。
    """
    if "protocols" in sys.modules:
        return
    spec = importlib.util.spec_from_file_location(
        "protocols", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    if spec is None or spec.loader is None:
        raise ProtocolError("无法注册 protocols 包")
    package = importlib.util.module_from_spec(spec)
    sys.modules["protocols"] = package
    spec.loader.exec_module(package)


def _module_name(protocol_id: str) -> str:
    """把目录名变成可缓存、可相对 import 的模块名。"""
    return "protocols." + protocol_id.replace("-", "_").replace(".", "_")


def _load_module(protocol_id: str) -> ModuleType:
    cached = sys.modules.get(_module_name(protocol_id))
    if cached is not None:
        return cached
    directory = ROOT / protocol_id
    if not (directory / "__init__.py").is_file():
        raise ProtocolError(f"协议 {protocol_id!r} 没有 __init__.py")
    _load_package()
    name = _module_name(protocol_id)
    spec = importlib.util.spec_from_file_location(
        name, directory / "__init__.py",
        submodule_search_locations=[str(directory)])
    if spec is None or spec.loader is None:
        raise ProtocolError(f"无法加载协议包 {protocol_id!r}")
    package = importlib.util.module_from_spec(spec)
    sys.modules[name] = package
    spec.loader.exec_module(package)
    return package


def load_protocol(protocol_id: str):
    """按 id 构造一份聊天协议适配器。"""
    identifier = str(protocol_id or "").strip()
    if not identifier:
        raise ProtocolError("协议 id 不能为空")
    if identifier == STORE_PROTOCOL:
        raise ProtocolError("maxStore 是存储协议，不能作为聊天协议使用")
    if identifier not in chat_protocol_ids():
        raise ProtocolError(f"协议 {identifier!r} 未在 enabled_protocol.json 登记")
    package = _load_module(identifier)
    creator = getattr(package, "create", None)
    if not callable(creator):
        raise ProtocolError(f"协议 {identifier!r} 没有导出 create()")
    protocol = creator()
    if not hasattr(protocol, "encode_message") or not hasattr(protocol, "decode_message"):
        raise ProtocolError(f"协议 {identifier!r} 不符合 ChatProtocol 契约")
    return protocol
