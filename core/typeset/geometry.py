"""Visual Typesetting 지오메트리 코어 — \\pos/\\move/\\frz/\\org/\\clip 읽기·쓰기.

순수 함수. mpv 오버레이 위젯(드래그/회전 핸들)은 이 함수들을 호출해 이벤트
텍스트를 갱신하고, UI 는 UpdateEventCommand 로 적용한다(= undo 가능).

좌표/시간은 ASS 의 화면 좌표(play_res 기준), 시간은 ms. 숫자는 정수면 정수로,
아니면 소수로 직렬화해 round-trip 노이즈를 줄인다.
"""
from __future__ import annotations

import re
from typing import Any

from core.ass.tag_tokenizer import find_tag, remove_tag, upsert_tag

_NUMS = re.compile(r"-?\d+(?:\.\d+)?")


def _fmt(v: Any) -> str:
    f = float(v)
    return str(int(f)) if f == int(f) else f"{f:.3f}".rstrip("0").rstrip(".")


def _nums(args: str) -> list[float]:
    return [float(m) for m in _NUMS.findall(args or "")]


# ---- 정렬 앵커 좌표 (pos 가 없을 때 드래그 시작점 계산) -----------------

def anchor_position(
    play_res_x: int,
    play_res_y: int,
    alignment: int,
    margin_l: int = 10,
    margin_r: int = 10,
    margin_v: int = 10,
) -> tuple[float, float]:
    """numpad 정렬(1~9)과 여백으로부터 기본 앵커 좌표를 계산."""
    col = (alignment - 1) % 3      # 0=left,1=center,2=right
    row = (alignment - 1) // 3     # 0=bottom,1=middle,2=top
    if col == 0:
        x = margin_l
    elif col == 1:
        x = play_res_x / 2
    else:
        x = play_res_x - margin_r
    if row == 0:
        y = play_res_y - margin_v
    elif row == 1:
        y = play_res_y / 2
    else:
        y = margin_v
    return float(x), float(y)


# ---- 위치 (\pos / \move) ---------------------------------------------

def get_position(text: str) -> tuple[float, float] | None:
    """\\pos 가 있으면 그 좌표, 없으면 \\move 의 시작점. 둘 다 없으면 None."""
    t = find_tag(text, "pos")
    if t is not None:
        ns = _nums(t.args)
        if len(ns) >= 2:
            return ns[0], ns[1]
    m = find_tag(text, "move")
    if m is not None:
        ns = _nums(m.args)
        if len(ns) >= 2:
            return ns[0], ns[1]
    return None


def effective_position(
    text: str,
    alignment: int,
    play_res_x: int,
    play_res_y: int,
    margin_l: int = 10,
    margin_r: int = 10,
    margin_v: int = 10,
) -> tuple[float, float]:
    """드래그 시작용 — \\pos/\\move 가 없으면 정렬 앵커로 폴백."""
    pos = get_position(text)
    if pos is not None:
        return pos
    return anchor_position(play_res_x, play_res_y, alignment, margin_l, margin_r, margin_v)


def set_position(text: str, x: float, y: float) -> str:
    """\\pos(x,y) 설정. 충돌하는 \\move 는 제거."""
    cleaned = remove_tag(text, "move")
    return upsert_tag(cleaned, "pos", f"({_fmt(x)},{_fmt(y)})")


def apply_move(
    text: str,
    x1: float, y1: float, x2: float, y2: float,
    t1: int | None = None, t2: int | None = None,
) -> str:
    """\\move 설정. 충돌하는 \\pos 는 제거. t1,t2 가 주어지면 시간 구간 포함."""
    cleaned = remove_tag(text, "pos")
    if t1 is not None and t2 is not None:
        args = f"({_fmt(x1)},{_fmt(y1)},{_fmt(x2)},{_fmt(y2)},{int(t1)},{int(t2)})"
    else:
        args = f"({_fmt(x1)},{_fmt(y1)},{_fmt(x2)},{_fmt(y2)})"
    return upsert_tag(cleaned, "move", args)


# ---- 회전 (\frz) + 원점 (\org) ---------------------------------------

def get_rotation(text: str) -> float:
    t = find_tag(text, "frz")
    if t is None:
        t = find_tag(text, "fr")  # \fr 은 \frz 의 별칭
    if t is not None:
        ns = _nums(t.args)
        if ns:
            return ns[0]
    return 0.0


def set_rotation(text: str, degrees: float) -> str:
    return upsert_tag(text, "frz", _fmt(degrees))


def get_org(text: str) -> tuple[float, float] | None:
    t = find_tag(text, "org")
    if t is not None:
        ns = _nums(t.args)
        if len(ns) >= 2:
            return ns[0], ns[1]
    return None


def set_org(text: str, x: float, y: float) -> str:
    return upsert_tag(text, "org", f"({_fmt(x)},{_fmt(y)})")


# ---- 사각 클립 (\clip 4-인자 형태만) ----------------------------------

def get_clip_rect(text: str) -> tuple[float, float, float, float] | None:
    """\\clip(x1,y1,x2,y2) 사각형만 해석. 드로잉 클립이면 None."""
    t = find_tag(text, "clip")
    if t is not None:
        ns = _nums(t.args)
        if len(ns) == 4:
            return ns[0], ns[1], ns[2], ns[3]
    return None


def set_clip_rect(text: str, x1: float, y1: float, x2: float, y2: float) -> str:
    lo_x, hi_x = sorted((x1, x2))
    lo_y, hi_y = sorted((y1, y2))
    args = f"({_fmt(lo_x)},{_fmt(lo_y)},{_fmt(hi_x)},{_fmt(hi_y)})"
    return upsert_tag(text, "clip", args)


def clear_clip(text: str) -> str:
    return remove_tag(text, "clip")
