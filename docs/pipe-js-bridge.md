# 在 QQ 进程内执行 JavaScript 并导出 API

pipe 实例的注入 DLL 能在 `QQ.exe` 里执行 JavaScript，并通过命名管道或任务队列把结果
交给 QSICK。本文记录**怎么做到的**、**为什么前面几条路不通**，以及**实测边界**。

## 结果

已验证的 API 面（真实实例，非推断）：

| 对象 | 结果 |
| --- | --- |
| `wrapper.node` | 98 个 `NodeIKernel*` 导出 |
| `NodeIKernelMsgService` | 299 个方法，含 `sendMsg` |
| `NodeIKernelGroupService` | 243 个方法 |
| `NodeIQQNTWrapperSession` | 90 个方法（各服务 getter + `init`） |
| `NodeIQQNTWrapperEngine` | 12 个方法，`get()` 每次返回新包装对象 |
| `ipc.node` | `NodeParentIpcInterface` / `NodeChildIpcInterface` |

## 硬限制：JS 主线程会停

这是整个设计里最重要的事实，必须写在前面。

**QQ 的 JS 主线程只在启动期间执行 napi 调用，之后就完全静止了。**

实测证据：

| 观察 | 数值 |
| --- | --- |
| `g_qsickCallFunctionCalls` | 冻结在 80（采样到 24 秒不再增长） |
| `g_qsickGetCbInfoCalls` | 冻结在 10 |
| TSFN 队列 | 排空一次，之后永不排空 |
| JS `setInterval` 心跳 | 最多写出一条 `beat 1` |
| ready() 后 40 次尝试（间隔 100ms） | **只成功 1 次，在 t=2.1s** |
| 窗口可见时重复 | 同样只 1 次（排除后台节流） |

所以：**注入后约两秒内可求值，之后不能。**

试过并失败的四条进入方式（每条都有上面那类数据支撑）：

1. threadsafe function 队列 —— 排空一次即止
2. `uv_async_init` + `uv_ref` 保活 —— init 在该上下文阻塞
3. JS `setInterval` 保活 —— 心跳只跳一次
4. 空闲时直接跨线程调用 `napi_run_script` —— **导致进程崩溃**（管道停止应答），
   不是阻塞，连超时保护都救不了，已删除

## 技术路线：为什么是 hook napi 导入表

### 尝试一：hook `QQNT.dll` 的 `qq_magic_napi_register`

`wrapper.node` 不导出 `napi_register_module_v1`。用 dumpbin 确认，它**导入**的是
`QQNT.dll` 的 `qq_magic_napi_register` —— QQ 用自己的私有钩子替代了标准 addon 入口。

inline hook 打中了，也拿到了参数，**那确实是 `napi_env`**：

```
qq_magic_napi_register(arg=00007FFD7EBD8000) -> 0000000000000000
napi_get_version(arg) -> status=0 version=10
```

但同一个 env，两种行为：

| 调用 | 结果 |
| --- | --- |
| `napi_get_version` | 成功（只读 env 结构体字段） |
| `napi_create_string_utf8` | 永久阻塞 |
| `napi_get_uv_event_loop` | 永久阻塞 |

推迟 2 秒、换线程发起，依然阻塞——排除了加载器锁，只剩一个解释：**env 属于创建它
的那个线程**，从别处调用会在 isolate 锁上无限等待。

### 尝试二（否决）：按偏移读 env 结构体

理论上能绕过，但手工解码 `sharedInterface` 的全局时算出 `0xC1DA0`，而按 RIP 相对
寻址精算得到 `0xE9800`。一个偏移错就是往 QQ 堆上写数据，而且版本一变就静默失效。没做。

### 实际方案：hook 各 `.node` 模块自己的 napi 导入表

关键测量：`.node` 模块**通过自己的 IAT 导入 napi**，那些调用发生在 JS 线程上。

```
ipc.node     36 个 napi_* 导入 —— 但启动时根本不加载
             实测 6/12/20/30 秒，进程里只有 major.node、wrapper.node、broadcast_ipc.dll
major.node   38 个 napi_*，含 napi_module_register / napi_run_script
wrapper.node 40 个 napi_*
```

目标改成**所有已加载的 `.node` 模块**，`patched=4`。hook 到的调用带 env，而且在
JS 线程上。

> `ipc.node` 启动时不加载本身是个发现。早期把 hook 目标定为它的版本，`patched`
> 一直是 0。它在登录后才按需 require。

## 任务队列：不追窗口，提前布置

既然窗口短暂，就不要试图赶上它。

```python
from qsick.headless import HeadlessManager

manager = HeadlessManager("headlessClient")
api = manager.startup_api("C1")

api.add("process.version")
api.add("global.__qsick.methods('NodeIKernelMsgService').length")
api.write()              # 必须在 start() 之前
manager.start("C1")

for r in api.wait(30):
    print(r.id, r.ok, r.value)
```

实测输出恰好五个任务、五个结果，无重复：

```
t0: 0
t1: 2
t2: 4
t3: 6
t4: 8
```

| 文件（都在 `owndata` 下） | 内容 |
| --- | --- |
| `tasks.jsonl` | 每行 `{"id":"...","code":"..."}`，客户端写入 |
| `results.jsonl` | 每行 `{"id":...,"ok":bool,"result"或"error":...}` |

### 执行机制：napi hook 里排空队列

任务不靠定时器执行，而是**挂在 napi hook 上**：`napi_call_function` 和
`napi_get_cb_info` 每被调用一次，就尝试排空队列。理由是——napi 活动成"阵发"出现，
阵发期正是可以安全执行 JS 的时刻。

关键设计点：

* **每个任务自建 `global.__qsick`**。这是实测出来的一个陷阱：启动脚本里发布的
  `global.__qsick` 对后续任务**完全不可见**。证据——启动脚本确实执行并返回了
  结果（`probe written (3735 bytes)`），但任务里查
  `global.__qsickTimerWrapped` 得到 `false`、`typeof __qsickKeepAlive` 是
  `undefined`。**两次 `napi_run_script` 不共享同一个 global 对象。**
  所以任务不能依赖任何"之前留下的东西"，必须在自己这次求值里建立。
* **每个任务只执行一次**：用原子 claim（自旋锁内查表并占位），不是先查后标。
* **两套执行机制绝不能并存**：早期让启动脚本的 `runTasks` 和 C++ 的
  `DrainTaskQueue` 同时跑，两个都往同一个 results.jsonl 写，于是每个任务重复两行。
  现在只有 C++ 那一套。
* **250ms 门控**：hook 每秒可能被调用上千次，文件 IO 不能每次都做。
* **重入保护**：任务自身可能触发 napi，必须防止递归回排空器。

结果由 C++ 侧写文件，不是 JS 回调——即使任务自己弄坏了 `fs`，只要它返回了值，
结果仍然记得下来。

### 这个机制能走多远（实测边界）

| 场景 | 结果 |
| --- | --- |
| 启动窗口内的任务 | ✅ 全部执行 |
| 启动后追加的任务 | ❌ 永不执行 |

未登录实例启动完成后**完全空闲**：追加任务后轮询 30 秒，`DrainTaskQueue` 一次都
没被调用。这与计数器数据一致（`napi_call_function` 冻结在 80）。

预期差别在**登录后的实例**：QQ 会持续派发监听器回调，那本身就是
`napi_call_function`。这个推断**没有实测**（需要真实账号登录），但机制上成立——
所以任务队列不是"只在启动时能用"，而是"有多少 napi 活动就能排空多少"。

## 控制管道

```
\\.\pipe\qsick-<实例名>
```

一次连接一个请求，UTF-8 JSON 双向：

```json
-> {"id":"1","op":"ping"}
<- {"id":"1","ok":true,"result":"pong"}

-> {"id":"2","op":"eval","code":"1+1"}
<- {"id":"2","ok":true,"result":"2"}

-> {"id":"3","op":"eval","code":"...","raw":true}
<- {"id":"3","ok":true,"result":{...}}
```

`raw` 存在的原因：JS 侧返回字符串，而想看模块 key 列表的调用方需要结构。

管道在 `DllMain` 里启动，不再等 TSFN——窗口是稀缺资源，管道必须早于它可用。

### eval 里可用的变量

每次 `eval` 都是全新的 `napi_run_script`，**在全局作用域求值**。裸的 `require`
是 undefined（实测 `ReferenceError: require is not defined`）。wrapper 因此解析后注入：

| 变量 | 内容 |
| --- | --- |
| `req` | 解析好的 require |
| `path` | `path` 模块 |
| `appDir` | `versions/<ver>/resources/app` 的绝对路径 |
| `load(name)` | 从 appDir require 一个 `.node` 模块 |

任务队列里则是 `global.__qsick` 上的方法：

| 方法 | 作用 |
| --- | --- |
| `exports()` | wrapper.node 的导出名（98 个） |
| `methods(cls)` | 某个 kernel 服务类的方法名 |
| `load(name)` | 从 app 目录 require 一个 `.node` |
| `wrapper()` | wrapper.node 模块本身 |
| `locate()` | app 目录绝对路径 |
| `eval(code)` | 再求值一段代码 |

## 踩过的坑

**1. 字符串字面量没有引号**

`eval(...)` 里的代码必须是完整字符串字面量。早期版本只做转义不加引号，于是
`String(typeof req)` 变成 `eval(String(typeof req))`，报
`SyntaxError: Unexpected end of input`；另一些输入报
`ReferenceError: object is not defined`。两者都像用户写错，其实不是。

**2. `wsprintfA` 有 1024 字节上限**，wrapper 脚本会被静默截断在语句中间。改用拼接。
这个坑我踩了**两次**：第二次是任务脚本加了 bootstrap 之后又超长，症状是
**一个结果都不出现**——因为 `napi_run_script` 拒绝了被截断的源码，而没有任何地方
会报告缓冲区被截断。拼接是唯一可靠的做法。

**3. TSFN 的 data 指针会被解引用**：`QsickIpcTick` 曾传 `(void*)1` 做存活探测。
有了自定义 `call_js_cb` 之后，每个排队指针都被当成 `EvalRequest*` 解引用——会让
客户端崩溃。已删除。

**4. 响应包被静默截断**：`wsprintfA` 的 `"%s"` 没有长度限制，大结果（比如 299 个
方法名）被截在中间，客户端报「不是合法 JSON」。现在按 `cch` 显式拼接。

**5. 两个类名映射表漂移**：`SERVICE_GETTERS` 和 `_class_name` 各写一份，导致
`call()` 对自己支持的服务报「未知服务」。现在从同一张表推导。

**6. 查找心跳文件的路径错误**：我曾在 `owndata` 下找 `hasbeat.txt`，实际写在
`versions/<ver>/` 下，因此误判「事件循环完全静止」。文件存在但只跳了一次——
结论不变，但过程是错的。

## 文件

| 文件 | 职责 |
| --- | --- |
| `native/qsick-pipe/src/ipc_probe.cpp` | napi 导入表 hook、TSFN、任务排空 |
| `native/qsick-pipe/src/ipc_pipe.cpp` | 命名管道服务、JSON 协议、eval 包装 |
| `src/qsick/transport/bridge.py` | QSICK 侧管道客户端 |
| `src/qsick/transport/kernel_api.py` | 类型化 API + `StartupApi` |
| `src/qsick/transport/tasks.py` | 任务队列文件协议 |
| `tests/test_bridge_pipe.py` | 管道协议与错误处理 |
| `tests/test_kernel_api.py` | JS 生成、参数序列化、错误映射 |
| `tests/test_task_queue.py` | 队列协议、去重、窗口未命中 |
| `tests/test_kernel_api_wiring.py` | manager 接线 |

## 已知边界

* **`eval` 是通用能力，不是类型化 API**：调用方传 JavaScript 字符串。要做成
  `client.send_message(group, text)` 这样的形状，需要在 JS 侧再包一层针对
  `NodeIKernelMsgService` 的封装。
* **发消息未验证**：`NodeIKernelMsgService` 可以 `new` 出来（299 个方法），但那是
  没有会话支撑的空壳。真正调用需要已登录会话，而消息元素结构取决于实例用的是哪个
  session 工厂。`NodeParentIpcInterface.sharedInterface()` 拿到了完整方法集，但
  `sendMessage('','','',fn)` 直接调用会让进程无响应，所以没有继续。
* **同步模型**：一次请求一次往返，长任务撞 1.5 秒超时。事件订阅（消息推送）需要
  另一条方向相反的通道。
* **符号相关但不依赖偏移**：hook 依赖 `napi_*` 符号名从 `QQNT.dll` 导出。这比硬编码
  偏移稳，但如果哪天 QQ 改了导出名就会失效（会以 `patched=0` 表现出来，不会静默出错）。
