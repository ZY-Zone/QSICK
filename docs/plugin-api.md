# 插件 API

插件是 `plugins/` 下的一个目录，含一份 `basic.json` 清单和一个 Python 入口文件。
它在进程内运行，与内核共用同一个事件循环，因此可以影响内核行为——这正是为什么
清单必须事先声明意图，并由宿主强制执行。

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
| `actions.invoke` | 对任意动作调用 `ctx.call(action, **params)` |
| `actions.register` | 在注册表中新增或替换动作 |
| `config.read` / `config.write` | 读写自己的 `data/plugins/<id>/config.json` |
| `runtime.read` | `ctx.runtime_info()` |
| `core.hooks` | 保留给声明范围之外的钩子 |

未声明权限就调用对应 API 会抛出 `PermissionDenied`，宿主会记录并将其隔离。
`ctx.drop()` 是唯一会向外传播的例外——它是控制流信号，不是错误。

## 3. 生命周期钩子

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
  `qsick.protocol.message.Message`。
- `on_event` 在映射**之后**运行，每个 OneBot 事件执行一次；返回 dict 会为下游
  客户端替换该事件。
- 任何异常都会被捕获并记录；该插件在本次事件中被跳过。
- 热重载会监视插件目录下所有 `*.py` 与 `basic.json`，文件一变就重载（≤ 2 s）。
  配置文件通过 `ctx.read_config()` 读取，插件也可以自行重复读取以便更快响应。

## 4. 上下文提供的接口

```python
ctx.log.info("...", key=value)      # namespaced logger: Plugin.<id>
ctx.read_config(default) / ctx.write_config(data)
ctx.drop(reason)                    # requires events.drop
await ctx.call("get_group_list")    # requires actions.invoke
await ctx.send_group_msg(30001, "hi")
ctx.register_action(spec)           # requires actions.register
ctx.runtime_info()                  # requires runtime.read
ctx.has("events.drop")
```

## 5. 配置界面

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

## 6. 参考插件：`guard`

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

## 7. 从插件添加动作

```python
from qsick.onebot.registry import ActionSpec, Param

spec = ActionSpec(
    name="my_ping",
    summary="example",
    category="extended",
    params=[Param(name="echo", kind="str", required=False, default="")],
    handler=my_handler,          # async def handler(ctx, echo): return {...}
)
ctx.register_action(spec)
```

该动作会立即出现在 `/actions`、WebUI 和 OneBot 接口中。

## 8. 故障隔离

插件无法拖垮运行时：

- 导入 / `on_load` 失败 ⇒ 插件不被加载，记录错误；
- 钩子抛出异常 ⇒ 该钩子在本次事件中被跳过；
- 钩子内出现 `PermissionDenied` ⇒ 记录后事件继续；
- 阻塞事件循环的插件**确实**会让一切卡住——请写异步代码，或用
  `asyncio.to_thread` 把阻塞工作卸载出去。


## 9. WebUI 中的管理操作

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
