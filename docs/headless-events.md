# 事件从哪来：QQ → 宿主 → 协议层

这份文档回答一个问题：**某个事件为什么有、或者为什么没有**。它按"来源"分类，不按事件名，
因为缺的东西在同一类里往往一起缺。

## 两步链路

一条事件到客户端要经过两次转换，任一处缺失都会让事件彻底消失：

```
QQ NT 内核
   │  ① 宿主把它变成 OneBot v11 形状   ← src/qsick/headless_host.js
   ▼   {post_type, notice_type/request_type, ...}
传输层 → runtime → onebot/events.py
   │  ② 协议层把它变成各条连接要的形状   ← protocols/*/events.py
   ▼  onebotv11: 原样   milky: 事件名与字段都换
```

第 ② 步是通用的：`onebot/events.py` 把 `cmd` 首个点之后的部分直接当成 `notice_type`，所以
宿主只要抛 `notice.group_name`，v11 客户端就收到 `notice_type=group_name`，Milky 连接则收到
`group_name_change`。

## ① 宿主能产出的东西，取决于它挂在哪个通道上

QQ NT 有两种推送方式，宿主只接了第一种。

### 通道 A：消息里的灰条（已接）

系统提示（"某某 修改了群名"、"某某 被禁言"）不是独立回调，而是**一条只含灰条元素的消息**
（`elementType` 8）。宿主已经通过 `addKernelMsgListener` 收到这条消息，所以直接解析它。

`src/qsick/headless_notices.cjs` 读这些结构：

| `subElementType` | 结构 | 变成 |
| ---: | --- | --- |
| 4 | `groupElement.type` 5，`.groupName` | `notice.group_name` |
| 4 | `groupElement.type` 8，`.shutUp.{duration, member.uid, admin.uid}` | `notice.group_ban`（有 member）/ `notice.group_whole_ban`（无 member） |
| 4 | `groupElement.type` 1，`.memberUid`、`.adminUid` | `notice.group_increase`（**仅当被加的是自己**） |
| 4 | `groupElement.type` 3，`.memberUid` | `notice.group_decrease`，`sub_type=leave` |
| 4 | `groupElement.type` 2 / 10 | `notice.group_disband` |
| 17 | `jsonGrayTipElement.busiId` `"1061"`，`.jsonStr.items[].uid` | `notice.notify`，`sub_type=poke` |

另外：**纯灰条消息不再作为聊天消息下发**。以前它会被编码成一堆 `unknown` 段然后当成一条
`message` 事件发出去，客户端看到的是"[未知消息]"。现在只发通知。

### 通道 B：包推送（未接）

成员**被踢**、**别人入群**、**管理员任免**、**好友/入群请求**都只从
`trpc.msg.olpush.OlPushService.MsgPush` 出来，按 `contentHead.type` 分：

| `contentHead.type` | 内容 | 对应事件 |
| ---: | --- | --- |
| 33 | 成员增加 | `group_member_increase` |
| 34 | 成员减少（含被踢） | `group_member_decrease`，`sub_type=kick` |
| 44 | 管理员任免 | `group_admin_change` |
| — | 通知列表推送 | `friend_request` / `group_join_request` / … |

QSICK **没有**包级别的钩子（全仓没有 `packetHandler` / `onCmd` 之类），所以这一整类事件目前
发不出来。它们不是"忘了解析"，是缺一条订阅通道。

要接上，宿主需要拿到 QQ NT 的 packet handler 并按 `contentHead.type` 解 protobuf——每种 type
都有自己的字段布局，没有真实报文时写出来的只会是猜的。

## ② 已经做完但宿主不产生的

协议层的转换写好了，宿主一旦开始抛对应 `cmd` 就立刻生效，不用再改协议层：

| 事件 | 缺什么 |
| --- | --- |
| `group_admin_change` | 通道 B（type 44） |
| `group_member_increase`（别人） | 通道 B（type 33）；灰条 only 说自己 |
| `group_member_decrease`（被踢） | 通道 B（type 34）；灰条只说自愿退群 |
| `friend_request` / `group_join_request` / `group_invited_join_request` / `group_invitation` | 通道 B（通知列表） |
| `group_essence_message_change` | 灰条里有一个 busiId，但要拿消息 seq 才能对齐 |
| `group_message_reaction` | 灰条是 XML，回应人和表情 id 在文档内部，离线无法确认形状 |
| `friend_file_upload` / `group_file_upload` / `peer_pin_change` | 没有任何来源 |

`group_essence_message_change` 与 `group_message_reaction` 的取舍写在
`headless_notices.cjs` 顶部的注释里：**宁可不发，也不发一个错账号的事件**。

## `notice.group_disband` 不是 v11 标准事件

发出去时严格客户端会忽略它，宽松的会显示它。保留的理由是：丢掉这条信息没有任何好处，而标准之外
的 `notice_type` 本来就是可以被忽略的。Milky 侧它是规范事件 `group_disband`。

## 怎么验证

`tests/test_headless_notices.py` 用按上表构造的报文跑真实提取器，`tests/test_protocol_milky.py`
验证到 Milky 事件。这两组是**形状**测试：证明"字段这么放会得到这个事件"，证明不了
"QQ 一定这么放"。

补上真实抓包后应当加一组回归用例；在那之前，任何关于"这个事件一定能收到"的说法都超出了现有
证据。
