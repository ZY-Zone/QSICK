# QSICK

一个QQNT的api导出工具

***
声明：仅项目主要结构、readme.md、以及少量代码由创作者人工编写\
其余部分均由cc和ds混合工作完成\
本项目在少部分实现上有所参考napcat/snowluma，实属无奈之举，望见谅\
有任何疑问可以前往QQ群聊：https://qm.qq.com/q/IDnnkW9eSs
***

## 快速开始（Windows）

双击仓库根目录的 **`start.bat`**\
首次运行：\
1.uv安装依赖\
2.从联网服务器获取QQNT-9.9.33-52230（这会需要一些时间）\
***如果你能够开启魔法，并且魔法稳定性不错 \
也可以选择双击 **`获取QQnt.bat`** 以提前完成 **`\runtime`** 的下载***\
3.启动 WebUI 组件并打开 <http://127.0.0.1:5754/>

```bat
start.bat            :: WebUI + mock（安全的默认值）
start.bat --check    :: 检查 uv / node / Python 依赖
```

```bash
uv sync --extra dev
uv pip install -e . --no-deps   # 可编辑安装
uv run qsick --mock
```

- WebUI：<http://127.0.0.1:5754>
- OneBot HTTP：`POST http://127.0.0.1:3000/`，body 为 `{"action": "...", "params": {...}}`
- OneBot WS：`ws://127.0.0.1:3000/ws`
- OneBot SSE：`http://127.0.0.1:3000/sse`

## 实现链路（以pipe启动为主）

```
┌──────────────────────────── QQ.exe ────────────────────────────┐
│  QSICK payload（C++ DLL，MSVC）                                 │
│    · 命名管道  \\.\pipe\qsick.<pid>.ctl / .evt                  │
│    · 钩子引擎（偏移表 + 跳板）→ 收发路径                          │
└───────────────┬────────────────────────────────────────────────┘
                │ 帧协议（32 字节头 + JSON 体）
┌───────────────▼────────────────────────────────────────────────┐
│  qsick 内核（Python）                                           │
│   实例中心 → 解码 → 插件钩子 → OneBot 事件 → 客户端               │
│                     ↘ 动作 ← 注册表 ← 插件                      │
└───────────────┬────────────────────────────────────────────────┘
                │ HTTP / WebSocket / SSE
        OneBot v11 客户端 · WebUI（FastAPI + Jinja2）
```

## 项目结构

```
src/qsick/
  runtime/     源QQnt客户端，创建实例时会引用
  transport/   帧协议、mock、管道客户端、headless 桥、Windows 注入器
  protocol/    元素、消息模型、rkey、媒体、protobuf 辅助
  instances.py 实例中心：每实例的库、处理器、网络配置
  raw_store.py maxStore 之上的 MessageStore 实现（原始 payload 证据库）
  onebot/      注册表、actions/<类别>/<动作>.py、事件、服务器、反向 WS
  plugins/     清单、带权限的上下文、宿主、热重载
  webui/       FastAPI 应用、Jinja2 模板、静态资源
  headless*    无头宿主桥（host.js / codec / sender / media）
protocols/            聊天/存储协议适配器（一个目录 = 一个协议）
plugins/              插件目录
assets/qq-faces/      QQ 表情资源（图片需用 tools/import_emotions.py 生成）
headlessClient/       实例存放目录，headless为早期设计时命名，现在略有歧义
market/               远程插件市场服务的源码
native/qsick-payload/ C++ DLL（管道 + 跳板 + 钩子）
build/                QQNT.dll 垫片的源码与产物（build_qqnt_shim.bat 构建）
docs/                 架构、协议、实例、payload ABI、插件 API、审计记录
tools/                动作同步、表情导入、审计生成、冒烟测试
tests/                pytest 套件 + Node 宿主测试
```

## 文档

| 文档                                                                             | 内容                                        |
|--------------------------------------------------------------------------------|-------------------------------------------|
| [docs/architecture.md](docs/architecture.md)                                   | 架构、多实例数据流、消息与动作流水线、失败姿态                   |
| [docs/PROTOCOLS.md](docs/PROTOCOLS.md)                                         | 协议层：onebotv11 / EXTemotions / maxStore 协作 |
| [docs/plugin-api.md](docs/plugin-api.md)                                       | 插件清单、权限、钩子、配置界面、市场                        |
| [docs/payload-abi.md](docs/payload-abi.md)                                     | 原生 payload 的帧协议、op 契约、偏移表                 |
| [docs/payload-ops.md](docs/payload-ops.md)                                     | 242 个动作的清单与 QQ 侧实现状态                      |
| [docs/headless-capabilities.md](docs/headless-capabilities.md)                 | 无头宿主的能力边界与已知限制                            |
| [docs/instances.md](docs/instances.md)                                         | 实例类型（headless / pipe）、创建对话框、模板与硬链接        |
| [docs/onebot-action-audit-summary.md](docs/onebot-action-audit-summary.md)     | 动作实现状态的静态审计结论                             |
| [docs/audit-onebot11-protocol-notes.md](docs/audit-onebot11-protocol-notes.md) | OneBot v11 协议与本地实现的差异记录                   |

## 免责声明

**请在使用前完整阅读本节。**

- **自担风险。** QSICK 会向 `QQ.exe` 注入代码并接管其收发路径。这违反了腾讯的用户协议，
  可能导致**账号被限制、封禁或永久终止**。作者不对任何账号损失、数据丢失或服务中断负责。
- **仅限学习与研究。** 本项目用于协议互操作的研究与学习，不鼓励也不支持任何违反服务条款
  或当地法律的用途。
- **账号安全。** 注入类工具会让登录态暴露在额外的攻击面上。不要在主力账号上使用，
  更不要在公用或来路不明的机器上运行。建议全程使用一次性账号。
- **无任何担保。** 本项目按"现状"提供，不附带任何明示或暗示的担保，包括适销性、
  特定用途适用性与非侵权。
- **第三方组件。** `qsick_payload.dll` 是专有二进制组件，不在 AGPL 范围内，仅随 QSICK
  分发且仅限非商业用途；条款见 [NATIVE-NOTICE.md](native/qsick-payload/NATIVE-NOTICE.md)。
  源码部分以 AGPL-3.0-or-later 授权，见 [LICENSE](LICENSE)。
- **与腾讯无关。** QQ 与腾讯是各自权利人的商标；QSICK 与腾讯无隶属、背书或支持关系。

**如果你不同意以上任何一条，请不要使用本项目。**
