# OneBot action 审计 — 2026-11-11

## 方法与边界

本次为**静态源码审计**，而非在真实 QQ 上验证。目录中的每个条目及其 Python 模块都以 Python AST 解析；辅助函数调用被追踪到传输层操作。审计检查了原生 headless 别名、服务依赖要求、结果校验、发送方/编解码器、mock 处理器、C++ 管道分发器以及运行时分发。审计者未执行项目导入、在线运行时/原生/API 测试、配置写入或源码修改。仅在 `docs` 下写入了新的审计文件。父代理的源码修正已体现在最终快照与 SHA-256 清单中。

权威的 [OneBot v11 公开 API 规范](https://11.onebot.dev/api/public.md) 已成功获取。GitHub/raw URL 无法访问。其 **38 个公开 API 名称全部存在于目录中**；注册覆盖率并不等于实现覆盖率或完整契约覆盖率。

每个条目的完整数据、操作证据、mock 行为、原生前置条件、注意事项以及源码哈希，由脚本生成到 `temp/onebot-action-audit.json`（产物不入库，可随时重新生成）；可复现的静态提取脚本本身见 [`tools/onebot_action_audit.py`](../tools/onebot_action_audit.py)。

## 总计

| 清点项 | 数量 |
|---|---:|
| 目录 action 条目 | 241 |
| 注册表别名 | 3 |
| 含别名在内的对外暴露名称 | 244 |
| 标准 v11 名称 | 38 |
| 扩展条目 | 203 |
| 通用 `PayloadAction` 透传模块 | 168 |
| 具有已实现静态契约的原生规范操作 | 26 |
| 到达这些原生契约的目录 API 名称 | 28 |

三个发送 API 共享 `message.send`：**26 个操作不应被当作 26 个 OneBot API 来调用**。共有 33 个可用的本地/原生/受限 API 名称（22 个标准、11 个扩展），它们并不是 33 个完全合规、无限制的 API。额外的 `get_rkey` 条件条目被有意排除在此可用列表之外，因为其空的成功响应可能掩盖不受支持的抓取行为。

| 实现类别 | 全部条目 | 标准条目 |
|---|---:|---:|
| `core_local` | 4 | 4 |
| `headless_verified_contract` | 23 | 12 |
| `conditional` | 7 | 6 |
| `adapter_only` | 195 | 14 |
| `unsupported` | 12 | 2 |

`headless_verified_contract` 表示源码层面已存在结果/事件/回读处理，**并不意味着当前 QQ 构建版本/账号已被实际演练过**。登录、服务签名、权限、已初始化的缓存、已验证的 UID 以及原生回执/引用数据仍是前置条件。

## 标准 API 可用性

### 本地：4

`can_send_image`、`can_send_record`、`get_status`、`get_version_info`。

两个能力查询在 headless 下会正确返回 false，但对于随附的不受支持的 pipe 脚手架则会错误地返回 true。`get_status.good` 反映的是登录状态，而非整个子系统的健康状况。

### 原生契约：12

`delete_msg`、`get_friend_list`、`get_group_info`、`get_group_list`、`get_group_member_info`、`get_group_member_list`、`get_stranger_info`、`set_group_admin`、`set_group_ban`、`set_group_card`、`set_group_name`、`set_group_whole_ban`。

撤回需要保留原生消息引用。禁言需要已验证的 UID 映射。名片/管理员设置器采用强制的完整回读，绝不从 void 返回值推断成功。成员查询在 Python 归一化过程中仍会丢失标准字段。缓存与刷新限制在每个条目中详述。

### 受限/条件性：6

`get_login_info`、`get_msg`、`send_group_msg`、`send_msg`、`send_private_msg`、`set_group_leave`。

- 登录要求存在有效的已建立身份，现在会故障关闭（fail closed）。
- 消息获取可基于本地已观察消息存储工作；原生缓存未命中查找不受支持。
- 发送支持文本、群提及、0–221 的传统表情、JSON，以及在具备充足原生引用时的回复。不支持媒体上传、XML/forward 发送、临时会话或频道（guild）。同时要求 ACK **和**相关联的已发送记录。超时意味着投递结果未知。
- 退群可用；`is_dismiss=true` 不可用。
- 原生十进制字符串形式的 uint64 消息 ID 是有意设计的无损扩展，并非严格的 v11 数值 int32 线上兼容。

### 在随附的 headless 中不可用：16

**仅适配器（14）：** `clean_cache`、`get_cookies`、`get_credentials`、`get_csrf_token`、`get_forward_msg`、`get_group_honor_info`、`get_image`、`get_record`、`send_like`、`set_friend_add_request`、`set_group_add_request`、`set_group_anonymous`、`set_group_anonymous_ban`、`set_restart`。

**明确不支持（2）：** `set_group_kick`、`set_group_special_title`。

注册中并未缺失任何标准名称，但这 16 个缺少可用实现。其余 22 个也存在已记录的局限；不得将 38/38 的注册情况呈现为完整的 OneBot v11 合规。

## 可用的扩展名称：11

`get_friends_with_category`、`get_group_at_all_remain`、`get_group_share_link`、`get_group_shut_list`、`nc_get_user_status`、`set_group_remark`、`get_group_admin_list`、`get_group_member_count`、`get_group_share_ark`、`get_friend_status_list`、`get_friend_relation_list`。

分享类查询只获取内容，不发送任何内容。管理员/数量类查询要求得到完整且去重后的成员结果。状态/关系类查询要求完整且已初始化的带类型缓存，原样保留原生代码/标志位，不推断在线布尔值，且无法强制刷新。全部 203 个扩展名称及其分类均在 JSON 中逐一列出。

## 伪成功与空操作发现

1. mock 的未知操作返回成功的 `{mock:true, op, params}`；许多显式 mock 处理器返回空集合或空操作成功。这无法确立原生能力。
2. 随附的 C++ 管道分发器会拒绝每一个 action 操作。它不是 headless action 的替代实现。
3. `get_rkey` 会压制原生抓取失败，并可能返回成功的 `[]` 或缓存令牌。
4. `clean_cache` 与 `set_restart` **不是本地变更型的空操作**：它们转发的是不受支持的 `cache.clean` / `runtime.restart`。mock 的通用成功使其看起来成功，而实际工作并未执行。运行时核心并未实现这些操作。
5. 在最终 action 快照中未发现无条件的本地变更型空操作。原生名片/管理员在已处于目标状态时返回成功，是在经过验证的状态读取之后作出的，属于合法的幂等行为，而非伪造的变更确认。
6. 共享的 `call` 会把假值负载默认为 `{}`，并把原生带类型错误坍缩为通用 UPSTREAM。成功的畸形 mock 数据可能变成空的/默认的 action 结果。

## 尚待修正项

- 在 Python 中保留原生带类型错误码，而不是把不受支持/未找到/参数非法一律展平为 UPSTREAM。
- 支持标签应依据传输层与操作层实现来推导，而不是依据 `status=implemented` 或 `wire=core`。
- 让未知 mock 操作默认失败，或将模拟能力与受支持能力明确隔离。
- 在不捏造事实的前提下补全成员响应字段；为 cookies/credentials 实现域内转发、音频 `out_format` 转换、媒体的已下载文件语义，以及请求的 `type` 别名。
- 实现真正的异步重启/延迟与缓存清理，或明确声明其为不受支持。
- 修正 pipe 模式下的能力查询。一致地转发 `no_cache`；`get_group_member_list_no_cache` 目前既不注入也不遵守强制刷新。
- 账号变更时限定范围/清理依赖身份的核心缓存查询结果。
- 替换生成的 op 文档假设：action 名称并不总是传输层 op 名称，且 `wire=core` 并不意味着无需原生实现工作。

## 已反映的父级修复

- `get_version_info` 现已正确导入根版本；本地标准 API 可用。
- `get_login_info` 现会检查就绪状态并向上传播上游失败，而不是捏造用户 ID 零。
- `set_group_special_title` 现路由到 `group.special_title`；原生支持仍有意缺位。
- `get_forward_msg` 现输出标准的 `message` 节点段以及传统的 `messages`；原生获取仍缺位。
- 扩展 CQ 解析并不会扩展原生编解码器的发送能力集。

本次审计不作任何在线网络/原生验证的声明。集成与测试由父代理单独负责。
