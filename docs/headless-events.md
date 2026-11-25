# 事件从哪来：QQ → 宿主 → 协议层

这份文档回答一个问题：**某个事件为什么有、或者为什么没有**。它按"来源"分类，不按事件名，
因为缺的东西在同一类里往往一起缺。

## 两步链路

一条事件到客户端要经过两次转换，任一处缺失都会让事件彻底消失：

```
QQ NT 内核
   │  ① 宿主把它变成 OneBot v11 形状
   ▼   {post_type, notice_type/request_type, ...}
传输层 → runtime → 三条流水线
   │  ② 协议层把它变成各条连接要的形状
   ▼  onebotv11: 原样   milky: 事件名与字段都换
```

第 ② 步是通用的：`onebot/events.py` 把 `cmd` 首个点之后的部分直接当成 `notice_type`，所以
宿主只要抛 `notice.group_name`，v11 客户端就收到 `notice_type=group_name`，Milky 连接则收到
`group_name_change`。**一个事件来源喂所有协议**，不存在"只给 Milky 做"的事件。

## ① 宿主能产出什么，取决于它挂在哪个通道上

QQ NT 有三条推送路径：灰条、群文件消息，以及 pipe-session 的原始包接收通道。

### 通道 A：消息里的灰条（已接）

系统提示（"某某 修改了群名"、"某某 被禁言"）不是独立回调，而是**一条只含灰条元素的消息**
（`elementType` 8）。宿主通过 `addKernelMsgListener` 已经收到这条消息，直接解析它。

`src/qsick/headless_notices.cjs`：

| 来源 | 结构 | 变成 |
| --- | --- | --- |
| `groupElement.type` 5 | `.groupName` | `notice.group_name` |
| `groupElement.type` 8 | `.shutUp.{duration, member.uid, admin.uid}` | `notice.group_ban`（有 member）/ `group_whole_ban`（无 member） |
| `groupElement.type` 1 | `.memberUid`、`.adminUid` | `notice.group_increase`（**仅自己被拉入群**） |
| `groupElement.type` 3 | `.memberUid` | `notice.group_decrease`，`sub_type=leave` |
| `groupElement.type` 2 / 10 | — | `notice.group_disband` |
| json tip `busiId` `"1061"` | `.jsonStr.items[].uid` | `notice.notify`，`sub_type=poke` |
| json tip 尾项文本 `头衔` | `.items[1].param[0]`、`.items[3].txt` | `notice.group_title` |
| json tip 首项 `jp` URL | `?groupCode=&msgSeq=` | `notice.group_essence`，`sub_type=add` |
| xml tip `templId` `10382` | `<qq jp>`、`<url msgseq>`、`<face id>` | `notice.group_msg_emoji_like` |

**纯灰条消息不再作为聊天消息下发**。以前它被编码成一堆 `unknown` 段然后当成一条 `message`
事件发出去，客户端看到的是"[未知消息]"。

### 通道 B：群文件型消息（已接）

群文件上传没有独立通知：上传本身就是**一条带 `fileElement` 的消息**（`elementType` 3）。

`headless_notices.cjs` 把 `{fileName, fileSize, fileUuid, fileBizId}` 转成 OneBot 的
`notice.group_upload`（`file: {id, name, size, busid}`）。消息本身照常下发——上传事件与消息
并存，不是二选一。

### 通道 C：OLPush 包推送（pipe 接收通道）

被踢出群、别人入群、本号进群、管理员任免、好友申请、入群申请只从
`trpc.msg.olpush.OlPushService.MsgPush` 出来，按 `contentHead.type` 分派。

**解码器已完整实现并测试**（`src/qsick/olpush.py` + `tests/test_olpush.py`：合成帧 + 实抓帧），
字段号来自三份独立实现与实抓的交叉验证：

| 来源 | 提供什么 |
| --- | --- |
| NapCat `packages/napcat-core/packet/transformer/proto/message/{message,groupAdmin,notify}.ts` | `PushMsgBody`、`ContentHead`、`ResponseHead`、`GroupChange`、`GroupAdmin`、`GroupInvite` 的字段号与类型 |
| Lagrange `Internal/Packets/Notify/*.cs` | 同一批结构的 `[ProtoMember(n)]`，以及 `FriendRequest`、`GroupJoin`、`SelfJoinInGroup`、`Event0x20D`、`GroupReaction`、`NotifyMessageBody` 的定义 |
| SnowLuma `packages/proto-defs/src/notify.ts`、`protocol/src/msg-push/enums.ts` | `GroupChange`/`GroupAdmin`/`GroupJoin`/`SelfJoinInGroup` 的类型化 schema，`PkgType`（85 = 本号进群）与 `Event0x210/0x2DC` 的子类型表 |

三者对 `GroupChange` 完全一致（1 群号、2 flag、3 成员 uid、4 decreaseType、5 operatorInfo bytes、
6 increaseType、7 其它），这是照着实现的依据。

2026-11-24 用 QQ 9.9.33-52230 实抓了 39 帧（23:37:30–23:42:14），把 33/34/44/84/85/87 的字段号
与语义复核了一遍，其中三处改写了原来的认知：

- **34 的 `operatorInfo` 全部是内嵌 protobuf** `{1: {1: operatorUid, 2: 6, 3: bytes, 4: 0}}`，
  包括 131 他人被踢；33 的 `operatorInfo` 是纯 uid 字符串；
- **群主自己解散群**时收到的是 34 / `decreaseType` **1**（成员与操作人都是本号）；参考实现里
  129 才是群解散，那应是非群主成员侧收到的值；
- **本号经管理员批准 / 接受邀请入群**时发的是 **85（`SelfJoinInGroup`）**，33 不发；不接 85 就
  会漏掉"本号进群"。

群主直接拉本号进群时，33 之后 **266 ms** 会跟一条同群的 87。那条不是待审批的邀请，本号入群后
30 秒内同群的 87 会被丢弃（`SELF_JOIN_INVITE_WINDOW`，33 与 85 共用这个状态）。

`decreaseType 2`（本号自己退群、operatorInfo 为空 `{2: {1: 0}}`）来自 2026-11-22 的上一轮抓包，
但那份文件已丢失、本轮 39 帧没有复现，映射保留、结论待复核。

| `contentHead.type` | subType | 事件 |
| ---: | ---: | --- |
| 33 | — | `group_increase`（130 批准 / 131 邀请） |
| 34 | — | `group_decrease`（2 本号退群（待复核）/ 3 本号被踢 / 130 他人退群 / 131 他人被踢）；1 群主解散、129 群解散 → `group_disband` |
| 44 | — | `group_admin`（`extraEnable` = 任命，`extraDisable` = 撤职）；body 的 3 号分支（群主转让）静默忽略 |
| 84 | — | `request.group`（入群申请） |
| 85 | — | `group_increase`（本号进群，`SelfJoinInGroup`，`sub_type=approve`） |
| 87 / 525 | 87 | `request.group`，`sub_type=invite` |
| 528 | 35 | `request.friend` |
| 732 | 20 + 内层 19 | `notify(poke)`，带文案与表情图 |
| 732 | 16 + 内层 35 | `group_msg_emoji_like` |

#### 实抓字段速查（2026-11-24，QQ 9.9.33-52230）

| 类型 | 结构 |
| ---: | --- |
| 33 | `GroupChange{1 群, 2 flag, 3 成员 uid, 4 increaseType（130 批准/131 邀请）, 5 操作人 uid（纯字符串）, 6 increaseType, 7 bytes, 8}` |
| 34 | `GroupChange{1 群, 2 flag, 3 成员 uid, 4 decreaseType（1/2/3/129/130/131）, 5 操作人 GroupChangeInfo（内嵌 `{1:{1:uid,2:6,3:bytes,4:0}}`）}` |
| 44 | `{1 群, 2 flag, 3 1/0（群主转让时为 255）, 4 body{1 extraDisable, 2 extraEnable}{1 adminUid, 2 1/0}, 3 群主转让{1 旧群主 uid, 2 新群主 uid}}` |
| 84 | `GroupJoin{1 群, 2=1, 3 申请人 uid, 4=30（参考实现未命名）, 5 附言, 6, 8}`；只用到 1/3/5 |
| 85 | `SelfJoinInGroup{1 群, 2=1, 3 审批人 uid, 4=0, 5 bytes, 6=0}` |
| 87 | 扁平 `GroupInvite{1 群, 2=1, 3=4, 4=0, 5 邀请人 uid, 6 hashes}`；84/87 另有包一层 `{1: body}` 的 Lagrange 形态，解码器两种都认 |

44 的 `body.3`（群主转让）在 OneBot v11 里没有对应事件，解码器不产出事件——这是有意忽略，
不是漏解。`ResponseHead` 的 5/6 号是本号 uin/uid（每帧都有），1/2 号是来源（消息里是发送者，
群事件里是群号）；`feed_packet` 先用 5/6 把 uid→uin 表补热，避免 fresh login 时本号事件被丢、
87 余波被误报成邀请。

实际 pipe-session 注入的是 `qsick_pipe.dll`。本次在这条链路加入只读的接收观察器，
不使用旧 `qsick-payload` 的通用 `trace.call` 探针，也不改群文件使用的 `inithook`。
原生源码与实现分析继续保持私有，不随仓库发布。

```text
QQ 接收回调 → 只选完整 MsgPush → 有界内存队列
  → Pipe/dll/<sessionid>/packet-events.jsonl
  → read_pipe_events → Runtime._feed_pipe_packet → feed_packet
  → 插件过滤 → 按连接协议分发
```

- 原生线程只复制，不调用 N-API、不等待 Python；文件写入交给独立工作线程。
- 包事件使用独立文件，避免与 JS 消息监听器的 `events.jsonl` 并发写入交错。
- 包携带 `uin`、`sessionid`、`cmd`、`seq`、`body_hex`、`byte_length`、`truncated:false`。
  Python 校验接收账号与帧头 `toUin`（若存在）一致，然后才允许身份学习和事件分发。
- 最大完整包为 1 MiB；超限、队列溢出或读取失败丢弃整包并记录诊断，绝不以截断数据冒充完整帧。
- 仅对核对过的 Windows x64 QQ 构建启用；文件哈希或函数签名不匹配则拒绝装钩。
  `QSICK_PACKET_HOOK=0` 可在启动 session 前关闭该观察器；已运行的 session 需重启才能换 DLL。
- 每次 QSICK 启动按既有队列清理规则清理包事件文件，不删除 session 配置。

`feed_packet(data, instance=, account=)` 后的插件过滤、协议分流、各连接编码均复用原有路径。
2026-11-25 已在新 DLL 的真实 session 捕获管理员任命/取消包（44，set/unset 各两条），
经实际 `_feed_pipe_packet` 离线回放得到正确 OneBot 事件，并核对 Milky 的 `is_set` 为
true/false/true/false。回放不发送网络消息，尚不能据此宣称外部 OneBot/Milky 客户端已收到。
其它群事件的 DLL 实抓动作验收仍需分别验证。

原生安装只在 session 启动期尝试；线程快照无法原子排除新线程创建，极端的线程恢复或页面保护
恢复失败可能终止 QQ，而不是保证无影响。建议先在测试账号使用。文件写入中途失败会关闭本次
包观察器，避免残缺 JSON 行吞掉下一帧；查看 session 目录的 `packet-hook.log`，重启后再验证。

## 明确不实现

| 能力 | 原因 |
| --- | --- |
| 会话置顶（`get_peer_pins` / `set_peer_pin` / `peer_pin_change`） | 不在本程序范围内。QSICK 侧直接回"没有相关 API"，而不是"不支持" |
| 转存永久群文件 | QQ 侧没有这个接口 |
| 重命名群文件夹 | 宿主只有 create / delete |

## 已知的限制（不是缺通道，是缺证据）

- **`group_member_increase` 的"别人入群"**：灰条不写邀请人，也不写是否申请通过。通道 C 接上后
  这个限制消失。
- **"某人加入群"（`busiId` 51）**：这条灰条不写是谁。要认出来只能对比前后成员列表，那是异步
  服务调用。发一个猜的账号比不发更糟。
- **`group_member_decrease` 的"被踢"**：灰条只说自愿退群。同上。
- **`friend_file_upload` 没有宿主通知**：QQ NT 不发"好友发来文件"的事件。Milky 把它定义成独立
  事件，所以由 Milky 适配器从**好友私聊里带文件的消息**派生（`milky/events.friend_file_upload`），
  在本层之上做，v11 流里不会因此多出一条非 v11 通知。约束：群文件不误报（那是
  `group_file_upload`）、自己发时 `is_self=true`、缺 `file_id`/`file_name` 不发、`file_hash`
  是 TriSHA1 拿不到就留空（不拿 MD5 冒充）。单测：`tests/test_milky_friend_file_upload.py`。
- **`group_card`（群名片变更）**：NapCat 靠"消息里的 `sendMemberName` 与缓存对比"来识别。缓存
  是异步的，第一次见到某人会误报，所以不做。

## 怎么验证

- `tests/test_headless_notices.py`：按上表构造报文，跑**真实提取器**（Node）。
- `tests/test_olpush.py`：合成帧 + `TestCapturedShapes`（按 2026-11-24 实抓帧形状写成，抓包文件
  本身不入库），跑**真实解码器**与事件映射；`TestFeedPacketIdentity` 覆盖"帧头先补热 uid→uin
  表"（否则 fresh login 时本号入群事件会被丢、87 余波会被误报成邀请）。
- `tests/test_protocol_milky.py`：验证到 Milky 事件的字段名与规范一致。
- `tests/test_milky_friend_file_upload.py`：好友文件事件的派生规则，含"群文件不误报""自己发
  标 is_self""缺 ID 不发""哈希未知不冒充"四类反例。

这些证明"字段这么放会得到这个事件"；实抓回归只覆盖抓到的那几种形状。没抓到的情况（例如
decreaseType 2、非群主视角的 129、真·邀请入群的 87）仍只有参考实现作依据，不能对外宣称
"一定能收到"。21 个 Milky 事件**尚未实机取证**：需要挂 SSE/WS 长连接再重放管理操作，
方法和进度见 `docs/milky-live-test.md`。
