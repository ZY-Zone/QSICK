# 架构

QSICK 沿一条不可移动的分界线切开：**必须跑在 `QQ.exe` 里的代码是原生实现，其余全部是 Python。**

```
┌──────────────────────────── QQ.exe ────────────────────────────┐
│  QSICK payload（C++ DLL，MSVC）                                 │
│    · 命名管道  \\.\pipe\qsick.<pid>.ctl / .evt                  │
│    · 钩子引擎（偏移表 + 跳板）→ 收发路径                        │
└───────────────┬────────────────────────────────────────────────┘
                │ 帧协议（32 字节头 + JSON 体）
┌───────────────▼────────────────────────────────────────────────┐
│  qsick 内核（Python）                                           │
│   实例中心 → 解码 → 插件钩子 → OneBot 事件 → 客户端             │
│                     ↘ 动作 ← 注册表 ← 插件                      │
└───────────────┬────────────────────────────────────────────────┘
                │ HTTP / WebSocket / SSE
        OneBot v11 客户端 · WebUI（FastAPI + Jinja2）
```

## 为什么这样切

| 关注点 | 位置 | 理由 |
| --- | --- | --- |
| 内联钩子、跳板、调用约定 | payload（C++） | 需要原始内存访问与逐版本偏移；Python 做不安全 |
| 注入驱动（进程发现、`CreateRemoteThread`） | Python（`transport/injector.py`） | 普通 Win32 API，`ctypes` 完全够用 |
| 帧协议 | 两边 | 一份头布局、两套实现，两侧都有测试 |
| 消息语义、OneBot 映射 | Python | 可热重载，改字段不用重新编译 |
| 动作 | Python | 约 240 个入口；插件还能在运行时追加 |
| WebUI、插件 | Python | 与内核同进程，不额外跳一跳 |

### 无头宿主的 `QQNT.dll` 垫片

无头实例不启动完整的 QQ 客户端，而是让 `host.js` 直接加载 QQ 的 `wrapper.node`。
但 `wrapper.node` 需要从 `QQNT.dll` 导入一百多个 `napi_*` / `uv_*` / V8 符号——没有
QQ 主程序，这些符号就没人提供，模块加载会失败。

`build/` 下的垫片正是补这一层：

```
wrapper.node --导入--> QQNT.dll（我们造的导出表，102 个名字）
                          │
                          └─ StubN: mov rax, [ptrN]; jmp rax   ← 尾跳，不破坏调用约定
                                     ▲
                          DllMain 里用 GetProcAddress(host, "...") 填好
                                     ▲
                          host = GetModuleHandleW(nullptr) = node.exe
                          （它已经加载了真正的 napi/uv 实现）
```

三个文件各司其职：

| 文件 | 作用 |
| --- | --- |
| `build/QQNT.def` | 导出表：把 `napi_call_function`、`?GetCurrent@Isolate@v8@@...` 等 102 个名字映射到 `StubN` |
| `build/host_stubs.asm` | `ptrN` 数据槽 + 99 个 `StubN` 跳板（`ml64` 汇编） |
| `build/host_init.cpp` | `DllMain` 在 `DLL_PROCESS_ATTACH` 时逐条 `GetProcAddress` 填充 `ptrN` |

**构建**（需要 Visual Studio Build Tools 的 C++ 工作负载）：

```bat
build\build_qqnt_shim.bat
```

脚本自己找 `vcvars64.bat`（先看 `VCINSTALLDIR`，再用 `vswhere`），然后 `ml64` → `cl`
→ `link`，最后用 `dumpbin` 自检：导出数必须是 102、入口点不能是 0、依赖只能是
`KERNEL32.dll` / `ADVAPI32.dll`。

`build/QQNT.dll` 是**运行时依赖**（`headless.py` 会把它复制进每个实例的 runtime），
所以它以预编译形式入库，编译中间文件（`.obj/.lib/.exp/.pdb/.ilk/.tlog`）被 `.gitignore`
排除。

两个容易踩的坑，都写进了脚本注释：

- **不能加 `/NOENTRY`**。那样出来的 DLL 入口点为 0，`DllMain` 永不执行，`ptrN` 全是
  空指针——DLL 能加载，但第一次调 `napi_*` 就崩。
- **不要编译 `host_stub.cpp`**。它唯一那行 `IsEnvStopping` 与 `host_init.cpp:110`
  完全重复，一起链接会 `LNK2005`。

## 多实例：一条消息的归属

消息处理已经从"一个全局运行时"下放到**每个实例**。一个实例（`C0`、`C1` …）在 QQ 侧
就是一个登录的号，它拥有自己的消息数据库、自己的消息处理器、自己的一组网络配置。

```
QQ 宿主 --事件--> 实例的消息处理器
                     |-- 1. 先用 maxStore 把原始 payload 完整落盘（证据）
                     |-- 2. 再按该实例每一条网络配置的协议编码后分发
网络配置 --动作--> 所属实例的处理器执行
```

顺序不能反：分发是"按协议裁剪成对方能懂的形状"，一旦先裁剪再存，历史数据就只剩
当时那个协议要的字段，将来别的协议要别的字段时已经无从找回。

**动作因此不需要 `account` 字段**：它从哪条连接来，就属于哪个实例，归属写在配置上。

## 消息流水线

```
宿主机事件
  → HeadlessTransport.push（按实例归类，附带 instance / account）
  → Runtime._handle
      · login 事件        → 该实例的 LoginState
      · message.*         → Message.from_payload
          → PluginHost.dispatch_message   （守卫可在此丢弃——最省的一步）
          → 实例库 put（maxStore，原始 payload + 解析后字段合并）
          → MediaResolver（rkey → URL）
          → EventMapper.message → OneBot 事件
      · notice/request/meta → EventMapper.map
      · noise.*            → 仅 TRACE 日志（默认永不外发）
  → PluginHost.dispatch_event（只读观察者）
  → EventHub.broadcast → WebSocket / SSE 客户端
```

三条刻意的规则：

1. **先丢弃，再干活。** 插件在 URL 解析之前就看到 `Message`，守卫不必为一张即将被
   丢弃的图片去取 rkey。
2. **看不懂的是数据，不是错误。** 未识别的元素变成 `unknown` 段，保留原始类型与载荷。
   解码器永远不会把一条消息悄悄清空。
3. **噪声只进 TRACE。** 输入状态/正在输入事件写入 `Noise` 日志器，级别 `TRACE`；
   配置里的 `forward_noise` 是唯一能把它们放出来的开关。

## 动作：一个动作一个模块

`onebot/actions/actions.json` 是注册表；每条记录指名 `onebot/actions/<类别>/<名称>.py`
下的模块与实现类。加载器只导入列出的模块，因此某个动作坏掉只是一个孤立的导入错误，
而不是启动失败。加一个动作只碰两个文件（模块与 JSON 条目）；把 `status` 翻一下，
就是"计划中的占位"与"可用处理器"的区别。

条目长这样：

```json
{
  "name": "send_group_msg",
  "module": "message/send_group_msg",
  "class": "SendGroupMsgAction",
  "category": "message",
  "status": "implemented",
  "params": [{"name": "group_id", "kind": "int", "required": true}]
}
```

目录形状：

```
src/qsick/onebot/actions/
  actions.json           注册表 + 状态 + 参数
  base.py                Action、PlannedAction、辅助函数（CQ 解析、send_message……）
  loader.py              JSON -> 模块 -> ActionRegistry
  message/send_group_msg.py
  group_admin/set_group_ban.py
  ...
```

- 加一个动作 = 建一个模块 + 加一条 JSON。没有中央的 `if/elif`。
- 计划中的条目也有模块（`class X(PlannedAction)`）；把 `status` 改成 `implemented`
  并实现 `run()` 就是全部升级步骤。当前 242 条全部已是 `implemented`；QQ 能否真的执行
  由运行时判定（不支持的 op 回 `1502`）。
- `verify_catalog()`（测试使用）检查每条 JSON 都有模块、有类、并且真的覆写了 `run()`。

按类别的分布（总数 / 内核实现）：

| 类别 | 动作数 | 内核实现 |
| --- | ---: | ---: |
| extended | 53 | 4 |
| group | 30 | 12 |
| qzone | 29 | 0 |
| group_admin | 23 | 8 |
| system | 19 | 18 |
| group_file | 19 | 2 |
| friend | 18 | 11 |
| message | 12 | 9 |
| group_album | 9 | 0 |
| interaction | 7 | 5 |
| face | 6 | 0 |
| stream | 5 | 0 |
| flash | 3 | 1 |
| packet | 3 | 2 |
| guild / ark / request | 各 2 个 | 0 / 0 / 2 |

## 动作分发

```
OneBot 请求（HTTP / WS）
  → runtime.invoke
  → ActionRegistry.dispatch
      · 名字未知            → retcode 1404
      · 计划中但未接线      → retcode 1405 + 提示
      · 参数非法            → retcode 1400
      · 处理器抛异常        → retcode 1500（带堆栈记日志）
  → ActionContext.call(op) → transport.send(op, params)
  → payload 在 QQ 侧执行 → JSON 结果
```

`ActionContext` 是处理器唯一能碰到的面：transport、config、store、rkey、媒体解析器
与登录状态。处理器从不直接接触管道或 payload，因此可以对着 `MockTransport` 测试。

## 失败时的姿态

| 失败 | 行为 |
| --- | --- |
| payload 缺失 / 管道不存在 | 启动报错，附上期望路径与环境变量 |
| payload 中途死掉 | transport 推送 `runtime.closed`，运行时记日志并继续提供 WebUI |
| 某个事件处理器抛异常 | 记日志、跳过该事件、循环继续 |
| 某个插件抛异常 | 记日志、该事件跳过该插件、其它插件照常 |
| 某个 WS 客户端很慢 | 它的队列丢掉最旧帧；其它客户端不受影响 |
| rkey 刷新失败 | 回落到静态/env 值，再不行媒体 URL 留空（id 保留） |

## 非目标

- 打包 JS 运行时：QSICK 不在 QQ 内执行 JavaScript。
- 在 QQ 内重写加密：登录与包签名属于 payload 的职责，目前**尚未实现**
  （见 `docs/payload-abi.md`）。

## 反向 WebSocket

`onebot.reverse_ws_urls` 让 QSICK 成为拨号方。它会发送 `X-Self-ID`、`X-Client-Role: Universal`，
以及配置了的话再发 `Authorization: Bearer <token>`；会话之后的行为与入站 WS 完全一致：
连上发生命周期事件、收动作、发事件。重连按 URL 各自退避，失败不会波及 HTTP/WS 监听器。

## 认证

- HTTP/WS/SSE 用 `secrets.compare_digest` 比对 `onebot.access_token`。
- 反向 WS 在 `Authorization` 头里发送同一个令牌。
- 管道认证有三种模式（默认 `random`、`fixed`、`none`）。`random` 模式下注入器把每会话
  令牌写到 `%TEMP%\qsick\sessions\<pid>.token`，payload 启动时读取，令牌同时出现在两侧
  管道名里，`HELLO` 必须回显它；不一致即 `status = -2`。`fixed` 使用配置里的令牌，
  `none` 供单用户调试机使用并会记一条警告。

## 持久化

每个实例一个库：`data/instances/C1/messages.db`，后端是 **maxStore**（`protocols/maxStore`）。

maxStore 的取向与"够用的历史"相反——它存的是**证据**：每条收到的 payload 落两个粒度，
一份整包 JSON（`raw_json`）加一份按 JSON 指针摊平的字段表（`raw_frames`）。冗余是刻意的：
整包保证能原样还回去，摊平表保证将来字段改名、嵌套加深、平台偷偷加字段之后，旧数据
仍然能被按路径取出来。所以"以后要兼容更多消息协议"不需要重写历史数据。

写入是把**原始 payload 与解析后补上的字段合并**：只存原样 raw 会丢掉 `local_path`
（下载落盘位置）和归属账号，读回来就看不到图。

`storage.py` 仍保留 SQLite 实现供 `storage.backend == "sqlite"` 的旧配置与测试使用；
`memory` 后端供测试与轻量运行，不落盘。

为什么要持久化：消息历史是持久记录。重启就丢会让 `get_msg` 与插件历史不可靠，
也会悄悄劣化每一个依赖回填的消费方。

## 临时产物都在 `temp/`

临时文件不再散落到系统 `%TEMP%`。统一入口是 `src/qsick/tempdir.py`：

```python
from qsick import tempdir
handle = tempdir.make_file(prefix="upload-", suffix=".png", category="uploads")
```

为什么集中：系统临时目录里的东西既看不见也管不着——调试一次上传、跑一次
解包，就在 `%TEMP%` 留下几百 MB，谁也不知道是谁的。项目自己的 `temp/` 可见、
可整体删除，而且已在 `.gitignore` 里。

`QSICK_TEMP_DIR` 可以把整个目录指到别处（并行测试时每个 worker 各一份，免得
互删文件）。

**唯一的例外**是管道认证令牌：它必须写在 payload（预编译 DLL，读的是系统临时
目录）能找到的位置，见 `transport/session.py`。那里保留系统路径不是疏漏，
改了会让注入握手失败。

## 目录布局

```
src/qsick/
  transport/   帧协议、mock、管道客户端、headless 桥、Windows 注入器
  protocol/    元素、消息模型、rkey、媒体、protobuf 辅助
  instances.py 实例中心：每实例的库、处理器、网络配置
  raw_store.py maxStore 之上的 MessageStore 实现（原始 payload 证据库）
  onebot/      注册表、actions/<类别>/<动作>.py、事件、服务器、反向 WS
  plugins/     清单、带权限的上下文、宿主、热重载
  webui/       FastAPI 应用、Jinja2 模板、静态资源
protocols/            聊天/存储协议适配器（一个目录 = 一个协议）
plugins/guard/        参考的白/黑名单插件
assets/qq-faces/      WebUI 用到的 QQ 表情资源
headlessClient/       每账号宿主实例（C0、C1……）——运行时数据
market/               远程插件市场服务的源码
native/qsick-payload/ C++ DLL（管道 + 跳板 + 钩子）
docs/                 架构、协议、payload ABI、插件 API
tools/                开发探针 + 可复用的同步/导入/冒烟脚本（多数探针不入库）
tests/                pytest 套件 + Node 宿主测试
```

> `tools/` 下的一次性探针写死了开发机的绝对路径，换机器无法运行，因此不进仓库
> （`.gitignore` 里按「整体忽略 + 逐个放行」管理）。留在本地的副本仅用于复核当时的分析过程。

## 路线图

1. **在真实客户端上验证偏移**并填好 `offsets.json`（真实流量唯一的阻塞点）。
2. **补齐仍返回 `1502` 的 payload op**（`group_file`、`qzone`、`group_album`……）。
   242 条目录项都已是注册适配器；缺的是 QQ 侧的线路支持，不是 Python 模块。
3. **Windows CI 上的 payload 测试**：对本地 DLL 的跳板探针、帧模糊测试。
4. **反向 WS 增强**：按 URL 的重连策略、`X-Client-Role` 变体。
5. **重连时的令牌轮换**，以及限制到当前用户的管道 ACL。
