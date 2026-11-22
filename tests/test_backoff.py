"""退避与熔断：失败时不能把"重试"变成风控加速器。

真实故障链是这样的：

    被踢 → auto_relogin 每 5 秒重启 + quick_login → 立刻又被踢 → 循环

腾讯侧看到的是"同一账号在秒级、规律、持续地重新登录"——这本身就是判定异常
客户端的典型特征，所以越是被风控，重试越会把账号推得更深。

这两份参考实现都把"降频"当正经设计：NapCat 的重启链路刻意串了 5s+3s 的等待，
并写明"不会出现同一账号秒级反复登录"；SnowLuma 宁可让接口慢 60 秒也不高频打
风控敏感的 OIDC 命令。这里把同样的约束落成可测的规则。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qsick.backoff import (
    BASE_DELAY_SECONDS,
    BOOK,
    MAX_ATTEMPTS,
    MIN_INTERVAL_SECONDS,
    BackoffBook,
)


def test_the_first_retry_never_happens_immediately():
    """被踢之后立刻重登是最坏的选择：风控往往还在生效。"""
    book = BackoffBook()
    assert book.ready("C0") is True          # 一开始允许尝试
    delay = book.fail("C0", "boot rejected")
    assert delay >= MIN_INTERVAL_SECONDS
    assert book.ready("C0") is False         # 立刻再问，必须被拒


def test_waits_grow_and_are_capped():
    """每次失败等更久，但不会无限增长到用户以为坏了。"""
    book = BackoffBook()
    delays = [book.fail("C0", "x") for _ in range(MAX_ATTEMPTS - 1)]
    assert delays == sorted(delays), delays
    # 抖动是乘性的，所以永远不会低于下限
    assert all(d >= MIN_INTERVAL_SECONDS for d in delays)
    assert BackoffBook.delay_for(20) <= 30 * 60 * 2


def test_repeated_failures_open_the_circuit():
    """连续失败到达阈值后停下来，把决定权交回用户。"""
    book = BackoffBook()
    for _ in range(MAX_ATTEMPTS):
        book.fail("C0", "still kicked")
    record = book.entry("C0")
    assert record.exhausted is True
    assert book.ready("C0") is False
    assert book.remaining("C0") == float("inf")


def test_one_success_clears_everything():
    """成功登录就该把账清零，包括熔断状态。"""
    book = BackoffBook()
    for _ in range(MAX_ATTEMPTS):
        book.fail("C0", "still kicked")
    assert book.entry("C0").exhausted is True
    book.succeed("C0")
    assert book.ready("C0") is True
    assert book.entry("C0").failures == 0


def test_instances_are_tracked_independently():
    """一个实例被风控不该拖住另一个。"""
    book = BackoffBook()
    for _ in range(MAX_ATTEMPTS):
        book.fail("C0", "x")
    assert book.ready("C0") is False
    assert book.ready("C1") is True


def _kicked_instance(root, delay_seconds=1800):
    import json
    import time

    path = root / "C0"
    (path / "data").mkdir(parents=True)
    # host.js marks the directory as a headless instance; without it the manager does not
    # recognise the directory and every assertion about it fails with an empty list.
    (path / "host.js").write_bytes(b"// fake host")
    (path / "data" / "status.json").write_text(json.dumps({
        "id": "C0", "phase": "kicked", "pid": 0, "updated_at": time.time(),
        "error": "账号已在其他设备登录",
    }), encoding="utf-8")
    (path / "instance.json").write_text(
        json.dumps({"autoRelogin": True, "autoReloginDelay": delay_seconds}), encoding="utf-8")
    return path


def test_the_kick_watcher_waits_for_the_selected_delay(tmp_path, monkeypatch):
    """识别到被踢就开计时器，没等满档位一律不动。"""
    import time

    from qsick import backoff, headless

    _kicked_instance(tmp_path, 1800)
    clock = backoff.KickClock()
    monkeypatch.setattr(backoff, "KICK_CLOCK", clock)
    monkeypatch.setattr(backoff, "BOOK", BackoffBook())

    calls: list[str] = []
    manager = headless.HeadlessManager(root=tmp_path)
    monkeypatch.setattr(manager, "recover_kicked",
                        lambda iid, port=0, **kw: calls.append(iid) or {"account": {}, "relogin": True})

    # 第一次检查：开始计时，不动作
    manager.watch_kicked(core_port=1)
    assert calls == []
    started = clock.since("C0")
    assert started is not None, "识别到被踢就该开始计时"

    # 反复检查不会把时钟重置——否则永远等不到
    for _ in range(5):
        manager.watch_kicked(core_port=1)
    assert clock.since("C0") == started

    # 等满档位才放行
    clock.clear("C0")
    clock.start("C0", now=time.time() - 1800)
    manager.watch_kicked(core_port=1)
    assert calls == ["C0"]


def test_a_healthy_instance_clears_the_kick_clock(tmp_path, monkeypatch):
    """重新登录成功后计时清零，下次被踢重新等满整个档位。"""
    import json
    import time

    from qsick import backoff, headless

    path = _kicked_instance(tmp_path, 60)
    clock = backoff.KickClock()
    monkeypatch.setattr(backoff, "KICK_CLOCK", clock)
    monkeypatch.setattr(backoff, "BOOK", BackoffBook())

    manager = headless.HeadlessManager(root=tmp_path)
    monkeypatch.setattr(manager, "recover_kicked",
                        lambda iid, port=0, **kw: {"account": {}, "relogin": True})
    manager.watch_kicked(core_port=1)
    assert clock.since("C0") is not None

    (path / "data" / "status.json").write_text(json.dumps({
        "id": "C0", "phase": "ready", "pid": 1, "updated_at": time.time(),
    }), encoding="utf-8")
    manager.watch_kicked(core_port=1)
    assert clock.since("C0") is None


def test_turning_auto_relogin_off_clears_the_clock(tmp_path, monkeypatch):
    from qsick import backoff, headless

    _kicked_instance(tmp_path, 60)
    clock = backoff.KickClock()
    monkeypatch.setattr(backoff, "KICK_CLOCK", clock)
    monkeypatch.setattr(backoff, "BOOK", BackoffBook())

    manager = headless.HeadlessManager(root=tmp_path)
    monkeypatch.setattr(manager, "recover_kicked",
                        lambda iid, port=0, **kw: {"account": {}, "relogin": True})
    manager.watch_kicked(core_port=1)
    assert clock.since("C0") is not None

    manager.set_auto_relogin("C0", False)
    manager.watch_kicked(core_port=1)
    assert clock.since("C0") is None


def test_the_kick_clock_never_restarts_on_repeated_polls():
    """起点必须只记一次，否则每次轮询都会把倒计时推后。"""
    from qsick.backoff import KickClock

    clock = KickClock()
    first = clock.start("C0", now=1000.0)
    again = clock.start("C0", now=9999.0)
    assert first == again == 1000.0
    assert clock.elapsed("C0", now=1060.0) == 60.0
    assert clock.elapsed_enough("C0", 60, now=1060.0) is True
    assert clock.elapsed_enough("C0", 61, now=1060.0) is False
