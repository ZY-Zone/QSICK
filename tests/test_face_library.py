"""The bundled face library must match QQ's own client, entry for entry.

These assertions are what keep the picker honest: every face QQ names is named
the same way here, every one of them has art, and every one is reachable from a
picker column. A regression in any of those silently shows placeholders or the
wrong label, which is exactly what this project must not do.
"""

from __future__ import annotations

import json

import pytest

from qsick.webui import face_catalog
from qsick.webui.face_assets import EMBEDDED_ROOT
from qsick.webui.face_table import face_descriptor

CONFIG = EMBEDDED_ROOT / "face_config.json"
QQ_CONFIG = EMBEDDED_ROOT / "face_config_qq.json"


def _entries(path):
    if not path.is_file():
        pytest.skip(f"{path.name} is absent")
    return json.loads(path.read_text(encoding="utf-8"))


def _name(entry):
    return str(entry.get("QDes") or "").lstrip("/").strip()


def _ids(path):
    payload = _entries(path)
    return [str(item) for item in payload.get("emojiids") or []]


def test_every_face_qq_names_is_named_identically():
    qq = _entries(QQ_CONFIG)
    checked = 0
    for group in ("sysface", "emoji"):
        for entry in qq.get(group) or []:
            face_id = str(entry.get("QSid") or "")
            expected = _name(entry)
            if not face_id or not expected:
                continue
            assert face_descriptor(face_id)["name"] == expected, face_id
            checked += 1
    assert checked > 400


def test_every_configured_face_has_art():
    config = _entries(CONFIG)
    if not any(EMBEDDED_ROOT.glob("*.png")):
        # 表情图片（约 58 MB）不随仓库分发：它们是第三方二进制资源，用
        # `python tools/import_emotions.py --source <语料目录>` 生成。
        # 没有资源时跳过，而不是把"没导入"报成"库是坏的"。
        pytest.skip("QQ 表情图片未导入；运行 tools/import_emotions.py 后可验证")
    missing = []
    for group in ("sysface", "emoji"):
        for entry in config.get(group) or []:
            face_id = str(entry.get("QSid") or "")
            if not face_id:
                continue
            image = EMBEDDED_ROOT / f"{face_id}.png"
            if not image.is_file() or image.stat().st_size == 0:
                missing.append(face_id)
    assert missing == [], f"faces without art: {missing[:20]}"


def test_every_grouped_face_is_visible_and_has_art():
    groups = [
        ("normal", _ids(EMBEDDED_ROOT / "normal_emojiids.json")),
        ("super", _ids(EMBEDDED_ROOT / "super_emojiids.json")),
        ("special", _ids(EMBEDDED_ROOT / "redheart_emojiids.json")),
    ]
    for label, ids in groups:
        assert ids, f"{label} column is empty"
        artless = [i for i in ids if not (EMBEDDED_ROOT / f"{i}.png").is_file()]
        assert artless == [], f"{label} lists ids without art: {artless[:20]}"


def test_no_face_with_art_is_left_out_of_the_picker():
    config = _entries(CONFIG)
    grouped = set()
    for filename in ("normal_emojiids.json", "super_emojiids.json", "redheart_emojiids.json"):
        grouped.update(_ids(EMBEDDED_ROOT / filename))
    orphans = []
    for group in ("sysface", "emoji"):
        for entry in config.get(group) or []:
            face_id = str(entry.get("QSid") or "")
            if face_id and (EMBEDDED_ROOT / f"{face_id}.png").is_file() and face_id not in grouped:
                orphans.append(face_id)
    assert orphans == [], f"faces with art but not in any column: {orphans[:20]}"


def test_catalog_exposes_the_whole_library():
    catalogue = face_catalog.catalog("")
    total = sum(len(faces) for faces in catalogue.values())
    # The library is large; a collapse back to a few dozen means the columns or
    # the art disappeared.
    assert total > 500, total
    for key in ("normal", "super", "emoji"):
        assert catalogue[key], key
    ids = {face["id"] for faces in catalogue.values() for face in faces}
    # Both numeric system faces and glyph-keyed emoji faces must survive.
    assert any(item.isdigit() for item in ids)
    assert any(not item.isdigit() for item in ids)

def test_catalog_is_fast_enough_for_the_picker():
    """The whole catalogue must build quickly.

    describe() runs once per face (500+). An earlier version did a filesystem
    lookup per face, which pushed this to ~17s and left the picker blank because
    the browser gave up waiting.
    """
    import time

    started = time.perf_counter()
    catalogue = face_catalog.catalog("")
    elapsed = time.perf_counter() - started
    assert sum(len(v) for v in catalogue.values()) > 400
    assert elapsed < 5.0, f"catalog took {elapsed:.1f}s"
