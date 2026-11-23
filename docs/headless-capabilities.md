# 无头桥接能力与验证边界

本页描述新增的 QQNT 桥接实现，不代表所有注册的 OneBot 动作都已实现。
启动日志中的 adapter_modules 仅表示 Python 适配模块存在。

## 客户端副本（`runtime/`）

无头实例需要 QQ 的 `wrapper.node` 才能加载，因此项目需要一份完整的 QQNT 客户端
副本。它放在**项目根**的 `runtime/`，所有实例共用同一份，且**不入库**（约 1.1 GB）。

首次运行时按下列顺序获取（`src/qsick/runtime_fetch.py`）：

1. **`runtime/` 已存在** → 直接使用，不下载、不覆盖用户手工放入的版本。
2. **自动下载锁定版本** → `http://49.235.134.245/qq_got/QQNT-9.9.33-52230`。
   下载到 `runtime.download.tar.gz`，用 HTTP Range 支持断点续传，校验 sha256
   后解包到 `runtime.staging/`，确认 `wrapper.node`、`package.json`、`major.node`
   齐全才整体换成 `runtime/`——半个包不会被当成可用客户端。
3. **回退到本机 QQ** → 从 `%QSICK_QQ_DIR%` 或已安装的 QQNT 复制一份。

相关开关：

| 环境变量 | 作用 |
| --- | --- |
| `QSICK_RUNTIME_DIR` | 指定 runtime 目录（默认项目根 `runtime/`） |
| `QSICK_RUNTIME_URL` | 指向镜像，默认上表那个地址 |
| `QSICK_SKIP_RUNTIME_DOWNLOAD=1` | 关闭自动下载，自己准备目录 |
| `QSICK_QQ_DIR` | 指定本机 QQNT 安装位置（下载失败时的回退来源） |

为什么锁定 9.9.33-52230：`wrapper.node` 的偏移与版本强相关，混用版本会出现
"能加载但登录失败"。锁一份完整客户端，是为了让 `clientVer`/`qua` 与磁盘上的
二进制始终一致。

### 布局：完整客户端，不是只有应用层

`runtime/` 保留客户端原始目录结构：

```
runtime/
  QQ.exe                          ← 启动器（无头模式不用，但保留）
  extension/
  versions/9.9.33-52230/
    QQNT.dll                      ← 客户端原生库原件（204 MB，保留不动）
    ffmpeg.dll                    ← Electron 组件（flue.dll 依赖它）
    icudtl.dat, libGLESv2.dll ... ← Chromium 运行时
    resources/app/                ← 真正加载的那一层
      wrapper.node  (115 MB)
      package.json  major.node
```

**`runtime/` 是纯客户端副本**，不往里写任何东西——它可能被当作模板硬链接给
`pipe` 实例，混进别的东西会污染那些实例。

**为什么不只抽 `resources/app`**：不丢原件（将来需要真客户端时直接可用）；
而且 app 层里的库可能依赖上层组件——`wmpfsdk/runtime/flue.dll` 导入
`ffmpeg.dll`，后者只在 `versions/<ver>/` 下，抽掉应用层就会缺。

### 垫片（`QQNT.dll`）放哪

垫片本身存在 `build/QQNT.dll`（随仓库分发，15 KB）。**每个实例启动时复制一份到
自己的目录**，并把实例目录加进 `PATH`：

```
headlessClient/C0/QQNT.dll    ← 启动时从 build/ 复制
headlessClient/C0/host.js  config.json  ...
```

为什么靠 `PATH` 而不是放在 `wrapper.node` 旁边：实测结论有点反直觉——

| 条件 | 结果 |
| --- | --- |
| 垫片在 `resources/app/` | 生效 |
| 垫片在实例目录，`PATH` 含实例目录 | **生效**（加载的就是垫片） |
| 垫片在实例目录，`PATH` 不含它 | 失败：`The specified module could not be found` |
| 哪里都没有垫片 | 失败，同上 |

也就是说 `.node` 按绝对路径加载时**不会**优先搜自己所在目录；真正决定的是
**当前工作目录与 `PATH`**。宿主会 `chdir` 到 app 层（那里没有垫片），所以必须把
实例目录塞进 `PATH` 才能让垫片盖过 `versions/<ver>/` 那个 204 MB 原库。

这个替换是安全的：实测 `wrapper.node` 从 `QQNT.dll` 只导入 **99 个符号**，垫片导出
102 个，**缺口为 0**；原库其余 3283 个导出是 QQ 主程序用的，无头模式不碰。

宿主通过 `config.json` 的 `appDir` 拿到 app 层路径，`runtime` 字段仍指向完整客户端根。
旧的"应用层平铺"布局（`runtime/wrapper.node`）同样支持——`app_dir()` 会先看根目录。

为什么用 IP 而不是域名下载：`sickcc.online` 前面挂了 Tencent EdgeOne，它会把
**每一个路径**（连 `/healthz` 和站点根都一样）替换成 ICP 备案提示页，所以那个
域名下取不到任何真实内容。改用源站 IP 直连，并在 nps 上按 `Host: 49.235.134.245`
+ `Location: /qq_got` 加了一条转发规则——只放行这个前缀，该 IP 上的其它路径仍然
匹配不到规则、照旧被丢弃。若将来域名备案完成，把 `DEFAULT_BASE_URL` 换回域名即可。

## 查看运行时能力

WebUI 鉴权保护的 `GET /api/headless/capabilities` 查询当前 C0 宿主的
`runtime.capabilities`，列出方法是否存在、附加条件及明确不支持原因。
未登录、缓存未建立、权限不足、版本变化都可能使某项不能执行。

宿主启动时会复制 `headless_codec.cjs`、`headless_actions.cjs`、
`headless_sender.cjs`、`headless_login.cjs` 到实例目录，并生成仅供本机桥接使用的随机 token。
升级后需要重启 QSICK/实例才能加载这些文件；不会自动修改已有 QQ 登录目录。

## 登录状态持久化

QQ 能否免扫码重启，取决于三件事，缺一不可（对照 NapCat `napcat-shell/data-path.ts`）：

1. **票据目录必须是 global 路径**。Windows 下是 `<QQ 数据根>/nt_qq/global`，
   不是数据根本身。QSICK 以前把 `commonPath` 和 `desktopGlobalPath` 都指向数据根，
   票据无处落地，于是每次重启都报「登录态已失效」。
2. **等网络层就绪再尝试**。`getMsfStatus()` 为 3 时登录服务尚未连上，
   此时调用快捷登录会得到误导性的失败。现在最多等 15 秒。
3. **先用 `getLoginList()` 确认票据存在**。本地登录列表里没有该账号时直接返回
   `NO_TICKET` 并提示只能扫码，不会拿一个注定失败的请求去打 QQ。

**二维码不会被快捷登录挡住**。以前 `onQRCodeGetPicture` 在快捷登录进行中直接丢弃 QQ 返回的
二维码，结果票据失效时面板永远是空的。现在只要会话尚未就绪就发布二维码；
`onLoginConnected` 未触发时启动 2.5 秒后兜底取码；二维码过期自动重新获取。

另外成功判定与 NapCat 一致：`result === "0"` **且** `loginErrorInfo.errMsg` 为空，
两者任一不满足都算失败。旧实例升级后首次启动会创建 global 目录，
但**必须重新扫码一次**才能把票据写进去，此后重启才可能免扫码。

## 手动登录上次账号

账户管理每个实例提供「登录上次账号」。没有历史记录时禁用；已就绪/正在初始化时不重复登录。
停止的实例会以禁止自动登录模式启动，再由鉴权的 `POST /api/accounts/{id}/quick-login`
调用相同实例的 `/quick-login`，不会调用 C0 代替其他实例。
手动尝试不要求上次 `ready` 标记仍为真，QQ 返回失效票据时展示原始原因，不自动弹出二维码。
并发尝试互斥；超时后的 Promise 返回不会再启动旧会话；扫码流程可使旧快捷登录失效。
只在消息服务和监听器就绪后标记成功并保存账号信息，身份缓存不是登录票据。

## 收消息：不再只保留文本

已按本地 QQNT 类型定义实现：

- 文本、@全体、@用户（有可验证的 UIN/UID 映射时）。
- 普通表情、骰子/猜拳实际结果、戳一戳、商城表情。
- 图片、语音、视频、文件的名称、大小、文件标识、原始字段与可用 URL。
- 回复及 native 消息引用信息。
- Ark JSON/XML、长消息/合并转发的资源引用。
- Markdown 和内联键盘数据。

缺失/未知元素保留为 `unknown`，不会把整条非文本消息丢弃。
不从消息内容自动读取本地文件或下载 URL；“解析媒体元数据”不等于已实现媒体上传、
下载凭证或消息面板内播放。位置/联系人结构无可靠字段时保留原始 unknown/JSON。
临时会话、频道等未确认的 peer 类型不伪装成普通私聊。

消息 ID 在 Node 与桥接 JSON 中使用十进制字符串，不再 `Number(msgId)`；
Python 保持大整数，SQLite 使用 uint64 到有符号主键的可逆映射，并在 JSON 中保留原 ID。
先前已经因浮点数而损坏的历史 ID 无法自动恢复。

## 动作分发

独立 native 适配操作（含对应 Python / OneBot 别名）：

| 操作 | 条件/确认方式 |
| --- | --- |
| login.info | 真实已就绪的登录身份 |
| group.list / group.info | 请求 ACK 成功且收到匹配的完整列表/群详情回调 |
| group.members / group.member | getAllMemberList 完整结果与 errCode 校验 |
| group.admins / group.member_count | 仅从完整且 UID/UIN 唯一的成员集合查询管理员/人数 |
| group.share_link / group.share_ark | QQ 返回有效链接/卡片且 ACK 成功；只查询，不发送 |
| friend.statuses / friend.relations | 完整好友缓存的原生状态码/关系标志；缺字段不猜测 |
| friend.list / friend.categories | 已初始化的好友分类缓存 + 完整资料映射 |
| friend.info / user.status | 查询返回身份匹配；状态必须存在，不编造“离线” |
| group.at_all_remain | QQ 的 @全体剩余次数字段 |
| group.shut_list | 请求 ACK + 对应群的禁言列表回调 |
| group.mute / group.whole_ban | 已验证 UID 映射；原生结果码必须为 0 |
| group.name / group.remark | 原生结果码必须为 0 |
| group.card / group.admin | 完整成员列表预读；只写一次；强制回读确认同一 UID 的名片/角色已变更 |
| group.kick | kickMemberV2 结果码为 0，且目标已从完整成员列表消失；不在列表里就不发踢人 |
| friend.like | 结果码为 0 且 succCounts 大于 0；为 0 时不谎报成功 |
| cache.clean | 必须显式给出缓存键；clearCacheDataByKeys 返回结果码为 0 |
| forward.get | getMultiMsg 结果码为 0 且返回非空消息列表；空转发报 NOT_FOUND |
| media.download | 只有 msgId + elementId 都匹配的完成事件、错误码为 0 且有本地路径才算成功 |
| recent.list | 最近会话快照结果码与 info.errCode 均为 0，按 QQ 原生排序返回 |
| message.reactions | 先用宿主缓存的原生消息引用定位，errCode 为 0 才返回列表与分页 |
| system.ocr | Windows OCR 返回 GeneralCallResult，结果码为 0 才返回识别文本 |
| group.leave | 只退群，不解散、不删除本地消息 |
| message.recall | 宿主本次会话缓存中存在该消息的精确 native peer/id |
| message.send | 完整转换后发送，ACK 和对应发送记录都确认才返回成功 |

## 发消息

可转换：纯文本（保留空格/换行）、@用户/@全体、旧式表情 0..221、JSON、带可验证
native 引用的回复、**图片**和**文件**。私聊必须解析出真实 `u_...` UID；无法解析就明确失败。

### 图片与文件上传

聊天输入框支持直接粘贴或拖入图片和文件，也支持发送本地路径。实现要点：

- QQ 没有单独的聊天媒体上传接口，图片/文件在 `sendMsg` 时由 QQ 自行上传；
  宿主只需构造正确的 `picElement` / `fileElement` 并把源文件放进 QQ 富媒体目录。
- 图片会解析真实宽高（PNG/JPEG/GIF/WebP 头），计算 md5，`picType` 对 GIF 用 2000、
  其余用 1000；文件元素带文件名、大小与 `folderId`。
- 也接受 `base64://` 载荷与 `file://` URI。
- 发送完成后清理暂存副本与临时上传文件。
- 媒体发送的超时比文本长 120 秒，因为上传发生在发送过程中。

发送使用 QQ 生成的 ID 作为 `peer.guildId` 关联标识，调用 `sendMsg('0', ...)`，
等待匹配的 `onMsgInfoListUpdate` 成功记录，返回记录中的真实消息 ID，而非关联 ID。
只有 ACK、超时、非零结果、未知字段都不是成功。超时表示送达未知，不自动重试。
混合消息含不支持元素时整条拒绝，绝不只发其中的文本。

聊天页对当前无头账号开放私聊文本发送；OneBot/聊天页均要求 UID 缓存可解析且收到 QQ 发送确认，否则明确失败。其他实例不会偷偷落到 C0 代发。

## 仍明确不支持

- 语音/视频发送（需要 SILK 转码与缩略图）、转发消息构造、XML 发送、较新的商城表情发送。
- 群头衔设置：原生服务没有该方法，仅存在不解析回复的 OIDB 包路径，无法确认成功。
- 解散群、好友删除与备注：native 返回 void/unknown，或完成语义不明确。
- 群签到：仅有不读回复的包路径，没有可确认的接口。
- 精华消息、忽略群通知、频道、空间等未核实完整 native/Web API 合约的动作。
- 匿名聊天：相关原生方法全部返回 unknown 且无调用先例。
- 模块未注册的其他动作：返回 `UNSUPPORTED`，不返回空对象/空列表冒充实现。
- 每个实例的事件进入它自己的消息处理器与数据库（`data/instances/<实例>/messages.db`），
  不再共用一条主通道；实例之间不会互相替换核心身份，`message_id` 相同也不会互相覆盖。

注意：`get_image`/`get_record` 只有拿到消息 ID 与元素 ID 才能下载到本地文件；
只给文件标识时返回远程 URL 并明确标注“未下载本地文件”。语音不提供转码，
请求转码会如实说明未实现。清理缓存与重启是两回事：缓存清理有结果码确认，
而 QQ 没有原生重启接口，`set_restart` 仍不可用。

## 安全与测试

`/action`、`/send`、`/refresh` 和状态/能力接口使用实例随机 bearer token。
核心事件入口要求 loopback + C0 标识 + 同一 token。管理操作不做破坏性自动重试。

当前适配层声明 47 个规范操作，其中 34 个有实现路径、13 个明确不支持，并提供 56 个别名。新增查询的 Python OneBot 路由也已注册，不仅是 Node 内部函数。是否可在当前 QQ 上执行仍以运行时能力与返回结果为准，不用注册数冒充可用数。

## OIDB 包通道（rkey）

之前判断「QSICK 没有包通道、拿不到 rkey」是**错的**。核实 NapCat 源码后确认：
发包并不需要 hook，QQ 的 `NodeIKernelMsgService` 上就有

```js
sendSsoCmdReqByContend(cmd, buffer) -> Promise<{ rspbuffer }>
```

它接受任意 SSO 命令名与二进制负载。QSICK 的宿主本来就能拿到 msg service，
所以只缺一个 protobuf 编解码器——现已实现 `headless_oidb.cjs`（自写编解码，
无第三方依赖）：

- `encodeOidb` / `decodeOidb`：OIDB 信封。字段映射按
  `OidbSvcTrpcTcpBase`：1=command、2=subCommand、**3=errorCode、4=body**、
  5=errorMsg、12=isReserved。（把 3 和 4 弄反会让每个响应体变成一个整数。）
- `encodeFetchRkey` / `decodeFetchRkeyResponse`：`0x9067_202` 取 rkey，
  返回 `{rkey, ttl, time, type}`，type 10=私聊、20=群。
- `sendOidb`：带超时、错误码校验。

对应动作 `system.rkey`，Python 侧 `rkey` 请求会映射到它，
媒体 URL 构建因此可以拿到真实 rkey。

## 被踢下线与自动重新登陆

QQ 通过 `onKickedOffLine(KickedOffLineInfo)` 明确告知会话为何终止，其中
`securityKickedType` 表示是否由腾讯安全策略强制下线。QSICK 现在区分三种情况：

| 状态 | 含义 |
|---|---|
| `kicked` | 账号被踢（可能来自其他设备登录，也可能安全策略） |
| `offline` | 登录连接断开 |
| `login_failed` | 登录流程本身失败 |

界面上会显示「被安全策略踢下线」或「已被踢下线」及 QQ 给出的原因，
而不是笼统的「登录异常」。手机 QQ 仍显示 Windows 在线、而 QSICK 显示未登录，
通常是会话已被服务端回收但本地尚未感知；此时点快捷登录会得到 `kicked` 或失败原因。

每个实例有「自动重新登陆」开关（默认关闭）。开启后，运行时会每 5 秒检查一次，
发现该实例处于 `kicked` 就结束它的子进程、重新启动并尝试登录上次账号。
关闭时只显示状态，不做任何自动操作。悬停开关旁的 `?` 可看到完整说明。

## 系统表情

表情图来自 QQ 客户端自己的资源目录，构建时复制到项目内的 `assets/qq-faces/`
（294 个 PNG，约 4.9 MB），随项目分发，不依赖运行机器的 QQ 安装。

- 找不到对应资源时，界面显示固定文案 `[QQ版本过低，无法显示对应表情]`，
  **不会用外观相似的 Unicode emoji 冒充**。
- QQ 的系统表情里混有 **APNG 动态表情**（`367`、`466`、`468`、`469`，
  69–72 帧、无限循环）。服务端检测 `acTL` 块并标记 `face_animated`，
  前端对这些图不做懒加载，避免停在第一帧。
- 商城表情（`mface`）的动态效果依赖网络图片地址，需要包通道与 rkey，
  无头桥接取不到，因此不伪造 URL；拿到真实地址时按 GIF/WebP 直接播放。

## 与 NapCat / SnowLuma 的对照

以两仓的动作注册表为准做名集对比（不是读文档）：

- **OneBot v11 标准动作 36/36 全部注册**，按本仓实际实现路径区分可用性。
- **NapCat 206 个动作里，QSICK 覆盖 202 个**。真正缺口只有 `.ocr_image`（本轮已补，Windows OCR）；其余差异是测试脚手架、已废弃别名，以及 NavCat 自己注释掉的两个匿名动作。
- **SnowLuma 219 个动作全部覆盖**（差集里的 2 项是源码示例字符串误匹配，1 项是测试脚手架）。
- 反向看，QSICK 有约 45 个扩展动作在两仓中都没有对应实现，例如 `get_recent_contact_list`、`get_group_admin_list`、`oidb_call`。

NapCat 中有一批**返回硬编码或空实现却报成功**的动作，QSICK 没有照抄：
`_get_model_show`、`_set_model_show`、`check_url_safely`、`can_send_record`、
`get_guild_list`、`get_guild_service_profile`、`nc_get_packet_status`。
QSICK 对同类接口返回 `UNSUPPORTED`，不返回假数据。

新增的 Node codec/actions/sender/host 测试全用合成数据和假 native 服务；
Python 用临时 SQLite/内存库及本机假 HTTP 宿主。本轮隔离回归结果：Python 276 项通过（主动跳过 1 个运行时启动测试），Node 132 项通过。
没有对真实 QQ 发消息、禁言、退群、
撤回或删除数据。离线测试通过证明了转换和协议处理，不等于每个 QQ 版本均已实测。
