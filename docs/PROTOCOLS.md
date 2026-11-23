# 协议层

QSICK 把「协议」从功能里拆出来，放在项目根的 `protocols/` 下。每个目录就是一份
实现，目录名即协议名。

| 目录 | 角色 | 说明 |
|---|---|---|
| `onebotv11/` | 聊天 | OneBot v11。默认协议；事件本来就是 v11 形状，转换是恒等。 |
| `milky/` | 聊天 | Milky（v1.3）。调用 `POST /api/{api}`、推送 `GET /event`，带 `{status,retcode,message,data}` 信封。 |
| `maxStore/` | 存储 | 不是聊天协议，**不可被选作聊天协议**。保留收到的全部原始信息，包括未知字段与未识别元素。 |

## 注册表

`enabled_protocol.json` 是可选项清单：网络配置的协议下拉框只列里面 `chat` 数组
的条目。`store` 字段固定指向 maxStore。

清单与磁盘不一致时**明确报错**，不静默跳过——少一个协议比多一个错更容易查。

## 三条流水线

分流点只有两处，进出口各一处。分工是：

```
进（事件推送）
QQ 宿主 ──▶ runtime._dispatch_event ──▶ runtime._publish
                                          │  ① maxStore：原始 payload 落该实例的库
                                          │     （先于一切裁剪，所以历史里字段是全的）
                                          │  ② 按**每条连接自己的协议**编码后投递
                                          ▼
                                    各连接（HTTP / WS / SSE / 反向上游）

出（动作执行）
连接收到调用 ──▶ pipeline.decode_action(该连接的协议) ──▶ runtime.invoke
                   │                                        │
                   └─ 协议名与参数翻译成内部名                 └─ 插件过滤、账号级出入限制、
                                                               只读缓存、发送锁全在这里
```

| 流水线 | 事件推送 | 动作执行 | 说明 |
| --- | --- | --- | --- |
| `maxStore` | ✅ | ❌ | 只写消息缓存。它是**汇**不是通道：没有连接、没有端口 |
| `onebotv11` | ✅ | ✅ | 默认协议，转换恒等 |
| `milky` | ✅ | ✅ | 事件与调用名都要翻译 |

**`maxStore` 没有动作侧**，因为它不是聊天协议（注册表本来就禁止把它选作聊天协议）。把它摆在
这一层是为了让「事件先进库再分发」这个次序**看得见**：它此前只写在 `_dispatch_event` 的一条
注释里，一次重构就可能被挪到裁剪之后，那时历史数据就只剩当时那个协议要的字段了。

### 为什么协议要跟着连接走

`EventHub` 与反向 WS 客户端各自登记一个 `Connection`（`src/qsick/onebot/pipeline.py`）：谁连上
来的、用哪条配置、什么协议、哪个账号。分流点只读它，不认识任何具体协议。

改动前这一切不存在：`protocol` 字段存在配置里、显示在下拉框里，但一条消息进来后**所有连接
收到的 JSON 完全相同**。同一实例下挂一条 `onebotv11` 和一条 `milky`，两条都收到 v11 形状。

### 失败怎么处理

两个方向相反，这是刻意的：

- **事件编码失败**：只跳过走那个协议的连接，其余照发。一条消息喂不了某个协议，不该让所有
  客户端都收不到。同一个错误只打一行 WARN —— 每条消息都打会把日志刷满，那正是它失去作用的
  方式。
- **动作解码失败**：向上抛，调用方回一个明确的失败。静默跳过会让客户端一直等到超时，而它
  发出的调用其实早就被拒绝了。

### 一条不能让步的约束

**动作必须经过 `runtime.invoke`。** 插件过滤、账号级出入限制、只读缓存、发送串行化全在它的
入口。绕开它直接把调用转给宿主，core 插件的封禁就形同虚设——而那从外面完全看不出来。

## Milky

### 复用了什么

**动作逻辑一行没写。** 65 个 API 里今天能执行的 33 个，全部翻译成内部已有的动作名
（`protocols/milky/actions.py`），执行走 `invoke`。

名字认识但底层没实现的 32 个单独列在 `NOT_IMPLEMENTED` 里，回一个说明原因的失败——
「做了却悄悄没做」比「明确说不支持」坏得多。

**消息段借道 v11。** 内部事件的 `message` 是 v11 段数组，转 Milky 段时先用
`protocols/onebotv11/codec.py` 还原成内部部件，再用 `protocols/milky/codec.py` 编码。两条路
只有一份 v11 解析。

### 端点

每种协议在自己的路径前缀下，**不做别名**：

| | OneBot v11 | Milky |
| --- | --- | --- |
| 前缀 | `/ONEBOTV11/` | `/MILKY/` |
| 调用 | `POST /ONEBOTV11/`（动作名在 body 里） | `POST /MILKY/api/{api}`（动作名在路径里） |
| 调用体 | `{action, params, echo}` | 参数直接是 body，至少要 `{}` |
| 应答 | `{status, retcode, data, echo}` | `{status, retcode, message, data}` |
| 推送 | `GET /ONEBOTV11/`（WS/SSE），另有 `/ONEBOTV11/ws`、`/ONEBOTV11/sse` | `GET /MILKY/event`（WS/SSE），SSE 帧是 `event: milky_event` |
| 健康检查 | `GET /ONEBOTV11/health` | — |

两种协议挂在同一个端口的不同前缀上：方向一致（协议端都是服务器），前缀不重叠。

**前缀本身就是协议声明**，这比分流点里的 profile 反查更可靠——请求自己带着答案，不依赖配置里
恰好只有一条匹配的记录。所以 `hub.connection()` 在这些路径上直接写死协议。

旧的无前缀路径（`/`、`/ws`、`/sse`、`/api/*`、`/event`）**已移除**。多留一个别名就多一条会被
误用的路径：客户端分不清自己连的是哪套协议。

### API 覆盖（65/65 全部归类）

| 类 | 数量 | 含义 | 调用结果 |
| --- | ---: | --- | --- |
| `SUPPORTED` + `DYNAMIC` | 52 | 名字与参数都能对上（其中 3 个按参数决定目标动作） | 真正执行 |
| `NEEDS_TRANSLATION` | 8 | 名字有、参数模型对不上 | **明确失败**并说明差什么 |
| `NO_ACTION` | 4 | 连内部动作名都没有 | **明确失败**并说明 |
| `LOCAL` | 1 | `get_impl_info`，适配器自己回答 | 本地返回 |

`NEEDS_TRANSLATION` 全是请求处理类：Milky 用 `notification_seq` / `initiator_uid`，内部用 OneBot
的 `flag`——一个由平台在推送时生成的不透明字符串。两者之间没有换算关系，只能等某一侧的映射表。
硬翻过去的结果是动作作用在错的对象上，比报错危险。

`NO_ACTION` 是 QQ 侧真的没有接口：置顶列表的读与写、转存永久文件、重命名群文件夹。

`DYNAMIC` 的三项里 `get_history_messages` 最典型：Milky 用一个 API 覆盖群/好友/临时三种会话，
内部按会话分成 `get_group_msg_history` 与 `get_friend_msg_history`，所以目标动作只能在调用时定。

**翻译表里不能有指向不存在动作的条目**——那条测试是这张表唯一的硬约束。表写错的结果是运行时
的 `unknown action`，而它看起来像内部坏了，不像协议层写错。

### 事件覆盖

21 个 Milky 事件里，能从宿主真正产生的事件转出 3 个（`message_receive`、`message_recall`、
`bot_offline`）；另有 7 个的转换已写好但宿主今天不产生（`group_member_increase`、
`group_admin_change`、`group_mute`、`friend_request`、`group_join_request` 等），宿主一旦开始发
`notice.group_*` / `request.*` 就立刻生效；其余 11 个内部连槽位都没有。

这个中间状态写在这里而不是藏起来：加一个事件不是改协议层，是先让宿主产生它。

## 加一个协议

在 `protocols/` 下新建一个目录、导出 `create()`，在 `enabled_protocol.json` 的 `chat` 数组里
登记，然后在网络配置里选它。`ChatProtocol` 的契约见 `protocols/base.py`：

| 方法 | 必须 | 作用 |
| --- | --- | --- |
| `encode_message` / `decode_message` | ✅ | 消息段 ⇄ 内部部件 |
| `encode_action` | ✅ | 内部动作 → 本协议的请求体 |
| `capabilities` | ✅ | 能表达什么、要降级什么 |
| `encode_event` | 有默认 | 内部事件 → 本协议事件（默认恒等，即 v11） |
| `decode_action` | 有默认 | 本协议调用名 → 内部动作名（默认恒等） |
| `format_reply` | 有默认 | 内部结果 → 本协议应答信封（默认恒等） |

后三个有默认实现，所以只做消息格式的适配器不用管它们。

`protocols/onebotv11/` 是最小样板；`protocols/milky/` 是完整的例子（含事件与调用翻译）。

## 已移除的协议

`onebotv11-EXTemotions` 曾在此处：OneBot v11 承载消息、表情改用 EXTemotions 对象，
好让动态性与真实文件格式都能表达。移除的原因是它是纯 v11 的严格超集，而纯 v11 的
`face` 段只装得下 `id` —— 两个协议的差别全在一个 WebUI 聊天栏的发送路径上，聊天栏
删除后没有任何消费方。

带走的还有：`protocols/extemotions/`（规范数据模型）、`src/qsick/protocol/extemotion.py`
（`[CQ:ext_emotion,...]` 解析）、`tools/import_emotions.py` 与 `tools/import_from_client.py`
（表情素材导入），以及 `assets/qq-faces/` 的全部消费方。

要加回来照上面「加一个协议」那节做，`emotion` 部件的字段一直保留着完整形状——纯 v11
编码时只取 `id`，但因为是在编码那一刻才丢，将来接一个能表达动态性的协议不需要回头重解析历史。
