"""Regressions for standard v11 protocol gaps found in the audit."""
from __future__ import annotations

import pytest

from qsick.onebot.actions.base import cq_to_segment, parse_cq
from qsick.onebot.actions.system.get_version_info import _handle as version_info


@pytest.mark.parametrize(
    "cq,expected_type,must_have",
    [
        ("[CQ:json,data=%7B%22a%22%3A1%7D]", "json", "a"),
        ("[CQ:xml,data=%3Cx%3E1%3C/x%3E]", "xml", "x"),
        ("[CQ:dice,result=3]", "dice", "result"),
        ("[CQ:rps,result=2]", "rps", "result"),
        ("[CQ:contact,type=qq,id=123456]", "contact", "id"),
        ("[CQ:location,lat=1.5,lon=2.5,title=t]", "location", "title"),
        ("[CQ:music,type=163,id=42]", "music", "id"),
        ("[CQ:mface,emoji_id=e1,summary=s]", "mface", "emoji_id"),
        ("[CQ:markdown,data=hello]", "markdown", "hello"),
    ],
)
def test_standard_cq_segments_are_parsed(cq, expected_type, must_have):
    segments = parse_cq(cq)
    assert len(segments) == 1
    segment = segments[0]
    assert segment["type"] == expected_type
    assert must_have in str(segment["data"])


def test_unknown_cq_type_is_not_silently_claimed_as_supported():
    assert cq_to_segment("invented_type", {"x": "1"})["type"] == "unknown"


async def test_version_info_reports_real_app_version():
    data = await version_info(None)
    assert data["protocol_version"] == "v11"
    assert data["app_version"]
    assert data["app_version"] != ""
