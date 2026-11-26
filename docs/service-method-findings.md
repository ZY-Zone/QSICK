# 四项能力的实测记录

四项都**不是"客户端没有能力"**——方法都在，参数形状一部分已确定。本文只记录实测到的
东西，没验证过的不写成结论。

探针：`tools/probe_service_shapes.py`、`tools/probe_arg_types.py`、`tools/probe_nudge.py`、
`tools/strings_around.py`。调用通道是 `service_call`（`{service, method, args}`）。

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

## 二、设置群成员专属头衔 —— 形状接近，卡在群功能开关

```
Group.setIdentityTitleInfo(obj)
```

单个对象参数。**字段二分结论**：`groupId` 是唯一承重的字段——去掉它错误从
`10006 other error(0x899_0)` 退回 `10005 not group member(0x899_0)`；其余十几个候选
（`uid` / `memberUid` / `uids` / `uin` / `memberUin` / `title` / `specialTitle` / `duration` …）
全部无效，加不加都一样。

关键一步：`groupId` 传**字符串**而不是数字时，错误变成

```
20000 medal not available
```

这不再是参数问题，是**群没有开通头衔（勋章）功能**。测试群 2169010946 只有 3 个成员，大概率
没有这个功能。

所以下一步不是继续猜字段，而是换一个有头衔功能的群再试。成员标识的字段名仍未确定。

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

## 四、入群 / 邀请请求的同意与拒绝 —— 需要真实待处理请求

```
Group.operateSysNotify(a, b)
```

2 个参数（原生断言）。任何填充都报客户端侧的

```
Cannot convert undefined or null to object
```

说明它要展开某个属性，而那个属性必须由真实的通知对象提供。**没有待处理请求就无法测**。

配套方法存在：`Group.fetchGroupNotify`、`Group.getGroupNotifiesUnreadCount`、
`Group.setRcvJoinVerifyMsg`。合理的顺序是先取真实通知，再从它的字段构造操作参数。

## 附：几条通用经验

- 原生绑定会断言参数个数（`assertion (argc == N) failed`），空参调用一次就能拿到 N，
  比猜快得多。
- `service.methods` 能一次问出所有服务的方法名，但**看不到参数**。
- `service.call` 的报错会原样带回方法抛出的那句话，那是签名线索的主要来源
  （`invalid ReqBody.MessageSeqno` 就是这么定位到 `msgSeq` 的）。
- 客户端 JS 里搜不到这些方法名（只在 `wrapper.node` 里），二进制里也只有 arity 断言表，
  没有字段名。
