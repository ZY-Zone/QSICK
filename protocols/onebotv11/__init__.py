"""纯 OneBot v11 协议实现。

对外只暴露两样东西：适配器 :class:`OneBotV11Protocol`，和它在
:mod:`protocols.base` 里继承的契约。需要更底层的段/CQ 转换时直接用
:mod:`protocols.onebotv11.codec`，不必绕适配器。
"""

from __future__ import annotations

from .adapter import OneBotV11Protocol, create, from_runtime
from .codec import (
    escape_param,
    escape_text,
    parse_cq_string,
    part_to_segment,
    parts_to_segments,
    render_cq_string,
    segment_to_part,
    segments_to_parts,
    unescape,
)

__all__ = [
    "OneBotV11Protocol",
    "create",
    "from_runtime",
    "escape_param",
    "escape_text",
    "parse_cq_string",
    "part_to_segment",
    "parts_to_segments",
    "render_cq_string",
    "segment_to_part",
    "segments_to_parts",
    "unescape",
]
