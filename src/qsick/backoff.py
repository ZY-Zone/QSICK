"""退避与熔断：避免把"重试"变成一种风险行为。

这一层的存在理由来自账号被风控踢下线时的真实行为链：

    被踢 → auto_relogin 立刻重启 → 1.5 秒后 quick_login → 再次被踢 → 循环

每 5 秒一轮。腾讯侧看到的是"同一个账号在持续、规律、秒级地重新登录"，而这本身
就是判定异常客户端的典型特征——**重试越勤，风控咬得越紧**，账号寿命反而更短。

参考 SnowLuma / NapCat 的取舍（两者都以"风控"为由主动降频）：

* NapCat 的重启链路是"prepare → 等 5s → shutdown → kill → 再等 3s → 重启"，并
  明确写着"不会出现同一账号秒级反复登录"；10 秒内崩 3 次就停止重拉。
* SnowLuma 宁可让 `get_group_shut_list` 有 60s 延迟，也不高频打 `0xfe7`——注释
  直言"高频会触发腾讯封号"。

所以这里的策略是**指数退避 + 硬性冷却 + 熔断**：

1. 每次失败等待时间翻倍（带抖动，避免多实例同步尖峰）；
2. 任何一次重登之间都有最小间隔，绝不做秒级重试；
3. 连续失败到阈值就停下来，把决定权交回用户，而不是无限重试。

注意：退避不改变"能不能登录成功"，它只改变"失败时我们对服务器做了什么"。
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field


#: 两个实例的重登之间至少隔这么久。秒级重登是最典型的风控告警信号。
MIN_INTERVAL_SECONDS = 60.0

#: 首次失败后的等待，之后每次翻倍。
BASE_DELAY_SECONDS = 60.0

#: 单次等待的上限：再久用户会以为坏了。
MAX_DELAY_SECONDS = 30 * 60.0

#: 连续失败这么多次就熔断，等用户手动处理。
MAX_ATTEMPTS = 5

#: 抖动比例：多个实例同时被踢时，错开它们的重登时刻。
JITTER_RATIO = 0.25


@dataclass
class Attempt:
    """一条重试记录，用来算出下一次该等多久。"""

    failures: int = 0
    last_at: float = 0.0
    next_allowed_at: float = 0.0
    opened: bool = False
    last_error: str = ""

    @property
    def exhausted(self) -> bool:
        return self.opened or self.failures >= MAX_ATTEMPTS


@dataclass
class BackoffBook:
    """按 key（实例 id）记录重试状态。

    刻意做成进程内的内存表而不是落盘：退避状态属于"这次运行"，重启程序后重新
    计算更合理——用户重启 QSICK 通常意味着他已经决定再试一次。
    """

    _entries: dict[str, Attempt] = field(default_factory=dict)

    def entry(self, key: str) -> Attempt:
        return self._entries.setdefault(key, Attempt())

    def ready(self, key: str, *, now: float | None = None) -> bool:
        """现在允许尝试吗？"""
        moment = time.time() if now is None else now
        record = self.entry(key)
        if record.exhausted:
            return False
        return moment >= record.next_allowed_at

    def remaining(self, key: str, *, now: float | None = None) -> float:
        """距离下一次允许尝试还有多少秒（0 表示现在就可以）。"""
        moment = time.time() if now is None else now
        record = self.entry(key)
        if record.exhausted:
            return float("inf")
        return max(0.0, record.next_allowed_at - moment)

    def fail(self, key: str, error: str = "", *, now: float | None = None) -> float:
        """记一次失败，返回下一次允许尝试前要等的秒数。"""
        moment = time.time() if now is None else now
        record = self.entry(key)
        record.failures += 1
        record.last_at = moment
        record.last_error = str(error or "")
        if record.failures >= MAX_ATTEMPTS:
            record.opened = True
            record.next_allowed_at = float("inf")
            return float("inf")
        delay = self.delay_for(record.failures)
        record.next_allowed_at = moment + delay
        return delay

    def succeed(self, key: str) -> None:
        """成功一次就把账清空（连熔断也一并解除）。"""
        self._entries.pop(key, None)

    def reset(self, key: str) -> None:
        """用户手动干预后重新开始计数。"""
        self._entries.pop(key, None)

    @staticmethod
    def delay_for(failures: int) -> float:
        """第 ``failures`` 次失败之后应该等多久。

        指数增长 + 抖动。抖动是乘性的（只在正方向），保证结果永远不小于
        ``MIN_INTERVAL_SECONDS``——宁可等久一点，也不要在风控期里贴着下限试探。
        """
        exponent = max(0, failures - 1)
        raw = BASE_DELAY_SECONDS * (2 ** exponent)
        raw = min(raw, MAX_DELAY_SECONDS)
        jitter = 1.0 + random.random() * JITTER_RATIO
        return max(MIN_INTERVAL_SECONDS, raw * jitter)


#: 进程内共享的退避账本。运行时与无头管理器都从它取状态。
BOOK = BackoffBook()


class KickClock:
    """记录"第一次发现某实例被踢"的时刻。

    这解决一个很具体的陷阱：``watch_kicked`` 每 5 秒跑一次，如果拿"现在"当起点，
    那么每次检查都会把倒计时重置，实例永远等不到重登；如果拿"上次重登时刻"当
    起点，则从未重登过的实例会立刻被放行。

    所以起点必须是**首次观测到被踢的那一刻**，并且只记一次——之后再怎么轮询
    都不会改写它。清空的唯一时机是"不再被踢"（重新登录成功、或用户关闭开关），
    这样下一次被踢会重新等满整个档位。
    """

    def __init__(self) -> None:
        self._since: dict[str, float] = {}

    def start(self, key: str, *, now: float | None = None) -> float:
        """登记被踢时刻；已登记过就原样返回，不重置。"""
        moment = time.time() if now is None else now
        return self._since.setdefault(key, moment)

    def clear(self, key: str) -> None:
        self._since.pop(key, None)

    def since(self, key: str) -> float | None:
        return self._since.get(key)

    def elapsed(self, key: str, *, now: float | None = None) -> float | None:
        started = self._since.get(key)
        if started is None:
            return None
        moment = time.time() if now is None else now
        return max(0.0, moment - started)

    def elapsed_enough(self, key: str, delay_seconds: float, *,
                       now: float | None = None) -> bool:
        """距离首次被踢是否已经等满 ``delay_seconds``。"""
        waited = self.elapsed(key, now=now)
        if waited is None:
            return False
        return waited >= max(0.0, float(delay_seconds))

    def remaining(self, key: str, delay_seconds: float, *,
                  now: float | None = None) -> float | None:
        """还要等多久才到档位；未登记返回 ``None``。"""
        waited = self.elapsed(key, now=now)
        if waited is None:
            return None
        return max(0.0, float(delay_seconds) - waited)


#: 被踢计时器，按实例 id 记录。
KICK_CLOCK = KickClock()


def describe_remaining(seconds: float) -> str:
    """把等待秒数变成人话，给日志/界面用。"""
    if seconds == float("inf"):
        return "已停止自动重试"
    if seconds <= 0:
        return "就绪"
    if seconds < 90:
        return f"{int(seconds)} 秒后"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} 分钟后"
    return f"{minutes // 60} 小时 {minutes % 60} 分钟后"
