# 载荷 ABI v1

Python core 与原生载荷是各自独立的构建产物，二者以本文档为共同约定。凡未在此写明的内容，都属于实现细节。

## 1. 传输

| 项目 | 值 |
| --- | --- |
| 控制管道 | `\\.\pipe\qsick.<pid>.ctl`（请求，client -> payload） |
| 响应管道 | `\\.\pipe\qsick.<pid>.rsp`（应答，payload -> client） |
| 事件管道 | `\\.\pipe\qsick.<pid>.evt`（事件，payload -> client） |
| 事件管道 | `\\.\pipe\qsick.<pid>.evt`（payload → core，单向） |
| 前缀覆盖 | `QSICK_PIPE_PREFIX`（载荷）/ `transport.pipe_prefix`（core） |
| 编码 | 小端序，正文为 JSON |
| 并发 | 每条管道同时只有一个在途请求；`request` id 用于关联应答 |

## 2. 帧

32 字节头部，随后是 `cmd | msg | body`。

| 偏移 | 字段 | 类型 | 说明 |
| --- | --- | --- | --- |
| 0 | magic | u32 | `0x314B5351`（`QSK1`） |
| 4 | version | u8 | `1`；不匹配即为硬错误 |
| 5 | op | u8 | 见下文 |
| 6 | flags | u16 | 保留 |
| 8 | request | u32 | 请求/应答关联 |
| 12 | status | i32 | 0 = 正常；负值 = 传输/载荷错误 |
| 16 | value | u64 | 与 op 相关的标量（uin、sequence） |
| 24 | cmd | u16 | utf-8 长度，≤ 4096 |
| 26 | msg | u16 | utf-8 长度，≤ 65535 |
| 28 | body | u32 | 字节长度，≤ 16 MiB |

Ops：`1 HELLO`、`2 SEND`、`3 ACK`、`4 REPLY`、`5 EVENT`、`6 LOGIN`、`7 ERROR`、
`8 PING`、`9 PONG`、`10 SHUTDOWN`。

握手过程：

1. core 连接两条管道；
2. core 发送 `HELLO`（请求 id 为 1）；
3. 载荷以状态 0 回复 `HELLO`；
4. 载荷至少推送一次 `LOGIN`（`{"logged_in":bool,"uin":"...","nickname":"..."}`）；
5. 只有在真实的 `uin` 到达之后，core 才把账号标记为就绪。

## 3. 操作（`SEND` + `cmd`）

`params` 是一个 JSON 对象；应答 body 同样是 JSON 对象。成功时 status 为 `0`，否则
core 会上报 `retcode = 1502`。

| op | params | 结果 |
| --- | --- | --- |
| `login.info` | — | `{uin, nickname}` |
| `status` | — | `{online, good, uin}` |
| `message.send` | `{chat:{type,id}, elements:[...]}` | `{message_id, seq, time}` |
| `message.get` | `{message_id}` | 消息信封 |
| `message.recall` | `{message_id}` | `{}` |
| `message.read` | `{message_id}` | `{}` |
| `message.emoji_like` | `{message_id, emoji_id, set}` | `{}` |
| `history.group` | `{group_id, message_seq, count}` | `{messages:[...]}` |
| `history.private` | `{user_id, message_seq, count}` | `{messages:[...]}` |
| `friend.list` | — | `[{uin, uid, nickname, remark}]` |
| `friend.info` | `{user_id}` | `{uin, nickname, sex, age}` |
| `friend.remark` | `{user_id, remark}` | `{}` |
| `friend.delete` | `{user_id}` | `{}` |
| `friend.like` | `{user_id, times}` | `{}` |
| `group.list` | — | `[{group_id, group_name, member_count, max_member_count}]` |
| `group.info` | `{group_id}` | `{group_id, group_name, member_count, max_member_count}` |
| `group.members` | `{group_id}` | `[{uin, nickname, card, role}]` |
| `group.member` | `{group_id, user_id}` | 单个成员 |
| `group.mute` | `{group_id, user_id, duration}` | `{}` |
| `group.kick` | `{group_id, user_id, reject_add_request}` | `{}` |
| `group.card` | `{group_id, user_id, card}` | `{}` |
| `group.name` | `{group_id, group_name}` | `{}` |
| `group.leave` | `{group_id, is_dismiss}` | `{}` |
| `group.whole_ban` | `{group_id, enable}` | `{}` |
| `group.admin` | `{group_id, user_id, enable}` | `{}` |
| `group.title` | `{group_id, user_id, special_title, duration}` | `{}` |
| `request.friend` | `{flag, approve, remark}` | `{}` |
| `request.group` | `{flag, sub_type, approve, reason}` | `{}` |
| `poke.send` | `{user_id, group_id}` | `{}` |
| `rkey` | — | `[{rkey, type, ttl, create_time}]` |
| `media.url` | `{kind, file_id, ...}` | `{url, expires_in}` |
| `forward.get` | `{res_id}` | `{messages:[...]}` |
| `raw.call` | `{cmd, body_hex}` | `{status, body_hex}` |
| `oidb.call` | `{command, sub_command, body_hex}` | `{status, body_hex}` |

未知的 op 必须回复 `status != 0` 并附带一条消息；core 会把它映射为 `retcode 1502`
并继续运行。

适配器动作以**动作名作为 op 名**，并将校验后的参数对象原样转发。
`docs/payload-ops.md` 是一份生成的清单，列出仍需线上实现的 168 个 op；由 core
直接支撑的 74 个动作永远不会到达载荷。

## 4. 事件（`EVENT` + `cmd`）

| cmd | body |
| --- | --- |
| `message.group` / `message.private` | 消息信封（见下文） |
| `message.sent` | 账号自身发出的消息所对应的信封 |
| `notice.group_recall` / `notice.friend_recall` | `{message_id, chat}` |
| `notice.group_increase` / `notice.group_decrease` | `{group_id, user_id, operator_id}` |
| `notice.group_admin` / `notice.group_ban` | `{group_id, user_id, ...}` |
| `request.friend` / `request.group` | `{flag, user_id/group_id, comment}` |
| `noise.typing` | `{chat, user_id}`（core 只记 TRACE 日志，从不转发） |
| `heartbeat` | `{}` |

消息信封：

```json
{
  "message_id": 1234,
  "seq": 42,
  "time": 1760000000,
  "chat": {"type": "group", "id": 30001},
  "sender": {"uin": 20001, "uid": "u_x", "nickname": "Alice", "card": "", "role": "member"},
  "elements": [
    {"type": "text", "data": {"text": "hi"}},
    {"type": "image", "data": {"file_id": "30abc", "appid": "1407"}},
    {"type": "flash_file", "data": {"file_set_id": "FS1", "title": "打包"}}
  ]
}
```

core 目前能够理解的元素类型：`text, at, face, reply, image, record,
video, file, json, xml, forward, mface, poke, markdown, flash_file, onlinefile,
inline_keyboard`。其他任何类型都按 `unknown` 保留 —— 载荷随时可能发送新的类型。

## 5. 钩子点

载荷必须暴露两个内部回调，并把它们桥接到事件：

- **send**：在 QQ 发送 SSO 包之前以 `(cmd, body)` 调用；仅当有订阅方关心时才作为
  `EVENT` 发布（core 目前并不订阅原始发送）。
- **recv**：以 `(cmd, body, uin, seq)` 调用；解码为事件并发布。

偏移属于配置，而不是代码：

```
QSICK_HOOK_MODULE=QQNT.dll
QSICK_SEND_RVA=<hex>
QSICK_RECV_RVA=<hex>
```

当偏移已设置但蹦床缺失时，脚手架会**拒绝打补丁**。这是有意为之：未挂钩的跳转会在
无声无息中破坏客户端，而拒绝只是一行日志。

## 6. 注入

1. `find_qq_pids()`（Toolhelp32）—— 最新的进程胜出；
2. 如果 `\\.\pipe\qsick.<pid>.ctl` 已存在：直接附着；
3. 否则 `inject_dll(pid, payload)` —— `OpenProcess` → `VirtualAllocEx` →
   `WriteProcessMemory` → `CreateRemoteThread(LoadLibraryW)` → 等待；
4. 轮询控制管道（8 秒），然后执行握手。

约束：64 位 Python、位数与 QQ 一致，以及当 QQ 以提权方式运行时需要提权。手动映射
（manual mapping）有意未实现（见 `src/qsick/transport/injector.py` 中的 docstring）。

## 7. 钩子偏移表

`offsets.json` 会被嵌入到载荷中：

```json
{
  "entries": [
    {"version": "9.9.22-40990", "arch": "x64", "send": "37a9004", "recv": "37a4bd0", "verified": true, "note": "..."}
  ]
}
```

- `version` 必须与客户端构建匹配（`versions/<ver>/config.json` 中的 `curVersion`）。
- `send` / `recv` 是相对于 `QSICK_HOOK_MODULE` 所指模块（默认 `QQNT.dll`）的 RVA。
- **只有 `verified: true` 的条目才会被使用。** 未验证的条目会被拒绝，除非设置
  `QSICK_ALLOW_UNVERIFIED=1`，因为错误的 RVA 会破坏客户端，而不是让操作失败。
- 环境变量中的 RVA（`QSICK_SEND_RVA` / `QSICK_RECV_RVA`）会针对单个构建覆盖该表。

随附的偏移表是空的：以上内容都尚未在真实客户端上验证过。

## 8. 蹦床

`trampoline.cpp` 会整条复制指令，直到腾出足以容纳 12 字节绝对跳转的空间
（`mov rax, imm64; jmp rax`），然后：

- 按源/目标差值修正 RIP 相对的 `disp32` 操作数；
- 跳回 `target + copied_len`；
- **拒绝**相对的 `jmp`/`call`/短分支以及未知操作码。

thunk 通过以同样的四个寄存器参数（Win64 整数 ABI）调用蹦床并返回 `rax`，来保留
原始调用。第四个之后的参数位于栈上，不会被转发 —— 对 QSICK 所针对的包入口点而言
这没有问题，在其他情况下则作为已知限制记录在案。

## 9. 捕获

`QSICK_CAPTURE_ARG`（0-3）和 `QSICK_CAPTURE_LEN_ARG`（0-3）用于指定持有
`(pointer, length)` 的寄存器参数。设置之后，每次调用都会输出：

```json
{"kind": "send", "args": [...], "body_hex": "...", "thread": 1234}
```

正文会被截断到 4096 字节，并作为 `trace.call` 事件上报，core 仅以 TRACE 级别记录。

## 10. 管道认证

三种模式（`transport.pipe_auth`，也可在 WebUI 设置页中编辑）：

| 模式 | 行为 |
| --- | --- |
| `random`（默认） | 注入器为每个会话生成一个 128 位令牌，写入 `<session_dir>/<pid>.token`，载荷在引导阶段读取它。管道名变为 `qsick.<pid>.<token>.ctl/.evt`；`HELLO.msg` 必须等于该令牌。 |
| `fixed` | 文件与管道名机制相同，但令牌来自 `transport.pipe_token` 或 `QSICK_PIPE_TOKEN`。 |
| `none` | 使用普通管道名，没有令牌，也没有校验 —— 仅用于本机单用户调试；core 会记录一条警告。 |

`session_dir` 默认为 `%TEMP%\qsick\sessions`，必须对 QSICK 和 QQ 进程都可写
（它们本来就属于同一用户，否则注入就需要提权）。令牌不匹配时表现为
`status = -2`、`msg = "nonce mismatch"`；core 会把它作为传输错误上报，而不是无限
重试。

## 11. 安全说明

- 管道按 PID 划分且仅限本地；它们**并未**经过认证。要保护的是机器，而不是管道。
  未来的修订版会在 `HELLO` 中加入每会话 nonce。
- 绝不要在 INFO 级别记录 `rkey` 值或载荷正文；core 在 WebUI 配置视图中会对令牌做
  脱敏，但只有在 TRACE 级别才会记录原始载荷正文。
- 载荷以 QQ 的权限运行；请让它暴露的表面尽可能小（即本文档）。

## 12. 分发、构建与校验

`native/qsick-payload` **只随包提供编译好的 DLL** 及其声明。C++ 源码、构建脚本与测试宿主
刻意不入库（见 `.gitignore`），这与其他 QQ 互操作项目的做法一致。条款见
[NATIVE-NOTICE.md](../native/qsick-payload/NATIVE-NOTICE.md)，本次构建的版本与 SHA-256
见 [`BUILD.txt`](../native/qsick-payload/BUILD.txt)。

DLL 是静态链接（`/MT`）：只导入 `KERNEL32.dll` 与 `ADVAPI32.dll`，因此**不需要安装
VC++ 运行库，也不需要 Rust 工具链**。

源码布局（给持有源码的维护者）：

- `src/protocol.h` —— `QSK1` 帧编解码；
- `src/json.h` —— 面向 op 契约的小型 JSON 读写器；
- `src/pipe.cpp`（在 `payload.cpp` 内）—— `.ctl`（请求）、`.rsp`（应答）、`.evt`（事件）
  的 `CreateNamedPipeW` 服务端，含 nonce 校验；
- `src/offsets.cpp` —— `offsets.json` 偏移表；**未验证的条目需要
  `QSICK_ALLOW_UNVERIFIED=1`**；
- `src/trampoline.cpp` —— x64 指令长度解码、RIP 相对寻址修正、绝对跳转写入，
  遇到相对分支即拒绝；
- `src/payload.cpp` —— 建立跳板、打 12 字节跳转、直通 thunk 保留原始调用并（可选地）
  把某个缓冲区参数按十六进制捕获下来。

维护者构建（装 C++ 工作负载的 Visual Studio Build Tools，不需要 CMake）：

```bat
native\qsick-payload\build.bat
uv run python tools\build_payload.py --skip-build   # 把 SHA-256 盖进 BUILD.txt
```

注入前校验分发来的二进制：

```powershell
Get-FileHash native\qsick-payload\qsick_payload.dll -Algorithm SHA256
```

## 13. 锁定的 QQNT 客户端

QSICK 瞄准**一个官方 QQNT 版本：9.9.33-52230（x64）**。它不修改、也不重新分发 QQ；
启动器会检查已安装的客户端，必要时下载那个官方安装包（URL + SHA-256）。

```bash
uv run qsick-launcher --check     # ready / version_mismatch / not_installed
uv run qsick-launcher --download <下载目录>
```

payload 钩的是 `wrapper.node`（不是 `QQNT.dll`），偏移来自该版本。`offsets.json`
把条目标为 **unverified**，因为它是对照 NapCat 的表交叉取得的、还没在真实客户端上验证：
只在一次性账号上设 `QSICK_ALLOW_UNVERIFIED=1`，抓包测试成功后再把 `verified` 翻成真。
