"""出站节流：让 QSICK 的动作节奏不要像脚本。

理由不是"礼貌"，而是**账号存活**。参考实现里最值钱的约束都在这一层：

* SnowLuma 宁可让 ``get_group_shut_list`` 的返回慢 60 秒，也不高频打 ``0xfe7``，
  注释直言"高频会触发腾讯封号"；
* NapCat 的重启链路刻意串了 5s+3s 的等待，并写明"不会出现同一账号秒级反复登录"。

它们都不是在隐藏什么，而是**真的少发请求**。这是本地隐藏替代不了的：本地藏得
再深，服务端统计到的请求密度仍然是真实的。

这里做三件事：

1. **同类动作最小间隔**：短时间反复调同一个敏感动作（群列表、成员列表）是最
   典型的脚本特征；
2. **发送抖动**：人不会每 200ms 精确发一条，固定节拍本身就是特征；
3. **突发上限**：允许偶尔连发几条（人也会），但不会无限连发。

刻意不做的事：不伪造时间戳、不改包内容、不干预任何上报。节流只减少我们自己
制造的请求，不改写事实。
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field


#: 敏感动作的最小间隔（秒）。这些接口被高频调用最容易触发风控。
#:
#: 依据是 SnowLuma 的取舍：它把群成员清单做成 ~60s TTL 的缓存，明确因为
#: "0xfe7 高频会触发腾讯封号"。
#:
#: 关键改动：这套间隔现在只约束**真正发出去的请求**。以前它是"让调用方等"，
#: 而实现在 `Runtime.invoke` 里对同一批动作做了结果缓存（TTL 见那里的
#: `_ACTION_CACHE_TTL`），重复调用直接命中缓存、不经过这里。于是：
#:
#:   * 出站频率没有变高——同一动作最快也只是每 TTL 一次真实请求
#:   * 调用方不再为一次读操作等 30-48 秒
#:
#: 实测这一条就是"动作执行太慢"的全部原因：`get_group_member_list` 每次
#: 24-46 秒，而底层请求在客户端已缓存时只要 0.2 秒。云崽每条消息都会调它。
#:
#: 间隔从 30/15/10 统一降到 10，是为了和 60 秒的缓存 TTL 配合：`record()` 之后
#: 要静默 `base*4` 秒才恢复突发额度，10*4=40 秒 < 60 秒缓存期，所以每次缓存
#: 过期后的那次真实请求正好落在突发额度内，不会再额外等一轮。
SENSITIVE_INTERVALS: dict[str, float] = {
    "get_group_member_list": 10.0,
    "get_group_list": 10.0,
    "get_group_info": 10.0,
    "get_group_member_info": 10.0,
    "get_friend_list": 10.0,
    "get_stranger_info": 10.0,
    "get_group_honor_info": 10.0,
    "get_group_shut_list": 10.0,
    "get_recent_contact": 10.0,
}

#: 消息发送的最小间隔（秒）。给人留出"读完再回"的余地。
SEND_MIN_INTERVAL = 1.2

#: 抖动比例：实际间隔在 [min, min*(1+ratio)) 之间随机。
JITTER_RATIO = 0.6

#: 允许的突发条数：人可以连发几条，但不该是机器式的无限连发。
BURST_ALLOWANCE = 3


@dataclass
class _Bucket:
    """一个动作的发送节奏。"""

    next_at: float = 0.0
    burst: int = 0
    last_at: float = 0.0


@dataclass
class Throttle:
    """按动作名记录节奏，必要时让调用方等一下。

    ``wait`` 返回需要等待的秒数；调用方 ``await asyncio.sleep`` 即可。做成"先算
    再等"而不是内部 sleep，是为了可测试——测试里不必真的等 30 秒。
    """

    #: 关掉后完全放行（供测试或用户显式关闭）。
    enabled: bool = True
    _buckets: dict[str, _Bucket] = field(default_factory=dict)

    def _interval_for(self, action: str) -> float:
        if action in SENSITIVE_INTERVALS:
            return SENSITIVE_INTERVALS[action]
        if action.endswith("_msg") or action in ("send_group_msg", "send_private_msg"):
            return SEND_MIN_INTERVAL
        return 0.0

    def wait_for(self, action: str, *, now: float | None = None) -> float:
        """算出这次调用前应该等多久（秒）。"""
        if not self.enabled:
            return 0.0
        base = self._interval_for(action)
        if base <= 0:
            return 0.0
        moment = time.time() if now is None else now
        bucket = self._buckets.setdefault(action, _Bucket())

        # 突发额度：短时间内允许连发几条，之后就要守间隔。
        if moment - bucket.last_at > base * 4:
            bucket.burst = BURST_ALLOWANCE
        if bucket.burst > 0 and moment >= bucket.next_at:
            bucket.burst -= 1
            bucket.last_at = moment
            return 0.0

        return max(0.0, bucket.next_at - moment)

    def record(self, action: str, *, now: float | None = None) -> None:
        """登记一次真正发出的调用，据此推算下次最早可发时刻。"""
        if not self.enabled:
            return
        base = self._interval_for(action)
        if base <= 0:
            return
        moment = time.time() if now is None else now
        bucket = self._buckets.setdefault(action, _Bucket())
        delay = base * (1.0 + random.random() * JITTER_RATIO)
        bucket.next_at = moment + delay
        bucket.last_at = moment

    async def gate(self, action: str) -> float:
        """等待到允许发送为止，返回实际等待的秒数。"""
        waited = self.wait_for(action)
        if waited > 0:
            await asyncio.sleep(waited)
        self.record(action)
        return waited

    def reset(self, action: str | None = None) -> None:
        if action is None:
            self._buckets.clear()
        else:
            self._buckets.pop(action, None)


#: 进程内共享的出站节流器。
THROTTLE = Throttle()
