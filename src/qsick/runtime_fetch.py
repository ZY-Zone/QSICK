"""首次运行时拉取锁定的 QQNT 客户端（runtime/）。

无头实例需要 QQ 的 ``wrapper.node``，但仓库不可能带上 1.1 GB 的客户端，也不该
要求用户自己装一份 QQ 再指望版本对得上。这里的做法是：``runtime/`` 不存在时，
从项目自己的发布地址下载一份**锁定版本**的完整客户端，解包到 ``runtime/``。

设计要点：

* **存在就跳过**。只要 ``runtime/wrapper.node`` 在，这里什么都不做——用户手工放的
  任何版本都不会被自动下载覆盖（``prepare_runtime`` 同样以现有 runtime 为准）。
* **可续传**。包有 480 MB，国内链路断一次很常见。下载先写 ``.part``，用 HTTP
  Range 从已下载处继续；校验通过才改名，避免半个包被当成完整 runtime。
* **先校验后使用**。服务器给出 sha256，下载完对不上就丢弃并报错，不把损坏的
  ``wrapper.node`` 交给宿主去加载——那会变成莫名其妙的登录失败。
* **可关闭**。``QSICK_SKIP_RUNTIME_DOWNLOAD=1`` 或配置里关掉，让无网环境能自己
  准备目录而不被一个超时缠住。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import sys
import tarfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import log as logmod

DEFAULT_BASE_URL = "http://49.235.134.245/qq_got/QQNT-9.9.33-52230"

#: 第一条源：GitHub 上的官方安装包，由 `qqnt_get.py` 解包。
#:
#: 与第二条源不是同一份东西。这一条下载的是腾讯官方安装包（PE 外壳 + 内嵌 7z，LZMA+BCJ2），
#: 由 `qqnt_get` 纯标准库解开；第二条是项目自己压好的 tar.gz，带 descriptor.json 描述大小与
#: sha256，支持 Range 续传。
#:
#: 放在前面是因为 GitHub 通常比自建服务器快，而且 `qqnt_get` 内置了 11 个 gh-proxy 镜像——
#: 国内直连 GitHub 经常握手慢，那些镜像就是为这种情况准备的。
GITHUB_INSTALLER_URL = (
    "https://github.com/Rodert/qq-versions/releases/download/"
    "qq-packages-20260813-1d08f1d4/QQ_9.9.33_260813_x64_01.exe"
)

#: 探测第一条源最多等多久（秒）。
#:
#: 5 秒是"能连上就能连上"的量级：够一次 DNS + TLS 握手，不够拖住整个启动。超时就跳到第二条，
#: 而不是在这里挂着——用户宁可慢一点拿到客户端，也不愿意对着一个不动的窗口猜它在干什么。
GITHUB_PROBE_TIMEOUT = 5.0

#: 解包后必须存在的文件；缺任何一个都不算成功。
REQUIRED = ("wrapper.node", "package.json", "major.node")
CHUNK = 1024 * 512
TIMEOUT = 60


class RuntimeDownloadError(RuntimeError):
    """下载或解包失败。消息面向用户，不夹带堆栈。"""


def _log():
    return logmod.get_logger("Runtime.Fetch")


def app_dir(runtime: Path) -> Path:
    """返回真正含 ``wrapper.node`` 的那一层目录。

    两种布局都要支持，因为两种都真实存在：

    * **完整客户端**（现在分发的就是这种）::

          runtime/versions/9.9.33-52230/resources/app/wrapper.node
          runtime/versions/9.9.33-52230/QQNT.dll          ← 原始 204 MB
          runtime/QQ.exe

      ``runtime/`` 是整个客户端，``wrapper.node`` 埋在 ``resources/app`` 下。

    * **应用层平铺**（早期手工放的那种，也是 ``pipe`` 实例模板的布局）::

          runtime/wrapper.node
          runtime/package.json

    保留完整客户端是为了不丢原件（那个 204 MB 的 QQNT.dll、Electron 运行时、
    启动器），需要时能整体换回真客户端；代价是加载路径要往下探一层。
    """
    runtime = Path(runtime)
    if (runtime / "wrapper.node").is_file():
        return runtime
    versions = runtime / "versions"
    if versions.is_dir():
        # 多版本时取版本号最大的那个：客户端自身也是这么选的。
        candidates = [d for d in versions.iterdir() if d.is_dir()]
        for version in sorted(candidates, key=lambda p: p.name, reverse=True):
            app = version / "resources" / "app"
            if (app / "wrapper.node").is_file():
                return app
    return runtime


def runtime_ready(runtime: Path) -> bool:
    """``runtime/`` 是否已经是一份可用的客户端。

    判断依据是 ``app_dir`` 里那三个文件，而不是 ``runtime`` 根目录——完整客户端
    布局下根目录没有 wrapper.node。
    """
    app = app_dir(runtime)
    return all((app / name).is_file() for name in REQUIRED)


def _enabled(config=None) -> bool:
    env = os.environ.get("QSICK_SKIP_RUNTIME_DOWNLOAD", "").strip().lower()
    if env in ("1", "true", "yes"):
        return False
    if config is not None:
        value = getattr(config, "runtime_download", True)
        if value is False:
            return False
    return True


def _descriptor(base: str) -> dict:
    url = base.rstrip("/")
    with urllib.request.urlopen(url, timeout=TIMEOUT) as response:  # noqa: S310 - 固定的自家地址
        payload = json.loads(response.read(65536).decode("utf-8"))
    if not isinstance(payload, dict) or not payload.get("archive"):
        raise RuntimeDownloadError("下载地址没有返回有效的描述信息")
    return payload


def _download(url: str, target: Path, expected_size: int | None, expected_sha: str | None) -> None:
    """下载到 ``.part`` 并校验；支持用 Range 续传。"""
    part = target.with_suffix(target.suffix + ".part")
    position = part.stat().st_size if part.is_file() else 0
    if expected_size and position > expected_size:
        # 比预期还大说明这个 .part 不是同一份文件，重来。
        part.unlink(missing_ok=True)
        position = 0

    headers = {}
    if position:
        headers["Range"] = f"bytes={position}-"
        _log().info("恢复下载", url=url, from_mb=position // (1024 * 1024))

    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310
            mode = "ab" if position and response.status == 206 else "wb"
            if mode == "wb":
                position = 0
            written = position
            last = -1
            with part.open(mode) as handle:
                while True:
                    block = response.read(CHUNK)
                    if not block:
                        break
                    handle.write(block)
                    written += len(block)
                    megabyte = written // (1024 * 1024)
                    if megabyte != last and megabyte % 32 == 0:
                        last = megabyte
                        total = expected_size or 0
                        suffix = f"/{total // (1024 * 1024)} MB" if total else ""
                        _log().info("下载中", done_mb=megabyte, total=suffix)
    except urllib.error.HTTPError as exc:
        if exc.code == 416:
            # Range 越界：本地 .part 已经是完整文件了，交给下面的校验判断。
            _log().warn("续传位置超出文件长度，改用本地文件校验")
        else:
            raise RuntimeDownloadError(f"下载失败：HTTP {exc.code}") from exc

    size = part.stat().st_size
    if expected_size and size != expected_size:
        raise RuntimeDownloadError(
            f"下载不完整：得到 {size} 字节，应为 {expected_size} 字节（已保留 .part，可重试续传）")

    if expected_sha:
        digest = hashlib.sha256()
        with part.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        actual = digest.hexdigest()
        if actual.lower() != expected_sha.lower():
            part.unlink(missing_ok=True)
            raise RuntimeDownloadError("下载内容校验失败（sha256 不匹配），已丢弃，请重试")

    part.replace(target)


def _extract(archive: Path, runtime: Path) -> None:
    """把包里的客户端展开到 ``runtime/``，**保留完整客户端结构**。

    发布包结构::

        QQNT-9.9.33-52230/            ← 只去掉这一层
            QQ.exe
            versions/9.9.33-52230/
                resources/app/wrapper.node

    解出来 ``runtime/`` 即完整客户端，真正加载的那一层由 :func:`app_dir` 定位到
    ``runtime/versions/<ver>/resources/app``。

    保留完整结构而不是只抽 app 层，是为了留下原始 ``QQNT.dll``（204 MB）、
    Electron 运行时和启动器：需要时能直接换回真客户端，也能让 app 层里那些
    依赖上层组件的库（例如 ``wmpfsdk/runtime/flue.dll`` 要 ``ffmpeg.dll``）
    找得到东西。

    先解到临时目录再整体搬移：中途失败不会在 ``runtime/`` 里留下半套文件，而
    ``runtime_ready()`` 只在必需文件齐了之后才返回真——所以"看起来有目录但加载
    不了"这种状态不会出现。
    """
    staging = runtime.parent / (runtime.name + ".staging")
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    _log().info("解包中", archive=str(archive))
    with tarfile.open(archive, "r:gz") as bundle:
        members = bundle.getmembers()
        # 只剥掉最外层的版本目录（QQNT-9.9.33-52230/），其余原样保留。
        tops = {member.name.split("/")[0] for member in members if member.name}
        top = tops.pop() if len(tops) == 1 else ""
        if top:
            _log().info("去掉包外层目录", top=top)
        for member in members:
            name = member.name
            if top and (name == top or name.startswith(top + "/")):
                name = name[len(top):].lstrip("/")
            if not name:
                continue
            target = staging / name
            resolved = target.resolve()
            try:
                resolved.relative_to(staging.resolve())
            except ValueError:
                raise RuntimeDownloadError(f"压缩包内含有越界路径：{member.name}") from None
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = bundle.extractfile(member)
            if source is None:
                continue
            with source, target.open("wb") as handle:
                shutil.copyfileobj(source, handle, CHUNK)
            mode = member.mode
            if mode:
                try:
                    os.chmod(target, mode)
                except OSError:
                    pass

    if not runtime_ready(staging):
        missing = [name for name in REQUIRED if not (staging / name).is_file()]
        shutil.rmtree(staging, ignore_errors=True)
        raise RuntimeDownloadError(f"解包结果不完整，缺少：{', '.join(missing)}")

    if runtime.exists():
        shutil.rmtree(runtime, ignore_errors=True)
    staging.replace(runtime)


def _archive_url(base: str, info: dict) -> str:
    """把描述信息里的 ``archive`` 解析成真正可下载的地址。

    服务器给的是站点绝对路径（``/qq_got/xxx.tar.gz``），直接拼到 base 后面会
    变成 ``.../QQNT-9.9.33-52230/qq_got/xxx.tar.gz``——多一层 404。这里区分两种
    写法：绝对路径取站点根重新拼，相对路径才接在 base 后面。
    """
    archive = str(info.get("archive") or "").strip()
    if not archive:
        return base if base.endswith(".tar.gz") else base + ".tar.gz"
    if archive.startswith(("http://", "https://")):
        return archive
    base = base.rstrip("/")
    if archive.startswith("/"):
        root = urllib.parse.urlsplit(base)
        return urllib.parse.urlunsplit((root.scheme, root.netloc, archive, "", ""))
    return base + "/" + archive.lstrip("/")


def _qqnt_get_module():
    """按路径加载仓库根（或 exe 旁）的 ``qqnt_get.py``。

    它不在包里，因为它同时是一个能单独运行的命令行工具（``获取QQnt.bat`` 就是调它）。为了复用
    那份 PE+7z 解包逻辑而把它复制进包内，会让两份实现逐渐分叉；按路径导入是这里更小的恶。
    """
    candidates: list[Path] = []
    override = os.environ.get("QSICK_QQNT_GET", "").strip()
    if override:
        candidates.append(Path(override))
    # 冻结版：脚本在 exe 旁边。
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent / "qqnt_get.py")
    # 源码树：`src/qsick/runtime_fetch.py` 往上两层是仓库根。
    candidates.append(Path(__file__).resolve().parents[2] / "qqnt_get.py")

    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            spec = importlib.util.spec_from_file_location("qsick_qqnt_get", candidate)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
        except Exception as exc:  # noqa: BLE001 - 一条源不可用不该拖垮整次下载
            _log().warn("qqnt_get 加载失败", path=str(candidate), error=f"{type(exc).__name__}: {exc}")
    return None


def _github_reachable(url: str, timeout: float = GITHUB_PROBE_TIMEOUT) -> bool:
    """5 秒内能否拿到这条地址的响应头。

    只探测可达性，不读正文：目的是"要不要走这条源"，不是下载。``HEAD`` 被某些镜像拒绝，所以
    退到 ``GET`` 并只读首块就关掉连接。
    """
    for method in ("HEAD", "GET"):
        try:
            request = urllib.request.Request(url, method=method)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                if 200 <= getattr(response, "status", 0) < 400:
                    return True
        except urllib.error.HTTPError as exc:
            # 405 = 这个方法不被允许，换一个再试；其它状态码说明服务器就是这样答的。
            if exc.code in (403, 405) and method == "HEAD":
                continue
            return False
        except Exception:  # noqa: BLE001 - 超时、DNS、TLS 都算"不可达"
            return False
    return False


def _fetch_from_github(runtime: Path) -> bool:
    """用 GitHub 上的官方安装包铺好 ``runtime/``。成功返回 True。"""
    module = _qqnt_get_module()
    if module is None:
        raise RuntimeDownloadError("找不到 qqnt_get.py，无法使用 GitHub 源")
    if not _github_reachable(GITHUB_INSTALLER_URL):
        raise RuntimeDownloadError(f"GitHub 源 {GITHUB_PROBE_TIMEOUT:.0f} 秒内没有响应")
    _log().info("从 GitHub 获取官方安装包", url=GITHUB_INSTALLER_URL)
    # `--dir` 显式给定：默认值是脚本所在目录，而这里的目标与它无关。
    # `--github-only`：`qqnt_get` 自身也带服务器兜底，但这里已经有自己的源顺序了；
    # 不关掉它，失败时会先被它切去服务器，上面那条"GitHub 失败"的分支就再也不会被走到，
    # 日志也会说谎。
    try:
        code = int(module.main(["--dir", str(runtime), "--github-only"]) or 0)
    except SystemExit as exc:
        code = int(exc.code or 0)
    if code != 0:
        raise RuntimeDownloadError(f"qqnt_get 退出码 {code}")
    if not runtime_ready(runtime):
        raise RuntimeDownloadError("安装包已解开，但 runtime/ 仍不完整")
    return True


def _fetch_from_server(runtime: Path, base: str) -> bool:
    """用项目自己的发布包铺好 ``runtime/``。成功返回 True。"""
    _log().info("从发布服务器获取", source=base)
    info = _descriptor(base)

    archive_path = runtime.parent / f"{runtime.name}.download.tar.gz"
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    if archive_path.is_file() and info.get("size") and archive_path.stat().st_size == int(info["size"]):
        _log().info("已存在完整压缩包，跳过下载", path=str(archive_path))
    else:
        url = _archive_url(base, info)
        _download(url, archive_path,
                  int(info["size"]) if info.get("size") else None,
                  str(info.get("sha256") or "") or None)

    _extract(archive_path, runtime)
    archive_path.unlink(missing_ok=True)
    if not runtime_ready(runtime):
        raise RuntimeDownloadError("压缩包已解开，但 runtime/ 仍不完整")
    return True


def ensure_runtime(runtime: Path, base_url: str | None = None, config=None) -> bool:
    """确保 ``runtime/`` 可用；需要时按源优先级下载。

    顺序：GitHub 官方安装包 → 发布服务器。每条源单独失败不影响下一条，只有全部失败才抛
    :class:`RuntimeDownloadError`，消息里按顺序列出每条源失败的原因——"两条都试过"和
    "第一条没试"是完全不同的诊断信息。

    返回 ``True`` 表示这次做了下载，``False`` 表示原本就可用或已关闭下载。

    ``base_url`` / ``QSICK_RUNTIME_URL`` 给定时只用那一条源：调用方明确指定了地址，就不该再
    擅自回退到别的地址。
    """
    runtime = Path(runtime)
    if runtime_ready(runtime):
        return False
    if not _enabled(config):
        _log().info("已关闭 runtime 自动下载，跳过")
        return False

    explicit = (base_url or os.environ.get("QSICK_RUNTIME_URL") or "").strip()
    _log().info("runtime 缺失，准备下载", runtime=str(runtime),
                pinned=explicit or "(无，按优先级)")

    attempts: list[str] = []
    if explicit:
        sources = [("服务器(指定)", lambda: _fetch_from_server(runtime, explicit.rstrip("/")))]
    else:
        sources = [
            ("GitHub", lambda: _fetch_from_github(runtime)),
            ("服务器", lambda: _fetch_from_server(runtime, DEFAULT_BASE_URL.rstrip("/"))),
        ]

    for label, run in sources:
        try:
            run()
        except Exception as exc:  # noqa: BLE001 - 一条源失败就换下一条，理由记下来
            reason = str(exc) or type(exc).__name__
            _log().warn("下载源失败", source=label, reason=reason[:220])
            attempts.append(f"{label}: {reason[:220]}")
            continue
        _log().info("runtime 就绪", runtime=str(runtime), source=label)
        return True

    raise RuntimeDownloadError(
        "runtime/ 无法就绪，所有下载源都失败：" + "；".join(attempts)
    )
