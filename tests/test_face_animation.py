"""Lottie 动画的定位与暴露它的路由。

动画来源是导入索引：``tools/import_emotions.py`` 把每个表情真实的 Lottie 文件
收进 ``super/<id>/``，并在 ``index.json`` 的 ``extras`` 里登记。运行时不再按
``qlottie/<pack>/<id>/<id>.json`` 硬拼路径——那套目录只存在于完整安装的客户端，
且需要 ``AniSticker*`` 三元组，而索引里的 419+ 等表情根本没有那些字段。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from qsick.webui import face_animation


def _write_index(root: Path, entries: list[dict]) -> None:
    (root / "index.json").write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")


def _write_lottie(root: Path, face_id: str, frames: int = 120, rate: int = 60) -> Path:
    target = root / "super" / face_id / f"{face_id}.lottie.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({
        "v": "5.7.8", "fr": rate, "ip": 0, "op": frames, "w": 512, "h": 512,
        "layers": [], "assets": [], "markers": [],
    }), encoding="utf-8")
    return target


@pytest.fixture()
def asset_root(tmp_path, monkeypatch):
    """把解析器指向一次性资源树。"""
    monkeypatch.setattr(face_animation, "EMBEDDED_ROOT", tmp_path)
    face_animation._INDEX_CACHE.clear()
    return tmp_path


def test_animation_resolves_for_an_animated_entry(asset_root):
    _write_index(asset_root, [
        {"id": "364", "name": "超级赞", "category": "超级表情", "moveable": True,
         "fileType": "lottie", "extras": {"lottie": "x/364.json"}},
    ])
    expected = _write_lottie(asset_root, "364")
    assert face_animation.animation_path("", "364") == expected


def test_static_face_has_no_animation(asset_root):
    _write_index(asset_root, [{"id": "14", "name": "微笑", "category": "小黄脸",
                               "moveable": False, "fileType": "png"}])
    assert face_animation.animation_path("", "14") is None
    assert face_animation.animation_payload("", "14") is None


def test_missing_file_is_reported_as_no_motion(asset_root):
    # 索引说有动画，文件却不在：报"没有动效"，而不是给客户端一个会 404 的 URL。
    _write_index(asset_root, [
        {"id": "9", "category": "超级表情", "moveable": True,
         "fileType": "lottie", "extras": {"lottie": "x/9.json"}},
    ])
    assert face_animation.animation_path("", "9") is None


def test_entry_without_lottie_extra_is_static(asset_root):
    # 只有 APNG 没有 Lottie 的条目不该被当成动画来源。
    _write_index(asset_root, [
        {"id": "466", "category": "超级表情-拓展", "moveable": True,
         "fileType": "apng", "extras": {"apng": "x/466.png"}},
    ])
    assert face_animation.animation_path("", "466") is None


def test_payload_reports_duration_from_the_document(asset_root):
    _write_index(asset_root, [
        {"id": "364", "category": "超级表情", "moveable": True,
         "fileType": "lottie", "extras": {"lottie": "x/364.json"}},
    ])
    _write_lottie(asset_root, "364", frames=120, rate=60)
    payload = face_animation.animation_payload("", "364")
    assert payload == {"url": "/api/chat/face/animation?id=364", "duration": 2.0}


def test_leading_zeroes_resolve_like_the_png_lookup(asset_root):
    _write_index(asset_root, [
        {"id": "5", "category": "超级表情", "moveable": True,
         "fileType": "lottie", "extras": {"lottie": "x/5.json"}},
    ])
    expected = _write_lottie(asset_root, "5")
    # "005" 与 "5" 是同一个表情；PNG 查找已经做了归一，动画查找必须一致。
    assert face_animation.animation_path("", "005") == expected
    assert face_animation.animation_path("", "5") == expected


def test_shipped_library_resolves_every_lottie_it_ships():
    """内置库里有 Lottie 的表情必须都能解析出文件。"""
    index = face_animation.EMBEDDED_ROOT / "index.json"
    if not index.is_file():
        pytest.skip("bundled index is absent")
    if not any(face_animation.EMBEDDED_ROOT.rglob("*.png")):
        # 表情图片（含 Lottie 资源，约 58 MB）不随仓库分发，见 .gitignore
        # 与 tools/import_emotions.py。资源未导入时跳过，而不是判"库是坏的"。
        pytest.skip("QQ 表情资源未导入；运行 tools/import_emotions.py 后可验证")
    entries = json.loads(index.read_text(encoding="utf-8"))
    animated = [item for item in entries
                if isinstance(item.get("extras"), dict) and "lottie" in item["extras"]]
    if not animated:
        pytest.skip("bundled index lists no lottie")
    resolved = [item for item in animated
                if face_animation.animation_path("", item.get("id")) is not None]
    assert len(resolved) == len(animated), (
        f"{len(animated) - len(resolved)} 个 Lottie 无法解析")


def test_animation_route_is_gated():
    source = (Path(face_animation.__file__).parents[1] / "webui" / "chat_api.py").read_text(encoding="utf-8")
    marker = '@app.get("/api/chat/face/animation")'
    assert marker in source
    body = source.split(marker, 1)[1].split('@app.get("/api/chat/face")', 1)[0]
    assert "denied(request)" in body
