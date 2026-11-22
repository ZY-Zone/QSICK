r"""按 ``表情索引.csv`` 重建 QSICK 的表情库。

旧的 ``assets/qq-faces`` 是历代累积的快照：文件名与内容对不上、来源无法追溯、
同一 ID 有多份互相矛盾的资源。这里改成**单一可信来源**：上游语料
``<语料目录>`` 的分类索引 CSV，逐行导入，不做任何推测。

导入产出（全部写入 ``assets/qq-faces``）：

- ``<id>.png`` / ``<字符>.png`` —— 按索引的 ``表情ID`` 命名，供查图
- ``super/<id>/`` —— 超级表情的全部形态（png / apng / lottie）
- ``index.json`` —— 索引条目，包含分类、名称、Unicode、ID、原始文件、说明
- ``face_config.json`` —— 从索引推导出的表情表（QSid/QDes/EMCode…）

用法::

    python tools/import_emotions.py --source <语料目录> [--dry-run]
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "assets" / "qq-faces"
# 表情语料不随仓库分发（体积大且属于第三方资源），导入时用 --source 指定，
# 或设 QSICK_EMOTION_SOURCE 环境变量。默认值只是一个占位，不存在时会明确报错。
DEFAULT_SOURCE = Path(os.environ.get("QSICK_EMOTION_SOURCE", "qq-emotions"))

# 索引里的分类名 -> 库里使用的稳定键
CATEGORY_KEYS = {
    "小黄脸": "face",
    "超级表情": "super",
    "超级表情-拓展": "super",
    "emoji": "emoji",
}


def _head(path: Path, limit: int = 65536) -> bytes:
    try:
        with path.open("rb") as handle:
            return handle.read(limit)
    except OSError:
        return b""


def sniff(head: bytes) -> str:
    """按魔数判定真实格式。索引里的 ``.png`` 有不少其实是 APNG。"""
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        actl, idat = head.find(b"acTL"), head.find(b"IDAT")
        return "apng" if actl >= 0 and (idat < 0 or actl < idat) else "png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[:3] == b"\xff\xd8\xff":
        return "jpg"
    if head[:2] == b"BM":
        return "bmp"
    if head.lstrip()[:1] == b"{":
        return "lottie"
    return "other"


def png_dims(head: bytes) -> tuple[int, int] | None:
    if head[:8] != b"\x89PNG\r\n\x1a\n" or len(head) < 24:
        return None
    return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")


def apng_frames(head: bytes) -> int:
    actl = head.find(b"acTL")
    if actl < 0 or actl + 8 > len(head):
        return 0
    return int.from_bytes(head[actl + 4:actl + 8], "big")


def read_rows(source: Path) -> list[dict[str, str]]:
    index = source / "表情索引.csv"
    if not index.is_file():
        raise SystemExit(f"索引不存在: {index}")
    with index.open(encoding="utf-8-sig", newline="") as handle:
        return [row for row in csv.DictReader(handle) if (row.get("表情ID") or "").strip()]


def describe_row(row: dict[str, str], source: Path) -> dict[str, object]:
    """把一行索引变成一条库记录，缺失或可疑的地方照实写出来。"""
    category = row["分类"].strip()
    identifier = row["表情ID"].strip()
    name = (row.get("名称") or "").strip()
    unicode_text = (row.get("Unicode") or "").strip()
    relative = (row.get("文件") or "").strip()
    note = (row.get("说明") or "").strip()
    path = source / relative.replace("\\", "/")

    record: dict[str, object] = {
        "id": identifier,
        "category": category,
        "kind": CATEGORY_KEYS.get(category, "other"),
        "name": name,
        "unicode": unicode_text,
        "file": relative,
        "note": note,
        "exists": path.is_file(),
    }
    if not path.is_file():
        return record

    head = _head(path)
    file_type = sniff(head)
    record["fileType"] = file_type
    record["size"] = path.stat().st_size
    record["md5"] = hashlib.md5(path.read_bytes()).hexdigest()
    dims = png_dims(head)
    if dims:
        record["width"], record["height"] = dims
    if file_type == "apng":
        frames = apng_frames(head)
        if frames:
            record["frameCount"] = frames
    record["moveable"] = file_type in ("apng", "gif", "webp", "lottie")

    # 完整版表情的说明里会点名同名 lottie / apng，按说明把它们收进来，
    # 这样超级表情的动态形态不会丢。两种写法都要认：
    #   02_超级表情  -> "Lottie 动画：01_超级赞.json"（同级目录，只有文件名）
    #   06_拓展      -> "Lottie 动画：...\419\lottie\419.json"（完整相对路径）
    extras: dict[str, str] = {}
    for marker, label in (("Lottie 动画：", "lottie"), ("APNG：", "apng")):
        if marker not in note:
            continue
        fragment = note.split(marker, 1)[1].split("；", 1)[0].strip()
        if not fragment:
            continue
        for candidate in (source / fragment.replace("\\", "/"), path.parent / fragment.replace("\\", "/")):
            if candidate.is_file():
                extras[label] = str(candidate.relative_to(source))
                break
    # 拓展目录把动画版单独放在 apng/ 下：主图只是静态首帧，真正的 APNG 在那里。
    # 不取它会把动态表情全判成静态。
    apng_dir = path.parent.parent / "apng" / path.name
    if "apng" not in extras and apng_dir.is_file():
        extras["apng"] = str(apng_dir.relative_to(source))
    if extras:
        record["extras"] = extras
    # 主呈现应是动画：取 apng/lottie 的真实格式，而不是静态首帧的名字。
    if "apng" in extras:
        animated_path = source / extras["apng"].replace("\\", "/")
        animated_head = _head(animated_path)
        record["fileType"] = sniff(animated_head)
        record["moveable"] = True
        record["size"] = animated_path.stat().st_size
        dims = png_dims(animated_head)
        if dims:
            record["width"], record["height"] = dims
        frames = apng_frames(animated_head)
        if frames:
            record["frameCount"] = frames
    elif "lottie" in extras:
        record["fileType"] = "lottie"
        record["moveable"] = True
    return record


def load_sticker_triples() -> dict[str, dict[str, str]]:
    """QQ 侧的贴纸三元组（``AniSticker*``）。

    索引 CSV 只描述资源文件，不含发送动态表情所必需的包/贴纸编号。这些编号
    不来自推测，而是 QQ 自己维护的 ``face_config.json``：它随客户端分发，是
    发送时的权威映射。导入时把能对上索引 id 的条目合并进来，对不上的丢弃。
    """
    for candidate in (ROOT / "assets" / "qq-faces" / "face_config_qq.json",
                      ROOT / "assets" / "qq-faces" / "face_config.json"):
        if not candidate.is_file():
            continue
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        triples: dict[str, dict[str, str]] = {}
        for entry in payload.get("sysface") or []:
            if not isinstance(entry, dict):
                continue
            qsid = str(entry.get("QSid") or "").strip()
            pack = str(entry.get("AniStickerPackId") or "").strip()
            sticker = str(entry.get("AniStickerId") or "").strip()
            sticker_type = str(entry.get("AniStickerType") or "").strip()
            if qsid and pack and sticker and sticker_type:
                triples[qsid] = {
                    "AniStickerPackId": pack,
                    "AniStickerId": sticker,
                    "AniStickerType": sticker_type,
                    "AniStickerPackName": str(entry.get("AniStickerPackName") or ""),
                    "EMCode": str(entry.get("EMCode") or ""),
                }
        if triples:
            return triples
    return {}


def build_face_config(records: list[dict[str, object]]) -> dict[str, list[dict[str, str]]]:
    """从索引推导表情表，并合并 QQ 的贴纸三元组。

    不再整份抄 QQ 的 ``face_config``：那份表缺 419+ 等新表情，且与索引的命名
    不一致。但它的 ``AniSticker*`` 是发送动态表情必需的，所以按 id 合并进来。
    """
    triples = load_sticker_triples()
    sysface: list[dict[str, str]] = []
    emoji: list[dict[str, str]] = []
    for record in records:
        if not record.get("exists"):
            continue
        kind = record.get("kind")
        identifier = str(record.get("id") or "")
        if kind == "emoji":
            emoji.append({
                "QSid": str(record.get("unicode") or identifier),
                "QCid": identifier,
                "QDes": "/" + str(record.get("name") or ""),
                "EMCode": identifier,
                "AniStickerType": "",
                "AniStickerPackId": "",
                "AniStickerId": "",
                "fileType": str(record.get("fileType") or ""),
                "moveable": False,
                "category": str(record.get("category") or ""),
            })
            continue
        entry: dict[str, str] = {
            "QSid": identifier,
            "QDes": "/" + str(record.get("name") or ""),
            "EMCode": "",
            "AniStickerType": "",
            "AniStickerPackId": "",
            "AniStickerId": "",
            "AniStickerPackName": "",
            "fileType": str(record.get("fileType") or ""),
            "moveable": bool(record.get("moveable")),
            "category": str(record.get("category") or ""),
        }
        sticker = triples.get(identifier)
        if sticker:
            entry.update(sticker)
        sysface.append(entry)
    return {"sysface": sysface, "emoji": emoji}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default=str(DEFAULT_SOURCE))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    source = Path(args.source)
    if not source.is_dir():
        raise SystemExit(f"语料目录不存在: {source}")
    rows = read_rows(source)
    records = [describe_row(row, source) for row in rows]

    missing = [r for r in records if not r.get("exists")]
    print(f"index rows: {len(records)}  missing files: {len(missing)}")
    for item in missing[:5]:
        print("  MISSING", item.get("file"))

    if args.dry_run:
        for record in records[:5]:
            print(json.dumps(record, ensure_ascii=False))
        return 0

    # 清理旧库：旧文件与索引对不上，留着只会让"这个 id 到底是哪张图"无从判定。
    if TARGET.is_dir():
        for child in TARGET.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
    TARGET.mkdir(parents=True, exist_ok=True)

    copied = 0
    for record in records:
        if not record.get("exists"):
            continue
        relative = str(record["file"]).replace("\\", "/")
        source_path = source / relative
        identifier = str(record["id"])
        unicode_text = str(record.get("unicode") or "")
        # 主文件名：emoji 用字符（配置以字符为键），其余用数字 id。
        stem = unicode_text or identifier
        suffix = source_path.suffix.lower() or ".png"
        destination = TARGET / f"{stem}{suffix}"
        try:
            shutil.copy2(source_path, destination)
            copied += 1
        except OSError as exc:
            print(f"  copy failed {relative}: {exc}", file=sys.stderr)
            continue

        extras = record.get("extras") or {}
        if extras:
            bundle = TARGET / "super" / identifier
            bundle.mkdir(parents=True, exist_ok=True)
            for label, extra_relative in extras.items():
                extra_source = source / str(extra_relative).replace("\\", "/")
                if not extra_source.is_file():
                    continue
                extra_suffix = extra_source.suffix.lower()
                name = f"{identifier}.{label}{extra_suffix}" if label == "lottie" else f"{identifier}.apng.png"
                try:
                    shutil.copy2(extra_source, bundle / name)
                except OSError:
                    continue

    (TARGET / "index.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8")
    config = build_face_config(records)
    (TARGET / "face_config.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"copied: {copied}")
    print(f"sysface: {len(config['sysface'])}  emoji: {len(config['emoji'])}")
    print(f"wrote {TARGET.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
