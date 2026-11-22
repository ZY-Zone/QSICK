# 实例：类型、创建与目录

QSICK 的每个账号是一个**实例**。实例放在 `headlessClient/` 下，目录名是稳定的
`C0`、`C1`……，同时有一个可改的显示名。

## 两种类型

创建实例时选类型，这个选择决定目录里放什么、以及它怎么跑。

| | `headless`（默认） | `pipe` |
| --- | --- | --- |
| 怎么跑 | 裸 Node 直接 `require` `runtime/wrapper.node` | 启动原版 `QQ.exe`，再注入 payload DLL |
| 谁提供 Electron | 没有（自造 `QQNT.dll` 垫片补符号） | QQ 自己的 Electron 主进程 |
| 界面 | 无（扫码在 WebUI） | **有窗口**（原版入口就是建窗口的那个） |
| 磁盘 | 几乎不占，全实例共用一份 `runtime/` | 每实例一份完整客户端（约 1.1 GB **表面**体积） |
| native 能力 | 只有 `wrapper.node` 暴露的那些 | 完整，原生环境就是原版那份 |
| 适合 | 批量挂号、只要协议收发 | 需要完整原生环境；能接受窗口 |

`pipe` 不开窗口这条路走不通：客户端对 `package.json` 等内容做**内容哈希**校验，
改一个字节（哪怕只动缩进）就报「文件已损坏」。详见下面的实测表。

`pipe` 的 1.1 GB 是**表面体积**：文件用硬链接指向模板，磁盘上只存一份。见下。

## 创建

WebUI 的 **账户管理 → 新建实例** 打开一个对话框：

- **实例名称** —— 必填，不可重复（大小写不敏感，`客服A` 与 `客户a` 视为相同）。
  它只用于界面显示；目录名仍然分配 `C0`、`C1`…，所以改名不会让配置或数据库失联。
- **实例类型** —— `headless` / `pipe` 下拉。选中后在下方显示该类型的代价与收益。

对应的接口：

```http
POST /api/accounts
Content-Type: application/json

{"name": "客服A", "kind": "pipe"}
```

两个字段都可省略：不带 body 时按"下一个 `C<n>` + headless"创建，保持与旧脚本兼容。
名称冲突或类型非法返回 `400`，模板/磁盘问题返回 `500`。

### 对话框里为什么用系统下拉

`<dialog showModal()>` 会被浏览器提升到 **top layer**，那一层的层级高于任何
`z-index`。项目自制的下拉菜单 `.dd-menu` 是挂在 `body` 上的普通元素，无论把
`z-index` 调多大都压不过它——用户看到的就是"下拉菜单被小窗口盖住"。

所以对话框里的 `<select>` 标了 `data-native-select`，`app.js` 的事件委托会跳过
它，交给系统自带的弹出层（它在 top layer 之上，永远可见）。页面其它地方的
下拉仍然是自制的。

另一个坑：浏览器给 `<dialog>` 的默认外框是 2px 纯黑，不写 `outline: none`
就会显示成"一圈黑边"。

## pipe 实例的模板

`pipe` 实例不能靠"写几个 json"建出来——它要能立刻双击启动，就得有 `QQ.exe`、
`versions/<版本>/resources/app/wrapper.node` 和全部依赖 DLL。所以先从一份官方
客户端生成**模板**，之后每次建实例把它铺过去：

```
instanceTemplates/pipe-9.9.33-52230/    ← 模板（不入库）
headlessClient/C0/                      ← 实例（不入库）
    QQ.exe
    launch.bat                          ← 双击启动这个实例的 QQ
    versions/9.9.33-52230/resources/app/wrapper.node
    data/qq/                            ← 该实例自己的登录态
```

### 硬链接，不是复制

一份客户端约 1868 个文件、1.1 GB。真复制 5 个实例就是 5.5 GB，而其中 99% 是
一模一样的只读 DLL 与资源。铺实例时文件全部用 `os.link` 建硬链接：

```
模板 QQ.exe inode == 实例 QQ.exe inode    → 磁盘只存一份数据
```

实测：生成模板 0.4 秒 / `linked=1868 copied=0`；建一个实例 0.5 秒 / 1871 个文件，
磁盘几乎不增。跨卷或不支持硬链接时自动退回复制（宁可占空间，也不能建不出来）。

### 一次性可用

模板生成和实例铺设时会顺手做三件事，否则"复制过去"并不等于"能用"：

1. **对齐版本** —— 把 `resources/app/package.json` 的 `version` 改成与
   `versions/` 目录名一致。启动器靠这个字段判断该运行哪个版本；不一致会以为
   需要更新，第一次启动就卡在下载上。
2. **隔离数据目录** —— `launch.bat` 里带
   `--user-data-dir="<实例>/data/qq"`。不指定的话多个实例会共用
   `%APPDATA%\Tencent\QQ`，后启动的那个要么抢到前一个的登录态，要么直接抱怨
   "已在运行"。
3. **给一个启动入口** —— 生成 `launch.bat`，双击即启动该实例的 QQ。

`pipe` 实例不写 `host.js`：它的宿主就是自己那份 `QQ.exe`，不需要 Node 壳。

## 启动链路

`start()` 按实例类型分岔，这是唯一的分歧点：

| | headless | pipe |
| --- | --- | --- |
| 启动什么 | `node host.js --config config.json` | `QQ.exe --user-data-dir=<实例>/data/qq` |
| 谁提供 Electron / 原生环境 | 裸 Node + 自造 `QQNT.dll` 垫片 | QQ 自己的 Electron 主进程 + 原版 `QQNT.dll` |
| 自己的客户端副本 | 不需要（共享 `runtime/`） | 需要（`instanceTemplates` 硬链接而来） |
| 入口（`package.json` 的 `main`） | 不适用 | **原样不动**（改了会报「文件已损坏」） |
| 收发怎么接管 | 由 `host.js` 内的 Node 代码完成 | 注入 `qsick_payload.dll`，走命名管道 |
| 状态文件 | `data/status.json` | **同一个** `data/status.json` |

状态文件刻意共用同一套字段（`pid` / `phase` / `qr` / `uin` …），额外写一个
`transport: "pipe"` 供排查。这样账户表、自动重登、聊天页都不需要为 pipe 分支
——否则每加一个功能都得多写一遍两条路径。

`stop()` 不需要分支：它按 `status.json` 里的 pid 杀进程，对两种类型都成立。

### 为什么 pipe **不**改 `main`（实测结论）

NapCat 的思路是把客户端 `resources/app/package.json` 的 `main` 换成自己的 JS
入口，让 QQ 启动后直接执行它。这条路上 QSICK 走过一次，**在本版本客户端上必然
失败**，而且失败得很彻底：

| 对 `package.json` 的动作 | `[preload]` 成功次数 | 结果 |
| --- | --- | --- |
| 不动 | 2 | 正常启动 |
| 加一个无害的键 | 0 | 弹「退出」框 |
| **只改缩进** | 0 | 弹「退出」框 |

`[preload] succeeded` 是客户端自己打的日志，`major.node` 与 `wrapper.node` 各
一条；0 条就意味着引擎根本没起来。

**只改缩进也失败**这一行是关键：QQ 对 `package.json` 做的是**内容哈希**校验，
不是解析后比对字段。改一个字节就等于改文件，没有"小心地改"的余地。

校验清单是四个文件，签名存在 `resources/app/application.json` 里（每个 344 字符，
RSA-3072），校验实现位于 `major.node`：

```
application.asar   wrapper.node   major.node   package.json
```

### NapCat 为什么不会被检出

它**不动磁盘上的文件**。改好的 `package.json` 放在 NapCat 自己的目录
（`qqnt.json`），再由 `NapCatWinBootHook.dll` 在进程内拦截文件读取
（`CreateFileW` / `GetFileInformationByHandleEx` / `GetFileInformationByName`），
把路径重定向到那份副本。磁盘上的文件签名依旧有效。

整套链路（摘自 `NapCatQQ-4.18.42` 的 `NapCat-main-integrity-report.md`，并已核对
`NapCat.Shell.Windows.OneKey` 里的二进制）：

```
NapCatWinBootMain.exe
  CreateProcessW(QQ.exe, CREATE_SUSPENDED)   → 注入 NapCatWinBootHook.dll
  CreateRemoteThread(LoadLibraryW) → ResumeThread
    DllMain 读 NAPCAT_PATCH_PACKAGE / NAPCAT_LOAD_PATH
    Patch QQ.exe IAT: GetProcAddress
      → 拿到 QQNT.dll 句柄后 Patch 它的 IAT:
        CreateFileW / GetFileInformationByName / GetFileInformationByHandleEx
          resources\app\package.json → qqnt.json
          loadNapCat.js             → NapCat 自己的 loader
```

一个易踩的细节：`NapCatWinBootMain.exe` 是按**自己所在目录**拼
`"<dir>\QQ.exe"` 和 `"<dir>\NapCatWinBootHook.dll"` 的，不读取命令行里的路径。
所以这套 kit 必须放进客户端目录才能跑（`launcher.bat` 也是靠 `cd` 到 NapCat
目录来实现这一点）。

### 为什么 QSICK 不照搬这套 hook

试过了，在本版本客户端上**注入 hook 反而会失败**。对照实验（同一份客户端副本，
唯一差别是走不走 NapCat 启动器）：

| 场景 | `[preload]` 成功次数 | 结果 |
| --- | --- | --- |
| A 直接启动 `QQ.exe`，无 kit | 2 | 正常启动 |
| B 经 `NapCatWinBootMain.exe` 注入 hook | 0 | 弹「退出」框（文件已损坏） |

也就是说：不注入一切正常，一注入就挂。那份 hook 是为 9.9.22 / 9.9.37 写的，
与本版本客户端不匹配。

报告自己也列了失效条件，其中两条正好对上：

- hook 只覆盖 `CreateFileW` / `GetFileInformationByName` /
  `GetFileInformationByHandleEx`。若校验改用 `NtCreateFile`、`CreateFile2`、
  `GetFileAttributesExW`、内存映射或直接解析 ASAR，就会漏掉；
- 报告称「NapCat 能跑 ⟹ 校验器不通过被 hook 的 API 做字节比对」，但那条推论
  在本客户端不成立——实测连**只改缩进**都会失败，说明校验确实比对了字节。

要自己走这条路的代价：写一个 hook 上述文件 API 的 DLL，并且逐版本跟进客户端的
校验实现。这是一块独立的 native 工作，目前没有做。

### 所以 pipe 这样启动

原版 `QQ.exe` 照常启动，**四个受校验的文件一个字节都不动**，校验必定通过；
收发由 `transport/pipe.py` 用 `CreateRemoteThread(LoadLibraryW)` 注入
`qsick_payload.dll` 接管命名管道。

代价是**窗口会出现**——原版入口就是建窗口的那个。换来的是"一定能启动"，而不是
卡在「文件已损坏」上。

实测（全新 pipe 实例，直接启动它自己的 `QQ.exe`）：

```
[preload] succeeded : 2
文件已损坏          : False
```

### 老实例

早前版本的 `start()` 会往实例里装一个 `qsick_pipe_loader.js` 并改 `main`，那种
实例现在启动会弹「文件已损坏」。`start()` 每次启动前都会调用
`remove_loader()` 把它还原：`main` 换回原版、删掉我们加的文件。幂等——没装过的
实例不被触碰。

## 获取模板的来源

模板从一份**完整官方客户端**生成，按顺序找：

| 顺序 | 位置 |
| --- | --- |
| 1 | `%QSICK_QQ_SOURCE%` |
| 2 | `{项目根}\QQNT-<版本>` |
| 3 | `%ProgramFiles%\Tencent\QQNT` |
| 4 | `%ProgramFiles(x86)%\Tencent\QQNT` |

找不到就是明确报错（不会静默建出一个起不来的实例），"新建实例"对话框会把原因
显示出来。模板本身不随仓库分发（1.1 GB），已在 `.gitignore` 里。

## 三条路线的区别

同样是"不打开 QQ 界面也能收发消息"，三条路线的切入点不同。下面 NapCat 一行是
读它的源码与二进制确认过的；SnowLuma 只标注了已知的一项（注入），其余没有本地
源码可查，不臆测：

| | NapCat | SnowLuma | QSICK 的 `headless` | QSICK 的 `pipe` |
| --- | --- | --- | --- | --- |
| 起点 | 原版 `QQ.exe` | 原版 `QQ.exe` | 裸 Node | 原版 `QQ.exe` |
| 关键手段 | 改写 `main` + hook 文件读取 | 注入 DLL | 直接 `require` wrapper.node | 注入 DLL |
| 动磁盘上受校验的文件 | **不动**（改的是副本） | 不动 | 不适用 | **不动** |
| 用 Electron 主进程 | 用 | 未知 | **不用** | 用 |
| 有窗口 | 无 | 未知 | 无 | **有** |

**NapCat 是"改 main + hook 读取"。** 官方客户端的 `resources/app/package.json` 里
`main` 原本指向 `./application.asar/app_launcher/index.js`；NapCat 把它换成自己的
`./loadNapCat.js`，于是 `QQ.exe` 启动后执行的是 NapCat 的入口：在 Electron 主进程里
`await app.whenReady()`，然后直接 `dlopen` 加载 `wrapper.node` 并初始化引擎与登录
服务。**没有创建 BrowserWindow**——它换的是 JS 入口，接管的是主进程。

关键在于它**不改磁盘上那份**：改好的 `package.json` 放在 NapCat 自己的目录，由
`NapCatWinBootHook.dll` 在进程内拦截文件读取把路径重定向过去。所以完整性校验看到
的仍是原文件，能过。

**SnowLuma 是"注入"。** 据其说明，客户端照常启动，另用注入器把 DLL 塞进已经
跑起来的 `QQ.exe`，靠 hook 接管收发路径。（本地没有它的源码，除"注入"这一点外
未做核实，所以上面表格里其余几项标为未知。）

**QSICK 的 pipe 走的是注入这条路**：原版 `QQ.exe` 启动，四个受校验的文件一字节
不动，再用 `CreateRemoteThread(LoadLibraryW)` 注入 `qsick_payload.dll`。代价是
窗口会出现。

**QSICK 的 headless 比两者都更彻底**：连 `QQ.exe` 都不启动，用裸 Node 进程
（不是 Electron）直接 `require` `wrapper.node`，由自造的 `QQNT.dll` 垫片补上它
需要的 napi/uv 符号。

`pipe` 之所以要一整份客户端，是因为它真的要跑 `QQ.exe`；`headless` 只要
`wrapper.node`，所以能共用一份 `runtime/`。

## 目录

```
headlessClient/
  C0/                                  ← headless：几个 json + host.js + data/
  C1/                                  ← pipe：完整客户端 + data/（不含任何改写）
    QQ.exe
    launch.bat                         ← 手工启动用（正常由 WebUI 的「启动」拉起）
    versions/9.9.33-52230/resources/app/
      package.json                     ← 原版，一字节未改
      wrapper.node / major.node /      ← 原版客户端文件（硬链接）
      application.asar / application.json
    data/status.json                   ← 与 headless 同一套字段
instanceTemplates/                     ← pipe 模板，不入库
runtime/                               ← headless 共用的客户端副本，不入库
temp/                                  ← 所有临时产物，不入库
```

`headlessClient/` 整个不入库：里面是登录态与客户端副本，都不该进版本历史。

`launch.bat` 现在只是"手工排查时双击看看"的入口；正常流程是从 WebUI 点「启动」，
由 `start()` 拉起同样的命令。它不再需要用户先手动开 QQ。
