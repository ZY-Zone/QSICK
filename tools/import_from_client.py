"""把本机 QQ 客户端里的表情资源补进 ``assets/qq-faces``。

为什么需要这个脚本：表情图片不随仓库分发（约 58 MB 第三方二进制，见 .gitignore），
所以新克隆的仓库里没有它们。运行时 WebUI 会**优先**从每个实例自己的 QQ 数据目录
读表情（``webui/face_assets.py`` 的 ``client_face_path``），内置库只是兜底：

    数字表情：QQ 客户端资源能覆盖约 65%，剩下 35% 才落到内置库
    emoji 字符表情：客户端资源树里没有，全部依赖内置库

因此"只有客户端资源"时数字表情是残缺的、emoji 是全空的。这个脚本把客户端里
已有的原图抓进内置库，补上那一块。

用法::

    python tools/import_from_client.py                      # 自动找已登录实例
    python tools/import_from_client.py --headless-root <目录>
    python tools/import_from_client.py --dry-run            # 只看能补多少，不写盘

它只复制**本机 QQ 客户端自己下载的**资源，不联网、不生成、不推测：抓不到的表情
就留空，绝不拿一张来源不明的图冒充（``face_assets.verified_face_path`` 也会挡住
索引里没有的 id）。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "assets" / "qq-faces"
INDEX = TARGET / "index.json"

#: 与 ``src/qsick/webui/face_assets.py`` 保持一致的两处资源位置。
ACCOUNT_TREE = Path("nt_data") / "Emoji" / "BaseEmojiSyastems" / "EmojiSystermResource"


def load_index() -> list[dict[str, object]]:
    if not INDEX.is_file():
        raise SystemExit(f"缺少索引文件: {INDEX}")
    try:
        payload = json.loads(INDEX.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"索引无法解析: {exc}") from exc
    if not isinstance(payload, list):
        raise SystemExit("索引必须是 JSON 数组")
    return [item for item in payload if isinstance(item, dict)]


def wanted_names(records: list[dict[str, object]]) -> dict[str, str]:
    """内置库里的文件名 -> 用于日志的标识。

    命名规则与 ``tools/import_emotions.py`` 一致：emoji 用字符当文件名，其余用数字 id。
    """
    names: dict[str, str] = {}
    for record in records:
        identifier = str(record.get("id") or "").strip()
        if not identifier:
            continue
        stem = str(record.get("unicode") or "").strip() or identifier
        for suffix in (".png", ".gif", ".webp"):
            names[f"{stem}{suffix}"] = identifier
    return names


def scan_client(root: Path) -> dict[str, Path]:
    """扫一遍实例数据，返回 ``文件名 -> 源路径``。

    只收 ``<id>.png`` 与 ``s<id>.png`` 两种命名，映射成内置库用的 ``<id>.png``。
    同一 id 出现多次时取最大的那个文件：动图/高清版通常更大，静态缩略图更小。
    """
    found: dict[str, tuple[int, Path]] = {}

    def consider(face_id: str, path: Path) -> None:
        try:
            size = path.stat().st_size
        except OSError:
            return
        if size <= 0:
            return
        name = f"{face_id}.png"
        if name not in found or size > found[name][0]:
            found[name] = (size, path)

    if not root.is_dir():
        return {}

    for instance in root.iterdir():
        if not instance.is_dir() or not re.fullmatch(r"C\d+", instance.name):
            continue

        # 账号级：.../EmojiSystermResource/<id>/png/<id>.png
        for resource in instance.rglob("EmojiSystermResource"):
            if not resource.is_dir():
                continue
            for folder in resource.iterdir():
                if not folder.is_dir() or not re.fullmatch(r"\d{1,4}", folder.name):
                    continue
                face_id = str(int(folder.name))
                for candidate in (folder / "png" / f"{folder.name}.png", folder / f"{folder.name}.png"):
                    if candidate.is_file():
                        consider(face_id, candidate)

        # 全局：.../emoji-resource/sysface_res/{apng,static}/s<id>.png
        for res in instance.rglob("sysface_res"):
            if not res.is_dir():
                continue
            for variant in ("apng", "static"):
                folder = res / variant
                if not folder.is_dir():
                    continue
                for png in folder.glob("s*.png"):
                    match = re.fullmatch(r"s(\d{1,4})\.png", png.name)
                    if match:
                        consider(str(int(match.group(1))), png)

    return {name: path for name, (_, path) in found.items()}


def main() -> int:
    parser = argparse.ArgumentParser(description="从本机 QQ 客户端补充表情图片")
    parser.add_argument("--headless-root", default=str(ROOT / "headlessClient"),
                        help="实例目录（默认 headlessClient）")
    parser.add_argument("--dry-run", action="store_true", help="只报告，不写文件")
    args = parser.parse_args()

    records = load_index()
    names = wanted_names(records)
    available = scan_client(Path(args.headless_root))

    TARGET.mkdir(parents=True, exist_ok=True)

    copied = 0
    for name, source in sorted(available.items()):
        if name not in names:
            # 客户端里有、索引里没登记的 id：不导入。库里多一张来路不明的图，
            # 会让聊天里画出一张错的脸，比不显示更糟。
            continue
        destination = TARGET / name
        if destination.is_file() and destination.stat().st_size > 0:
            continue
        if args.dry_run:
            copied += 1
            continue
        try:
            shutil.copy2(source, destination)
            copied += 1
        except OSError as exc:
            print(f"  copy failed {name}: {exc}", file=sys.stderr)

    numeric = [str(r.get("id") or "") for r in records
               if str(r.get("id") or "").isdigit()]
    emoji = [r for r in records if str(r.get("unicode") or "").strip()]
    present = 0
    for record in records:
        stem = str(record.get("unicode") or "").strip() or str(record.get("id") or "")
        if stem and (TARGET / f"{stem}.png").is_file():
            present += 1

    verb = "可补充" if args.dry_run else "已补充"
    print(f"索引登记: {len(records)} 条（数字 {len(numeric)}，emoji {len(emoji)}）")
    print(f"客户端可见: {len(available)} 个表情文件")
    print(f"{verb}: {copied} 个")
    print(f"内置库当前: {present} / {len(records)}")
    if emoji:
        # emoji 的资源在客户端树里不以 id 命名，抓不到；内置库是它们的唯一来源。
        covered = sum(1 for r in emoji
                      if (TARGET / (str(r.get("unicode") or "").strip() + ".png")).is_file())
        print(f"  其中 emoji: {covered} / {len(emoji)}（客户端资源树不提供，只能靠内置库）")
    if args.dry_run:
        print("（--dry-run：未写入任何文件）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
