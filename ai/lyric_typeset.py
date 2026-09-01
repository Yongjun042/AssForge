r"""가사 → 그래픽 우선 타이프셋 자막 계획 (완성본 형식).

수작업 완성본('밤하늘로 이어지는 언덕길' 레퍼런스) 실측 규칙을 자동화한다:
  · 시작 = 화면 가사 그래픽의 페이드 시작 — 보컬보다 1~2초 앞선다.
    보컬 정렬은 '어느 등장 이벤트가 이 줄 것인지' 고르는 사전정보로만 쓰고,
    등장 이벤트는 가사 순서대로 하나씩 소비한다 (창 안의 가장 늦은 것을
    고르면 다음 줄의 그래픽을 훔친다).
  · 끝 = 같은 자리 교체(다음 등장, 근접) 또는 근처 소멸. 같은 절에서 나뉜
    구들은 블록 페이드로 함께 끝난다.
  · 위치 = 등장 이벤트의 변화 영역 중심 (새 텍스트가 뜬 곳).
  · 밝은 장면(주간)은 검은 글자, 어두운 장면은 흰 글자 스타일.
  · 원문 1~2자 꼬리(글자 분할 연출)는 세로 스택 + 시차 등장, 공통 소멸.

00001 영상을 수작업 완성본과 구 단위 39줄로 비교한 실측: 시작 오차 중앙값
0.90s, 끝 1.20s. 상수들은 그 튜닝 결과다.
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from typing import Optional

from ai.alignment_song import LineAlignment
from ai.lyric_text import LyricPair
from media.video_analysis import GraphicEvent, LineVisual

# 완성본 형식의 스타일 이름 — 없으면 생성해서 쓴다.
LIGHT_STYLE = "가사 하양"
DARK_STYLE = "가사 검정"

_APPEAR_LOOKBACK_MS = 2500   # 그래픽은 보컬보다 이만큼까지 앞서 뜬다
_APPEAR_LOOKAHEAD_MS = 500
_END_BACKTRACK_MS = 800      # 보컬 끝 추정이 늦을 수 있어 그만큼 앞부터 탐색
_END_SEARCH_MS = 7000
_SWAP_DIST = 0.18            # 같은 자리 교체로 보는 중심 거리 (0..1 좌표)
_VANISH_DIST = 0.35
_TAIL_MAX_LETTERS = 1        # 이 이하 원문 = 글자 분할 연출 (駈/け/上/が/る).
                             # 2로 두면 끝머리의 정상 2자 구절(永遠 등)을 오판한다.
_DARK_BRIGHTNESS = 0.62      # 이보다 밝은 장면은 검은 글자
_MOVE_DRIFT = 0.02           # 그래픽 드리프트가 이보다 크면 \move


def lyric_style_props(dark: bool) -> dict:
    """완성본과 같은 꼴의 가사 스타일 속성 (서울한강체 B 96)."""
    from core.style.schema import default_style_props
    props = default_style_props()
    props.update(
        fontname="서울한강체 B",
        fontsize=96,
        bold=0,
        primary_colour="&H00000000" if dark else "&H00FFFFFF",
        # 완성본 패턴: 외곽선/그림자는 완전 투명 (텍스트만)
        outline_colour="&HFF000000",
        back_colour="&HFF000000",
    )
    return props


@dataclass(slots=True)
class PlannedLine:
    """생성할 이벤트 1줄 — 태그 포함 텍스트, 시간, 스타일."""
    text: str
    start_ms: int
    end_ms: int
    style: str
    via: str        # graphic|vocal|stack|gap — 시간의 근거 (통계/로그용)


@dataclass(slots=True)
class _Row:
    start: Optional[int] = None
    end: Optional[int] = None
    pos: Optional[tuple[float, float]] = None
    via: str = "-"
    stack: int = -1     # 글자 스택 인덱스 (해당 없으면 -1)


def _nletters(s: Optional[str]) -> int:
    return sum(1 for ch in (s or "")
               if unicodedata.category(ch)[0] in ("L", "N"))


def _dist(e: GraphicEvent, cx: float, cy: float) -> float:
    return ((e.cx - cx) ** 2 + (e.cy - cy) ** 2) ** 0.5


def plan_times(
    pairs: list[LyricPair],
    groups: list[int],
    aligns: list[Optional[LineAlignment]],
    events: list[GraphicEvent],
    vocal_end_ms: int,
) -> list[_Row]:
    """줄별 시작/끝/위치 계획. pairs·groups·aligns 는 병렬 리스트.

    Args:
        groups: 각 줄이 나온 원래 절 인덱스 — 같은 절이면 블록 공통 소멸.
        aligns: 보컬 정렬 결과 (정렬 대상이 아니면 None).
        events: detect_graphic_events 결과 (시간순).
        vocal_end_ms: 마지막 실제 보컬 끝 (엔딩 크레딧 환청 세그먼트 제외).
    """
    n = len(pairs)
    rows = [_Row() for _ in range(n)]
    appears = [e for e in events if e.appear]
    used: set[int] = set()

    for i in range(n):
        al = aligns[i]
        if al is None:
            continue
        vs, ve = int(al.start_ms), int(al.end_ms)
        r = rows[i]
        r.start, r.end, r.via = vs, ve + 300, "vocal"
        if al.matched_token_count < 2 or al.match_ratio < 0.3:
            # 매칭이 없거나 신뢰 불가(1토큰 우연 일치, 비율<0.3 — 실측:
            # 환청 세그먼트에 인트로 라인들이 0.08~0.25 로 끌려갔다) —
            # 비례 추정 시간만 임시로 두고 gap 단계에서 등장 이벤트로
            # 재배정한다.
            r.via = "vocal0"
            continue
        cand = [e for e in appears
                if vs - _APPEAR_LOOKBACK_MS <= e.ms <= vs + _APPEAR_LOOKAHEAD_MS
                and id(e) not in used]
        if not cand:
            continue
        ev = cand[0]
        used.add(id(ev))
        r.start, r.pos, r.via = ev.ms, (ev.cx, ev.cy), "graphic"
        end = None
        for e in events:
            if e.ms < max(r.start + 400, ve - _END_BACKTRACK_MS):
                continue
            if e.ms > ve + _END_SEARCH_MS:
                break
            if e.appear and _dist(e, *r.pos) < _SWAP_DIST:
                end = e.ms
                break
            if not e.appear and _dist(e, *r.pos) < _VANISH_DIST:
                end = e.ms
                break
        r.end = end if end is not None else ve + 800

    # 2차 배정 — 보컬 정렬은 됐지만 좁은 창에서 등장 이벤트를 못 받은 줄이
    # 이웃 그래픽 줄 '사이'의 남은 등장 이벤트를 순서대로 받는다 (가사와
    # 그래픽은 같은 순서로 뜬다). 넓은 창(-4s~+1.5s)이지만 이웃 경계로
    # 제한되므로 엉뚱한 이벤트를 훔치지 않는다.
    for i in range(n):
        r = rows[i]
        if r.via != "vocal":
            continue
        vs = int(aligns[i].start_ms)
        prev_g = max((rows[j].start for j in range(i)
                      if rows[j].via == "graphic" and rows[j].start is not None),
                     default=0)
        next_g = min((rows[j].start for j in range(i + 1, n)
                      if rows[j].via == "graphic" and rows[j].start is not None),
                     default=None)
        cand = [e for e in appears
                if id(e) not in used
                and vs - 4000 <= e.ms <= vs + 1500
                and e.ms > prev_g
                and (next_g is None or e.ms < next_g)]
        if not cand:
            continue
        ev2 = cand[0]
        used.add(id(ev2))
        r.start, r.pos, r.via = ev2.ms, (ev2.cx, ev2.cy), "graphic"
        ve = int(aligns[i].end_ms)
        end = None
        for e in events:
            if e.ms < max(r.start + 400, ve - _END_BACKTRACK_MS):
                continue
            if e.ms > ve + _END_SEARCH_MS:
                break
            if e.appear and _dist(e, *r.pos) < _SWAP_DIST:
                end = e.ms
                break
            if not e.appear and _dist(e, *r.pos) < _VANISH_DIST:
                end = e.ms
                break
        r.end = end if end is not None else ve + 800

    # 꼬리 글자 분할(원문 1~2자 연속) — DTW 로는 못 잡는다 (한 단어를 글자로
    # 쪼갠 연출 + kakasi 단독 한자 오독). 직전 줄 끝~마지막 보컬(또는 그
    # 직후의 소멸 이벤트)에 균등 시차 등장, 공통 소멸.
    tail: list[int] = []
    for i in range(n - 1, -1, -1):
        if (rows[i].start is not None and pairs[i].source
                and _nletters(pairs[i].source) <= _TAIL_MAX_LETTERS):
            tail.append(i)
        else:
            break
    tail.reverse()
    if tail:
        prev_end = rows[tail[0] - 1].end if tail[0] > 0 else None
        prev_end = prev_end if prev_end is not None else 0
        end_ms = vocal_end_ms
        fade = [e for e in events if not e.appear
                and vocal_end_ms - 500 <= e.ms <= vocal_end_ms + 3000]
        if fade:
            end_ms = fade[0].ms
        # 직전 줄 끝이 소멸 시점까지 닿아 있으면 스택 창을 앞으로 당긴다 —
        # 시차 등장이 소멸 뒤로 밀리는 역전을 막는다.
        prev_end = max(0, min(prev_end, end_ms - 1000))
        span = max(1000, end_ms - prev_end)
        for k, i in enumerate(tail):
            rows[i].start = prev_end + span * k // len(tail)
            rows[i].end = end_ms
            rows[i].via = "stack"
            rows[i].stack = k

    # 매칭 없는 줄(제목 카드·프롤로그·정렬 실패) — 이웃 사이의 등장 이벤트로.
    # 화면 텍스트는 다음 컷 전까지 떠 있는 게 보통이다.
    tail_set = set(tail)
    for i in range(n):
        r = rows[i]
        if i in tail_set or r.via not in ("-", "vocal0"):
            continue
        nxt = None
        for j in range(i + 1, n):
            if rows[j].start is not None and rows[j].via in ("graphic", "vocal"):
                nxt = rows[j].start
                break
        lo = rows[i - 1].end if i > 0 and rows[i - 1].end is not None else 0
        cand = [e for e in appears
                if id(e) not in used
                and lo <= e.ms <= (nxt if nxt is not None else lo + 8000)]
        if cand:
            used.add(id(cand[0]))
            r.start = cand[0].ms
            r.pos = (cand[0].cx, cand[0].cy)
        elif r.start is None:
            r.start = lo
        r.via = "gap"
        r.end = nxt if nxt is not None else r.start + 4000

    # gap 줄 끝 보정 — 바로 다음에 시작하는 줄이 '같은 자리'에서 뜨면
    # (교체) 그 시작에서 끝난다. 다른 자리면 블록 페이드까지 유지.
    for i in range(n):
        r = rows[i]
        if r.via != "gap" or r.pos is None or r.start is None:
            continue
        for j in range(i + 1, n):
            if rows[j].start is None or rows[j].start <= r.start:
                continue
            jp = rows[j].pos
            if (jp is not None and rows[j].start < (r.end or 0)
                    and ((jp[0] - r.pos[0]) ** 2
                         + (jp[1] - r.pos[1]) ** 2) ** 0.5 < _SWAP_DIST):
                r.end = rows[j].start
            break

    # 같은 절에서 나뉜 구들은 블록 페이드로 함께 사라진다 — 그룹 공통 끝.
    by_group: dict[int, list[int]] = {}
    for i in range(n):
        if rows[i].start is not None and i not in tail_set:
            by_group.setdefault(groups[i], []).append(i)
    for idxs in by_group.values():
        if len(idxs) >= 2:
            common = max(rows[i].end for i in idxs)
            for i in idxs:
                rows[i].end = common

    # 가사 순서 유지 — 시작 단조 증가 + 최소 길이
    prev = 0
    for r in rows:
        if r.start is None:
            continue
        r.start = max(r.start, prev)
        r.end = max(r.end, r.start + 300)
        prev = r.start
    return rows


def compose_lines(
    pairs: list[LyricPair],
    rows: list[_Row],
    visuals: list[LineVisual],
    play_res_x: int = 1920,
    play_res_y: int = 1080,
) -> list[PlannedLine]:
    """계획 + 시각 분석 → 태그 붙은 최종 줄. visuals 는 시간 있는 줄 순서."""
    out: list[PlannedLine] = []
    n_stack = sum(1 for r in rows if r.stack >= 0)
    wi = 0
    for p, r in zip(pairs, rows):
        if r.start is None:
            continue
        v = visuals[wi] if wi < len(visuals) else None
        wi += 1
        text = p.translation or p.reading or p.source
        if not text:
            continue
        if r.pos is not None:
            cx, cy = r.pos
        elif v is not None and v.sampled and v.salient > 0.002:
            cx, cy = v.gx, v.gy
        else:
            cx, cy = 0.5, 0.83
        x = round(min(0.92, max(0.08, cx)) * play_res_x)
        y = round(min(0.92, max(0.08, cy)) * play_res_y)
        if r.stack >= 0:
            # 글자 스택 — 아래→위 (완성본 패턴)
            y = round(play_res_y * (0.833 - r.stack * (0.6 / max(1, n_stack - 1))))
        dark = bool(v is not None and v.sampled
                    and v.brightness > _DARK_BRIGHTNESS)
        drift = (abs(v.gx1 - v.gx0) + abs(v.gy1 - v.gy0)
                 if v is not None and v.sampled and v.salient > 0.003 else 0.0)
        if r.stack < 0 and drift > _MOVE_DRIFT:
            dx = round((v.gx1 - v.gx0) * play_res_x)
            dy = round((v.gy1 - v.gy0) * play_res_y)
            motion = f"\\move({x},{y},{x + dx},{y + dy})"
        else:
            motion = f"\\pos({x},{y})"
        tags = f"{{\\an5{motion}\\fad(330,330)}}"
        out.append(PlannedLine(
            text=tags + text,
            start_ms=r.start,
            end_ms=r.end,
            style=DARK_STYLE if dark else LIGHT_STYLE,
            via=r.via,
        ))
    return out
