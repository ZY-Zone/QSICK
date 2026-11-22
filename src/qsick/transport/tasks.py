"""Task queue for the startup window.

## Why this exists

The injected DLL can reach QQ's kernel API, but only during a narrow window: QQ's JS
main thread executes napi calls while it initialises and then goes idle, after which
nothing queued can be made to run. Measured on a live instance, 40 attempts at 100 ms
intervals yielded exactly one success, at t=2.1 s.

Rather than pretend the window is not there, this module works with it: tasks are
written to a file *before* the instance starts, and the script that runs during the
window drains them. The client does not have to be fast enough to catch the window,
because it does not have to catch it at all.

## Files

Both live in the instance's ``owndata`` directory:

``tasks.jsonl``    one ``{"id": "...", "code": "..."}`` per line, written by this module
``results.jsonl``  one ``{"id": ..., "ok": bool, "result"|"error": ...}`` per line

Append-only results mean a task that crashes the window still leaves a record of how
far it got.

## What the code can see

Inside a task, these are in scope (published by the DLL's startup script):

  ``global.__qsick.exports()``        names exported by wrapper.node (98 of them)
  ``global.__qsick.methods(cls)``     prototype methods of a kernel service class
  ``global.__qsick.load(name)``       require a .node addon from the app directory
  ``global.__qsick.wrapper()``        the wrapper.node module itself
  ``global.__qsick.locate()``         the app directory
  ``global.__qsick.eval(code)``       evaluate more code
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

TASKS_FILE = "tasks.jsonl"
RESULTS_FILE = "results.jsonl"


class TaskWindowMissed(RuntimeError):
    """Raised when tasks were queued but the startup window produced no results."""


@dataclass
class TaskResult:
    id: str
    ok: bool
    value: Any = None
    error: str | None = None
    #: 这条结果属于哪个账号。``""`` 表示调用时没有指定（只有单一 session 时的常态）。
    #:
    #: 为什么要回传：一个注入的 DLL 现在可能同时服务多个 session，只靠 id 无法判断
    #: 结果属于谁。DLL 会把这个字段原样带回，调用方据此配对。
    account: str = ""

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "TaskResult":
        return cls(
            id=str(row.get("id") or ""),
            ok=bool(row.get("ok")),
            value=row.get("result"),
            error=row.get("error"),
            account=str(row.get("account") or ""),
        )


@dataclass
class TaskBatch:
    """A set of tasks to run during one instance's startup window."""

    owndata: Path
    tasks: list[dict[str, str]] = field(default_factory=list)
    _counter: int = 0
    #: 默认账号。未在 :meth:`add` 里单独指定的任务用它。
    #:
    #: pipe 实例与本机 QQ 共用同一份数据根，一个注入的 DLL 可能服务多个 session，
    #: 所以动作要说明是给哪个账号的。批量任务通常属于同一账号，因此留一个批次级的
    #: 默认值，单条任务仍可覆盖。
    default_account: str = ""

    def add(self, code: str, task_id: str | None = None,
            account: str | None = None) -> str:
        """Queues ``code``; returns the task id used.

        ``account`` selects which session the code acts on. Omit it to inherit
        ``default_account``; when both are empty the task runs against whichever session
        exists, which is the normal case for a single-account instance.
        """
        if task_id is None:
            self._counter += 1
            task_id = f"t{self._counter}"
        row = {"id": task_id, "code": code}
        chosen = self.default_account if account is None else account
        if chosen:
            row["account"] = chosen
        self.tasks.append(row)
        return task_id

    def write(self) -> Path:
        """Writes the queue before launch, clearing any stale results."""
        self.owndata.mkdir(parents=True, exist_ok=True)
        results = self.owndata / RESULTS_FILE
        if results.is_file():
            results.unlink()

        path = self.owndata / TASKS_FILE
        path.write_text(
            "\n".join(json.dumps(t, ensure_ascii=False) for t in self.tasks) + "\n",
            encoding="utf-8",
        )
        return path

    def results(self) -> list[TaskResult]:
        """Reads whatever results exist so far."""
        path = self.owndata / RESULTS_FILE
        if not path.is_file():
            return []
        out: list[TaskResult] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(TaskResult.from_row(json.loads(line)))
            except json.JSONDecodeError:
                continue
        return out

    def wait(self, timeout: float = 30.0,
             interval: float = 0.5) -> list[TaskResult]:
        """Waits until every queued task has a result, or the timeout expires.

        Raises ``TaskWindowMissed`` when nothing at all arrives -- that means the
        startup window was missed, which is a different problem from a task failing and
        should not be reported as one.
        """
        wanted = {t["id"] for t in self.tasks}
        deadline = time.time() + timeout
        while time.time() < deadline:
            found = {r.id for r in self.results()}
            if wanted and wanted <= found:
                return self.results()
            time.sleep(interval)

        got = self.results()
        if not got:
            raise TaskWindowMissed(
                f"启动窗口内没有执行任何任务（{self.owndata / TASKS_FILE}）。"
                "常见原因：DLL 未注入、任务文件写在错误的 owndata、或窗口已过。")
        return got


def batch_for_instance(instance_dir: Path | str,
                       account: str = "") -> TaskBatch:
    """Builds a batch bound to an instance's owndata directory.

    ``account`` becomes the batch default, so every task queued on it addresses that
    session unless a task overrides it. Pass it when one DLL serves several sessions;
    leave it empty for the ordinary single-account instance.
    """
    return TaskBatch(Path(instance_dir) / "owndata", default_account=account)


def run_tasks(instance_dir: Path | str, codes: Iterable[str],
              timeout: float = 30.0) -> list[TaskResult]:
    """Queues ``codes``, tells the caller what to do next, and is not enough alone.

    Kept as a thin helper for tests; real use needs the tasks written before the
    instance starts, so ``batch_for_instance`` plus an explicit ``write()`` is the
    honest API.
    """
    batch = batch_for_instance(instance_dir)
    for code in codes:
        batch.add(code)
    batch.write()
    return batch.wait(timeout)
