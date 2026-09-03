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
    layer: int = 0  # ASS 레이어 (AI 연출: 장식 0 / 별 1 / 본문 2)


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

    # gap 줄 무리 순차화 — 근거 없는 줄들이 같은 자리·같은 끝으로 쌓이면
    # 앞 줄의 끝을 뒤 줄의 시작으로 잘라 차례로 보이게 하고 y 를 계단식으로.
    # (시작이 확정된 뒤에 — 단조 보정 전의 임시 시작으로 칸을 나누면 틀린다)
    _sequence_gap_runs(pairs, rows)
    return rows


_GAP_RUN_MIN_MS = 1200          # 순차화 후 한 줄이 최소 이만큼은 보여야 한다
_GAP_RUN_SAME_START_MS = 60     # 시작이 이 안이면 같은 '칸' (같은 절의 구 — 함께 둔다)
_GAP_STAIR_Y: tuple[float, ...] = (0.35, 0.5, 0.65)   # 칸 순번별 y (프레임 비율)


def _same_spot(a: _Row, b: _Row) -> bool:
    """좌표 근거가 없는 줄(pos None)은 어느 자리와도 '같은 자리' 로 본다."""
    if a.pos is None or b.pos is None:
        return True
    return ((a.pos[0] - b.pos[0]) ** 2 + (a.pos[1] - b.pos[1]) ** 2) ** 0.5 < _SWAP_DIST


def _sequence_gap_runs(pairs: list[LyricPair], rows: list[_Row]) -> int:
    """연속된 gap 줄이 같은 자리·같은 끝으로 몰린 무리를 순차 표시로 바꾼다.

    인트로처럼 보컬/그래픽 근거가 없는 줄들은 모두 '다음 정상 줄 시작' 을 끝으로
    받아 한 자리에 10여 줄이 20초씩 겹친다. 각 칸(같은 시작의 줄들)의 끝을 뒤에
    오는 칸 중 _GAP_RUN_MIN_MS 이상 떨어진 첫 칸의 시작으로 자르고, y 를 칸 순번의
    계단(0.35/0.5/0.65)으로 둔다. 제목 카드도 같은 규칙으로 끝을 자르되(레퍼런스:
    제목·프롤로그는 첫 가사 줄이 뜰 때 사라진다) 세로 기둥이라 좌표는 그대로 둔다.
    반환: 끝이 바뀐 줄 수.
    """
    n = len(rows)
    changed = 0
    i = 0
    while i < n:
        r = rows[i]
        if r.via != "gap" or r.start is None:
            i += 1
            continue
        j = i
        while (j + 1 < n and rows[j + 1].via == "gap" and rows[j + 1].start is not None
               and _same_spot(r, rows[j + 1]) and rows[j + 1].end == r.end):
            j += 1
        run = list(range(i, j + 1))
        i = j + 1
        if len(run) < 2:
            continue
        # 칸 나누기 — 시작이 (거의) 같은 줄은 한 칸
        slots: list[list[int]] = []
        for k in run:
            if slots and abs(rows[k].start - rows[slots[-1][0]].start) <= _GAP_RUN_SAME_START_MS:
                slots[-1].append(k)
            else:
                slots.append([k])
        for s, members in enumerate(slots):
            s_start = rows[members[0]].start
            new_end = None
            for later in slots[s + 1:]:
                if rows[later[0]].start - s_start >= _GAP_RUN_MIN_MS:
                    new_end = rows[later[0]].start
                    break
            yfrac = _GAP_STAIR_Y[s % len(_GAP_STAIR_Y)]
            for k in members:
                rk = rows[k]
                if _role_of(pairs, k, rk) != "title":
                    # 제목은 세로 기둥(프레임 높이의 대부분)이라 y 계단에 넣지 않는다
                    px = rk.pos[0] if rk.pos is not None else 0.5
                    rk.pos = (px, yfrac)
                if new_end is not None and new_end < rk.end:
                    rk.end = max(new_end, rk.start + 300)
                    changed += 1
    return changed


@dataclass(slots=True)
class _Placed:
    """줄 1개의 배치 결정 — compose_lines / to_fx_lines 가 공유하는 중간값."""
    index: int                    # pairs 인덱스
    row: _Row
    visual: Optional[LineVisual]
    text: str                     # 표시 평문 (번역 > 독음 > 원문)
    x: int
    y: int
    dark: bool
    dx: int = 0                   # 그래픽 드리프트 (px, 0 이면 \pos)
    dy: int = 0


def _place_lines(
    pairs: list[LyricPair],
    rows: list[_Row],
    visuals: list[LineVisual],
    play_res_x: int,
    play_res_y: int,
) -> list[_Placed]:
    """계획 + 시각 분석 → 좌표/텍스트/흑백 결정 (태그 없음).

    compose_lines(태그 직접 생성)와 to_fx_lines(AI 연출 확장)가 같은 로직을
    쓰도록 뽑아낸 헬퍼. visuals 는 시간 있는 줄 순서.
    """
    out: list[_Placed] = []
    n_stack = sum(1 for r in rows if r.stack >= 0)
    wi = 0
    for i, (p, r) in enumerate(zip(pairs, rows)):
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
        dx = dy = 0
        if r.stack < 0 and drift > _MOVE_DRIFT:
            dx = round((v.gx1 - v.gx0) * play_res_x)
            dy = round((v.gy1 - v.gy0) * play_res_y)
        out.append(_Placed(index=i, row=r, visual=v, text=text,
                           x=x, y=y, dark=dark, dx=dx, dy=dy))
    return out


def compose_lines(
    pairs: list[LyricPair],
    rows: list[_Row],
    visuals: list[LineVisual],
    play_res_x: int = 1920,
    play_res_y: int = 1080,
) -> list[PlannedLine]:
    """계획 + 시각 분석 → 태그 붙은 최종 줄. visuals 는 시간 있는 줄 순서."""
    out: list[PlannedLine] = []
    for pl in _place_lines(pairs, rows, visuals, play_res_x, play_res_y):
        r = pl.row
        if pl.dx or pl.dy:
            motion = f"\\move({pl.x},{pl.y},{pl.x + pl.dx},{pl.y + pl.dy})"
        else:
            motion = f"\\pos({pl.x},{pl.y})"
        tags = f"{{\\an5{motion}\\fad(330,330)}}"
        out.append(PlannedLine(
            text=tags + pl.text,
            start_ms=r.start,
            end_ms=r.end,
            style=DARK_STYLE if pl.dark else LIGHT_STYLE,
            via=r.via,
        ))
    return out


# ---- AI 연출 경로 (effects.typeset_fx 확장) --------------------------------

def _role_of(pairs: list[LyricPair], i: int, r: _Row) -> str:
    """줄의 역할 — 디렉터가 fx 를 고르는 사전정보.

    tail: 꼬리 글자 스택 / title: 제목 카드 / prologue: 원문이 비일본어(영어
    머리말 등)이고 번역이 여러 행 / verse: 그 외.

    title 은 첫 줄이 '노래 구가 아니라는 근거' 가 있을 때만: 다른 쌍에는 독음이
    있는데 이 줄만 없고(3행 형식에서 제목 카드는 독음이 없다), 보컬/그래픽
    정렬 근거 없이 gap 으로 시간이 잡힌 경우. 2행(원문/번역)·원문만 형식은
    모든 쌍이 독음이 없으므로 첫 가사 줄을 세로 제목으로 오판하지 않는다.
    """
    from ai.lyric_normalize import detect_language
    p = pairs[i]
    if r.stack >= 0:
        return "tail"
    if (i == 0 and not p.reading and r.via in ("gap", "-", "vocal0")
            and any(q.reading for q in pairs)):
        return "title"
    tr = p.translation or ""
    if (p.source and detect_language(p.source) != "ja"
            and ("\\N" in tr or "\\n" in tr or "\n" in tr)):
        return "prologue"
    return "verse"


_COLLIDE_MS = 1000       # 이만큼 이상 동시에 보이면
_COLLIDE_PX = 220        # 중심 거리가 이보다 가까울 때 '배치 충돌'
_COLLIDE_COL_X = 0.20    # 2열 배치의 좌우 오프셋 (W 비율) — 레퍼런스 650/1445, 844/1480
_COLLIDE_COL3_X = 0.28   # 3열 이상일 때 열 간격 (W 비율)
_COLLIDE_WOBBLE_X = 0.06 # 1열 계단의 좌우 흔들림 — 레퍼런스 1052/1288/1280/1072
_COLLIDE_ROW_MIN = 110.0 # 1열 계단 행 간격(px) 하한/상한 (좌우 흔들림과 합쳐 220px 이상)
_COLLIDE_ROW_MAX = 220.0
_COLLIDE_GRID_ROW = 220.0  # 다열 격자의 행 간격 — 같은 열 이웃도 충돌 거리 밖
_COLLIDE_WIDE = 0.36     # 추정 폭이 W 의 이 비율을 넘는 줄은 다열 격자에 두면 겹친다
_COLLIDE_PX_PER_LETTER = 90.0  # fs 96 한글 폭 추정 (전각 0.9em + 공백)
_COLLIDE_GAP_PX = 40.0   # 2열 나란히 둘 때 두 줄 사이 최소 여백


def _est_width(text: str) -> float:
    return _nletters(text) * _COLLIDE_PX_PER_LETTER


_COLLIDE_ANCHOR_X = 0.30  # 고정 줄(세로 제목 기둥) 옆에 무리를 둘 때의 가로 거리 (W 비율)


def _spread_collisions(lines: list, rx: int, ry: int,
                       fixed: Optional[set[int]] = None) -> int:
    """동시에(≥1s) 보이면서 중심이 가까운(<220px) 줄 무리를 좌우/상하로 벌린다.

    같은 절의 구들(블록 페이드)과 인트로의 gap 줄들은 같은 교체 이벤트 중심을
    받아 한 자리에 쌓인다. 레퍼런스는 그런 쌍을 좌우로(x 650/1445), 서너 줄은
    아래로 흐르는 계단으로 배치한다: 2줄은 두 줄의 추정 폭이 열 간격에 들어가면
    2열(레퍼런스 '아무것도 바라지 않으려고'/'그렇지만' 650/1445), 아니면 1열 계단;
    3~6줄은 1열 계단; 7줄 이상은 2~4열 격자(행 간격 220px). 무리 전체를 추정
    폭까지 포함해 프레임 안(8~92%)으로 민 뒤 클램프. fixed 에 든 줄(세로 제목 —
    프레임 높이 대부분을 차지하는 기둥)은 움직이지 않고, 같은 무리의 나머지를
    기둥에서 0.30W 떨어진 쪽(기둥이 오른쪽이면 왼쪽)에 배치한다. 반환: 좌표가
    바뀐 줄 수. 결정적.
    """
    fixed = set(fixed or ())
    n = len(lines)
    parent = list(range(n))

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for i in range(n):
        a = lines[i]
        for j in range(i + 1, n):
            b = lines[j]
            if min(a.end_ms, b.end_ms) - max(a.start_ms, b.start_ms) < _COLLIDE_MS:
                continue
            if ((a.x - b.x) ** 2 + (a.y - b.y) ** 2) ** 0.5 >= _COLLIDE_PX:
                continue
            parent[find(i)] = find(j)
    clusters: dict[int, list[int]] = {}
    for i in range(n):
        clusters.setdefault(find(i), []).append(i)

    lo_x, hi_x = 0.08 * rx, 0.92 * rx
    lo_y, hi_y = 0.08 * ry, 0.92 * ry
    moved = 0
    for cluster in clusters.values():
        if len(cluster) < 2:
            continue
        anchors = [t for t in cluster if t in fixed]
        idxs = [t for t in cluster if t not in fixed]
        k = len(idxs)
        if k < 1:
            continue
        idxs.sort(key=lambda t: (lines[t].start_ms, t))
        cy = sum(lines[t].y for t in idxs) / k
        if anchors:
            ax = sum(lines[t].x for t in anchors) / len(anchors)
            cx = ax - _COLLIDE_ANCHOR_X * rx if ax >= rx / 2.0 else ax + _COLLIDE_ANCHOR_X * rx
        else:
            cx = sum(lines[t].x for t in idxs) / k
        widths = [_est_width(lines[t].text) for t in idxs]
        if k == 1:
            ncols = 1
        elif k == 2:
            # 두 줄을 나란히 둘 수 있나 — 반폭 합 + 여백이 열 간격(2·0.20W) 이하
            fits = (widths[0] + widths[1]) / 2.0 + _COLLIDE_GAP_PX <= 2 * _COLLIDE_COL_X * rx
            ncols = 2 if fits else 1
        elif k > 6 and max(widths) <= _COLLIDE_WIDE * rx:
            ncols = min(4, -(-k // 5))          # 열당 최대 5행 (행 간격 220px 이 프레임에 듦)
        else:
            ncols = 1
        nrows = -(-k // ncols)
        if nrows <= 1:
            dy = 0.0
        elif ncols == 1:
            dy = min(_COLLIDE_ROW_MAX, max(_COLLIDE_ROW_MIN, 0.6 * ry / (nrows - 1)))
        else:
            dy = _COLLIDE_GRID_ROW
        pts: list[tuple[float, float]] = []
        for s in range(k):
            col, row = s % ncols, s // ncols
            if k == 1:
                ox = 0.0
            elif ncols == 1:
                ox = (_COLLIDE_WOBBLE_X if s % 2 else -_COLLIDE_WOBBLE_X) * rx
            elif ncols == 2:
                ox = (_COLLIDE_COL_X if col else -_COLLIDE_COL_X) * rx
            else:
                ox = (col - (ncols - 1) / 2.0) * _COLLIDE_COL3_X * rx
            oy = (row - (nrows - 1) / 2.0) * dy
            pts.append((cx + ox, cy + oy))
        # 프레임 맞춤 — 다열이면 글자 폭까지 넣어 가장자리 줄이 잘리지 않게
        hw = [w / 2.0 if ncols > 1 else 0.0 for w in widths]
        min_x = min(p[0] - h for p, h in zip(pts, hw))
        max_x = max(p[0] + h for p, h in zip(pts, hw))
        min_y, max_y = min(p[1] for p in pts), max(p[1] for p in pts)
        sx = (lo_x - min_x) if min_x < lo_x else (hi_x - max_x) if max_x > hi_x else 0.0
        sy = (lo_y - min_y) if min_y < lo_y else (hi_y - max_y) if max_y > hi_y else 0.0
        for t, (px, py) in zip(idxs, pts):
            nx = int(round(min(hi_x, max(lo_x, px + sx))))
            ny = int(round(min(hi_y, max(lo_y, py + sy))))
            if (nx, ny) != (lines[t].x, lines[t].y):
                lines[t].x, lines[t].y = nx, ny
                moved += 1
    return moved


def to_fx_lines(
    pairs: list[LyricPair],
    rows: list[_Row],
    visuals: list[LineVisual],
    play_res: tuple[int, int] = (1920, 1080),
) -> "tuple[list, list[str], list[int]]":
    """계획 + 시각 분석 → 태그 없는 FxLine 목록 (AI 연출 디렉터 입력).

    compose_lines 와 같은 좌표/텍스트/스타일/흑백 결정을 공유하되, 동시에 같은
    자리에 뜨는 줄들은 _spread_collisions 로 좌우/계단 배치한다 (디렉터·확장기
    전 단계 — 글자별/잔상/막대 연출이 한 점에 쌓이지 않게).

    Returns:
        (fx_lines, roles, row_indices) — 병렬 리스트. roles 는
        title|prologue|verse|tail, row_indices 는 각 FxLine 의 pairs 인덱스.

    꼬리 글자 스택(rows[i].stack>=0)은 개별 줄로 두지 않고 하나의 FxLine
    ("뛰쳐올라가", 첫 시작~공통 끝)으로 합쳐 char_stack 에 맡긴다 — 근거:
      · 확장기의 char_stack 이 시차 등장·글자 크기 감소·상승 배치를 한 줄에서
        결정적으로 만들고(레퍼런스 뛰/쳐/올/라/가 와 같은 꼴), 디렉터가
        stagger 를 지속/글자 수로 조정한다. 글자를 따로 주면 디렉터가 각각에
        다른 fx 를 고르거나 스택 정합이 깨질 수 있다.
      · plan_times 의 stack 배치(균등 시차, 공통 소멸)는 char_stack 의
        stagger_ms + 공통 end 와 같은 모델이라 정보 손실이 없다.
    합쳐진 줄의 row_indices 는 첫 꼬리 쌍의 인덱스, 좌표는 맨 아래 글자의
    자리(stack 0), 흑백은 첫 꼬리 줄의 장면 분석을 따른다.
    """
    from effects.typeset_fx_schema import FxLine

    rx, ry = int(play_res[0]), int(play_res[1])
    placed = _place_lines(pairs, rows, visuals, rx, ry)
    fx_lines: list = []
    roles: list[str] = []
    row_indices: list[int] = []
    tail: list[_Placed] = []
    for pl in placed:
        r = pl.row
        if r.stack >= 0:
            tail.append(pl)
            continue
        fx_lines.append(FxLine(
            text=pl.text, start_ms=int(r.start), end_ms=int(r.end),
            style=DARK_STYLE if pl.dark else LIGHT_STYLE,
            x=pl.x, y=pl.y, dark=pl.dark))
        roles.append(_role_of(pairs, pl.index, r))
        row_indices.append(pl.index)
    if tail:
        tail.sort(key=lambda t: t.row.stack)
        first = tail[0]
        text = "".join(t.text.replace("\\N", "").replace("\n", "") for t in tail)
        start = min(int(t.row.start) for t in tail)
        end = max(int(t.row.end) for t in tail)
        fx_lines.append(FxLine(
            text=text, start_ms=start, end_ms=max(end, start + 300),
            style=DARK_STYLE if first.dark else LIGHT_STYLE,
            x=first.x, y=first.y, dark=first.dark))
        roles.append("tail")
        row_indices.append(first.index)
    _spread_collisions(fx_lines, rx, ry,
                       fixed={k for k, role in enumerate(roles) if role == "title"})
    # 시작 시간 순서 유지 (꼬리 합본은 원래도 마지막이지만 안전하게)
    order = sorted(range(len(fx_lines)),
                   key=lambda k: (fx_lines[k].start_ms, row_indices[k]))
    return ([fx_lines[k] for k in order], [roles[k] for k in order],
            [row_indices[k] for k in order])


def fx_visuals(
    rows: list[_Row],
    visuals: list[LineVisual],
    row_indices: list[int],
) -> list[Optional[LineVisual]]:
    """to_fx_lines 의 row_indices 에 맞춘 LineVisual 목록 (없으면 None).

    visuals 는 '시간 있는 줄 순서'(analyze_line_windows 입력 순서)라 pairs
    인덱스와 다르다 — 그 대응을 여기서 푼다.
    """
    timed = [i for i, r in enumerate(rows) if r.start is not None]
    by_index = {i: (visuals[k] if k < len(visuals) else None)
                for k, i in enumerate(timed)}
    return [by_index.get(i) for i in row_indices]


def expand_planned(
    fx_lines: list,
    directives: list,
    play_res: tuple[int, int] = (1920, 1080),
    vias: Optional[list[str]] = None,
) -> tuple[list[PlannedLine], list[str]]:
    """FxLine + FxDirective → 확장된 PlannedLine 들 (+ 폴백/오류 노트).

    effects.typeset_fx.expand_safe 를 쓰므로 예외를 내지 않는다 — 검증 실패나
    확장 예외는 plain 으로 폴백하고 notes 에 남긴다. directives 가 짧으면
    나머지는 plain. vias 는 줄별 시간 근거(없으면 fx 이름).
    """
    from effects.typeset_fx import expand_safe
    from effects.typeset_fx_schema import FxDirective

    out: list[PlannedLine] = []
    notes: list[str] = []
    res = (int(play_res[0]), int(play_res[1]))
    for i, line in enumerate(fx_lines):
        d = directives[i] if i < len(directives) and directives[i] is not None \
            else FxDirective("plain")
        events, errs = expand_safe(line, d, res)
        for e in errs:
            notes.append(f"[{i}] {line.text[:12]!r}: {e}")
        via = (vias[i] if vias is not None and i < len(vias) and vias[i]
               else str(getattr(d, "fx", "plain")))
        for ev in events:
            out.append(PlannedLine(
                text=ev.text, start_ms=int(ev.start_ms), end_ms=int(ev.end_ms),
                style=ev.style or line.style, via=via, layer=int(ev.layer)))
    return out, notes
