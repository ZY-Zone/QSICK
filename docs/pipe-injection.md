# pipe 实例的注入实现

pipe 实例启动的是**原版 QQ.exe**，不做任何文件改动；所有定制由一个注入的 DLL
（`native/qsick-pipe/`）在进程内完成。

## 为什么是这个形态

要求里说"阻止 electron 的启动"，实测下来要拆成两件事看：

| 做法 | Electron | 原生后端（`wrapper.node`） |
| --- | --- | --- |
| 拦掉 `ExportedContentMain` | 不启动（`blocked=1`，0 个 `--type=` 子进程） | **也没起来**——进程只有 40 个模块 |
| 正常启动 + 隐藏窗口 | 正常启动 | 正常加载（136~138 个模块） |

原因是 `wrapper.node` 有 41 个 `napi_*` / `node_api_*` 导入，**必须在 Node 运行时
里才能加载**，而把它 `require` 进来的是 Electron 主进程的 JS（`major.node`）。
拦掉 Electron 就等于把加载后端的那条链一起切断了，实例拿不到协议层，对 QSICK
没有意义。

所以最终采用第二种：**保留 Electron 主进程（后端走官方路径），隐藏所有窗口**。

## 启动链路

```
QSICK
  └─ Popen(QQ.exe --no-sandbox --user-data-dir=<实例>/owndata/userdata)
       └─ 20~40 ms 内 CreateRemoteThread(LoadLibraryW) 注入 qsick_pipe.dll
            ├─ 隐藏窗口（IAT hook + 扫窗线程）
            ├─ 数据目录重定向（Documents → <实例>/owndata）
            └─ 记录握手值供 QSICK 读回
```

`QQ.exe` 是 QSICK 的子进程；父进程退出后实例也随之结束。

### 为什么不用 `CREATE_SUSPENDED`

要求里写的是"先凝滞 QQ.exe 的启动，注入，再恢复"。实测**这条路走不通**：

```
普通启动                      -> 存活，有窗口，4 个子进程
CREATE_SUSPENDED + ResumeThread -> 12 秒后 0x80000003 退出
空白 DLL（DllMain 只 return TRUE）注入挂起进程 -> 同样 0x80000003
```

空白 DLL 也崩，说明不是注入代码的问题，而是挂起本身破坏了 QQ 的初始化。
逆向笔记把这个 `int3` 定位在 `QQNT.dll+0x26B6E3A`，是启动/sandbox 校验
（`0x26B6DFD` 处检查 `--no-sandbox`）。

所以改成**正常启动、立刻注入**：实测注入在 19~40 ms 内完成，远早于 Electron
创建窗口（正常启动到出现窗口要 1 秒以上），目的（在 Electron 起来之前接管）同样
达到，而且不会踩那个校验。

### `--no-sandbox` 是必需的

不加它，QQ 会在启动约 12 秒后以 `0x80000003` 退出——即使完全不注入也一样。
逆向笔记对这个校验的描述：

```
0x26b6dfd  lea  rdx, "no-sandbox"
0x26b6e07  call CommandLine::HasSwitch
0x26b6e11  jne  0x26b6e31      ; 有 --no-sandbox -> 直接返回
0x26b6e2c  jmp  0x26b6e3a      ; 否则校验失败 -> int3
```

## DLL 做了什么

### 窗口隐藏

两条机制并用，因为只靠 hook 不够：

1. **IAT hook** `CreateWindowExW` / `ShowWindow`。这两个在 `QQNT.dll` 里是
   **延迟导入**（实测：常规导入表里没有，`ImgDelayDescr` 在 RVA `0xc545a20`），
   所以 patch 时必须同时走 delay-load 表，只改 `FirstThunk` 会"报告成功但没生效"。
2. **扫窗线程**：每 400 ms 枚举本进程的顶层窗口，可见的一律 `SW_HIDE`。
   Chromium 那个真正的主窗口不是通过我们能看到的那条导入路径创建的，只能靠扫。

实测结果：

```
对照（不注入）: IsWindowVisible=True  WS_VISIBLE=True
注入后        : MainWindowHandle=0    扫窗线程隐藏 10 个窗口
```

### 数据目录重定向

把 `SHGetFolderPathW` / `SHGetKnownFolderPath` 的 **Documents** 结果换成
`<实例>/owndata`。QQ 由此把 `Tencent Files` 写进实例目录，而不是用户
`Documents` 下——多实例不会互相抢，也不会碰到用户自己那份。

只重定向 Documents：其它 shell 文件夹必须保持原值，否则 QQ 找不到自己的安装目录
和临时目录。

Electron 自己的 userData（Cache、Cookies、Local Storage、Crashpad）由命令行参数
`--user-data-dir` 一并指到 `<实例>/owndata/userdata`，那是参数管得着的部分。

### 与 QSICK 的握手

DLL 在自己的数据段里放出几个可导出的计数器，注入方用 `ReadProcessMemory` 读回：

| 导出 | 含义 |
| --- | --- |
| `g_qsickMagic` | `0xC0DE0001` 表示 `DllMain` 已跑完 |
| `g_qsickAttachPid` | 目标进程 PID，用来确认读到的不是缓存 |
| `g_qsickDataDirHooked` | 数据目录重定向是否生效 |
| `g_qsickWindowHooked` | 窗口 IAT hook 是否装上 |
| `g_qsickWindowsHidden` | 已隐藏的窗口数 |

不用日志判断成功：早期版本靠写日志，而日志路径依赖模块句柄（`g_hInstance`），
拿到的是宿主 EXE 的目录，结果"注入成功但日志一个字都没有"。计数器走内存，
不受这条依赖影响。

## 配置

DLL 从**自己旁边**的 `qsick_pipe.json` 读配置：

```json
{ "owndata": "D:\\QSICK\\headlessClient\\C1\\owndata" }
```

不用环境变量传递：实测 `CreateProcessW` 只要收到非 NULL 的 `lpEnvironment` 就
返回 `ERROR_INVALID_PARAMETER (87)`，与本项目所用工具链下用哪种指针类型无关。
文件更可靠，出问题时也能直接打开看。

因为配置在 DLL 旁边，**每个实例必须拿到自己那份 DLL 副本**。`start()` 会把
`native/qsick-pipe/qsick_pipe.dll` 复制到 `<实例>/data/qsick_pipe.dll`，并把
`qsick_pipe.json` 写在同一目录。

这一点是有代价才学到的：早先所有实例共用仓库里那一份，同时起 C1 和 C2 之后
json 里只剩下 C2 的 owndata——C1 看起来正常，只是因为它恰好在被覆盖前读完了
配置。这类竞态在单实例测试里测不出来，现在由 `tests/test_pipe_isolation.py`
盯着。

## 多实例隔离

两处共享资源都必须隔离，否则实例会互相干扰：

| 资源 | 隔离方式 | 验证 |
| --- | --- | --- |
| Electron userData（Cache、Cookies、Local Storage） | `--user-data-dir=<实例>/owndata/userdata` | 两个实例各自生成 userdata |
| 原生数据根（`Documents\Tencent Files`） | DLL hook 文件夹 API → `<实例>/owndata` | 两实例 `datadir=1`，owndata 各 40+ 文件 |
| 单实例锁 | 上述两者都隔离后自然不冲突 | 同时运行互不干扰 |

实测同时启动 C1 和 C2：

```
C1: alive=True children=3 magic=0xC0DE0001 datadir=1 hidden=11 owndata=45
C2: alive=True children=3 magic=0xC0DE0001 datadir=1 hidden=9  owndata=44
child overlap: set()      # 进程树没有交叉
```

## 生命周期

`stop()` 会清理整棵进程树。pipe 实例不是单进程——QQ.exe 会派生 3 个子进程
（gpu/utility/crashpad），只杀主 PID 会留下孤儿并保持登录态。实测：

```
started pid=36448  children=[32752, 36872, 35988]
--- stop() ---
main pid still alive: False
surviving QQ.exe: []          # 无残留
status phase: stopped  pid=0
```

## 已知边界

* **握手不是控制通道**：目前 DLL 只做窗口/数据目录的静默化，消息和动作仍然走
  现有的 API（`headless_actions.cjs`）。要做成"由 DLL 直接暴露动作"，需要在
  DLL 里起一个 Node 运行时来 `require("wrapper.node")`，工作量比现在大得多。
* **`ipc.node` 未必加载**：实测有的实例里它是 `False`，与 QQEX 子进程的启动
  时机有关，不影响 `wrapper.node` 这条主链路。
* **窗口是"藏起来"而不是"不创建"**：Chromium 仍会建窗口对象，只是不可见。
  对批量挂号够用，但严格说不是无窗口进程。

## 验证方法

```bat
rem 1. 构建 DLL
native\qsick-pipe\build\build_pipe_dll.bat

rem 2. 建一个 pipe 实例并启动（需要先有 pipe 模板）
uv run python -c "from qsick.headless import HeadlessManager; m=HeadlessManager('headlessClient'); print(m.create(kind='pipe'))"

rem 3. 看 DLL 日志与 owndata
type headlessClient\C1\owndata\qsick_pipe.log
dir  headlessClient\C1\owndata
```

日志里应有：

```
attach pid=... exe=...\QQ.exe
hook set for LoadLibraryExW
window sweep thread started
late hook watcher started
QQNT.dll loaded at ... -> installing late hooks
shell folder hooks applied to QQNT.dll
window sweep hid 1 window(s)
```

同时 `Get-Process -Id <pid>` 的 `MainWindowHandle` 应为 **0**。
