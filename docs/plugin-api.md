# 插件 API

插件是 `plugins/` 下的一个目录，含一份 `basic.json` 清单和一个 Python 入口文件。
它在进程内运行，与内核共用同一个事件循环，因此可以影响内核行为——这正是为什么
清单必须事先声明意图，并由宿主强制执行。

当前 API 版本为 **1**（`"api": 1`）。

## 1. `basic.json`

```json
{
  "id": "guard",
  "name": "消息白名单 / 黑名单",
  "version": "1.0.0",
  "api": 1,
  "entry": "plugin.py:GuardPlugin",
  "tier": "core",
  "permissions": ["events.read", "events.drop", "config.read", "config.write"],
  "hooks": ["on_message"],
  "accounts": [],
  "log": {"register": true},
  "priority": 10,
  "enabled": true,
  "description": "…"
}
```

| 字段 | 必填 | 含义 |
| --- | --- | --- |
| `id` | ✅ | `^[a-z][a-z0-9_-]{1,31}$`，全局唯一 |
| `name` / `version` | ✅ | 用于显示与审计 |
| `entry` | ✅ | `file.py:ClassName`；该类以无参方式构造 |
| `api` | — | 必须为 `1` |
| `tier` | — | `sandbox` < `standard` < `core`（默认 `standard`） |
| `permissions` | — | 下表的子集；出现其他值即加载错误 |
| `hooks` | — | 说明该类实现了哪些钩子 |
| `accounts` | — | 允许收发的 QQ 号数组，或 `["ALL"]`。**省略 = 账号无关**（见 §3） |
| `log` | — | 是否把自己的日志分类登记到日志页筛选（见 §6） |
| `priority` | — | 数值越小越先运行（守卫使用 10） |
| `enabled` | — | 为 `false` 时跳过该插件；运行时状态同时存入 `data/plugins/state.json` |

tier 是一道上限检查：`plugins.max_tier`（默认 `core`）会拒绝要求更高层级的插件。
`sandbox` 插件用于承载不受信任的代码，默认不获得任何权限。

## 2. 权限

| 权限 | 授予的能力 |
| --- | --- |
| `events.read` | 接收 `Message` 与 OneBot 事件 |
| `events.drop` | 在消息到达客户端/动作之前将其拦下（`ctx.drop()`） |
| `messages.send` | `ctx.send_group_msg` / `ctx.send_private_msg` |
| `actions.invoke` | `ctx.call()` / `ctx.invoke_for()`——对任意动作的调用 |
| `actions.register` | 在注册表中新增或替换动作 |
| `config.read` / `config.write` | 读写自己的 `data/plugins/<id>/config.json` |
| `runtime.read` | `ctx.runtime_info()` / `ctx.accounts()` |
| `core.hooks` | 保留给声明范围之外的钩子 |

未声明权限就调用对应 API 会抛出 `PermissionDenied`，宿主会记录并将其隔离。
`ctx.drop()` 是唯一会向外传播的例外——它是控制流信号，不是错误。

> **`messages.send` 与 `actions.invoke` 是分开的两件事。**
> 早先 `send_*` 内部走的是 `call()`，于是一个只声明 `messages.send` 的插件会被拒绝——
> 文档承诺的和代码执行的不一致。现在 `send_*` 只要求 `messages.send`。

## 3. 账号

QSICK 同时驱动多个 QQ 账号，而"当前主账号"会随会话登录、掉线而变化。所以账号要能点名，
范围要能声明。

### 三种状态，别混淆

| `accounts` | 含义 | 能调账号类接口吗 |
| --- | --- | --- |
| 省略 / `[]` | **账号无关**：这个插件不参与账号维度，`on_message` 照常收到**所有**账号的消息 | ❌ |
| `["10001", "20002"]` | 只看、也只发这两个号 | ✅ 仅限这两个 |
| `["ALL"]` | 明确表示全部账号 | ✅ 全部 |

省略与 `["ALL"]` **不是一回事**：前者是"这个插件跟账号没关系"，后者是"我知道有多个账号，
全都要"。按账号收发的接口要求你必须说清是哪一种——"忘了写"不该和"故意要全部"长得一样。

`["ALL"]` 不能和具体号码混写（`["ALL", "10001"]` 无意义），会加载失败。

### 接口

```python
await ctx.send_group_msg(30001, msg, account="10001")   # 点名；不点名 → 主账号
await ctx.send_private_msg(20002, msg)
await ctx.invoke_for("C1", "send_group_msg", ...)        # 点名实例，不依赖主实例
ctx.accounts()                                            # 全部账号 + 本插件的 allowed 判定
```

未声明 `accounts` 时调用这些会抛 `AccountScopeUndeclared`（`PermissionDenied` 子类），
错误信息会告诉你要写 `["ALL"]` 还是写号码。点名了范围外的账号则抛 `AccountOutOfScope` ——
两者分开是因为修法不同。

**范围由宿主强制**，不靠插件自觉：收到的消息若不在范围内，`on_message` 根本不会被调用。

### 运行时收窄（仅影响本插件）



```python
ctx.block_account("20002")      # 这个号的消息以后不再进本插件
ctx.unblock_account("20002")
ctx.blocked_accounts()          # ['20002']
```

`block_account` 只能作用于自己已声明的范围内——想借它绕过清单声明的账号会被拒绝。
它只让**本插件**不再看/不再发某个号，别的插件和 OneBot 客户端照旧。

```python
for entry in ctx.accounts():
    # {'instance': 'C0', 'uin': '10001', 'nickname': '…', 'logged_in': True,
    #  'online': True, 'kicked': False, 'reason': '', 'account': '10001', 'allowed': True}
    if not entry["allowed"]:
        continue   # 它在跑，只是本插件碰不到——这跟"没登录"是两回事
```

`accounts` 返回**全部**账号并附上本插件的判定，而不是只返回允许的那些：
否则"什么都没发生"和"这个号没在线"在插件看来完全一样。

### 内核级全局限制（仅 `tier: "core"`）

上面两节都只影响插件自己。如果你要拦的是**整个运行时**——包括其他插件、OneBot 客户端、
WebUI 的发送——那是内核级的，只有 `core` 级插件能设：

```python
ctx.restrict_account("10001", direction="out")   # 'in' / 'out' / 'both'
ctx.unrestrict_account("10001", direction="in")  # 只解一个方向，剩下的仍是 'out'
ctx.restrictions()                               # {'10001': 'out'}
```

| 方向 | 后果 |
| --- | --- |
| `in` | 该账号的消息**不落库、不发插件、不发客户端**——和 `events.drop` 的后果一样 |
| `out` | 发送动作被拒，**不论谁发起**：本插件、其他插件、OneBot HTTP/WS、WebUI |

要点：

- **门槛是 tier，不是权限。** `tier` 本来就是"这个插件被信任到什么程度"，再要一个权限去表达
  同一件事，等于让 `standard` 插件申请那件 tier 本来就想不给它的能力。
- **不需要声明 `accounts`。** 这个接口管的是别人的调用，不是本插件能用哪些号。
- **只在内存里**，进程重启即清空——插件在 `on_load` 里重新施加即可。它不该是那种在写它的插件
  卸载之后还悄悄生效的配置。
- 两个方向相加而不是互相覆盖：先 `in` 再 `out` 得到 `both`。

## 4. 生命周期钩子

```python
class MyPlugin:
    def on_load(self, ctx): ...          # may be async
    def on_message(self, message, ctx):  # return False / ctx.drop() to stop
        ...
    def on_event(self, event, ctx):      # read-only; return a dict to replace
        ...
    def on_unload(self, ctx): ...
```

- `on_message` 在 URL 解析与存储**之前**运行；`message` 是
  `qsick.protocol.message.Message`，其 `account` 字段是这条消息属于哪个号。
  不在清单 `accounts` 范围内的消息会直接跳过（TRACE 级日志）。
- `on_event` 在映射**之后**运行，每个 OneBot 事件执行一次；返回 dict 会为下游
  客户端替换该事件。
- 任何异常都会被捕获并记录；该插件在本次事件中被跳过。
- 热重载会监视插件目录下所有 `*.py` 与 `basic.json`，文件一变就重载（≤ 2 s）。
  配置文件通过 `ctx.read_config()` 读取，插件也可以自行重复读取以便更快响应。

## 5. 上下文提供的接口

```python
# 日志（分类为 Plugin.<id>，或清单里 log.category 指定的名字）
ctx.log.info("...", key=value)
ctx.log.warn / error / debug / trace

# 配置
ctx.read_config(default) / ctx.write_config(data)

# 消息流
ctx.drop(reason)                                    # 需要 events.drop

# 发送（需要 messages.send + 声明 accounts；account 可选）
await ctx.send_group_msg(30001, "hi", account="10001")
await ctx.send_private_msg(20002, "hi")             # 不点名 → 主账号

# 动作（需要 actions.invoke）
await ctx.call("get_group_list", account="10001")
await ctx.invoke_for("C1", "send_group_msg", group_id=30001, message="hi")

# 注册新动作（需要 actions.register）
ctx.register_action(spec)

# 状态（需要 runtime.read）
ctx.runtime_info()
ctx.accounts()               # 需要声明 accounts

# 账号范围（本插件）
ctx.account_scope            # ['10001'] / ['ALL'] / []（未声明 = 账号无关）
ctx.account_allowed("10001") # 声明范围 + 运行时屏蔽的综合判定
ctx.block_account / unblock_account / blocked_accounts   # 需要声明 accounts

# 内核级全局限制（仅 tier=core，见 §3）
ctx.restrict_account("10001", direction="out")
ctx.unrestrict_account("10001")
ctx.restrictions()

# 权限自查
ctx.has("events.drop")
```

`invoke_for(instance, …)` 与 `call(…)` 的区别只在身份：前者明确点名实例，不依赖
"谁是主实例"这种随时会变的全局状态。多账号并行时这是唯一稳的写法。

## 6. 日志分类

日志页的"分类"下拉框按**已声明的分类**填充，而不是按"已经出现过的记录"。
两者不同：一个还没输出过日志的插件，恰恰是你想单独筛选它的时候。

- 核心分类（`Plugins`、`OneBot.WS`、`OneBot.HTTP`、`OneBot.Reverse`、
  `MessageCache`、`Transport.Pipe` …）随构建注册，永远可选。
- 插件分类要在清单里显式声明：

```json
{ "log": { "register": true, "category": "Guard" } }
```

`log` 也可以直接写成 `true`。`category` 省略时用 `Plugin.<id>`。
不声明就不会出现在筛选里——一个从不写日志的插件不该往列表里加条目。
卸载插件会同时撤掉它登记的分类（核心分类不可撤）。

相关分类里现在能看到的内容：

| 分类 | 记什么 |
| --- | --- |
| `Plugins` | 加载成功/失败、tier 超限、清单错误、热重载、卸载、配置保存。加载行带 `tier`/`hooks`/`accounts`/`log_category` |
| `Plugin.<id>` | 插件自己的运行日志 |
| `OneBot.WS` | WebSocket 接入、断开、鉴权拒绝、连接错误（带 `client`） |
| `OneBot.HTTP` | 监听就绪/绑定失败（带 `endpoints`）、鉴权拒绝 |
| `OneBot.Reverse` | 反向 WS 连接、重连、断线 |

## 7. 配置界面

`basic.json` 决定插件如何被配置：

```json
{
  "config_ui": "default",
  "config_fields": [
    {"key": "mode", "label": "模式", "type": "str", "default": "blacklist"},
    {"key": "groups", "label": "群名单（JSON）", "type": "json", "default": {"whitelist": [], "blacklist": []}},
    {"key": "log_drops", "label": "记录日志", "type": "bool", "default": true}
  ]
}
```

| 字段 | 含义 |
| --- | --- |
| `config_ui` | `default`（由 QSICK 渲染表单）或 `custom`（插件自带 HTML 页面） |
| `config_fields` | default 模式的 schema：`key`、`label`、`type`（`str/int/float/bool/json`）、`default`、`help` |
| `config_page` | custom 模式的 HTML 文件，相对于插件目录 |

**默认页面**（`/plugins/<id>/config`）：每个字段一个文本框（JSON 字段使用
textarea），一个"启用此插件"复选框，以及一个保存按钮。保存会写入
`data/plugins/<id>/config.json` 并切换插件的启用状态。

**自定义页面**（`config_ui: "custom"`）：插件的 HTML 以 iframe 形式在
`/plugins/<id>/config/page` 提供（已做路径穿越防护）。它与以下接口通信：

| 端点 | 方法 | 用途 |
| --- | --- | --- |
| `/api/plugins/<id>/config` | GET | 以 JSON 返回当前配置（已合并声明的默认值） |
| `/api/plugins/<id>/config` | POST | 用一个 JSON 对象替换配置 |

### 字段级控件提示（`configEnable`）

除了 `config_ui`，单个字段还可以用 `configEnable` 指定控件与可见性：

```json
{
  "key": "mode",
  "label": "模式",
  "type": "str",
  "options": ["blacklist", "whitelist"],
  "configEnable": {"ifShow": true, "type": "select"}
}
```

- `configEnable.ifShow` 默认为 `false`；没有 `configEnable` 的字段保持可见以兼容旧插件。
- `configEnable.type` 默认为 `text`；支持的控件是 `text`、`switch`、`select`。
- `select` 从字段本身（或 `configEnable.options`）读取 `options`。
- 保存表单时隐藏字段会被保留；未勾选的开关存为 `false`。

## 8. 参考插件：`guard`

策略存放在 `data/plugins/guard/config.json`（首次运行时创建）：

```json
{
  "mode": "blacklist",
  "scope": "both",
  "groups": {"whitelist": [], "blacklist": [30001]},
  "users": {"whitelist": [], "blacklist": []},
  "always_allow_users": [10001],
  "log_drops": true
}
```

| 字段 | 行为 |
| --- | --- |
| `mode: "blacklist"` | 当消息所在的群**或**发送者在名单中时丢弃该消息 |
| `mode: "whitelist"` | 丢弃一切不在名单中的消息（名单为空 ⇒ 全部丢弃，只警告一次） |
| `scope` | `group` / `private` / `both`——策略适用于哪些会话 |
| `always_allow_users` | 绕过两种模式（号主账号） |
| `log_drops` | 每条被丢弃的消息记一行 DEBUG 日志 |

插件用 DEBUG 而非 INFO 记录丢弃：一个活跃的群可能丢弃上千条消息，那不算事故。
它的清单声明了 `"log": {"register": true}`，所以日志页能单独筛出 `Plugin.guard`。

## 9. 从插件添加动作

```python
from qsick.onebot.registry import ActionSpec, Param

async def my_handler(ctx, echo=""):
    return {"pong": echo}

spec = ActionSpec(
    name="my_ping",
    summary="example",
    category="extended",
    params=[Param(name="echo", kind="str", required=False, default="")],
    handler=my_handler,
)
ctx.register_action(spec)
```

该动作会立即出现在 `/actions`、WebUI 和 OneBot 接口中。

## 10. 故障隔离

插件无法拖垮运行时：

- 导入 / `on_load` 失败 ⇒ 插件不被加载，记录错误；
- 钩子抛出异常 ⇒ 该钩子在本次事件中被跳过；
- 钩子内出现 `PermissionDenied` ⇒ 记录后事件继续；
- 阻塞事件循环的插件**确实**会让一切卡住——请写异步代码，或用
  `asyncio.to_thread` 把阻塞工作卸载出去。

## 11. WebUI 中的管理操作

| 按钮 | 路由 | 行为 |
| --- | --- | --- |
| 配置 | `GET /plugins/<id>/config` | 默认表单或自定义 iframe |
| 更新 | `POST /plugins/<id>/update` | 从磁盘重新读取清单与模块 |
| 卸载 | `POST /plugins/<id>/uninstall` | 卸载，然后把目录移动到 `data/plugins/.trash/<id>-<ts>` |
| 恢复默认 | `POST /plugins/<id>/config/reset` | 删除 `config.json` 并重载插件 |

卸载在设计上可恢复：不删除任何内容，只是移动目录，并把插件 id 以禁用状态写入
`data/plugins/state.json`。

插件市场（`/plugins/market`）列出部署在 `plugins.market_url`（默认
`https://sickcc.online/QSICK/plugins-market`）的服务，支持搜索、排序、点赞与
安装/更新。安装时会下载归档包、校验 `basic.json`、解压到插件目录并热加载。
由于安装会执行第三方代码，可设置 `plugins.allow_remote_install=false` 将其禁用；
服务端源码位于 `market/`。

## 12. 从旧版迁移

| 旧写法 | 现在 | 原因 |
| --- | --- | --- |
| 只声明 `messages.send` 却收到 `PermissionDenied` | 已修复，`messages.send` 足够 | 文档与实现不一致 |
| 无账号概念的 `send_group_msg(gid, msg)` | 仍然可用；要指定账号时加 `account=` | 默认落到主账号，多账号下不可靠 |
| 靠"主账号"猜身份 | `ctx.invoke_for(instance, …)` 或 `account=` | 主账号随登录变化 |
| 用 `send_*` / `accounts()` 但没写 `accounts` | 会抛 `AccountScopeUndeclared`；写号码或 `["ALL"]` | 省略与"全部"必须可区分 |
| 想拦别的插件/客户端的收发 | `ctx.restrict_account(…)`（仅 core） | 每插件 `block_account` 只管自己 |
| 日志分类要等首次输出才出现 | 清单写 `"log": {"register": true}` | 空分类也需要可筛选 |
