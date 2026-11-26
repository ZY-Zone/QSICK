# Milky 实机测试

`protocols/milky/` 的能力判断分两层：**离线回归**（`tests/` 里的用例）和**实机测量**（对真实 QQ
调真实接口）。两者结论不一致时以实机为准。这份文档记录实机怎么做、测到了什么、哪些还不可信。

## 为什么需要这一层

离线用例只能证明"字段这么放会得到这个响应形状"。它测不出宿主在真实网络下**拒绝**或**回执缺失**
的情形，而本轮实机一口气暴露了四个，其中一个是致命的：

> 按 Milky 规范形状发送的消息，**一条都发不出去**（`-400 unsupported message segment type None`）。

原因是 Milky 段被转成"内部部件"交给动作层，而动作层按 v11 段的 `segment["type"]` 解析。
离线用例当时用的是 v11 形状，所以全绿。这类缺陷只有真机调用才会出现。

## 怎么做

| 步骤 | 做法 |
| --- | --- |
| 起监听 | 在 WebUI 的网络配置里建一条 `kind=http`、`protocol=milky` 的记录（绑 `127.0.0.1`，无 token 时**必须**绑本机），保存即热生效 |
| 打接口 | `POST {path}/api/{api}`，body 里带 `account` 选定账号。`{path}` 是该网络配置的「路径」，填 `/` 就是规范原路径 `/api/{api}` |
| 验证 | 不只看 `retcode == 0`，还要**读回**：改名后读群信息、禁言后读成员、建文件夹后读目录 |
| 落证据 | 一次调用一个 JSON（完整 request / response / retcode / 耗时），按时间戳分目录 |

判定一条 API **只有同时满足**才算通过：HTTP 200；`retcode == 0`；关键字段存在且类型符合
`04-数据结构.md`；读回校验通过。只看 `retcode` 会把下面两个"返回失败但其实成功"的反例当成失败。

## 工具

| 脚本 | 用途 |
| --- | --- |
| `tools/milky_live_probe.py` | 只读层：跑 `get_*` 与"否"的确认失败 |
| `tools/milky_live_write_probe.py` | 写入层：`--stage l2/l3/l4`，每步读回验证，能还原的还原 |
| `tools/milky_recall_paths.py` | 单独测撤回：自己的消息 vs 别人的消息 |
| `tools/milky_verify_defects.py` | 缺陷回归，建的东西全部自清理 |

写入层的分层是**按风险**分的，不是按 API 字母序：L1 只读、L2 只在测试群/私聊发消息、L3 改群设置
（可逆，逐条还原）、L4 踢人与退群（不可逆，需显式指定目标）。`--segments v11` 只用于修复前的
服务端，正常不要用。

## 结论（2026-11-25，账号 3905548034 / 2208948865）

只读 24 条：**15 ok / 9 明确非零**，9 条全部给出可读原因，**没有一条超时或崩溃**。
写入 17 条：发送、撤回、改群名、改名片、禁言、全员禁言、设管理员、退群、踢人、上传、
文件改名/删除均已实机通过并读回校验。

完整逐条结论见仓库外的 `milky实现/动作类.csv`（`实测` / `实测证据` 两列），证据在
`milky实现/实测/<时间戳>/`。

### 已修复并复测通过

| 缺陷 | 修复要点 |
| --- | --- |
| 规范段形状发送全部 -400 | 发送方向也交 v11 段；"已经是 v11 段"按 `data` 为对象判断，不是按键存在（`light_app`/`xml` 的 `data` 是字符串） |
| 撤回/读取自己刚发的消息 -404 | 自发消息在 `report_self_message=false` 时不落库，历史回查又不返回锚点；发送回执里的 seq + message_id 记进进程级有界表，`_resolve` 先查它 |
| `kick_group_member` 恒失败 | ack 没有整数 `result`；改为以成员列表为准，并按实际字段名（`errCode`/`code`）校验状态 |
| 多账号下 `get_login_info` 回错号 | `self_id` 优先用请求的 `account`；`login_state_for()` 按 uin 匹配（headless 按实例名做键、pipe 按 uin 做键） |

### 未修好

**`create_group_folder` / `upload_group_file` 的"副作用成功、回执缺失"。**

- `createGroupFolder` 创建成功但响应不带 folderId（三个候选字段全空）
- `group.file.upload` 可能在文件已上传时超时
- 两者都改成事后回查文件列表，但**列表滞后不可预测**：三次实测 **6.1 s / 16.8 s / > 30 s**，
  30 秒窗口跑满仍未命中

也就是说回查不是可靠的取 ID 手段。当前实现用 60 s 窗口，查不到就明确失败（绝不编 ID），
创建前的目录快照用来区分"同名文件夹原本就存在"。**下一步应查清创建响应里 ID 的真实字段名**
——`kickMemberV2` 就是这么查出来的，payload 已经把 `result` 原样带回，打一份字段名即可。

### 未取证

21 个 Milky 事件**一个都没有实机取证**。`group_admin_change`/`group_mute`/`group_whole_mute`/
`group_name_change` 等操作在写入实测中真实发生过，但没有客户端长连 SSE 抓帧，所以不能声称
"事件发出去了"。下一步：挂 SSE/WS 长连接，重放这些操作，逐条取证。

## 数据质量（"部分"的实质）

| API | 缺口 |
| --- | --- |
| `get_group_info` | Group 缺 `remark`/`created_time`/`description`/`question`/`announcement` |
| `get_friend_list` / `get_friend_info` | Friend 缺 `qid`、`category` |
| `get_group_member_info` | **11 个规范字段齐全** |
| `get_group_files` | **9 个字段齐全**，`file_id` 真实 |
| `get_history_messages` | 以尾部 seq 为游标返回空列表且 `next_message_seq=null` |
| `get_group_file_download_url` | 真实 URL，但 `?fname=` 为空 |
| `get_resource_temp_url` | 仅传 `resource_id` 时被要求补 `user_id`（规范不提供） |
| `get_impl_info` | `qq_protocol_version` 为空（未取到运行中 QQ 版本，不猜） |

## 复现

```powershell
# 只读层（安全，随时可跑）
.\.venv\Scripts\python.exe tools\milky_live_probe.py --port 5755 --account <QQ号>

# 缺陷回归（自清理；--kick 需显式给出目标，且需要该账号在群里能踢人）
.\.venv\Scripts\python.exe tools\milky_verify_defects.py --port 5755 --group <群号>

# 写入分层（L3 会改真实群设置，但逐条还原）
.\.venv\Scripts\python.exe tools\milky_live_write_probe.py --stage l3 --group <群号> --peer <QQ号>
```

改动的 Python 需要重启 QSICK 生效；改到 `headless_actions.cjs` 这类 payload JS 时，
**必须重建 session**——它在会话启动时复制进客户端副本，重启进程不够。核对方式：

```powershell
(Get-FileHash headlessClient\Pipe\versions\<版本>\resources\app\headless_actions.cjs).Hash
(Get-FileHash src\qsick\headless_actions.cjs).Hash    # 两个必须一致
```
