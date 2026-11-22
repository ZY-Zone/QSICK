"""出站节流：让请求密度不像脚本。

这一层是两份参考实现里最值钱的部分，而且它**不靠隐藏**：

* SnowLuma 宁可让群禁言清单慢 60 秒，也不高频打风控敏感的命令，注释直言
  "高频会触发腾讯封号"；
* NapCat 的重启链路刻意串了 5s+3s 的等待，写明"不会出现同一账号秒级反复登录"。

它们真的减少了发给服务器的请求，而不是把请求藏起来。本地隐藏改变不了服务端
统计到的密度，所以这一层才是能影响账号存活的地方。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qsick.throttle import (
    BURST_ALLOWANCE,
    JITTER_RATIO,
    SEND_MIN_INTERVAL,
    SENSITIVE_INTERVALS,
    Throttle,
)


def test_reading_a_group_list_twice_in_a_row_is_delayed():
    """连续拉群列表是最典型的脚本特征。"""
    throttle = Throttle()
    action = "get_group_list"
    assert throttle.wait_for(action) == 0.0   # 第一次放行
    throttle.record(action)
    wait = throttle.wait_for(action)
    assert wait > 0, "紧接着的第二次必须被推迟"
    # 间隔带抖动，所以上限是最小间隔的 (1 + JITTER_RATIO) 倍。
    assert wait <= SENSITIVE_INTERVALS[action] * (1 + JITTER_RATIO)


def test_the_burst_allowance_is_bounded():
    """允许偶发连发，但不是无限连发。"""
    throttle = Throttle()
    action = "send_group_msg"
    allowed = 0
    for _ in range(BURST_ALLOWANCE + 5):
        if throttle.wait_for(action) == 0.0:
            allowed += 1
            throttle.record(action)
    assert allowed <= BURST_ALLOWANCE, f"burst 额度失效，放行了 {allowed} 次"


def test_delays_carry_jitter_so_the_cadence_is_not_a_metronome():
    """固定节拍本身就是特征：每次间隔应当不完全相同。"""
    throttle = Throttle()
    intervals = []
    base = 10_000.0
    for index in range(8):
        action = "get_group_info"
        now = base + index * 3600  # 每次间隔一小时，避开突发额度
        throttle.record(action, now=now)
        bucket = throttle._buckets[action]
        intervals.append(round(bucket.next_at - now, 3))
    assert len(set(intervals)) > 1, f"间隔完全相同，没有抖动：{intervals}"


def test_unlisted_actions_are_not_delayed():
    """只对敏感动作与发送限速；普通查询不该被拖慢。"""
    throttle = Throttle()
    assert throttle.wait_for("get_login_info") == 0.0
    throttle.record("get_login_info")
    assert throttle.wait_for("get_login_info") == 0.0


def test_disabling_the_throttle_lets_everything_through():
    """测试与排障需要一键关闭。"""
    throttle = Throttle(enabled=False)
    action = "get_group_list"
    for _ in range(10):
        assert throttle.wait_for(action) == 0.0
        throttle.record(action)


def test_sending_keeps_a_minimum_gap():
    """消息发送之间留出"读完再回"的余地。"""
    throttle = Throttle()
    throttle.record("send_private_msg")
    wait = throttle.wait_for("send_private_msg")
    # 用掉突发额度后才会等；这里先耗尽它
    for _ in range(BURST_ALLOWANCE):
        throttle.record("send_private_msg")
    wait = throttle.wait_for("send_private_msg")
    assert wait > 0
    assert wait <= SEND_MIN_INTERVAL * 1.6
