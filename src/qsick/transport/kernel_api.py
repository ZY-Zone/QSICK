"""Typed access to QQ's kernel services, on top of the control pipe.

``bridge.PipeClient`` gives raw ``eval``; this module turns that into something a
caller can use without writing JavaScript. It is deliberately thin: every method
builds a small expression, sends it through the pipe, and parses the answer. No
business logic lives here, because the authoritative behaviour is inside QQ.

## What is verified, and what is not

Verified against a live instance:

  * ``wrapper.node`` exposes 98 ``NodeIKernel*`` interfaces
  * ``NodeIKernelMsgService`` has 299 prototype methods including ``sendMsg``
  * ``NodeIQQNTWrapperSession`` exposes its per-service getters
  * ``ipc.node`` has ``NodeParentIpcInterface`` / ``NodeChildIpcInterface``

NOT verified: actually sending a message. That needs a logged-in session, and the
call shape depends on which session factory QQ used for this instance. ``send_msg``
below therefore requires the caller to supply the message element shape, and raises a
clear error rather than guessing -- a wrong guess would post malformed content to a
real conversation.

The design goal here is that a caller can *discover* the API from Python
(``methods("msg")``) before committing to a call.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .bridge import PipeClient, PipeError
from .tasks import TaskWindowMissed

#: Accessor on wrapper.node for each service the session exposes.
SERVICE_GETTERS = {
    "msg": "getMsgService",
    "buddy": "getBuddyService",
    "group": "getGroupService",
    "login": "getLoginService",
    "profile": "getProfileService",
    "avatar": "getAvatarService",
}


class KernelError(RuntimeError):
    """Raised when the JS side reports a failure."""


def _js_string(value: str) -> str:
    """Encodes a Python string as a JavaScript string literal."""
    return json.dumps(value, ensure_ascii=False)


@dataclass
class KernelApi:
    """Talks to one pipe instance's kernel services."""

    client: PipeClient

    # -- discovery ---------------------------------------------------------
    def modules(self) -> dict[str, int]:
        """Returns ``{module: export count}`` for the .node addons.

        Only ``wrapper.node`` and ``major.node`` are queried.

        ``ipc.node`` is deliberately excluded. Requiring it through the bridge left the
        JS runtime unable to answer any subsequent eval -- measured: the call itself
        returned 2 exports, then every later request timed out. It is a NAPI addon that
        registers IPC singletons on load, so loading it a second time from outside
        QQ's own module graph destabilises the runtime. Nothing here is worth that;
        the module can be inspected through ``eval`` by a caller who accepts the risk.
        """
        counts: dict[str, int] = {}
        for name in ("wrapper.node", "major.node"):
            try:
                result = self.client.eval(
                    f"String(Object.keys(load({_js_string(name)})).length)")
                counts[name] = int(result.value)
            except (PipeError, TypeError, ValueError):
                # Report per module instead of losing the whole picture.
                counts[name] = -1
        return counts

    def exports(self, module: str = "wrapper.node") -> list[str]:
        """Lists a module's exported names.

        ``ipc.node`` is refused for the reason given in ``modules()``: loading it
        through the bridge destabilises the JS runtime.
        """
        if module == "ipc.node":
            raise KernelError(
                "ipc.node 不能经 bridge 加载：实测加载后后续 eval 全部超时。"
                "它会在加载时注册 IPC 单例，重复加载会破坏运行时状态。")
        result = self.client.eval(
            f"JSON.stringify(Object.keys(load({_js_string(module)})))", raw=True)
        return list(result.value or [])

    def session_methods(self) -> list[str]:
        """Lists the wrapper session's methods (its service getters)."""
        result = self.client.eval(
            "var w=load('wrapper.node');"
            "JSON.stringify(Object.getOwnPropertyNames("
            "w.NodeIQQNTWrapperSession.prototype||{}))",
            raw=True)
        return list(result.value or [])

    def methods(self, service: str = "msg") -> list[str]:
        """Lists a kernel service's methods.

        ``service`` is a key of ``SERVICE_GETTERS`` or a full class name such as
        ``NodeIKernelMsgService``.
        """
        class_name = self._class_name(service)
        result = self.client.eval(
            "var w=load('wrapper.node');"
            f"var C=w[{_js_string(class_name)}];"
            "JSON.stringify(C&&C.prototype?"
            "Object.getOwnPropertyNames(C.prototype):[])",
            raw=True)
        return list(result.value or [])

    def _class_name(self, service: str) -> str:
        """Normalises a short service key or a full class name to the class name."""
        if service.startswith("Node"):
            return service
        # Derived from SERVICE_GETTERS so the two can never drift apart. An earlier
        # version kept a separate literal map, and a key present in one but not the
        # other made every call() raise "unknown service" for a service it supported.
        mapping = {
            key: f"NodeIKernel{key.capitalize()}Service" for key in SERVICE_GETTERS
        }
        if service not in mapping:
            raise KernelError(
                f"未知服务 {service!r}；可用: {sorted(mapping)} 或完整类名")
        return mapping[service]

    # -- calls -------------------------------------------------------------
    def eval(self, code: str, raw: bool = False) -> Any:
        """Escape hatch: runs ``code`` with ``load``/``req``/``appDir`` in scope."""
        result = self.client.eval(code, raw=raw)
        if raw:
            return result.value
        value = result.value
        # The DLL reports JS exceptions as {"__err": "..."} JSON.
        if isinstance(value, str) and value.startswith('{"__err":'):
            try:
                raise KernelError(json.loads(value)["__err"])
            except (json.JSONDecodeError, KeyError):
                raise KernelError(value) from None
        return value

    def call(self, service: str, method: str, *args: Any) -> Any:
        """Calls ``service.method(...)`` if the session exposes that service.

        Returns whatever the method returns, or raises ``KernelError``. The session
        object is looked up through ``NodeIQQNTWrapperSession``'s getter, because that
        is the only way to reach the live instance -- constructing a new service would
        give a detached object with no session behind it.
        """
        class_name = self._class_name(service)
        getter = None
        for key, candidate in SERVICE_GETTERS.items():
            if self._class_name(key) == class_name:
                getter = candidate
                break
        if getter is None:
            # Discoverable from Python, so point at the discovery help rather than
            # leaving the caller to work out that no session getter is known for it.
            known = ", ".join(sorted(self._class_name(k) for k in SERVICE_GETTERS))
            raise KernelError(
                f"{class_name} 没有已知的 session getter，无法从 Python 侧调用。"
                f"已支持: {known}。"
                f"可以先用 session_methods() 看看这个实例暴露了哪些 getter。")

        expr_args = ",".join(_literal(a) for a in args)
        code = (
            "var w=load('wrapper.node');"
            "var S=w.NodeIQQNTWrapperSession;"
            "var s=null;"
            "try{s=(typeof engine!=='undefined'&&engine&&engine.getSession)"
            "?engine.getSession():null;}catch(e){}"
            "if(!s){return JSON.stringify({__err:'no live session; "
            "call session_methods() to inspect the API first'});}"
            f"var svc=s.{getter}();"
            f"if(!svc||typeof svc.{method}!=='function')"
            f"{{return JSON.stringify({{__err:'{method} not available'}});}}"
            f"var r=svc.{method}({expr_args});"
            "return typeof r==='string'?r:JSON.stringify(r===undefined?null:r);"
        )
        return self.eval(code)

    # -- convenience -------------------------------------------------------
    def ping(self) -> bool:
        return self.client.ping()

    def ready(self, timeout: float = 30.0) -> bool:
        return self.client.wait_ready(timeout)


def _literal(value: Any) -> str:
    """Renders a Python value as a JavaScript literal."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    return json.dumps(value, ensure_ascii=False)


def api_for_instance(instance_dir: Any) -> KernelApi:
    """Builds a KernelApi for an instance directory."""
    from pathlib import Path

    return KernelApi(PipeClient(Path(instance_dir).name))


class StartupApi:
    """Kernel API that works through the startup window instead of the pipe.

    ``KernelApi`` talks over the control pipe and is only usable while QQ's JS thread
    is still running, which is a window of about two seconds after injection. This class
    takes the approach that works afterwards: queue the work before the instance starts
    and read the answers back.

    Usage:

        batch = StartupApi(instance_dir)
        batch.add("global.__qsick.methods('NodeIKernelMsgService').length")
        batch.write()                 # must happen before start()
        manager.start(instance)
        results = batch.wait()

    The split is deliberate. A method that silently queued work after launch would
    produce timeouts that look like failures, so the "write before start" step is the
    caller's to make explicit.
    """

    def __init__(self, instance_dir: Any, account: str = "") -> None:
        """``account`` becomes the default session for every task queued here.

        A pipe instance shares the system-configured data root with the real client, so one
        injected DLL can serve more than one session. Naming the account makes each action
        unambiguous; leaving it empty means "whichever session exists", which is right for a
        single-account instance.
        """
        from pathlib import Path
        from .tasks import TaskBatch

        self.instance_path = Path(instance_dir)
        self.batch: TaskBatch = TaskBatch(self.instance_path / "owndata",
                                          default_account=account)

    # -- queueing ----------------------------------------------------------
    def add(self, code: str, task_id: str | None = None,
            account: str | None = None) -> str:
        """Queues a task. Call ``write()`` before starting the instance."""
        return self.batch.add(code, task_id, account=account)

    def add_methods(self, service: str, task_id: str | None = None) -> str:
        """Queues a request for a kernel service's method names."""
        class_name = _class_name_for(service)
        return self.add(
            f"JSON.stringify(global.__qsick.methods('{class_name}'))", task_id)

    def add_exports(self, task_id: str | None = None) -> str:
        """Queues a request for wrapper.node's export names."""
        return self.add("JSON.stringify(global.__qsick.exports())", task_id)

    def write(self) -> None:
        self.batch.write()

    def wait(self, timeout: float = 30.0) -> list:
        return self.batch.wait(timeout)

    # -- convenience -------------------------------------------------------
    def method_names(self, service: str = "msg", timeout: float = 30.0) -> list[str]:
        """Queues a method-list request and waits for the answer.

        Only meaningful as one step of a launch, not as a query after the fact. Two
        honest behaviours follow from that:

        * If the batch has not been written yet, write it now (so a caller who forgot
          gets the expected result instead of silence).
        * If the window has already passed, raise rather than return ``[]``: an empty
          list reads as "this service has no methods", which is a different and wrong
          conclusion.

        The reliable pattern is ``add``/``add_methods`` + ``write()`` + ``start()`` +
        ``wait()``; this helper covers the single-question case.
        """
        task_id = self.add_methods(service, f"methods_{service}")
        if not (self.batch.owndata / "tasks.jsonl").is_file():
            self.write()

        try:
            results = self.wait(timeout)
        except TaskWindowMissed as exc:
            raise KernelError(
                f"没拿到 {service} 的方法列表：启动窗口已经过去。"
                "任务必须在 start() 之前写入，见 StartupApi 的说明。") from exc

        for result in results:
            if result.id != task_id:
                continue
            if not result.ok:
                raise KernelError(f"取 {service} 方法列表失败：{result.error}")
            try:
                return list(json.loads(result.value))
            except (TypeError, json.JSONDecodeError):
                raise KernelError(
                    f"{service} 方法列表不是合法 JSON：{str(result.value)[:120]}")
        raise KernelError(f"没有拿到 {task_id} 的结果")


def _class_name_for(service: str) -> str:
    if service.startswith("Node"):
        return service
    mapping = {k: f"NodeIKernel{k.capitalize()}Service" for k in SERVICE_GETTERS}
    return mapping.get(service, service)
