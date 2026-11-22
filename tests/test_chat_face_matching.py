"""Faces must survive both directions of the composer.

Two measured defects are pinned here: QQ delivers a system face as
``<faceType=1,faceId="102",ext="...">`` markup, and QQ matches a whole column of
faces by the Unicode character itself. Getting either wrong shows text where the
client shows a face, which is exactly the mismatch being fixed.
"""

from __future__ import annotations

import base64
import json

import pytest

from qsick.webui.chat_api import _encode_chat_parts
from qsick.webui.face_catalog import emoji_index, face_send_meta, glyph_faces


def _kinds(segments):
    return [(item["type"], item["data"].get("id") or item["data"].get("text")) for item in segments]


def test_cq_code_still_becomes_a_face():
    segments = _encode_chat_parts("[CQ:face,id=5]")
    assert _kinds(segments) == [("face", "5")]
    # Animated faces need the sticker triple or QQ drops them.
    assert segments[0]["data"]["sticker_type"] == 1
    assert segments[0]["data"]["sticker_id"] == "16"


def test_a_bare_glyph_is_sent_as_that_face():
    # QQ keys a whole column by the character, so a pasted glyph must not go out
    # as ordinary text.
    glyph = "☺"
    segments = _encode_chat_parts(f"你好{glyph}再见")
    assert _kinds(segments) == [("text", "你好"), ("face", glyph), ("text", "再见")]
    assert segments[1]["data"]["name"] == "可爱"


def test_plain_text_is_untouched():
    for text in ("普通消息", "no faces", "a < b"):
        segments = _encode_chat_parts(text)
        assert _kinds(segments) == [("text", text)]


def test_glyphs_do_not_collide_with_normal_characters():
    glyphs = glyph_faces()
    assert glyphs, "the glyph column must be present"
    # Nothing ASCII, and no common Chinese character, may be swallowed as a face.
    assert not any(ord(char) < 0x2000 for char in glyphs)
    for char in "的一是不了在人有我他这为之大来以个中上们":
        assert char not in glyphs


def test_glyph_column_is_indexed_for_sending():
    index = emoji_index()
    glyph_keys = [key for key in index if not key.isdigit()]
    assert len(glyph_keys) > 150
    meta = face_send_meta("☺")
    assert meta["id"] == "☺"
    assert meta["text"] == "可爱"


def test_decoded_markup_matches_the_face_qq_named():
    # The live payload decoded to {"text":"左哼哼"}, and face 102 is that face.
    payload = '<faceType=1,faceId="102",ext="eyJ0ZXh0Ijoi5bem5ZO85ZO8In0=">'
    ext = payload.split('ext="')[1].rstrip('">')
    decoded = json.loads(base64.b64decode(ext + "=" * (-len(ext) % 4)).decode("utf-8"))
    assert decoded == {"text": "左哼哼"}
    assert face_send_meta("102")["text"] == "左哼哼"
