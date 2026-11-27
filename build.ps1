#Requires -Version 5.1
<#
.SYNOPSIS
    构建 QSICK 的分发版本：一个 QSICK.exe 加若干配套目录。

.DESCRIPTION
    用 uv 管理环境与依赖，用 PyInstaller 冻结解释器与第三方依赖，再把**被 git 跟踪的**运行时
    资源按原目录结构复制到产物旁边。

    为什么资源是复制而不是打进 exe：QSICK 用 `Path(__file__)` 的 `parents[2]` 定位项目根，
    `runtime/`、`temp/`、`downloads/`、`protocols/`、`native/` 都挂在它下面。打进 exe 会让
    `__file__` 指向退出即删的临时目录，那些路径全部指向不存在的地方。复制到 exe 旁边，
    冻结运行看到的目录结构与源码检出完全一致。

    一条硬规则：资源清单来自 `git ls-files`，不是文件系统遍历。这样 .gitignore 排除的东西
    （`runtime/`、`headlessClient/`、`data/`、`__pycache__`、本地探针脚本……）一定不会混进产物，
    而被跟踪的文件也不会漏。缺什么、多什么都由 git 说了算。

.PARAMETER Clean
    先删掉 dist 下的旧产物再构建。不加这个参数时 PyInstaller 也会覆盖，但残留的旧文件会留下。

.PARAMETER NoSync
    跳过 uv sync，直接用现有环境构建（依赖已装好时省一次解析）。

.EXAMPLE
    .\build.ps1

.EXAMPLE
    .\build.ps1 -Clean -NoSync
#>
[CmdletBinding()]
param(
    [switch]$Clean,
    [switch]$NoSync
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# --------------------------------------------------------------------------- 常量

$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$DistRoot = Join-Path $RepoRoot 'dist'
$AppDir = Join-Path $DistRoot 'QSICK'
$WorkDir = Join-Path $DistRoot '.pyinstaller'
$SpecFile = Join-Path $RepoRoot 'packaging\qsick.spec'

#: 构建用的独立虚拟环境。
#:
#: 不用 `.venv`：QSICK 若正在运行，它的 `Scripts\qsick.exe` 被占用，`uv sync` 会以
#: `failed to remove file ... os error 32` 失败。构建也不该改动开发环境。
#:
#: `UV_PROJECT_ENVIRONMENT` 是 uv 自己的开关，让它把"本项目的环境"指向这里，
#: 于是 `uv sync --group build` 仍然按 pyproject 解析依赖，只是装到别处去。
$BuildVenv = Join-Path $RepoRoot '.venv-build'

#: 需要在产物里保留原目录结构的运行时资源。
#:
#: 每一项都有明确理由：
#:   src/        代码、模板、静态资源、注入用的 .cjs —— 少一个就起不来或者打不开页面
#:   protocols/  协议注册表，network_profiles 按文件路径读它
#:   native/     qsick_pipe.dll（注入用）、qsick_payload.dll、inithook.node（群文件下载的原生钩子）
#:   build/      QQNT.dll —— headless 实例的宿主垫片，缺失时创建实例会报错
#:   plugins/    随发行提供的内置插件
$ResourceRoots = @('src', 'protocols', 'native', 'build', 'plugins')

#: 单独复制的根目录文件。
#:
#: `qqnt_get.py` 是 `QSICK.exe --fetch-runtime` 的实现，等同原来的 `获取QQnt.bat`：从腾讯官方安装包
#: 解出客户端，纯标准库，支持 `--source` 用本地安装包离线解。它与启动时自动下载是两条不同的路
#: （那条走项目自己的发布包，可续传、校验 sha256），留着是因为发布服务器不可达时只剩这条能用。
$ResourceFiles = @('qsick.example.json', 'qqnt_get.py')

#: 产物里预先建好的空目录。exe 启动后按需写入；预先建出来，运维一眼能看出哪些是可写数据。
$DataDirs = @('data', 'logs', 'downloads', 'temp')

# --------------------------------------------------------------------------- 工具函数

function Write-Step([string]$Message) {
    Write-Host ''
    Write-Host "== $Message" -ForegroundColor Cyan
}

function Write-Info([string]$Message) {
    Write-Host "   $Message"
}

function Invoke-Checked([string]$Exe, [string[]]$Arguments) {
    Write-Info "$Exe $($Arguments -join ' ')"
    # 原生程序的 stderr 不是错误。
    #
    # 本脚本全局是 Stop，而 uv / pyinstaller 把进度和警告都写 stderr。默认设置下 PowerShell 会把
    # 那些行当成终止性错误抛出来，于是"Using CPython 3.13.14"这样一句提示就让构建失败了。
    # 这里只按退出码判断成功与否，输出原样转印出来供人看。
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $Exe @Arguments 2>&1 | ForEach-Object { Write-Host "     $_" -ForegroundColor DarkGray }
    }
    finally {
        $ErrorActionPreference = $previous
    }
    if ($LASTEXITCODE -ne 0) {
        throw "$Exe 退出码 $LASTEXITCODE"
    }
}

# --------------------------------------------------------------------------- 前置检查

Write-Step '检查构建前提'

if (-not (Test-Path $SpecFile)) {
    throw "找不到 PyInstaller 配置：$SpecFile"
}
Write-Info "spec      $SpecFile"

$uv = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uv) {
    throw '找不到 uv。安装：powershell -ExecutionPolicy Bypass -c "irm https://astral.sh/uv/install.ps1 | iex"'
}
Write-Info "uv        $(& uv --version)"

$git = Get-Command git -ErrorAction SilentlyContinue
if (-not $git) {
    throw '找不到 git。资源清单来自 `git ls-files` —— 没有它就无法保证"排除被忽略的文件"。'
}

# 必须在仓库里：资源清单依赖 git 索引。
Push-Location $RepoRoot
try {
    & git rev-parse --is-inside-work-tree *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "$RepoRoot 不是 git 工作树"
    }
    $headShort = (& git rev-parse --short HEAD).Trim()
    Write-Info "repo      $RepoRoot @ $headShort"
}
finally {
    Pop-Location
}

# --------------------------------------------------------------------------- 同步依赖

# 指向独立环境，必须在任何 uv 命令之前设置。
$env:UV_PROJECT_ENVIRONMENT = $BuildVenv
Write-Info "构建环境  $BuildVenv"

if ($NoSync) {
    Write-Step '跳过依赖同步（-NoSync）'
}
else {
    # 先把环境本身建出来。
    #
    # 只设 `UV_PROJECT_ENVIRONMENT` 还不够：uv 见到一个存在但没有 python 的目录时会拒绝
    # （"not a valid Python environment (no Python executable was found)"），而一次中断的 sync
    # 恰好会留下这样的空壳。显式 `uv venv` 是幂等的，也把"用哪个 python"摆在命令里。
    Write-Step '创建独立构建环境（uv venv）'
    Push-Location $RepoRoot
    try {
        Invoke-Checked 'uv' @('venv', $BuildVenv, '--allow-existing')
        Invoke-Checked 'uv' @('sync', '--group', 'build')
    }
    finally {
        Pop-Location
    }
}

$BuildPython = Join-Path $BuildVenv 'Scripts\python.exe'
$BuildPyInstaller = Join-Path $BuildVenv 'Scripts\pyinstaller.exe'
if (-not (Test-Path $BuildPython)) {
    throw "构建环境里没有 python：$BuildPython（去掉 -NoSync 重跑）"
}
if (-not (Test-Path $BuildPyInstaller)) {
    throw "构建环境里没有 pyinstaller：$BuildPyInstaller（去掉 -NoSync 重跑）"
}

# --------------------------------------------------------------------------- 清理

if ($Clean) {
    Write-Step '清理旧产物'
    foreach ($path in @($AppDir, $WorkDir)) {
        if (Test-Path $path) {
            Remove-Item -Recurse -Force $path
            Write-Info "已删除 $path"
        }
    }
}

New-Item -ItemType Directory -Force -Path $DistRoot | Out-Null

# --------------------------------------------------------------------------- 冻结

Write-Step '冻结解释器与依赖（PyInstaller）'

Push-Location $RepoRoot
try {
    Invoke-Checked $BuildPyInstaller @(
        '--noconfirm',
        '--distpath', $DistRoot,
        '--workpath', $WorkDir,
        $SpecFile
    )
}
finally {
    Pop-Location
}

$exePath = Join-Path $AppDir 'QSICK.exe'
if (-not (Test-Path $exePath)) {
    throw "PyInstaller 没有产出 $exePath"
}
Write-Info ("QSICK.exe  {0:N1} MB" -f ((Get-Item $exePath).Length / 1MB))

# --------------------------------------------------------------------------- 复制资源

Write-Step '复制被跟踪的运行时资源（清单来自 git ls-files）'

Push-Location $RepoRoot
try {
    # `core.quotepath=false`：否则中文文件名会被转义成 \345\244... 形式，Copy-Item 找不到。
    # `ls-files` 只列被跟踪的文件，.__pycache__、runtime/、headlessClient/ 之类天然不在其中。
    $tracked = & git -c core.quotepath=false ls-files -- $ResourceRoots
    if ($LASTEXITCODE -ne 0) {
        throw 'git ls-files 失败'
    }
    $tracked = @($tracked | Where-Object { $_ -and $_.Trim() })
    Write-Info "被跟踪的资源文件：$($tracked.Count) 个"

    # 防漏：资源目录里"存在但未被跟踪"的文件不会进产物。
    #
    # 这不是理论风险。`src/qsick/webui/static/logo.png` 被 `base.html` 引用，却从没被提交过，
    # 于是第一版构建产出的界面缺了 logo —— 而清单本身看起来完全正常，因为漏掉的东西不在清单里。
    # 让它在这里出声，而不是等到用户发现页面缺图。
    $untracked = & git -c core.quotepath=false ls-files --others --exclude-standard -- $ResourceRoots
    $untracked = @($untracked | Where-Object { $_ -and $_.Trim() })
    if ($untracked.Count -gt 0) {
        Write-Warning "以下文件存在于资源目录但未被 git 跟踪，不会进入产物："
        foreach ($item in $untracked) {
            Write-Warning "  $item"
        }
        Write-Warning '  如果是运行必需的，请先 `git add` 它。'
    }

    $copied = 0
    foreach ($relative in $tracked) {
        $source = Join-Path $RepoRoot $relative
        if (-not (Test-Path -LiteralPath $source)) {
            # 索引里有、磁盘上没有：文件被删了但还没提交删除。复制不了，但要说出来。
            Write-Warning "索引中有记录但磁盘上不存在，已跳过：$relative"
            continue
        }
        $target = Join-Path $AppDir $relative
        $targetDir = Split-Path -Parent $target
        if (-not (Test-Path -LiteralPath $targetDir)) {
            New-Item -ItemType Directory -Force -Path $targetDir | Out-Null
        }
        Copy-Item -LiteralPath $source -Destination $target -Force
        $copied++
    }
    Write-Info "已复制：$copied 个文件"

    foreach ($name in $ResourceFiles) {
        $source = Join-Path $RepoRoot $name
        if (Test-Path -LiteralPath $source) {
            Copy-Item -LiteralPath $source -Destination (Join-Path $AppDir $name) -Force
            Write-Info "已复制：$name"
        }
        else {
            Write-Warning "缺少 $name（它没有被 git 跟踪？）"
        }
    }
}
finally {
    Pop-Location
}

# --------------------------------------------------------------------------- 数据目录

Write-Step '预建可写数据目录'

foreach ($name in $DataDirs) {
    $path = Join-Path $AppDir $name
    if (-not (Test-Path -LiteralPath $path)) {
        New-Item -ItemType Directory -Force -Path $path | Out-Null
        Write-Info "新建 $name/"
    }
    else {
        Write-Info "已存在 $name/"
    }
}

# --------------------------------------------------------------------------- 使用说明

Write-Step '写入使用说明'

$readme = @"
QSICK 分发版
============

双击 QSICK.exe 启动。没有黑窗口——启动器会把输出写到 logs\console.log，
出错时会弹一个对话框并给出这个日志路径。

目录说明
--------
  QSICK.exe        启动入口（已并入原 start.bat 与 隐藏窗口启动.bat 的能力：
                   自动打开浏览器、已在运行时直接打开而不是起第二份）
  src\ protocols\  运行时资源，不要移动或删除
  native\          DLL：注入用 qsick_pipe.dll、qsick_payload.dll、inihook.node
  build\QQNT.dll   headless 实例的宿主垫片
  plugins\         内置插件
  qsick.example.json  配置样板。需要改端口/令牌时，复制成 qsick.json 再改
  data\ logs\ downloads\ temp\    运行时可写目录，可随时清空（logs 除外）

首次使用
--------
  1. 双击 QSICK.exe，浏览器会打开 http://127.0.0.1:5754/
  2. 在「账户管理」里新增 pipe session 并扫码登录
  3. QQ 客户端副本会在首次用到时自动下载到 runtime\（约 1.1 GB）

获取 / 修复 QQ 客户端副本
-------------------------
  正常情况下不用管：QSICK 启动时发现 runtime\ 缺失会自动下载（走项目发布包，可续传、校验 sha256）。

  如果那台服务器不可达，或要用本地安装包离线解，用这条路：

      QSICK.exe --fetch-runtime                 下载腾讯官方安装包并解出 runtime\
      QSICK.exe --fetch-runtime --source X.exe  用本地安装包，不联网
      QSICK.exe --fetch-runtime --force         强制重新下载
      QSICK.exe --fetch-runtime --clean         解压前清空 runtime\
      QSICK.exe --fetch-runtime --delete-source 解压完删掉安装包（省 300MB）

  窗口是隐藏的，进度写在 logs\console.log，结束时弹框告知结果。

构建信息
--------
  提交      $headShort
  构建时间  $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
"@
$readmePath = Join-Path $AppDir '使用说明.txt'
# UTF-8 with BOM：记事本对无 BOM 的 UTF-8 中文会显示成乱码。
[System.IO.File]::WriteAllText($readmePath, $readme, (New-Object System.Text.UTF8Encoding($true)))
Write-Info '使用说明.txt'

# --------------------------------------------------------------------------- 汇总

Write-Step '完成'

$files = @(Get-ChildItem -LiteralPath $AppDir -Recurse -File)
$bytes = ($files | Measure-Object -Property Length -Sum).Sum
Write-Host "   产物目录  $AppDir"
Write-Host ("   文件数    {0} 个，共 {1:N1} MB" -f $files.Count, ($bytes / 1MB))
Write-Host "   入口      $exePath"
Write-Host ''
Write-Host '   未包含（按 .gitignore 排除，运行时自动提供）：' -ForegroundColor DarkGray
Write-Host '     runtime\        QQ 客户端副本，首次使用时自动下载' -ForegroundColor DarkGray
Write-Host '     headlessClient\ 实例与账号数据，运行时创建' -ForegroundColor DarkGray
Write-Host '     data\ logs\ 等  已在产物中建为空目录' -ForegroundColor DarkGray
