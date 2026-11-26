# 四项能力的实测记录

四项都**不是"客户端没有能力"**——方法都在，参数形状一部分已确定。本文只记录实测到的
东西，没验证过的不写成结论。

探针在 `tools/` 下，按仓库惯例（`.gitignore` 忽略 `tools/probe_*.py`）只留在本地，不进版本库：

| 文件 | 作用 |
| --- | --- |
| `tools/probe_service_shapes.py` | 空参调用，拿原生断言的参数个数 |
| `tools/probe_arg_types.py` | 每个位置换成错误类型，看绑定期待什么 |
| `tools/probe_nudge.py` | 逐个形状试戳一戳，用事件流里的 poke 通知验证 |
| `tools/strings_around.py` | 从 `wrapper.node` 提取方法名附近的字符串 |

调用通道是 `service_call`（`{service, method, args}`），它是版本库里的东西。

## 一、发送群消息表情回应 —— 可用

```
Msg.setMsgEmojiLikes(peer, msgSeq, msgId, emojiId, setFlag)
```

- `peer` = `{chatType: 2, peerUid: "群号", guildId: ""}`
- `msgSeq` = 消息的 `message_seq`（数字，必须 > 0）
- `msgId` = 消息 id（字符串）
- `emojiId` = 表情 id 字符串，如 `"128077"`
- `setFlag` = `true` 设置；数字 `1` 是**取消**

实测：对测试群里一条真实消息调用返回 `{result: 0}`，**客户端界面上确实出现了表情回应**。
用 `"128077"` 时显示为空白占位——该表情 id 在当前客户端已不存在，不是调用失败。

读回用 `Msg.getMsgEmojiLikesList(peer, msgSeq, emojiId, emojiType, cookie, isFirst, count)`
（7 个参数，原生断言）。注意 `emojiType`：长度大于 3 的 id 是 `"2"`，否则 `"1"`。

## 二、设置群成员专属头衔 —— 两个候选方法都不是它

试过两个方法，都不是 OneBot 意义上的「专属头衔」：

**`Group.setIdentityTitleInfo(obj)`** —— 单对象参数。字段二分证明 `groupId` 是唯一承重字段
（去掉它错误从 `10006` 退回 `10005 not group member`）。`groupId` 传字符串时错误变成
`20000 medal not available`；即使该群已启用头衔，这个错误不变。名字里的 "Title" 指的应该是
**群勋章**。

**`Group.setGroupIdentitySpec(obj)`** —— 单对象参数，这条能一路跟着错误走：

```
{}                                        -> invalid SetSpecReq.GroupCode: value must be greater than 0
{groupCode}                               -> invalid SetSpecReq.Infos: value must contain at least 1 item(s)
{groupCode, infos: [{}]}                  -> invalid SetSpecReq.Infos[0]: ...
                                            caused by: invalid SpecInfo.SpecType: value must not be in list [UNKNOWN]
{groupCode, infos: [{specType: 1}]}       -> 10004 udc数据错误 ... [oidb] error login sig
{groupCode, infos: [{specType: 2}]}       -> 10001 错误互动标识ID
```

字段名是 `groupCode`（大写 C）、`infos`（数组）、内层 `specType`（枚举，1 和 2 都有不同反应）。
`10001 错误互动标识ID` 说明这条也是**互动标识**，不是头衔。

**`Group.getIdentityList(groupId, ?)`** —— 读侧，2 个参数。第二个参数给空串时报
`0x88d_131 ... rule type not match appid`，即它要一个 appid 类的值，不是随便的字符串。

结论：`headless_actions.cjs` 里那条原始判断（`No verified title-setting service signature`）
目前仍然站得住 —— 在这个客户端的 Group 服务上，找不到签名可验证的「设专属头衔」方法。

## 三、戳一戳 —— 形状未知

```
Msg.sendNudge(a, b, c)
```

3 个参数（原生断言）。试过的组合全部失败，且**没有一次在事件流里产生 poke 通知**：

| 参数 | 返回 |
| --- | --- |
| `[peer, {uin}, 1]` | -1 |
| `[peer, {uid}, 1]` | -1 |
| `[peer, "u_…", 1]` | -1 |
| `[peer, 1, {uin}]` | -2 |
| `[peer, [uin], 1]` | -200 |
| 全数字 `[1,1,1]` | -100 |
| 全字符串 / 全对象 | -1 |

群聊和私聊都试过。类型敏感性说明参数位置有意义，但错误码没有文字，无法判读含义。

验证方式：戳一戳成功会产生 `notice.notify`，`sub_type` 为 `poke`，带 `user_id`（发起者）、
`target_id`（被戳者）、`group_id`。事件流里已经有一条真实的 poke 通知可以对照。

## 四、入群 / 邀请请求的同意与拒绝 —— 卡在读取这一步

```
Group.operateSysNotify(a, b)
```

2 个参数（原生断言）。任何填充都报客户端侧的

```
Cannot convert undefined or null to object
```

说明它要展开某个属性，而那个属性必须来自真实的通知对象。

读侧的问题：`Group.fetchGroupNotify(x)` 是 **1 个参数的触发器**，返回只有 `{result: 0}`
—— 通知不会作为返回值给出，而是通过内核 Group **监听器**异步推送
（`addKernelGroupListener` 的 notify 回调）。QSICK 目前没有注册这类监听器，所以待处理请求
拿不到。

也就是说这一步缺的不是猜测，是一段接线：注册监听器 → 收到通知对象 → 按其字段构造操作。
事件流里也确认没有请求类事件（只有 poke 这类 `notice.notify`）。

配套方法都在：`fetchGroupNotify`、`getGroupNotifiesUnreadCount`、`clearGroupNotifies`、
`setRcvJoinVerifyMsg`、`operateSysNotifyV2`。

## 附：几条通用经验

- 原生绑定会断言参数个数（`assertion (argc == N) failed`），空参调用一次就能拿到 N，
  比猜快得多。
- `service.methods` 能一次问出所有服务的方法名，但**看不到参数**。
- `service.call` 的报错会原样带回方法抛出的那句话，那是签名线索的主要来源
  （`invalid ReqBody.MessageSeqno` 就是这么定位到 `msgSeq` 的）。
- 客户端 JS 里搜不到这些方法名（只在 `wrapper.node` 里），二进制里也只有 arity 断言表，
  没有字段名。
