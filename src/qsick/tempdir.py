"""项目内的临时目录：所有临时产物都放这里，不散落到系统 %TEMP%。

为什么集中：系统临时目录里的文件既看不见也管不着——调试一次上传、跑一次解包，
就在 ``%TEMP%`` 留下几百 MB，谁也不知道是谁的。项目自己的 ``temp/`` 是可见的、
可整体清理的，而且已经在 .gitignore 里。

唯一的例外是**跨进程约定**：管道认证令牌必须写在 payload（预编译 DLL，读的是
系统临时目录）能找到的位置，见 ``transport/session.py``。那里保留系统路径不是
疏漏，改了会让注入握手失败。
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

#: 项目根下的临时目录名。
DIR_NAME = "temp"

#: 环境变量：把临时目录指到别处（CI、只读检出、多实例并行测试）。
ENV_VAR = "QSICK_TEMP_DIR"


def project_root() -> Path:
    """仓库根目录（``src/qsick/tempdir.py`` 往上三层）。"""
    return Path(__file__).resolve().parents[2]


def temp_dir(create: bool = True) -> Path:
    """返回项目临时目录，必要时创建。

    环境变量 ``QSICK_TEMP_DIR`` 优先——测试要并行跑时可以把每个 worker 指到
    不同的目录，免得互删文件。
    """
    override = os.environ.get(ENV_VAR, "").strip()
    path = Path(override).expanduser() if override else project_root() / DIR_NAME
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def subdir(name: str, create: bool = True) -> Path:
    """临时目录下的一个分类子目录（例如 ``uploads``、``runtime``）。"""
    path = temp_dir(create=create) / name
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def cleanup(*names: str) -> list[Path]:
    """删除临时目录下的指定子目录，返回真正删掉的路径。

    只删 ``temp/`` 里面、且只删调用方点名的子目录：这个目录里可能还放着别人
    手工留下的排查素材，一律清空太粗暴。
    """
    removed: list[Path] = []
    root = temp_dir(create=False)
    for name in names:
        target = (root / name).resolve()
        try:
            target.relative_to(root.resolve())
        except ValueError:
            # 越界（``..`` 之类）直接拒绝，别把删除做成任意路径操作。
            continue
        if not target.is_dir():
            continue
        shutil.rmtree(target, ignore_errors=True)
        removed.append(target)
    return removed


def make_file(prefix: str = "", suffix: str = "", category: str = "misc") -> tempfile.NamedTemporaryFile:
    """在项目临时目录里开一个唯一文件（调用方负责关闭与删除）。

    等价于 ``tempfile.NamedTemporaryFile``，只是落在 ``temp/<category>/`` 下，
    这样出问题能直接在仓库里找到它。
    """
    directory = subdir(category)
    return tempfile.NamedTemporaryFile(
        prefix=prefix, suffix=suffix, dir=str(directory), delete=False)
