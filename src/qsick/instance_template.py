"""pipe 型实例：一份可直接启动的完整 QQNT 客户端副本。

两类实例的区别：

* **headless** —— 不启动 QQ，用一个裸 Node 进程直接 ``require`` ``wrapper.node``
  （自造的 ``QQNT.dll`` 垫片补上它需要的 napi/uv 符号），只做协议收发。适合批量
  挂号，但 QQ 的某些能力在没有 Electron 主进程时拿不到。
* **pipe** —— 启动真的 ``QQ.exe``，但把它的 ``resources/app/package.json`` 的
  ``main`` 换成我们的 loader（参照 NapCat 的做法）。于是进程仍在 Electron 里、
  原生环境是原版那份，却不会创建窗口——因为原版入口（建窗口的那个）没有被执行。
  代价是每个实例要一份完整客户端（约 1.1 GB）。

  QSICK 与 NapCat 的一处差别：NapCat 用共享的客户端目录，只能靠
  ``NapCatWinBootHook.dll`` 在运行期篡改；pipe 实例有自己独立的副本，所以
  **创建时直接改** ``package.json`` 就够了，不需要 hook DLL。

## 为什么需要"模板"

``create()`` 原本只写几个 json。pipe 实例不行：它要能被 ``start()`` 直接拉起来，
就得有 QQ.exe、versions/<ver>/resources/app/wrapper.node、全部依赖 DLL……少一个
都起不来。所以这里从仓库外的官方客户端生成一份**模板**，之后每次建实例把它铺
过去，再把 loader 装进它的 ``resources/app/``。

## 为什么用硬链接而不是真复制

一份客户端 1.1 GB / 约 1870 个文件。真复制 5 个实例就是 5.5 GB，而且其中
99% 是只读的 DLL 和资源，一模一样。硬链接让每个实例有自己的目录树（该改的
照样能改），但磁盘上只占一份——同卷 NTFS 支持，`os.link` 就能做。

真正会变的只有登录态和数据目录，那些本来就各写各的。

## 一次性可用

模板生成时会顺手把三件事做好，否则"复制过去"并不等于"能用"：

1. ``package.json`` 的 ``version`` 与 ``versions/`` 下的目录名对齐——启动器靠它
   判断当前版本，对不上会去下载更新；
2. 数据目录指向实例自己的 ``data/qq``，多个实例不会抢同一份登录态；
3. 生成一个 ``launch.bat``，双击即可启动这个实例的 QQ。
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from . import log as logmod

log = logmod.get_logger("Instance.Template")

#: 模板放在项目根的 ``instanceTemplates/`` 下（不入库，见 .gitignore）。
TEMPLATE_DIR_NAME = "instanceTemplates"
#: The project's own client copy, and the only thing templates are built from.
RUNTIME_DIR_NAME = "runtime"
#: 模板里必须存在的文件；缺任何一个都不算可用。
REQUIRED = ("QQ.exe",)

#: 这些目录里的文件不链接，直接跳过：登录态、缓存、崩溃报告都该由实例自己生成。
SKIP_DIRS = {"crashpad", "logs", "temp"}

#: 客户端会做完整性校验的四个文件：签名在 ``resources/app/application.json`` 里
#: （每个 344 字符，RSA-3072），校验实现位于 ``major.node``。
#:
#: 两条硬性约定都由实测得出：
#:   1. 这些文件的内容一个字节都不能改——改一个字节（哪怕只动缩进）QQ 启动就报
#:      「QQ 文件已损坏, 请重新安装QQ」，``[preload]`` 一次都不会成功；
#:   2. 它们不能被硬链接进实例——链接与源共享数据，写一次就污染源客户端。
#:
#: 所以 :func:`_link_tree` 对这几个名字一律真复制。
PROTECTED = ("application.asar", "wrapper.node", "major.node", "package.json")


class TemplateError(RuntimeError):
    """模板缺失或无法生成。消息面向用户。"""


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def template_root() -> Path:
    return repo_root() / TEMPLATE_DIR_NAME


def template_path(version: str) -> Path:
    return template_root() / f"pipe-{version}"


def runtime_root() -> Path:
    """The one and only client source: `runtime/` in the project root."""
    return repo_root() / RUNTIME_DIR_NAME


def _source_client() -> Path | None:
    """The client a pipe template is built from: `runtime/`, or nothing.

    `runtime/` is the only source, on purpose. The machine's own QQ install is never consulted, and
    neither is a `QQNT-*` folder someone left beside the project.

    Why that rule exists: whatever client is used here determines the version stamped into
    `instanceTemplates/pipe-<version>`, and the pipe client is then laid out from that template. Using
    the local install meant a machine whose QQ had auto-updated to 9.9.37-54395 produced a 9.9.37 pipe
    client, while the injected DLL and every reverse-engineering report target 9.9.33-52230 -- an
    injection/version mismatch that crashed QQ.exe in ntdll with heap corruption. A version that varies
    per machine is not something the rest of this project can be correct against.

    Missing runtime is not an error here: the locked build is downloaded from the project's own release
    host (`runtime_fetch.DEFAULT_BASE_URL`). `None` is returned only when neither the local copy nor the
    download works, and `build_template` turns that into a message naming the expected directory.

    `QSICK_QQ_SOURCE` still overrides, because tests and the odd offline repair need to point somewhere
    else; it is an explicit opt-in rather than a discovery order.
    """
    from . import runtime_fetch

    override = os.environ.get("QSICK_QQ_SOURCE", "").strip()
    if override:
        client = Path(override)
        return client if (client / "QQ.exe").is_file() else None

    runtime = runtime_root()
    if runtime_fetch.runtime_ready(runtime):
        return runtime
    try:
        runtime_fetch.ensure_runtime(runtime)
    except Exception as exc:  # noqa: BLE001 - 下载失败要退化成"没有客户端"，由调用方解释
        log.warn("runtime 下载失败", error=f"{type(exc).__name__}: {exc}", target=str(runtime))
        return None
    return runtime if runtime_fetch.runtime_ready(runtime) else None


def detect_version(client: Path) -> str:
    """客户端版本：优先 ``versions/`` 下唯一目录名，其次 ``package.json``。"""
    versions = client / "versions"
    if versions.is_dir():
        dirs = sorted(entry.name for entry in versions.iterdir() if entry.is_dir())
        if len(dirs) == 1:
            return dirs[0]
    for app in client.glob("versions/*/resources/app"):
        try:
            payload = json.loads((app / "package.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        value = str(payload.get("version") or "")
        if value:
            return value
    return ""


def _unlink_first(path: Path) -> None:
    """打断硬链接：删掉目录项，让后续写入落在**新** inode 上。

    这是整个硬链接方案里最容易踩的坑，也是实测踩到的：``os.link`` 建出来的
    文件与源共享同一份数据，直接 ``write_text`` 会**写穿透**——改一个实例的
    ``package.json``，源客户端的同名文件跟着变，其它实例也一起变。

    QQ 启动时按 ``application.json`` 里的签名核对 ``package.json``，被改过就报
    「文件已损坏」。所以凡是"要改内容"的文件（package.json，将来可能还有别的），
    写之前必须先把目录项摘掉，再新建一个普通文件。
    """
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _write_isolated(path: Path, text: str) -> None:
    """写一个**不与模板共享**的文件。

    先摘链接再写：这样源客户端与其它实例都不受影响。
    """
    _unlink_first(path)
    path.write_text(text, encoding="utf-8")


def _link_tree(source: Path, target: Path) -> tuple[int, int]:
    """把一棵树"复制"到 target：文件用硬链接，目录新建。

    返回 ``(链接数, 复制数)``。跨卷或目标文件系统不支持硬链接时自动退回复制
    ——宁可多占空间，也不要因为一个 OSError 让实例建不出来。

    **受完整性校验的文件不链接，改用真复制。**

    这不是性能取舍，是正确性。硬链接与源共享同一份数据，任何一次 ``write_text``
    都会**写穿透**到源客户端——实测发生过：改一个实例的 ``package.json``，
    ``{项目根}\\QQNT-9.9.33-52230`` 里那份跟着变，随后所有实例启动都报
    「文件已损坏」。

    ``PROTECTED`` 那四个文件合计约 420 MB。真复制它们意味着每个实例多占这些
    空间，换来的是"任何代码路径都不可能改到源客户端"。相比让整个客户端的完整性
    校验失效，这个代价值得。
    """
    linked = copied = 0
    target.mkdir(parents=True, exist_ok=True)
    for entry in source.iterdir():
        if entry.name in SKIP_DIRS:
            continue
        destination = target / entry.name
        if entry.is_dir():
            sub_linked, sub_copied = _link_tree(entry, destination)
            linked += sub_linked
            copied += sub_copied
            continue
        if not entry.is_file():
            continue
        if destination.exists():
            continue
        # 受校验的文件永远复制，绝不链接。
        if entry.name in PROTECTED:
            try:
                shutil.copy2(entry, destination)
                copied += 1
            except OSError as exc:
                log.warn("复制失败", file=str(entry), error=str(exc))
            continue
        try:
            os.link(entry, destination)
            linked += 1
        except OSError:
            try:
                shutil.copy2(entry, destination)
                copied += 1
            except OSError as exc:
                log.warn("复制失败", file=str(entry), error=str(exc))
    return linked, copied


def build_template(source: Path | None = None, force: bool = False) -> Path:
    """从一份官方客户端生成本地 pipe 模板，返回模板目录。

    模板不存在时才构建；``force=True`` 强制重建（客户端换了版本时用）。
    """
    client = Path(source) if source is not None else _source_client()
    if client is None or not (client / "QQ.exe").is_file():
        raise TemplateError(
            "找不到可用于生成模板的 QQ 客户端。\n"
            "  设置 QSICK_QQ_SOURCE 指向一份完整客户端（含 QQ.exe），"
            "或者把客户端放到项目旁边的 QQNT-<版本> 目录。")
    version = detect_version(client)
    if not version:
        raise TemplateError(f"无法判断客户端版本：{client}")

    target = template_path(version)
    if target.is_dir() and (target / "QQ.exe").is_file() and not force:
        return target

    log.info("生成 pipe 模板", source=str(client), version=version, target=str(target))
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    linked, copied = _link_tree(client, target)
    log.info("模板生成完成", linked=linked, copied=copied)
    return target


def prepare_instance(template: Path, destination: Path, name: str) -> dict[str, object]:
    """把模板铺进一个实例目录，并让它"开箱能启动"。

    返回一段摘要（文件数、链接数、版本），供创建接口回报。
    """
    linked, copied = _link_tree(template, destination)
    version = detect_version(template)

    app = destination / "versions" / version / "resources" / "app"
    if app.is_dir():
        # 只核对版本号，**不写任何受校验的文件**：客户端对 package.json 做内容
        # 哈希校验，改一个字节（哪怕只动缩进）就报「文件已损坏」。
        _check_manifest(app, version)

    _write_launch(destination, name)
    return {"version": version, "linked": linked, "copied": copied}


#: 早前版本装进实例的宿主模块名。现在只用于清理，不再安装。
#: ``headless_host.js`` 自己也要进来——loader 最后 require 的就是它，漏掉会得到
#: 一个"loader 装好了但 require 立刻 ENOENT"的实例。
HOST_MODULES = (
    "headless_host.js",
    "headless_codec.cjs",
    "headless_actions.cjs",
    "headless_sender.cjs",
    "headless_login.cjs",
    "headless_media.cjs",
    "headless_oidb.cjs",
)


def _host_module_dir() -> Path:
    """loader 与宿主模块的源目录（``src/qsick``）。

    单独抽成函数是为了让测试能指到别处——它同时是"这些文件从哪来"的唯一答案。
    """
    return Path(__file__).resolve().parent


#: 早前版本装进实例的 loader 文件名；现在只用于清理。
LEGACY_LOADER = "qsick_pipe_loader.js"


def remove_loader(destination: Path, app: Path) -> bool:
    """撤销早前版本装进去的 loader，把 ``package.json`` 还原成原版。

    ## 为什么是"撤"而不是"装"

    曾经按 NapCat 的思路把 ``resources/app/package.json`` 的 ``main`` 换成自己的
    loader。在本版本客户端上实测，这条路走不通，而且没有任何"温柔一点"的余地：

    ================  ===================  ==========
    改动               ``[preload]`` 成功   结果
    ================  ===================  ==========
    不动               2                    正常启动
    加一个无害的键      0                    弹「退出」框
    **只改缩进**       0                    弹「退出」框
    ================  ===================  ==========

    只改缩进也失败，说明 QQ 对 ``package.json`` 做的是**内容哈希**，不是解析后
    比对字段。改一个字节就等于改文件。

    NapCat 能绕开是因为它**不动磁盘上的文件**：改好的副本放自己目录，靠
    ``NapCatWinBootHook.dll`` 在进程内拦截文件读取把路径重定向过去。我拿它的
    原版二进制在本客户端上试过，hook 不匹配、进程直接退出，照搬不成立。

    所以这里只负责把历史实例改回原样，让校验重新通过。

    返回是否真的做了修改（幂等：没装过的实例返回 False，不碰它）。
    """
    changed = False

    manifest = app / "package.json"
    if manifest.is_file():
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict) and "originalMain" in payload:
            # 我们写过它：把 main 换回去、删掉自己加的字段。
            original = str(payload.pop("originalMain") or "")
            if original:
                payload["main"] = original
            _write_isolated(manifest, json.dumps(payload, ensure_ascii=False, indent=2))
            changed = True
            log.info("已还原 package.json", app=str(app), main=payload.get("main", ""))

    # 宿主模块是我们新增的文件，不在校验清单里；留着无害，但删掉更干净。
    #
    # 只删"确实是我们放的"那些：判据是同名文件也在 loader 的源目录里。早先无条件
    # 按名字删，结果在测试里把真实源码目录的 headless_host.js 一起删了——删文件
    # 这种操作不该只凭一个名字。
    origin = _host_module_dir()
    for name in (LEGACY_LOADER, *HOST_MODULES):
        target = app / name
        if not target.is_file():
            continue
        if not (origin / name).is_file() and name != LEGACY_LOADER:
            continue
        try:
            target.unlink()
            changed = True
        except OSError:
            pass
    return changed


def _check_manifest(app: Path, version: str) -> None:
    """核对 ``package.json`` 的 version 与目录名是否一致——**只检查，不改**。

    早先这里会直接改文件。那是个错误：``package.json`` 在完整性校验清单里，
    改一个字节（哪怕只动缩进）QQ 就报「文件已损坏」。实测见
    :func:`remove_loader` 的表格。

    所以现在只报警：两边不一致说明这份客户端本身就是残缺的（目录名被手工改过），
    改不改都起不来，至少不要让我们成为那个"改了它"的人。
    """
    manifest = app / "package.json"
    if not manifest.is_file():
        return
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(payload, dict):
        return
    declared = str(payload.get("version") or "")
    if declared and declared != version:
        log.warn("客户端版本与目录名不一致，可能无法启动",
                 declared=declared, directory=version, app=str(app))


def _write_launch(destination: Path, name: str) -> None:
    """写一个双击就能启动的 launch.bat。

    ``--user-data-dir`` 指向实例自己的 data 目录：不指定的话多个实例会共用
    ``%APPDATA%\\Tencent\\QQ``，后启动的那个要么抢到前一个的登录态，要么直接
    抱怨"已在运行"。
    """
    data_dir = destination / "data" / "qq"
    data_dir.mkdir(parents=True, exist_ok=True)
    exe = destination / "QQ.exe"
    # 用 newline="\r\n" 写，而不是把 \r\n 混进字符串：文本模式会把 \n 再翻成
    # \r\n，结果是每行之间多一个空行（.bat 里空行本身无害，但读起来像坏了）。
    lines = [
        "@echo off",
        "chcp 65001 >nul",
        f"REM QSICK pipe instance: {name}",
        "REM 数据目录独立，多个实例不会抢同一份登录态。",
        f'start "" "{exe}" --user-data-dir="{data_dir}" %*',
        "",
    ]
    (destination / "launch.bat").write_text("\r\n".join(lines), encoding="utf-8", newline="")


#: 注入 payload 时，实例目录里的 DLL 名字。
PAYLOAD_DLL = "qsick_payload.dll"


def payload_dll() -> Path | None:
    """QSICK 的 payload DLL（注入到 QQ.exe 里的那个）。"""
    candidate = repo_root() / "native" / "qsick-payload" / "qsick_payload.dll"
    if candidate.is_file():
        return candidate
    env = os.environ.get("QSICK_PAYLOAD_DLL", "").strip()
    if env and Path(env).is_file():
        return Path(env)
    return None


def launch_command(instance_dir: Path) -> list[str]:
    """启动这个 pipe 实例的命令行。

    跑的是**未经修改**的原版 ``QQ.exe``：受完整性校验的四个文件一个字节都没动，
    所以校验必然通过。接管收发出 ``transport/pipe.py`` 负责——它把
    ``qsick_payload.dll`` 注入进这个进程。

    代价是窗口会出现。这是不改受校验文件的必然结果（原版入口就是建窗口的那个），
    换来的是"一定能启动"，而不是卡在「文件已损坏」。

    ``--user-data-dir`` 指向实例自己的目录：不指定的话多个实例会共用
    ``%APPDATA%\\Tencent\\QQ``，后启动的那个要么抢到前一个的登录态，要么直接
    抱怨"已在运行"。
    """
    exe = instance_dir / "QQ.exe"
    data_dir = instance_dir / "data" / "qq"
    data_dir.mkdir(parents=True, exist_ok=True)
    return [str(exe), f"--user-data-dir={data_dir}"]
