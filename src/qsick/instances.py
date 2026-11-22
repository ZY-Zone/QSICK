"""实例中心：每个 QQ 实例一套独立的消息处理器。

一个实例（``C0``、``C1`` …）在 QQ 侧就是一个登录的号，它有自己的消息流、自己的
数据库、自己的一组网络配置。把它们混在一个运行时单例里，后果是具体的：

* message_id 会撞车——两个号各自都有 1 号消息，共用一个库就只能丢一条；
* ``self_id`` 会串号——C1 的消息被打上 C0 的号；
* "这个连接该看到哪些消息"无从表达。

所以这里的划分是：**实例拥有消息数据库与处理器，网络配置属于某个实例**。
一句话概括数据流：

    QQ 宿主 --事件--> 实例的处理器
                         |-- 1. 先用 maxStore 把原始 payload 完整落盘（证据）
                         |-- 2. 再按该实例的每一条网络配置，用各自的协议编码后分发

第 1 步在前、且不裁剪字段，是为了第 2 步：不同协议要的字段不一样，存的时候不知
将来谁会要什么，所以宁可多存。这就是 maxStore 存在的理由——它是"保留尽量多的
信息，以便以后兼容更多消息协议"的那一层。

动作方向相反：网络配置收到动作请求 -> 交给所属实例的处理器执行。因此动作不需要
（也不该有）``account`` 字段：它从哪条连接来，就属于哪个实例。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import network_profiles as netprofiles
from .config import RuntimeConfig
from .protocol.message import Message
from .raw_store import RawMessageStore, create_instance_store

_LOGGER = logging.getLogger(__name__)

STORAGE_PROTOCOL = "maxStore"


@dataclass(slots=True)
class NetworkBinding:
    """一条网络配置在运行时的形态。

    ``profiles`` 是该实例下所有启用的配置；每条配置各自选了自己的聊天协议，
    所以同一实例的消息会被编码成多种形状，分别送给对应的连接。
    """

    profile: dict[str, Any]

    @property
    def id(self) -> str:
        return str(self.profile.get("id") or "")

    @property
    def kind(self) -> str:
        return str(self.profile.get("kind") or "")

    @property
    def protocol(self) -> str:
        return str(self.profile.get("protocol") or "")

    @property
    def enabled(self) -> bool:
        return bool(self.profile.get("enabled"))


@dataclass(slots=True)
class InstanceContext:
    """一个实例的全部状态：数据库、处理器、网络配置。"""

    name: str
    store: RawMessageStore
    config: RuntimeConfig
    profiles: list[NetworkBinding] = field(default_factory=list)
    #: 该实例登录的 QQ 号；未登录时为空串。
    account: str = ""
    #: 每种聊胜协议各一个处理器（按需构造并缓存）。
    _processors: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------ 存储协议

    @property
    def storage_protocol(self) -> str:
        return STORAGE_PROTOCOL

    # -------------------------------------------------------------- 处理器

    def processor(self, protocol_id: str, dispatch: Callable | None = None):
        """取（或构造）该实例在某个协议下的处理器。

        缓存以协议 id 为键：同一实例的多条配置可能选同一个协议，各自再构造一份
        只是浪费；处理器本身无状态（除了注入的 dispatch），可以共享。
        """
        from .protocol_bridge import load_processor

        key = protocol_id or ""
        existing = self._processors.get(key)
        if existing is not None:
            return existing
        processor = load_processor(protocol_id or None, dispatch)
        self._processors[key] = processor
        return processor

    def processors(self) -> dict[str, Any]:
        return dict(self._processors)

    # ------------------------------------------------------------ 网络配置

    def bindings(self) -> list[NetworkBinding]:
        return [binding for binding in self.profiles if binding.enabled]

    def protocols_in_use(self) -> list[str]:
        return sorted({binding.protocol for binding in self.bindings() if binding.protocol})


class InstanceHub:
    """按实例名管理 :class:`InstanceContext`。

    惰性建库：只有真的有事件或有人查询时才打开数据库文件，避免启动时就为不存在
    的实例留下一堆空库。
    """

    def __init__(self, config: RuntimeConfig) -> None:
        self.config = config
        self._instances: dict[str, InstanceContext] = {}
        #: 注入的账号来源：``(instance) -> QQ 号``。运行时用它把登录状态喂进来，
        #: 这样实例的账号不是靠猜文件，而是宿主真实告知的。
        self._account_source: Callable[[str], str] | None = None

    def set_account_source(self, source: Callable[[str], str] | None) -> None:
        self._account_source = source

    # ---------------------------------------------------------------- 查询

    def names(self) -> list[str]:
        return sorted(self._instances)

    def get(self, name: str) -> InstanceContext | None:
        return self._instances.get(str(name or ""))

    def __len__(self) -> int:
        return len(self._instances)

    # ---------------------------------------------------------------- 构造

    def instance(self, name: str) -> InstanceContext:
        """取或创建实例上下文。名称为空时回落到 ``C0``。"""
        key = str(name or "").strip() or "C0"
        existing = self._instances.get(key)
        if existing is not None:
            return existing
        # 先读账号，再建库：库目录按 QQ 号命名（见 create_instance_store），所以
        # 号必须在建库之前就知道。先建后读会在登录前就落一个按实例名的库，登录后
        # 还得再建一个按号的库，历史就被拆成两处。
        uin = self._account_of(key)
        context = InstanceContext(
            name=key,
            store=create_instance_store(self.config, key, uin=str(uin or "")),
            config=self.config,
        )
        context.profiles = self._load_profiles(key)
        context.account = uin
        self._instances[key] = context
        _LOGGER.debug("instance ready", extra={"instance": key, "db": str(context.store.path)})
        return context

    def _load_profiles(self, instance: str) -> list[NetworkBinding]:
        """属于该实例的网络配置。

        配置的归属看 ``instance`` 字段。没有该字段的历史配置归 ``C0``——它们诞生于
        单实例时代，属于默认实例是唯一合理的解释。

        归属可能是**实例名**（``Pipe``、``C0``）也可能是**QQ 号**，两者都要认：

        网络配置界面按账号列出（一个号一份配置才说得通——同一条连接不可能同时是两个号
        的），所以那边写入的是 QQ 号；而这里是按实例加载的。只比对实例名会让界面新建的
        配置永远匹配不上，事件一条也转发不出去，且没有任何报错。

        反过来，按号匹配也让多 session 自然成立：两个 session 登录同一个号时，为该号配置
        的连接会同时收到两者的消息——那不是 bug，是"这条配置属于这个号"的直接含义。
        """
        account = ""
        try:
            account = str(self._account_of(instance) or "").strip()
        except Exception:  # noqa: BLE001 - 归属解析失败不该让配置整体失效
            account = ""
        bindings: list[NetworkBinding] = []
        for profile in netprofiles.load(self.config):
            owner = str(profile.get("instance") or profile.get("account") or "C0")
            if owner != instance and not (account and owner == account):
                continue
            bindings.append(NetworkBinding(profile=dict(profile)))
        return bindings

    def _account_of(self, instance: str) -> str:
        """解析实例对应的 QQ 号。

        顺序有讲究，每一级都是实测过真实写入位置的：
          1. 运行时已知的登录状态（最可靠——那是宿主亲口告诉我们的）；
          2. 宿主 ``config.json`` 的 ``qq``（宿主启动时写在这里）；
          3. ``instance.json`` 的 ``qq``（``create()`` 只写空串，通常没有）。
        以前只读第 3 级，而这个键从来没人写过，于是所有实例的账号都是空串，
        按账号找库永远回落到主库——"聊天栏看不到某个号的消息"就是这么来的。
        """
        accounts = getattr(self, "_account_source", None)
        if callable(accounts):
            known = str(accounts(instance) or "").strip()
            if known and known != "0":
                return known
        root = Path(str(getattr(self.config, "headless_root", "") or "")).expanduser()
        if not root.is_absolute():
            root = Path.cwd() / root
        for name in ("config.json", "instance.json"):
            meta = root / instance / name
            if not meta.is_file():
                continue
            try:
                import json

                payload = json.loads(meta.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(payload, dict):
                qq = str(payload.get("qq") or "").strip()
                if qq and qq != "0":
                    return qq
        return ""

    def refresh_accounts(self) -> None:
        """登录状态变化后刷新各实例的账号。

        不刷新的话，实例在首次登录之后才拿到 QQ 号，而 ``context.account``
        仍是空的——WebUI 按账号找库就会一直落到主库上。
        """
        for name, context in self._instances.items():
            account = self._account_of(name)
            if account:
                context.account = account

    def refresh_profiles(self) -> None:
        """配置保存后重新读取，让改动立刻生效。"""
        for name, context in self._instances.items():
            context.profiles = self._load_profiles(name)

    def close(self) -> None:
        for context in self._instances.values():
            try:
                context.store.close()
            except Exception:  # noqa: BLE001 - 关库失败不该掩盖真正的退出异常
                pass
        self._instances.clear()


def instance_of_event(event: Any) -> str:
    """事件属于哪个实例。合成事件（mock/pipe）没有实例名，归 ``C0``。"""
    return str(getattr(event, "instance", "") or "C0")


def store_message(context: InstanceContext, message: Message) -> None:
    """先用 maxStore 完整落盘。

    单独成函数是为了让"先存后分发"这个顺序在代码里显式可见：任何绕过这一步
    去分发的写法，都会让历史数据依赖当时的协议，日后没法补。
    """
    context.store.put(message)
