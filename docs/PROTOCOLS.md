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

动作执行复用内部注册表，所有业务调用仍走 `runtime.invoke`。`protocols/milky/actions.py`
负责参数转换，`onebot/milky_bridge.py` 处理会话局部消息序号的解析与账号绑定，
`protocols/milky/replies.py` 按原 Milky API 名转换返回数据。HTTP、事件 WS 上的调用与反向 WS
共用交接入口；反向连接绑定的账号不能被请求参数覆盖。

消息序号不是消息 ID：先按账号、会话类型、目标及序号查缓存，必要时通过现有历史动作查询。
无法唯一定位时明确失败，不把序号当 ID。发送结果保留 QQ 确认回执中的真实序号和时间，缺失时
不伪造成功响应，也不自动重发。

参数映射表不等于完整能力证明：某些注册动作底层仍明确拒绝，某些返回还缺宿主元数据。
已有能力的交接缺口已补，不能取得的数据仍如实报告限制。

**消息段双向都借道 v11。** 内部事件的 `message` 是 v11 段数组，转 Milky 段时先用
`protocols/onebotv11/codec.py` 还原成内部部件，再用 `protocols/milky/codec.py` 编码；两条路
只有一份 v11 解析。

**发送方向也必须交 v11 段**，这不是对称性偏好：内部动作层（`onebot/actions/base.py` 的
`message_to_elements` → `elements.from_onebot`）就是按 `segment["type"]` 解析的。曾经这里把
Milky 段转成"内部部件"（`{kind, ...}`）就交给动作层，于是**每一个按规范发消息的 Milky 客户端
都拿到 `-400 unsupported message segment type None`**——离线测试用的是 v11 形状，所以没测出来，
是实机才暴露的。现在 Milky 段先用本协议 codec 解成部件、再用 v11 codec 编成段，跨协议的依赖
写在 `adapter._to_internal_segments` 里并注明了原因。

判"已经是 v11 段"的依据是 `data` 为**对象**，不是"有 `type` 和 `data` 两个键"：Milky 的
`light_app` 与 `xml` 段恰好也有 `data`，但它是字符串。按键存在判断会让这两类段原样透传，
最终以 `unknown` 到达客户端。

**一条内部事件可以产出多条协议事件。** `Pipeline.encode_events` 返回列表（适配器只实现
`encode_event` 也可以，返回值会被归一化），分发点按协议各发一次。这不是为将来预留，而是
Milky 的现实需要：好友私聊里带文件的消息同时是 `message_receive` 与 `friend_file_upload`
（规范把文件上传定义成独立事件），而 v11 流里不应因此多出一条非 v11 的通知。

**身份取请求的账号，不取全局主账号。** `ActionContext.self_id` 优先用请求里的 `account`，
`login_state_for()` 按 **uin** 匹配登录态——headless 实例用实例名做键、pipe 会话用 uin 做键，
只按其中一种查会漏。此前 `self_id` 恒等于运行时主账号，多账号下 `get_login_info` 会回另一个号
的 uin，即使请求与网络配置都指定了同一个账号。

### 端点

**URL 里不带协议前缀。** 一条连接说什么协议，由它落在哪条网络配置上决定，而不是由路径猜。
协议是网络配置的属性（下拉框里选的那一项），路径是那张配置自己的字段。

| | OneBot v11 | Milky |
| --- | --- | --- |
| 调用 | `POST {path}`（动作名在 body 里），另有标准地址 `POST /action` | `POST {path}/api/{api}`（动作名在路径里） |
| 调用体 | `{action, params, echo}` | 参数直接是 body，至少要 `{}` |
| 应答 | `{status, retcode, data, echo}` | `{status, retcode, message, data}` |
| 推送 | `GET {path}`（SSE/WS）；独立 WS 配置用 `{ws_path}` | `GET {path}/event`（SSE/WS），SSE 帧是 `event: milky_event` |
| 健康检查 | `GET /health` | — |

`{path}` 就是配置里的「路径」。**它对这条配置的所有传输生效**，不是只给 GET 用：

* v11：同一个 URL 上 `POST` 跑动作、`GET` 推事件（勾选 SSE 时）、`Upgrade` 建 WebSocket
  （勾选"同一路径也接受 WebSocket 客户端"时）。三者靠方法/升级头区分，互不遮蔽。
* Milky：配置的路径是**基路径**，规范后缀追加在它下面。路径填 `/` 就是 `/api/{api}` 与
  `/event`；填 `/bot` 就是 `/bot/api/{api}` 与 `/bot/event`。

**端口按地址分组监听。** HTTP 与 WS 配置写同一 `host:port` 时共用一个监听器（一个 uvicorn
server 同时提供两种传输）；写不同地址时**各自起一个** server，互不遮蔽。这正是不能靠"第一条
生效"来实现的地方：不同端口之间不存在竞争。

协议同理按配置各自独立：同一端口上可以 HTTP 走 v11、WS 走 Milky，两条连接各用自己的编码器。
`config.onebot` 里的 `http_protocol` / `ws_protocol` 就是这么来的；`apply_to_config` 保留的
只是**第一条**监听器的投影，给状态页和旧调用方看，不再决定实际监听几个端口。

旧前缀（`/ONEBOTV11/`、`/MILKY/`）**已移除**：多一个别名就多一条会被误用的路径，而且前缀
一旦保留，"协议由配置决定"就变成两套并存的规则。

### API 覆盖（65/65 全部归类）

| 类 | 数量 | 含义 | 调用结果 |
| --- | ---: | --- | --- |
| `SUPPORTED` + `DYNAMIC` | 53 | 已登记动作映射，不代表底层和返回字段全部可用 | 按已有能力执行或明确失败 |
| `NEEDS_TRANSLATION` | 8 | 请求审批标识缺转换 | **明确失败**并说明差什么 |
| `NO_ACTION` | 1 | 转存永久文件尚无对应能力 | **明确失败**并说明 |
| `UNSUPPORTED_BY_DESIGN` | 2 | 会话置顶读写不实现 | **明确失败**并说明 |
| `LOCAL` | 1 | `get_impl_info`，适配器自己回答 | 本地返回；QQ 版本未知时为空 |

`NEEDS_TRANSLATION` 全是请求处理类：Milky 用 `notification_seq` / `initiator_uid`，内部用 OneBot
的 `flag`——一个由平台在推送时生成的不透明字符串。两者之间没有换算关系，只能等某一侧的映射表。
硬翻过去的结果是动作作用在错的对象上，比报错危险。

重命名群文件夹已接到宿主现有 `group.folder.rename`；创建文件夹必须返回真实服务端 ID，
不再用名称代替。群文件上传支持本地路径、file URI、HTTP(S) 和 base64，读取有大小/时间上限；
结果仍 pending 或失败时保留临时源，防止 QQ 尚在读取，不自动重试写操作。

**这两个动作仍有实机未通过的部分**，原因在宿主而不在协议层：`createGroupFolder` 创建成功但
响应不返回 folderId，`group.file.upload` 可能在文件已上传的情况下超时。两者都改成"事后回查
文件列表"，然而列表滞后实测为 6.1 s / 16.8 s / **超过 30 s**，无规律可循，所以回查不是可靠的
取 ID 手段。当前实现用 60 s 窗口并在查不到时明确失败（绝不编 ID），此路仍待换方案——正确方向
是查清创建响应里 ID 的真实字段名。详见 `docs/milky-live-test.md`。

`kickMemberV2` 的 ack 在本机不带整数 `result`，按状态判会把一次已经发出的踢人报成失败。
现在 ack 有状态照旧判、无状态则以**成员列表**为准（人还在就是超时失败，不谎报成功），
并记录 ack 实际到达的字段名。

`DYNAMIC` 中 `get_history_messages` 最典型：Milky 用一个 API 覆盖群/好友/临时三种会话，
内部按会话分成 `get_group_msg_history` 与 `get_friend_msg_history`，所以目标动作只能在调用时定。

**翻译表里不能有指向不存在动作的条目**——那条测试是这张表唯一的硬约束。表写错的结果是运行时
的 `unknown action`，而它看起来像内部坏了，不像协议层写错。

### 事件覆盖

pipe 的原始 OLPush 通道已接到 `runtime.feed_packet`；灰条/群文件通知也复用现有提取器，
不再只在 headless 启用。纯灰条不作为聊天消息转发，群文件上传通知与文件消息共存。

Milky 消息事件提供顶层 `group_member` 并保留可识别的 temp 场景；撤回使用真正的 `msg_seq`
而非消息 ID，无法取得的操作者不冒充发送者。好友请求保留原始 UID。管理员任免方向已修复。

**`friend_file_upload` 已实现**，来源是好友私聊里带文件的消息：QQ NT 没有"好友发来文件"的
通知，规范却把这件事定义成独立事件，所以它在这一层派生。约束是：群文件不误报（那属于
`group_file_upload`）、自己发时 `is_self=true`、缺少 `file_id` 或 `file_name` 时干脆不发
（发出去也没人能用）、`file_hash` 是 TriSHA1，拿不到就留空——同一元素里的 MD5 放进这个字段
是**错的值**，而错值比明显缺失更糟。

仍然受限：群通知序号/邀请序号尚未可靠取得，请求审批未接；44 包没有可用的操作人字段；
邀请他人入群请求与置顶事件未实现。部分实体元数据、撤回操作者及戳一戳文案取决于宿主实际
提供的信息。灰条与包推送的重叠事件尚无统一跨来源去重，可能重复通知。
因此不能仅凭事件名已有转换器，就宣称完整符合 Milky 字段契约。

完整来源分类见 [docs/headless-events.md](headless-events.md)；实机测量怎么做、测到了什么、
哪些还不可信，见 [docs/milky-live-test.md](milky-live-test.md)。

## 加一个协议

在 `protocols/` 下新建一个目录、导出 `create()`，在 `enabled_protocol.json` 的 `chat` 数组里
登记，然后在网络配置里选它。`ChatProtocol` 的契约见 `protocols/base.py`：

| 方法 | 必须 | 作用 |
| --- | --- | --- |
| `encode_message` / `decode_message` | ✅ | 消息段 ⇄ 内部部件 |
| `encode_action` | ✅ | 内部动作 → 本协议的请求体 |
| `capabilities` | ✅ | 能表达什么、要降级什么 |
| `encode_event` | 有默认 | 内部事件 → 本协议事件（默认恒等，即 v11） |
| `encode_events` | 可选 | 内部事件 → **若干**本协议事件；不实现时用 `encode_event` 的单个结果 |
| `decode_action` | 有默认 | 本协议调用名 → 内部动作名（默认恒等） |
| `format_reply` | 有默认 | 内部结果 → 本协议应答信封（默认恒等） |

后四个有默认实现，所以只做消息格式的适配器不用管它们。

`encode_events` 不是为将来预留：一个内部事件确实可能对应多条本协议事件（Milky 的好友文件消息
同时是 `message_receive` 与 `friend_file_upload`）。返回值是 dict 还是 list 都会被接受，
所以实现其中一个即可。

**发送方向要交 v11 段**（见上面 "Milky" 一节）：内部动作层按 v11 段解析，把自家段直接丢给它
会得到 `unsupported message segment type None`。任何新协议都要在 `decode_action` 里把段编成
v11 形状。

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
