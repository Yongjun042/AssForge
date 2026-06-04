"""core.typeset — Visual Typesetting 지오메트리(순수 코어).

mpv 오버레이 위젯이 이 함수들로 \\pos/\\move/\\frz/\\org/\\clip 을 갱신한다.
"""
from __future__ import annotations

from core.typeset.geometry import (
    anchor_position,
    apply_move,
    clear_clip,
    effective_position,
    get_clip_rect,
    get_org,
    get_position,
    get_rotation,
    set_clip_rect,
    set_org,
    set_position,
    set_rotation,
)

__all__ = [
    "anchor_position",
    "effective_position",
    "get_position",
    "set_position",
    "apply_move",
    "get_rotation",
    "set_rotation",
    "get_org",
    "set_org",
    "get_clip_rect",
    "set_clip_rect",
    "clear_clip",
]
