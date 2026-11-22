# QSICK OneBot v11 协议审计笔记

## 范围与证据

针对 Python 协议/传输代码及测试定义的只读静态审计。未修改或检视任何源码、在线配置、账号数据或运行中的 QQ 实例；测试仅被**阅读，未被执行**。仅创建了本文档。逐个 action 的清点另行处理。

参考基线：[OneBot v11 规范仓库](https://github.com/botuniverse/onebot-11)，尤其是通信、事件与消息规范。在本环境中，尝试获取原始 GitHub 通信文档及其 GitHub 页面均失败。因此以下结论是基于代码、对照已知 v11 契约得出的，而非新下载的规范性一致性认证。

严重程度：**高** = 重大互操作性故障或凭据边界风险；**中** = 有损行为、不准确的协议语义或缺失的可选接口面。Python 处理器、适配器、mock 测试或本次审计均**不能确立**原生 QQ 操作成功。

## 发现与安全修复

### 1. 高 — 反向 WebSocket 在 connect 事件之后仅支持 action

[reverse.py:63–99](D:/QSICK/src/qsick/onebot/reverse.py#L63-L99) 连接、发出 lifecycle/connect，之后仅读取 action 并返回响应。它从不订阅事件分发。[EventHub:23–28,57–83](D:/QSICK/src/qsick/onebot/server.py#L23-L83) 只持有正向 socket 与 SSE 队列；[runtime.py:411–430](D:/QSICK/src/qsick/runtime.py#L411-L430) 仅向那里发送 message/notice/request/heartbeat 事件。

**影响：** 已配置的反向 WS 机器人可以调用 action，但收不到任何普通消息、通知、请求或周期性心跳。反向客户端 docstring 中关于心跳的声明并未实现。

**安全修复：** 为每个反向连接提供有界的 EventHub 订阅，以及一个同时处理 action 回复与事件的专用串行写出器；断连时取消订阅，避免无界队列，保持每连接内的投递顺序。应测试注入的 message/notice/request/heartbeat 投递、重连清理以及多个 URL，而不只是握手/action 回复。

### 2. 高 — HTTP 端点属于自定义 RPC 扩展，并非标准 v11 HTTP API 接口面

[server.py:118–144](D:/QSICK/src/qsick/onebot/server.py#L118-L144) 只挂载一个已配置路径（默认 `/`），期望 POST `{action, params, echo}`，或 GET `?action=...`。它没有 `/<action>` 路由，且 JSON 是唯一的 POST 解码器。因此规范的 action 路径 GET 与 POST 参数对象/表单数据都无法到达分发层。

**安全修复：** 增加经认证的 `/<action>` GET/POST 路由，接受查询参数、JSON 对象与 URL 编码表单参数，并保持一致的布尔/数值强制转换；将根 RPC 明确保留为扩展。应独立于现有根 RPC 测试，测试 `/get_login_info` 与 `/send_group_msg` 的各种形态。非法内容必须产生明确的错误，而不是依赖框架的失败。

### 3. 高 — GET 查询中的认证令牌会进入 action/原生参数

[server.py:86–96](D:/QSICK/src/qsick/onebot/server.py#L86-L96) 接受查询参数 `access_token`；[server.py:140–143](D:/QSICK/src/qsick/onebot/server.py#L140-L143) 只移除 `action`，从而把该凭据留在了分发参数中。[registry.py:182–185](D:/QSICK/src/qsick/onebot/registry.py#L182-L185) 会转发未知键，而 [PayloadAction:37–48](D:/QSICK/src/qsick/onebot/actions/base.py#L37-L48) 再把它们转发给 payload 操作。

**影响：** 通过查询参数认证的通用适配器调用，可能把 OneBot 凭据暴露给下游原生 RPC/插件代码以及相应的 payload 日志。这是一条静态确立的传播路径，并不表示观察到外部窃取行为。

**安全修复：** 在 action 校验/分发之前移除仅用于传输的字段（`action`、`access_token`、关联元数据）；单独传递 echo。增加 mock payload 捕获测试，断言不会转发任何凭据。

### 4. 高 — 原生 uint64/字符串消息 ID 未被转换为 v11 公开消息 ID 域

[message.py:43](D:/QSICK/src/qsick/protocol/message.py#L43) 把原生 ID 字符串转换为 Python 整数；[events.py:104–106](D:/QSICK/src/qsick/onebot/events.py#L104-L106) 直接把这些整数序列化为 `message_id`/`real_id`。[base.py:123–127](D:/QSICK/src/qsick/onebot/actions/base.py#L123-L127) 则原样复制发送结果中的原生值，可能是一个字符串。现有测试夹具在 [test_headless_bridge_rpc.py:26–37,57–79](D:/QSICK/tests/test_headless_bridge_rpc.py#L26-L79) 中明确演练了原生 `9007199254740993` 字符串以及最大 uint64 ID。

**影响：** 标准的 integer/int32 客户端可能拒绝这些 ID；JavaScript 在事件 JSON 中遇到大于 2^53−1 的数字会丢失精度；发送 ID 与事件 ID 的 JSON 类型可能不一致。额外的 `message_id_str` 虽有帮助，但并不能为既有消费者修复标准字段。Python/存储层面的内部精确性与线上兼容性属于两种不同的能力。

**安全修复：** 持久化一个双射的公开有符号 int32 OneBot ID 映射，映射到账号 + 会话 + 精确的原生 ID/引用；在 send/get/delete/reply 以及撤回通知中一致使用。仅在已文档化的扩展字段中保留原始原生字符串 ID。显式处理范围耗尽与迁移；绝不在无冲突处理的情况下截断/哈希。保留原生 RPC 现有的大整数转字符串行为。

### 5. 中 — 通道禁用开关并未真正禁用 HTTP/WS 路由

[runtime.py:248–252](D:/QSICK/src/qsick/runtime.py#L248-L252) 只要任一 HTTP/WS/SSE 标志启用，就会启动共享监听器。[server.py:118,135,146](D:/QSICK/src/qsick/onebot/server.py#L118-L146) 总是注册 HTTP 与 WS；只有 SSE 在 [server.py:190](D:/QSICK/src/qsick/onebot/server.py#L190) 处是条件性的。因此 `enable_http=False` 而 WS/SSE 启用时，HTTP 仍被暴露，反之亦然。

**安全修复：** 仅在启用时才注册对应传输。补充完整的启用/禁用矩阵。这属于实现/安全配置缺陷，而非新的 v11 协议要求。

### 6. 中 — 字符串 CQ 与数组消息能力不一致；CQ 输出有损

[base.py:108–120](D:/QSICK/src/qsick/onebot/actions/base.py#L108-L120) 只识别 CQ 的 text/at/face/reply/image/record/video/file。合法的 JSON/XML/dice/rps/contact/location/music/forward 字符串 CQ 会变成 `unknown` 并被 [base.py:144–146](D:/QSICK/src/qsick/onebot/actions/base.py#L144-L146) 拒绝，尽管在 [elements.py:267–328](D:/QSICK/src/qsick/protocol/elements.py#L267-L328) 中存在对应的数组映射。

[原始 CQ 渲染:342–387](D:/QSICK/src/qsick/protocol/elements.py#L342-L387) 对文本做了转义，但直接内插段参数值，未对逗号/方括号/与号做转义。JSON/XML/forward/poke 得到的是 `[QSICK:...]` 占位符，而非其标准 CQ 形式，location/music 则遗漏了重要字段。即使数组消息仍然含有信息，这也会影响输出的 `raw_message`。

**安全修复：** 为数组与 CQ 两种形式提供统一的段 schema/编解码器；对 `&`、`[`、`]` 以及参数值中的逗号做转义；保留所有标准字段。保持 `auto_escape=True` 作为字面文本行为。补充数据值往返测试（而不只是段类型匹配）、转义后的 URL/卡片数据，以及针对每个受支持标准类型的一致性夹具。

### 7. 中 — 若干标准段与媒体选项在 Python 适配器边界缺失/丢失

[已知类型:19–45](D:/QSICK/src/qsick/protocol/elements.py#L19-L45) / [发送转换:267–328](D:/QSICK/src/qsick/protocol/elements.py#L267-L328)：没有标准的 `share`、`shake`、`anonymous`、`node`；`poke` 在接收时会被输出，但发送转换器不接受它。原生操作无法挽回已被 Python 拒绝的段。

[媒体输入:331–336](D:/QSICK/src/qsick/protocol/elements.py#L331-L336) 只保留 file/url/name/file_id/file_size/path，丢弃了标准图片的 `type=flash`、语音的 `magic` 以及发送控制项 `cache`、`proxy`、`timeout`。[图片/语音输出:135–140](D:/QSICK/src/qsick/protocol/elements.py#L135-L140) 不映射标准图片 `type` 或语音 `magic`；图片的 `flash` 是一个扩展形态的字段，不能替代 `type=flash`。[XML/JSON 转换:177–180,288–291](D:/QSICK/src/qsick/protocol/elements.py#L177-L180) 还会丢弃可选的资源标识符。

**安全修复：** 实现显式的标准方向 schema，并端到端保留各项选项；在原生支持确实缺位之处增加校验与结构化的不支持错误。不要把任意选项的接受标记为真正的媒体/QQ 支持。

### 8. 中 — 消息事件语义被归一化抹除；notice/request schema 为未校验的透传

[events.py:100–125](D:/QSICK/src/qsick/onebot/events.py#L100-L125) 总是发出群 `sub_type=normal`、私聊 `sub_type=friend` 以及群 `anonymous=null`。[Message 模型:11–26,37–57](D:/QSICK/src/qsick/protocol/message.py#L11-L57) 不保留 subtype/anonymous/发送者人群统计字段。临时会话/群来源私聊消息、其他私聊来源以及匿名/通知类群消息都无法被如实表示。发送者输出遗漏了已文档化的 `sex`、`age` 以及群发送者的 `area`、`level`、`title`；应将此归类为形态覆盖缺失，上游可用性尚待确立。

[events.py:130–150](D:/QSICK/src/qsick/onebot/events.py#L130-L150) 从命令后缀泛化地推导 notice/request 类型，然后覆盖任意 payload 字段。没有按类型的必填字段校验器、强制转换、标志归一化，也没有防止 payload 覆盖信封的保护。这**并不能证明每个原生 notice/request 都是畸形的**：它确立的是合规性完全交由生产者负责，且在这一层目前未经测试。

**安全修复：** 在内部模型中保留有意义的 subtype/anonymous/发送者数据；为所有对外声明的事件定义带类型的逐事件映射器；仅在允许之处使用显式的未知值；把可信信封字段与扩展分离。用夹具测试每一种事件变体。

### 9. 中 — 生命周期/心跳可能上报虚假健康状况；一条错误路径会丢失 echo

[EventMapper:45–48,163–170](D:/QSICK/src/qsick/onebot/events.py#L45-L48) 把每个 `login` 命令都视为 lifecycle/enable，即使 payload 表示登出，且上游心跳映射总是报告 online/good 为 true。独立的周期路径 [runtime.py:419–430](D:/QSICK/src/qsick/runtime.py#L419-L430) 使用已登录状态，但不使用 `login.online`。

[runtime.py:446–450](D:/QSICK/src/qsick/runtime.py#L446-L450) 在 headless 账号不匹配时以 `fail(...)` 拒绝，却未带上传入的 echo，从而在这条扩展专有的错误路径上破坏了 WS 关联。常规注册表分发在成功/失败时都会保留 echo，不过 [registry.py:166–168](D:/QSICK/src/qsick/onebot/registry.py#L166-L168) 使用 `setdefault`，因此处理器提供的错误 echo 会被保留下来。

**安全修复：** 集中化状态/生命周期转换，保留准确的在线状态并按需发出 disable；在传输/分发边界对所有响应无条件复制请求 echo。应测试对象/列表/零/false/null 形式的 echo 以及错误情况，而不只是字符串/数字的成功场景。

## 覆盖清点（仅 Python 层）

| 领域 | 有证据支撑的覆盖 | 缺失或注意事项 |
|---|---|---|
| HTTP action | 自定义根 JSON-RPC 与查询式 action 端点、查询参数/Bearer 令牌认证 | 标准 action 路径/表单接口面缺失；认证密钥转发缺陷 |
| 正向 WebSocket | 一个可配置的通用端点（默认 `/ws`）、connect 生命周期、action/echo、EventHub 事件 | 无 `/api` 仅 API 或 `/event` 仅事件的角色端点；无按通道的开关强制 |
| 反向 WebSocket | 多个可配置的外发 URL、Universal 角色、X-Self-ID、可选 Bearer、重连、connect/action 回复 | 无事件/心跳订阅；无分别配置的 API/事件角色 |
| HTTP 事件上报 | 在所检视的传输/运行时代码中无 | 反向 HTTP POST、`X-Self-ID`、HMAC-SHA1 `X-Signature`、响应快速操作均缺失；这些是缺失的传输特性，而非要求启用所有模式 |
| SSE | 经认证的 EventHub 流、有界队列、keepalive | QSICK 扩展，并非标准 v11 传输 |
| API 信封 | `status`、数值 `retcode`、`data`、`echo`；成功/失败；`message`/`wording` 扩展 | 实现专有的 140x/150x 代码本身并不构成不符合规约的证明；异步/限速执行约定缺失 |
| 事件消息 | 群/私聊数组、原始文本/CQ、time/self/sender/会话 ID、font、存储/媒体富化 | 硬编码 subtype、发送者字段不完整、原始原生消息 ID、CQ 有损 |
| 事件通知 | 泛化的 `notice.*` 透传 | group upload/admin/decrease/increase/ban/friend_add/group_recall/friend_recall/notify poke/lucky_king/honor 等 schema 未逐一验证 |
| 事件请求 | 泛化的 `request.*` 透传 | 好友/群添加/邀请的必填 payload/flag 处理未独立验证 |
| 元事件 | WS 上的 connect、登录时的 enable、周期性正向心跳 | 反向心跳缺失；disable/健康语义不完整 |
| 接收标准段映射 | text、at、face、reply、image、record、video、json、xml、forward、poke、dice、rps、contact、location、music | 存在上述形态/选项丢失；原生事件产生未确立 |
| 发送数组标准映射 | text、at、face、reply、image、record、video、json、xml、forward、dice、rps、contact、location、music | 仅为适配器接受；缺少 share/shake/anonymous/node/poke 转换 |
| 发送字符串 CQ 映射 | text、at、face、reply、image、record、video | 远小于数组接口面；`file` 亦作为扩展被接受 |
| 扩展 | file/mface/markdown/miniapp、flash_file/onlinefile/inline_keyboard 接收形态、未知保留；message_sent、原生 ID 别名、可选 typing 噪声 | 不要把这些计为新增的标准 v11 能力；部分为仅接收 |
| 精确原生 ID 存储/RPC | Python 整数/uint64 持久化；超出安全范围的整数在内部转换为十进制字符串 | 不等同于公开 OneBot ID 兼容性 |

## 所检视测试及其不能确立的内容

- [test_onebot_http.py:17–112](D:/QSICK/tests/test_onebot_http.py#L17-L112)：根 RPC、查询参数/Bearer 认证、非法 JSON、未知 action 信封、正向 connect/action echo 以及被拒绝的 WS 令牌。无规范 HTTP 路径/表单数据、混合标志、事件 schema 或高级 echo 夹具。
- [test_reverse_ws.py:20–75](D:/QSICK/tests/test_reverse_ws.py#L20-L75)：仅本地 WS 握手/action 响应/头信息/重连；无常规事件或心跳投递断言。
- [test_elements.py:18–29,43–50,75–107](D:/QSICK/tests/test_elements.py#L18-L107)：基本类型往返、文本方括号转义、简单 CQ @ 解析、未知拒绝以及若干扩展。多数往返只断言段类型，而不断言被保留的数据/选项。
- [test_runtime.py:11–108](D:/QSICK/tests/test_runtime.py#L11-L108) 与 [test_logic.py:40–169](D:/QSICK/tests/test_logic.py#L40-L169)：mock 摄入、守卫/噪声、去重、发送序列化、SSE 队列溢出、缓存与整个监听器关停。并非逐事件 schema 一致性。
- [test_registry.py:9–14,56–60](D:/QSICK/tests/test_registry.py#L9-L60) 统计已接线的处理器，并确认通用适配器返回的是 **mock** payload。这确立的是注册/分发，而非 230+ 个原生 QQ 实现。
- [test_headless_bridge_rpc.py:26–79](D:/QSICK/tests/test_headless_bridge_rpc.py#L26-L79)：mock 的 bridge 响应、入口处精确的大 ID 以及 SQLite 存续。无真实 QQ 验证、公开 ID 范围/类型契约，也无端到端的 OneBot 撤回/回复映射。

建议顺序：先修复反向事件分发与标准 HTTP 形态；隔离传输凭据并实现稳定的公开 ID；统一 CQ/数组编解码器；增加带类型的事件契约、媒体选项保留与通道开关测试；然后以显式选择加入的集成夹具分别验证每个原生后端。本次审计未执行任何在线验证。
