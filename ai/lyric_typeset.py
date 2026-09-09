r"""가사 → 그래픽 우선 타이프셋 자막 계획 (완성본 형식).

수작업 완성본('밤하늘로 이어지는 언덕길' 레퍼런스) 실측 규칙을 자동화한다:
  · 시작 = 화면 가사 그래픽의 페이드 시작 — 보컬보다 1~2초 앞선다.
    보컬 정렬은 '어느 등장 이벤트가 이 줄 것인지' 고르는 사전정보로만 쓰고,
    등장 이벤트는 가사 순서대로 하나씩 소비한다 (창 안의 가장 늦은 것을
    고르면 다음 줄의 그래픽을 훔친다).
  · 끝 = 같은 자리 교체(다음 등장, 근접) 또는 근처 소멸. 같은 절에서 나뉜
    구들은 블록 페이드로 함께 끝난다.
  · 위치 = 화면에 그려진 원문 텍스트 영역(media.text_region) — 같은 창의 구들은
    글자 덩어리를 읽기 순서대로 하나씩 받는다. 영역이 없거나 신뢰가 낮으면
    등장 이벤트의 변화 영역 중심으로 폴백.
  · 밝은 장면(주간)은 검은 글자, 어두운 장면은 흰 글자 스타일 — 텍스트 영역의
    극성(dark_text)이 있으면 그것을 우선.
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
from media.text_region import TextRegion
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
_GAP_PRIOR_WINDOW_MS = 4000  # gap 줄: 비례 추정 시각 ±이 안의 등장 이벤트만 채택


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
        # 실측: 환청 세그먼트에 3/8(0.375) 우연 매칭된 줄이 'vocal' 로 잡히면
        # 앞 gap 줄들의 상한(nxt)이 6.6s 로 무너져 인트로 전체가 0.3초짜리로
        # 눌렸다 — 3토큰 미만 또는 비율 0.4 미만은 근거로 쓰지 않는다.
        if al.matched_token_count < 3 or al.match_ratio < 0.4:
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
                # 뒤로는 1차와 같은 2.5s 까지만 — 더 넓히면 앞 줄(아직 gap 단계를
                # 안 거친 vocal0 줄)의 그래픽을 훔친다 (실측: 30s 줄이 26.15s 이벤트를 가져감)
                and vs - _APPEAR_LOOKBACK_MS <= e.ms <= vs + 1500
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
        # 탐색 하한은 '이전 줄의 시작' — 이전 gap 줄이 이벤트를 받으면 그 끝이
        # nxt(다음 정상 줄)까지 늘어나므로, 끝을 하한으로 쓰면 뒤 gap 줄들의
        # 창이 텅 비어 비례 추정 시간에 갇힌다 (실측: 인트로 10줄이 0/2.5/5/7.5s
        # 로 균등 배치되고 그래픽 이벤트를 못 받음).
        lo = 0
        if i > 0 and rows[i - 1].start is not None:
            lo = rows[i - 1].start + 300
        hi = nxt if nxt is not None else lo + 8000
        window = [e for e in appears if id(e) not in used and lo <= e.ms <= hi]
        # 비례 추정(prior)이 있으면 그 근처(±4s)에서 가장 가까운 이벤트를,
        # 없으면 창의 첫 이벤트를 고른다. '창 안의 가장 이른 이벤트' 는 앞 줄이
        # 뒤 줄의 그래픽까지 차례로 먹어 남은 줄들이 한 시각으로 몰린다
        # (실측: 인트로 6줄이 전부 26.15s).
        prior = r.start
        if prior is not None:
            near = [e for e in window if abs(e.ms - prior) <= _GAP_PRIOR_WINDOW_MS]
            cand = sorted(near, key=lambda e: abs(e.ms - prior))[:1]
        else:
            cand = window[:1]
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
                if _role_of(pairs, k, rk) == "title":
                    # 제목 카드는 세로 기둥이라 y 계단에 넣지 않는다. 끝은 프롤로그
                    # 칸이 아니라 '그래픽 이벤트로 시작이 확정된' 다음 칸(첫 가사)
                    # 에서 자른다 — 레퍼런스에서 제목은 프롤로그와 나란히 떠 있다가
                    # 첫 가사 줄이 뜰 때 사라진다 (프롤로그 칸에서 자르면 1.4초 만에
                    # 사라지고, 안 자르면 30초 뒤 정상 줄까지 남는다).
                    for later in slots[s + 1:]:
                        lr = rows[later[0]]
                        if (lr.pos is not None and lr.start - s_start >= _GAP_RUN_MIN_MS
                                and _role_of(pairs, later[0], lr) != "prologue"):
                            if lr.start < rk.end:
                                rk.end = max(lr.start, rk.start + 300)
                                changed += 1
                            break
                    continue
                px = rk.pos[0] if rk.pos is not None else 0.5
                rk.pos = (px, yfrac)
                if new_end is not None and new_end < rk.end:
                    rk.end = max(new_end, rk.start + 300)
                    changed += 1
    return changed


# ---- 화면 텍스트 영역 기반 배치 ---------------------------------------------
#
# media.text_region.detect_text_regions 는 줄의 창 안에서 '기준 프레임(창 시작
# 직전) 대비 새로 그려진 가는 구조' 를 글자 덩어리(cluster)로 준다. 그 안에는
# 이 줄의 원문 말고도 (a) 앞 줄의 텍스트가 \move 로 흘러서 변화로 잡힌 것,
# (b) 창 안에서 나중에 뜬 다음 줄의 텍스트가 섞인다 (실측 00001: '고개 한 번'
# 창에 앞 두 줄이 3덩어리로, '별하늘로…' 창에 다음 절의 두 구가). 그래서
#   · (a) 는 앞 줄이 '자기 텍스트' 로 확정한 덩어리(또는 가운데 샘플이 이 줄
#     시작보다 앞선 앞 줄 영역의 덩어리)와 대조해 뺀다.
#   · (b) 는 뒤 줄이 자기 영역으로 같은 덩어리를 보고 있으면 그 줄 것으로 넘긴다.
#   · 같은 절의 구들(창이 겹치는 그룹 멤버)과 영역이 없는 뒤 줄(같은 장면, 이 창의
#     샘플 구간 안에서 시작)은 남은 덩어리를 읽기 순서(행 위→아래, 행 안 좌→우)로
#     하나씩 나눠 받는다 — 덩어리가 더 많으면 뒤쪽(최근에 뜬) 것들을.
#   · 원문 글자 수로 추정한 폭의 35% 에 못 미치는 덩어리(세로 제목의 한 글자,
#     꽃잎 잡티)는 이 줄들의 텍스트가 아니다.
#
# 번역의 자리는 원문 덩어리 '위' 가 기본이다 (레퍼런스 실측: 何も望まないように
# (654,810)→(650,721), 諦めながら (834,984)→(844,884), 私はそれを (962,398)→
# (949,301) — 원문 중심에서 h/2+35 위). 위가 다른 원문/먼저 놓인 번역과 겹치면
# 아래, 그다음 그 자리 아래쪽 빈 곳(화면 텍스트 열의 맨 아래에 쌓기: 美しいとは/
# 思ったことがない → (960,900)/(960,1045)), 마지막에 옆. 오른쪽 가장자리에 여러
# 행으로 쌓인 원문(迫りくる/暗い闇/二度と)은 옆(왼쪽)이 먼저다 (레퍼런스
# (1048,270)/(1138,436)/(1149,598)).
#
# 드리프트(원문이 \move 로 흐르는지)는 영역의 bbox 드리프트를 샘플 구간→줄 길이로
# 외삽하되, 창 안에 다른 줄이 시작하지 않고(bbox 가 그 줄 때문에 커진다) 면적비가
# 0.8~1.25 안이며 앞 줄 텍스트가 섞이지 않았을 때만 믿는다.

_REGION_MIN_CONF = 0.4        # 이 이상이면 텍스트 영역을 좌표 근거로 쓴다
_REGION_DARK_MIN_CONF = 0.2   # 흑백(dark_text)·장면 판정 하한 — 마스크가 조금만 잡혀도 극성은 맞다
_REGION_MOVE_PX = 40.0        # 텍스트 영역 드리프트(줄 길이 외삽)가 이 이상이면 \move (px, 1080p 기준)
_REGION_EDGE_MS = 400.0       # text_region 샘플 = 시작+400 ~ 끝-400 (짧은 창은 길이의 25%)
_REGION_APPEAR_MS = 300       # 이만큼 먼저 뜬 줄의 텍스트는 '이전 텍스트' 로 본다
_REGION_SCENE_BRIGHT = 0.25   # 장면 밝기 차가 이보다 크면 다른 장면 (영역 극성이 없을 때)
_REGION_DRIFT_SCALE = (0.8, 1.25)  # 드리프트를 믿는 면적비(scale) 범위 — 밖이면 창 안에서 텍스트가 늘거나 줄었다
_REGION_DRIFT_EXTRAP = 3.0    # 샘플 구간 → 줄 길이 외삽 배수 상한 (잡음 증폭 억제)
_REGION_DRIFT_NOISE_PX = 25.0 # 잰 드리프트가 이보다 작으면 같은 블록의 다른 줄에 물려주지 않는다 (잡음 실측 ≤16px)
_REGION_DRIFT_CAP_PX = 600.0  # 줄 길이로 외삽·상속한 드리프트 상한 (레퍼런스 최대 512px)
_REGION_LINGER_MS = 1500      # 앞 줄의 원문은 계획된 끝 뒤에도 이만큼 화면에 남아 있다고 본다 (자리 점유)
_REGION_OCCUPY_MS = 1000      # 이만큼 이상 같이 보여야 '자리를 차지한다' (교체 전환의 짧은 겹침은 무시, _COLLIDE_MS 와 같음)
_REGION_RETRY_BACK_MS = 2000  # 영역이 안 잡힌 줄의 재검출 창을 이만큼 앞으로 넓힌다 (계획 시각이 원문보다 늦을 때)
_REGION_MIN_WIDTH_FRAC = 0.45 # 원문 글자 수로 추정한 폭의 이 비율보다 좁은 덩어리는 그 줄의 텍스트가 아니다
_REGION_RECENT_MS = 2900      # 이 안에 뜬 앞 줄의 텍스트는 기준 프레임(재검출 창은 2.9s 전)에 없어 '변화' 로 잡힌다
_REGION_MOVED_PX = 20.0       # 앞 줄 덩어리가 이보다 옮겨졌으면 흐르는 텍스트 (변화로 잡힌 이유)
_REGION_GLYPH_PX = 86.0       # 원문 1자 폭 추정 (1080p, 실측 fs≈96)
_DIAG_MIN_CLUSTERS = 5        # 대각선 판정에 필요한 글자 덩어리 수 (실측 7; 잡티 4개 오판 방지)
_DIAG_MIN_CONF = 0.6
_OFF_GAP_PX = 40.0            # 번역 중심 = 원문 상단(하단) ± 이 값 (레퍼런스 실측 21~51)
_OFF_SIDE_GAP_PX = 20.0       # 옆에 둘 때 원문 끝과 번역 끝 사이 여백
_KOR_HALF_H = 34.0            # 충돌 판정용 번역 반높이 (fs 96 한글 실높이 ~70) — _OFF_GAP_PX 보다 작아야 위/아래 후보가 원문과 안 겹친다
_KOR_LINE_H = 96.0            # 아래로 쌓을 때 행 간격의 기준
_RIGHT_EDGE_FRAC = 0.82       # 원문 오른끝이 화면 폭의 이 비율을 넘으면 '가장자리 텍스트'
_STACK_DY = (60.0, 260.0)     # 같은 열에 이 범위의 행 간격으로 다른 원문이 있으면 '쌓인 블록'
_INHERIT_DY_MAX = 450.0       # 드리프트 상속은 3행 거리까지 (遠ざかる/白い壁/一度も 블록 실측 386px)
_SINGLE_LINE_H_PX = 150.0     # 이보다 높은 가로 덩어리는 두 행이 합쳐진 것 — bbox 드리프트를 믿지 않는다
_FRAME_LO, _FRAME_HI = 0.08, 0.92  # 번역 중심(시작점·\move 끝점)을 두는 프레임 안쪽 띠 — 배치·벌리기·클램프가 같은 값을 쓴다


def _region_window(tr: TextRegion, r: "_Row") -> tuple[int, int]:
    """영역이 실제로 잰 창 (ms). 재검출(창을 앞으로 넓힘)한 영역은 줄 구간과 다르다 —
    drift/scale 의 샘플 구간·오염 판정은 이 창 기준이어야 한다. 모르면 줄 구간."""
    ws = int(getattr(tr, "win_start_ms", -1))
    we = int(getattr(tr, "win_end_ms", -1))
    if ws >= 0 and we > ws:
        return ws, we
    return int(r.start), int(r.end)


def _fit_drift(x: float, y: float, d: tuple[float, float], rx: int, ry: int
               ) -> tuple[float, float]:
    """드리프트 (dx, dy) 를 끝점 (x+dx, y+dy) 이 프레임 안쪽 띠(8~92%)에 남도록 방향을
    유지한 채 줄인다. 시작점이 이미 띠 밖이면 0. 줄 길이로 외삽한 드리프트가 화면
    밖으로 나가는 것(실측 \\move(1554,748,1794,1298))을 막는다 — 확장기의 가장자리
    클램프는 글자 절반이 잘린 채 끝나므로 여기서 미리 줄인다."""
    dx, dy = float(d[0]), float(d[1])
    k = 1.0
    if dx > 0:
        k = min(k, (_FRAME_HI * rx - x) / dx)
    elif dx < 0:
        k = min(k, (_FRAME_LO * rx - x) / dx)
    if dy > 0:
        k = min(k, (_FRAME_HI * ry - y) / dy)
    elif dy < 0:
        k = min(k, (_FRAME_LO * ry - y) / dy)
    k = max(0.0, min(1.0, k))
    return (dx * k, dy * k)


@dataclass(slots=True)
class _RegionPlace:
    """텍스트 영역이 정한 줄 1개의 자리 (px)."""
    x: float
    y: float
    bounds: tuple[int, int, int, int]                  # 번역 상자 (x0, y0, x1, y1) — 같은 자리 줄들을 벌릴 때의 한계
    own: list[tuple[float, float, float, float]]       # 이 줄의 텍스트로 본 덩어리 (cx, cy, w, h)
    hint: Optional[dict]                               # 디렉터용 힌트 (typeset_director.hint_fx)
    leader: bool = True                                # 자기 창의 영역으로 배치됐는지 (드리프트 신뢰 조건)


def _px_cluster(c: tuple[float, float, float, float], rx: int, ry: int
                ) -> tuple[float, float, float, float]:
    return (c[0] * rx, c[1] * ry, c[2] * rx, c[3] * ry)


def _cluster_match(a: tuple[float, float, float, float],
                   b: tuple[float, float, float, float]) -> bool:
    """두 덩어리가 같은 텍스트인가 — 크기가 비슷하고(폭·높이 1.6배 안) 중심이 가깝다
    (느린 드리프트 허용: 폭의 30%/높이의 60%, 최소 60px). 크기 조건이 없으면 두
    구를 합친 넓은 새 줄이 앞 줄의 좁은 덩어리를 '품어서' 같은 텍스트로 오판하고,
    중심 허용이 넓으면 같은 행의 다른 구(絶えず vs 私はそれを, 216px)를 오판한다."""
    wa, wb = max(a[2], 1.0), max(b[2], 1.0)
    ha, hb = max(a[3], 1.0), max(b[3], 1.0)
    if max(wa, wb) > 1.6 * min(wa, wb) or max(ha, hb) > 1.6 * min(ha, hb):
        return False
    return (abs(a[0] - b[0]) <= max(60.0, 0.3 * max(wa, wb))
            and abs(a[1] - b[1]) <= max(60.0, 0.6 * max(ha, hb)))


def _moved(a: tuple[float, float, float, float],
           b: tuple[float, float, float, float]) -> bool:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5 > _REGION_MOVED_PX


def _inside(c: tuple[float, float, float, float],
            o: tuple[float, float, float, float], margin: float = 24.0) -> bool:
    """c 의 상자가 o 의 상자(여유 margin) 안에 든다 — 그려지는 중인 세로 제목 기둥의
    일부(높이가 절반이라 _cluster_match 의 크기 조건에 걸리지 않음)를 제목 것으로."""
    return (c[0] - c[2] / 2.0 >= o[0] - o[2] / 2.0 - margin
            and c[0] + c[2] / 2.0 <= o[0] + o[2] / 2.0 + margin
            and c[1] - c[3] / 2.0 >= o[1] - o[3] / 2.0 - margin
            and c[1] + c[3] / 2.0 <= o[1] + o[3] / 2.0 + margin)


def _same_slot(a: tuple[float, float, float, float],
               b: tuple[float, float, float, float]) -> bool:
    """같은 행(y 띠가 짧은 쪽의 50% 이상 겹침)·x 겹침 — 크기는 묻지 않는다 (교체된
    자리, 또는 두 구를 합친 넓은 덩어리 vs 한 구)."""
    ay0, ay1 = a[1] - a[3] / 2.0, a[1] + a[3] / 2.0
    by0, by1 = b[1] - b[3] / 2.0, b[1] + b[3] / 2.0
    ov = min(ay1, by1) - max(ay0, by0)
    if ov < 0.5 * max(1.0, min(ay1 - ay0, by1 - by0)):
        return False
    return (a[0] - a[2] / 2.0) < (b[0] + b[2] / 2.0) and (a[0] + a[2] / 2.0) > (b[0] - b[2] / 2.0)


def _reading_order(K: list[tuple[float, float, float, float]]
                   ) -> list[tuple[float, float, float, float]]:
    """행(y 띠가 짧은 쪽의 50% 이상 겹침) 위→아래, 행 안에서 좌→우. 결정적."""
    rows: list[list[tuple[float, float, float, float]]] = []
    for c in sorted(K, key=lambda o: (o[1], o[0])):
        y0, y1 = c[1] - c[3] / 2.0, c[1] + c[3] / 2.0
        for row in rows:
            ry0 = min(o[1] - o[3] / 2.0 for o in row)
            ry1 = max(o[1] + o[3] / 2.0 for o in row)
            ov = min(ry1, y1) - max(ry0, y0)
            if ov >= 0.5 * max(1.0, min(y1 - y0, ry1 - ry0)):
                row.append(c)
                break
        else:
            rows.append([c])
    rows.sort(key=lambda row: sum(o[1] for o in row) / len(row))
    out: list[tuple[float, float, float, float]] = []
    for row in rows:
        out.extend(sorted(row, key=lambda o: o[0]))
    return out


def _cluster_bbox(cl: list[tuple[float, float, float, float]]
                  ) -> tuple[int, int, int, int]:
    x0 = min(c[0] - c[2] / 2.0 for c in cl)
    x1 = max(c[0] + c[2] / 2.0 for c in cl)
    y0 = min(c[1] - c[3] / 2.0 for c in cl)
    y1 = max(c[1] + c[3] / 2.0 for c in cl)
    return (int(round(x0)), int(round(y0)), int(round(x1)), int(round(y1)))


def _region_usable(tr: Optional[TextRegion]) -> bool:
    return (tr is not None and tr.sampled and tr.confidence >= _REGION_MIN_CONF
            and bool(tr.clusters))


def region_usable(tr: Optional[TextRegion]) -> bool:
    """텍스트 영역이 좌표 근거로 쓸 만한가 (sampled, 신뢰 ≥0.4, 덩어리 있음) — 호출자
    (sync_service 의 재검출 판단)용 공개 이름."""
    return _region_usable(tr)


def _src_len(source: Optional[str]) -> int:
    """원문 글자 수 추정 — 공백 제외, 연속된 점(..., ･･･, …)은 1자로."""
    n = 0
    prev_dot = False
    for ch in (source or ""):
        if ch.isspace():
            continue
        dot = ch in ".･・…"
        if dot and prev_dot:
            continue
        n += 1
        prev_dot = dot
    return n


def _plausible_clusters(K: list[tuple[float, float, float, float]],
                        sources: list[Optional[str]]
                        ) -> list[tuple[float, float, float, float]]:
    """가로 덩어리 중 원문 글자 수로 추정한 폭(가장 짧은 후보 기준)의 45% 에 못
    미치는 것을 뺀다 — 세로 제목의 한 글자(は 176px), 꽃잎 잡티, 획 조각. 세로
    (h>1.5w) 덩어리는 폭으로 판단하지 않는다. 전부 빠지면 빈 목록 (영역 안 씀)."""
    lens = [_src_len(s) for s in sources if s]
    if not lens:
        return K
    exp = min(lens) * _REGION_GLYPH_PX
    return [c for c in K if c[3] > 1.5 * c[2] or c[2] >= _REGION_MIN_WIDTH_FRAC * exp]


def _region_hint(
    tr: TextRegion,
    clusters: list[tuple[float, float, float, float]],
    drift: Optional[tuple[float, float]],
    diagonal: bool,
    accent: Optional[str],
) -> dict:
    """typeset_director 가 읽는 힌트 — 화면에서 측정된 사실만 (px 단위).

    drift 는 신뢰할 때만 (dx, dy) — 줄 길이로 외삽한 값, 아니면 None(모름).
    layout 은 diagonal 을 덩어리 수/신뢰도로 검증한 뒤의 값. diag_start/diag_end
    는 대각선일 때 첫/끝 글자 덩어리 중심. accent_color 는 이 줄의 텍스트에 있는
    강조색(영역에 강조가 있어도 같은 창의 다른 줄 것이면 None).
    """
    if diagonal:
        layout = "diagonal"
    elif tr.layout in ("vertical", "scatter"):
        layout = tr.layout
    else:
        layout = "horizontal"
    frac = getattr(tr, "accent_frac", None)
    accent_full = bool(getattr(tr, "accent_full", False)
                       or (isinstance(frac, (int, float)) and frac >= 0.9))
    return {
        "layout": layout,
        "angle_deg": float(tr.angle_deg),
        "drift": drift,
        "scale": float(tr.scale),
        "accent_color": accent,
        "accent_full": accent_full if accent else False,
        "dark_text": bool(tr.dark_text),
        "bbox": _cluster_bbox(clusters),
        "clusters": [tuple(c) for c in clusters],
        "confidence": float(tr.confidence),
        "diag_start": (clusters[0][0], clusters[0][1]) if diagonal else None,
        "diag_end": (clusters[-1][0], clusters[-1][1]) if diagonal else None,
    }


def _rect_hit(x: float, y: float, kw: float,
              rects: list[tuple[float, float, float, float]]) -> bool:
    x0, x1 = x - kw / 2.0, x + kw / 2.0
    y0, y1 = y - _KOR_HALF_H, y + _KOR_HALF_H
    return any(x0 < r[2] and x1 > r[0] and y0 < r[3] and y1 > r[1] for r in rects)


def _cluster_rect(c: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    return (c[0] - c[2] / 2.0, c[1] - c[3] / 2.0, c[0] + c[2] / 2.0, c[1] + c[3] / 2.0)


def _offset_place(
    cluster: tuple[float, float, float, float],
    kw: float,
    occupied: list[tuple[float, float, float, float]],
    korean: list[tuple[float, float, float, float]],
    rx: int,
    ry: int,
    vertical: bool = False,
) -> tuple[float, float]:
    """원문 덩어리(cx, cy, w, h) 옆의 빈 자리에 번역(폭 kw)을 둔다.

    후보 순서: 기본 [위, 아래, 열 맨 아래에 쌓기, 옆]; 오른쪽 가장자리에 여러 행으로
    쌓인 원문은 [왼쪽 옆, 위, 아래, 쌓기]; 세로 기둥(vertical — 번역도 세로로
    그린다)은 [안쪽 옆, 바깥쪽 옆, 위, 아래]. 후보는 중심이 프레임 안쪽 띠(8~92% —
    _place_lines 의 클램프와 같은 띠라 나중에 좌표가 옮겨지지 않는다)에 있고 글자
    상자가 x 2~98% 안이며, 다른 원문(occupied — 이 덩어리와 같은 자리는 제외돼
    있어야 한다)·먼저 놓인 번역(korean)과 겹치지 않아야 한다. 전부 실패하면 원문
    중심(띠 안으로 클램프). 결정적.
    """
    cx, cy, w, h = cluster
    x0, x1 = cx - w / 2.0, cx + w / 2.0
    lo_x, hi_x = _FRAME_LO * rx, _FRAME_HI * rx
    lo_y, hi_y = _FRAME_LO * ry, _FRAME_HI * ry
    above = (cx, cy - h / 2.0 - _OFF_GAP_PX)
    below = (cx, cy + h / 2.0 + _OFF_GAP_PX)
    left = (x0 - _OFF_SIDE_GAP_PX - kw / 2.0, cy)
    right = (x1 + _OFF_SIDE_GAP_PX + kw / 2.0, cy)
    occ_rects = [_cluster_rect(o) for o in occupied]

    def stack_bottom() -> tuple[float, float]:
        bottoms = [o[1] + o[3] / 2.0 for o in occupied
                   if (o[0] - o[2] / 2.0) < cx + kw / 2.0 and (o[0] + o[2] / 2.0) > cx - kw / 2.0]
        y = max(bottoms + [cy + h / 2.0]) + _OFF_GAP_PX
        for _ in range(8):
            if not _rect_hit(cx, y, kw, korean):
                break
            y += _KOR_LINE_H + _OFF_GAP_PX     # 레퍼런스의 번역 행 간격 ~145px
        return (cx, y)

    stacked = any(abs(o[0] - cx) < 0.5 * max(w, o[2], 1.0)
                  and _STACK_DY[0] <= abs(o[1] - cy) <= _STACK_DY[1]
                  for o in occupied)
    if vertical:
        cands = ([left, right] if cx >= rx / 2.0 else [right, left]) + [above, below]
    elif x1 > _RIGHT_EDGE_FRAC * rx and stacked:
        cands = [left, above, below, stack_bottom()]
    else:
        side = left if cx >= rx / 2.0 else right
        cands = [above, below, stack_bottom(), side]
    for x, y in cands:
        if not (lo_x <= x <= hi_x and lo_y <= y <= hi_y
                and x - kw / 2.0 >= 0.02 * rx and x + kw / 2.0 <= 0.98 * rx):
            continue
        if _rect_hit(x, y, kw, occ_rects) or _rect_hit(x, y, kw, korean):
            continue
        return (x, y)
    return (min(hi_x, max(lo_x, cx)), min(hi_y, max(lo_y, cy)))


def _region_placements(
    pairs: list[LyricPair],
    rows: list[_Row],
    regions: list[Optional[TextRegion]],
    rx: int,
    ry: int,
    groups: Optional[list[int]] = None,
    visuals: Optional[list[Optional[LineVisual]]] = None,
) -> dict[int, _RegionPlace]:
    """텍스트 영역 → pairs 인덱스별 자리. regions·visuals 는 시간 있는 줄 순서.

    줄을 시간 순서로 보며:
      · title: bbox 중심 (세로 기둥 전체가 제목).
      · 신뢰 낮은/없는 영역, tail(글자 스택), prologue(화면 원문이 없는 머리말)는 건너뜀.
      · 앞 줄이 자기 텍스트로 확정한 덩어리(또는 가운데 샘플이 이 줄 시작보다
        앞선 앞 줄 영역의 모든 덩어리)와 겹치는 덩어리는 '이전 텍스트' 로 뺀다.
        전부 빠지면 (예: 제목 기둥만 잡힌 인트로 줄) 영역을 쓰지 않는다.
      · 대각선(덩어리 ≥5, 신뢰 ≥0.6)은 글자 사슬 하나 — bbox 중심, 힌트에 첫/끝 글자.
      · 다른 절의 뒤 줄이 자기 영역으로 같은 덩어리를 보고 있으면 그 줄 것 → 뺀다.
      · 남은 덩어리(원문 글자 수 대비 너무 좁은 것 제외)를 이 줄 + 창이 겹치는 같은
        절의 구들 + 영역 없는 같은 장면의 뒤 줄(이 창의 샘플 구간 안에서 시작)에
        읽기 순서로 하나씩 — 더 많으면 뒤쪽(최근) 것들을, 모자라면 앞 줄부터 받고
        나머지 줄은 자기 영역으로. 강조색은 마지막(가장 최근) 멤버의 것으로 본다.
      · 자리는 _offset_place (원문 위/아래/옆의 빈 곳). 결정적. 예외 없음.
    """
    timed = [i for i, r in enumerate(rows) if r.start is not None]
    reg: dict[int, Optional[TextRegion]] = {
        i: (regions[k] if k < len(regions) else None) for k, i in enumerate(timed)}
    vis: dict[int, Optional[LineVisual]] = {
        i: (visuals[k] if visuals is not None and k < len(visuals) else None)
        for k, i in enumerate(timed)}
    grp = {i: (int(groups[i]) if groups is not None and i < len(groups) else -1 - i)
           for i in timed}
    roles = {i: _role_of(pairs, i, rows[i], reg.get(i)) for i in timed}
    mids = {i: (rows[i].start + rows[i].end) / 2.0 for i in timed}
    Kpx: dict[int, list[tuple[float, float, float, float]]] = {
        i: _reading_order([_px_cluster(c, rx, ry) for c in reg[i].clusters])
        for i in timed if _region_usable(reg[i])}
    out: dict[int, _RegionPlace] = {}
    own: dict[int, list[tuple[float, float, float, float]]] = {}
    korean: list[tuple[int, int, tuple[float, float, float, float]]] = []   # (start, end, rect)

    def _text_of(i: int) -> str:
        p = pairs[i]
        return p.translation or p.reading or p.source or ""

    claims_cache: dict[int, list[tuple[float, float, float, float]]] = {}

    def _claims(j: int) -> list[tuple[float, float, float, float]]:
        """줄 j 의 영역에서 '그 창에 새로 뜬' 덩어리 — 읽기 순서의 뒤쪽 m 개 (m = j 와
        창이 겹치는 같은 절의 구 수). 앞쪽 덩어리는 흘러서 변화로 잡힌 앞 줄 텍스트일
        수 있다 (迫りくる/暗い闇 블록: 二度と 창에 앞 두 행이 함께 잡힌 실측)."""
        if j not in claims_cache:
            mates = [k for k in timed if k > j and grp[k] == grp[j]
                     and rows[k].start <= rows[j].end and roles[k] == "verse"]
            K = _plausible_clusters(Kpx[j], [pairs[m].source for m in [j] + mates])
            claims_cache[j] = K[-(1 + len(mates)):] if K else []
        return claims_cache[j]

    def _dark_of(i: int) -> Optional[bool]:
        t = reg.get(i)
        if t is not None and t.sampled and t.confidence >= _REGION_DARK_MIN_CONF:
            return bool(t.dark_text)
        return None

    def _same_scene(i: int, j: int) -> bool:
        di, dj = _dark_of(i), _dark_of(j)
        if di is not None and dj is not None:
            return di == dj
        vi, vj = vis.get(i), vis.get(j)
        if vi is not None and vj is not None and vi.sampled and vj.sampled:
            return abs(vi.brightness - vj.brightness) <= _REGION_SCENE_BRIGHT
        return True

    seen_from: dict[tuple[float, float, float, float], int] = {}

    def _visible_from(o: tuple[float, float, float, float], j: int) -> int:
        """덩어리 o (줄 j 의 영역에서 봄)가 화면에 뜬 시각 추정. j 와 창이 겹치는 줄의
        텍스트로 확정(own)됐으면 그 줄의 시작 (흐르는 블록은 뒤 창에도 다시 잡히지만
        그 줄 것이다: 暗い闇; 창이 안 겹치는 줄의 비슷한 자리 덩어리는 다른 텍스트). 아니면
        영역이 '창 시작 직전 대비 변화' 만 담는다는 점을 써서 o 를 담은 영역들 중
        가장 늦게 시작하는 창의 시작 (계획 시각이 늦은 줄의 긴 창에 다음 절의
        텍스트가 섞인 실측: 手放せなかった 가 93.7s 창에도 있지만 95.97s 창이 그것을
        다시 담는다 → 95.97s 부터)."""
        key = o
        if key not in seen_from:
            rj = rows[j]
            owners = [rows[e].start for e, oc in own.items()
                      if rows[e].start <= rj.end and rows[e].end >= rj.start
                      and any(_cluster_match(o, om) for om in oc)]
            if owners:
                seen_from[key] = min(owners)
            else:
                seen_from[key] = max([rows[m].start for m in Kpx
                                      if any(_cluster_match(o, om) for om in Kpx[m])]
                                     + [rows[j].start])
        return seen_from[key]

    def _occupied(i: int, c: tuple[float, float, float, float]
                  ) -> list[tuple[float, float, float, float]]:
        """줄 i 가 보이는 동안(1s 이상 같이) 화면에 있는 다른 원문 덩어리 — 시간이
        겹치는 줄들의 영역·확정 덩어리 (앞 줄 원문은 계획된 끝 뒤 1.5s 까지 남아
        있다고 봄; 뜬 시각은 _visible_from), c 와 같은 자리(교체된 자리/같은
        텍스트)는 제외."""
        r = rows[i]
        occ: list[tuple[float, float, float, float]] = []
        for j in timed:
            rj = rows[j]
            if (rj.start + _REGION_OCCUPY_MS >= r.end
                    or rj.end + _REGION_LINGER_MS <= r.start + _REGION_OCCUPY_MS):
                continue
            for o in Kpx.get(j, []) + own.get(j, []):
                if _same_slot(o, c) or o in occ:
                    continue
                if _visible_from(o, j) + _REGION_OCCUPY_MS >= r.end:
                    continue
                occ.append(o)
        return occ

    def _korean_rects(i: int) -> list[tuple[float, float, float, float]]:
        r = rows[i]
        return [rect for s, e, rect in korean
                if min(e, r.end) - max(s, r.start) > _REGION_OCCUPY_MS]

    pending: dict[int, tuple[tuple[float, float, float, float], dict, bool]] = {}

    def _place(i: int, c: tuple[float, float, float, float], hint: dict,
               leader: bool) -> None:
        """1차: 덩어리 배정만 (소유 확정). 좌표는 모든 배정이 끝난 뒤 2차에서 —
        자리 점유(_occupied)가 뒤 줄의 소유까지 알아야 흐르는 블록을 옳게 본다."""
        pending[i] = (c, hint, leader)
        out[i] = _RegionPlace(x=c[0], y=c[1], bounds=_cluster_bbox([c]),
                              own=[c], hint=hint, leader=leader)
        own[i] = [c]

    for i in timed:
        r = rows[i]
        if i in out or roles[i] in ("tail", "prologue"):
            continue
        tr = reg.get(i)
        if not _region_usable(tr):
            continue
        assert tr is not None
        K = Kpx[i]
        dur = max(1, int(r.end) - int(r.start))
        # 샘플 구간은 영역이 실제로 잰 창 기준 — 재검출 창은 줄보다 2s 앞에서 시작하므로
        # 줄 구간으로 계산하면 drift 외삽 배수(dur/span)가 최대 3배까지 부풀고 오염
        # 판정(창 안에서 시작한 다른 줄)도 앞 2s 를 놓친다.
        ws, we = _region_window(tr, r)
        wdur = max(1, we - ws)
        edge = min(_REGION_EDGE_MS, 0.25 * wdur)
        last_sample = we - edge
        first_sample = ws + edge
        widened = ws < int(r.start)
        if roles[i] == "title":
            # 세로 기둥만 제목 (같은 창에 뜬 가로 줄은 뒤 줄이 자기 것으로 받는다)
            Kt = [c for c in K if c[3] > 1.5 * c[2]] or K
            bb = _cluster_bbox(Kt)
            out[i] = _RegionPlace(x=(bb[0] + bb[2]) / 2.0, y=(bb[1] + bb[3]) / 2.0,
                                  bounds=bb, own=Kt,
                                  hint=_region_hint(tr, Kt, None, False, None))
            own[i] = Kt
            continue
        # (a) 이전 텍스트 제거 — 영역은 '창 시작 직전 대비 변화' 라서 앞 줄의 정지
        # 텍스트는 원래 안 잡힌다. 잡혔다면 흘렀거나(옮겨진 자리) 막 뜬 줄의 페이드가
        # 기준 프레임에 걸린 것 — 그 둘만 앞 줄 것으로 본다. 같은 자리에 새로 뜬
        # 다른 텍스트(春の陽を ← 美しいとは)는 옮겨지지 않았으므로 이 줄 것.
        K2: list[tuple[float, float, float, float]] = []
        for c in K:
            old = False
            for e, oc in own.items():
                re_ = rows[e]
                if re_.start + _REGION_APPEAR_MS > r.start or re_.end + _REGION_LINGER_MS <= r.start:
                    continue
                recent = re_.start >= r.start - _REGION_RECENT_MS
                # 아직 보이는 앞 줄의 세로 기둥 안에 든 세로 조각 = 그려지는 중인 그 기둥
                # (인트로 제목이 4.9s 창에 절반 높이로 잡힌 실측 — 크기 조건에 안 걸림)
                drawing = re_.end > r.start
                if any((_cluster_match(c, o) and (recent or _moved(c, o)))
                       or (drawing and o[3] > 1.5 * o[2] and c[3] > 1.5 * c[2] and _inside(c, o))
                       for o in oc):
                    old = True
                    break
                if (e in Kpx and mids[e] <= r.start
                        and any(_cluster_match(c, o) and (recent or _moved(c, o))
                                for o in Kpx[e])):
                    old = True
                    break
            if not old:
                K2.append(c)
        if not K2:
            continue
        clean = len(K2) == len(K)
        polluted = any(first_sample < rows[j].start <= last_sample
                       for j in timed if j != i)
        if widened and not polluted:
            # 넓힌 앞 구간에서 앞 줄이 사라지면(페이드) 첫 샘플의 bbox 는 그 텍스트다
            polluted = any(first_sample < rows[j].end <= r.start for j in timed if j != i)
        drift: Optional[tuple[float, float]] = None
        single_line = all(c[3] <= _SINGLE_LINE_H_PX or c[3] > 1.5 * c[2] for c in K2)
        if (not polluted and clean and single_line
                and _REGION_DRIFT_SCALE[0] <= tr.scale <= _REGION_DRIFT_SCALE[1]):
            span = max(1.0, wdur - 2.0 * edge)      # 실제 샘플 구간 (첫~끝 샘플)
            k = min(_REGION_DRIFT_EXTRAP, dur / span)
            drift = _cap_drift((tr.drift[0] * rx * k, tr.drift[1] * ry * k))
        diagonal = (tr.layout == "diagonal" and len(K2) >= _DIAG_MIN_CLUSTERS
                    and tr.confidence >= _DIAG_MIN_CONF)
        if diagonal:
            bb = _cluster_bbox(K2)
            out[i] = _RegionPlace(x=(bb[0] + bb[2]) / 2.0, y=(bb[1] + bb[3]) / 2.0,
                                  bounds=bb, own=K2,
                                  hint=_region_hint(tr, K2, drift, True, tr.accent_color))
            own[i] = K2
            continue
        # (b) 이 창의 텍스트를 나눠 갖는 줄들
        members = [i]
        for j in timed:
            if j <= i or j in out or roles[j] != "verse":
                continue
            rj = rows[j]
            if rj.start > r.end:
                break
            same_group = grp[j] == grp[i]
            if j in Kpx:
                if same_group:
                    members.append(j)
                elif rj.start >= r.start + _REGION_APPEAR_MS:
                    # 다른 절의 뒤 줄이 자기 영역에서 새로 뜬 것으로 보는 덩어리 = 그 줄의
                    # 텍스트 (전부 그 줄 것이면 이 줄은 영역을 못 쓴다 — 창이 늦은
                    # 줄까지 늘어난 '포기하며' 가 다음 절 何も望まないように 를 잡은 실측)
                    K2 = [c for c in K2 if not any(_cluster_match(c, cj) for cj in _claims(j))]
                continue
            if rj.start > last_sample and not same_group:
                continue
            if not _same_scene(i, j):
                continue
            members.append(j)
        if not K2:
            continue
        K3 = _plausible_clusters(K2, [pairs[m].source for m in members])
        if len(K3) >= len(members):
            chosen = K3[-len(members):]        # 뒤쪽 = 최신 텍스트
        else:
            chosen = K3                        # 모자라면 앞 줄부터, 나머지는 자기 영역으로
        for n_, (j, c) in enumerate(zip(members, chosen)):
            last = n_ == len(chosen) - 1
            h = _region_hint(tr, K3, drift if j == i else None, False,
                             tr.accent_color if last else None)
            if j == i:
                h["drift_measured_px"] = (abs(tr.drift[0] * rx) ** 2 + abs(tr.drift[1] * ry) ** 2) ** 0.5
            _place(j, c, h, leader=(j == i))

    # 2차: 번역 자리 (시간 순서 — 먼저 놓인 번역을 피한다)
    for i in timed:
        if i not in pending:
            continue
        c, hint, leader = pending[i]
        # 세로 원문은 번역도 세로로 그린다(디렉터 vertical_title) — 옆자리 폭은 한 글자
        vertical = hint.get("layout") == "vertical"
        kw = _KOR_LINE_H if vertical else _est_width(_text_of(i))
        x, y = _offset_place(c, kw, _occupied(i, c), _korean_rects(i), rx, ry, vertical)
        if hint.get("drift") is not None:
            hint = dict(hint)
            hint["drift"] = _fit_drift(x, y, hint["drift"], rx, ry)
        rect = (x - kw / 2.0, y - _KOR_LINE_H / 2.0, x + kw / 2.0, y + _KOR_LINE_H / 2.0)
        out[i] = _RegionPlace(x=x, y=y,
                              bounds=tuple(int(round(v)) for v in rect),  # type: ignore[arg-type]
                              own=[c], hint=hint, leader=leader)
        korean.append((int(rows[i].start), int(rows[i].end), rect))

    # (c) 블록 드리프트 상속 — 같은 열에 행으로 쌓여 함께 흐르는 원문(遠ざかる/白い壁/
    # 一度も)은 창 안에서 다음 행이 뜨는 줄(bbox 드리프트 오염)이 많다. 드리프트를
    # 믿을 수 있는(잰 값이 잡음보다 큰) 같은 블록 줄의 속도(px/s)를 이 줄 길이로 —
    # 단 근거 줄 길이의 3배(_REGION_DRIFT_EXTRAP)까지만: 1.4s/35px 측정이 잘못
    # 잡힌 17s 줄에서 600px 이동이 되지 않게. 끝점은 프레임 안으로.
    sources = [(e, re_) for e, re_ in out.items()
               if re_.hint is not None and len(re_.own) == 1
               and re_.hint.get("drift") is not None and any(re_.hint["drift"])
               and re_.hint.get("drift_measured_px", 0.0) >= _REGION_DRIFT_NOISE_PX]
    for i, rp in out.items():
        if rp.hint is None or rp.hint.get("drift") is not None or len(rp.own) != 1:
            continue
        if rp.hint.get("layout") != "horizontal":
            continue
        c = rp.own[0]
        r = rows[i]
        best: Optional[tuple[float, float, float]] = None
        for e, re_ in sources:
            if e == i:
                continue
            d = re_.hint.get("drift")
            rr = rows[e]
            # 같은 블록은 창이 겹치거나 맞닿는다 (白い壁 창 끝 = 一度も 창 시작)
            if rr.end < r.start - _REGION_OCCUPY_MS or rr.start > r.end + _REGION_OCCUPY_MS:
                continue
            o = re_.own[0]
            if not (abs(o[0] - c[0]) < 0.5 * max(c[2], o[2], 1.0)
                    and _STACK_DY[0] <= abs(o[1] - c[1]) <= _INHERIT_DY_MAX):
                continue
            dur_e = max(1.0, (rr.end - rr.start) / 1000.0)
            gap = abs(o[1] - c[1])
            if best is None or gap < best[2]:
                best = (d[0] / dur_e, d[1] / dur_e, gap, dur_e)
        if best is not None:
            dur_i = max(1.0, (r.end - r.start) / 1000.0)
            span_i = min(dur_i, _REGION_DRIFT_EXTRAP * best[3])
            h = dict(rp.hint)
            h["drift"] = _fit_drift(rp.x, rp.y, _cap_drift((best[0] * span_i, best[1] * span_i)),
                                    rx, ry)
            h["drift_src"] = "block"
            rp.hint = h
    return out


def _cap_drift(d: tuple[float, float]) -> tuple[float, float]:
    n = (d[0] ** 2 + d[1] ** 2) ** 0.5
    if n <= _REGION_DRIFT_CAP_PX or n <= 0:
        return d
    k = _REGION_DRIFT_CAP_PX / n
    return (d[0] * k, d[1] * k)


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
    pinned: bool = False          # 좌표가 화면 텍스트 영역에서 왔는지
    bounds: Optional[tuple[int, int, int, int]] = None   # 영역 bbox (px) — 벌리기 한계
    hint: Optional[dict] = None   # 디렉터 힌트 (없으면 None)
    role: str = "verse"           # _role_of (영역 포함) — title|prologue|verse|tail


def _place_lines(
    pairs: list[LyricPair],
    rows: list[_Row],
    visuals: list[LineVisual],
    play_res_x: int,
    play_res_y: int,
    regions: Optional[list[Optional[TextRegion]]] = None,
    groups: Optional[list[int]] = None,
) -> list[_Placed]:
    """계획 + 시각 분석(+ 텍스트 영역) → 좌표/텍스트/흑백 결정 (태그 없음).

    compose_lines(태그 직접 생성)와 to_fx_lines(AI 연출 확장)가 같은 로직을
    쓰도록 뽑아낸 헬퍼. visuals·regions 는 시간 있는 줄 순서.

    좌표 우선순위: 텍스트 영역(_region_placements) > 등장 이벤트 중심(r.pos)
    > 장면 돌출 중심 > 하단 중앙. 흑백: 영역 dark_text(신뢰 ≥0.2) > 장면 밝기.
    드리프트(\\move): 영역 배치 줄은 영역 드리프트(신뢰 시 ≥40px) 만, 아니면
    장면 변화 중심의 드리프트.
    """
    out: list[_Placed] = []
    n_stack = sum(1 for r in rows if r.stack >= 0)
    regions = list(regions or [])
    placed_by_region = (_region_placements(pairs, rows, regions, play_res_x, play_res_y,
                                           groups, list(visuals))
                        if regions else {})
    wi = 0
    for i, (p, r) in enumerate(zip(pairs, rows)):
        if r.start is None:
            continue
        v = visuals[wi] if wi < len(visuals) else None
        tr = regions[wi] if wi < len(regions) else None
        wi += 1
        text = p.translation or p.reading or p.source
        if not text:
            continue
        rp = placed_by_region.get(i)
        if r.pos is not None:
            cx, cy = r.pos
        elif v is not None and v.sampled and v.salient > 0.002:
            cx, cy = v.gx, v.gy
        else:
            cx, cy = 0.5, 0.83
        x = round(min(_FRAME_HI, max(_FRAME_LO, cx)) * play_res_x)
        y = round(min(_FRAME_HI, max(_FRAME_LO, cy)) * play_res_y)
        if rp is not None:
            x = int(round(min(_FRAME_HI * play_res_x, max(_FRAME_LO * play_res_x, rp.x))))
            y = int(round(min(_FRAME_HI * play_res_y, max(_FRAME_LO * play_res_y, rp.y))))
        if r.stack >= 0:
            # 글자 스택 — 아래→위 (완성본 패턴). x 는 영역이 잡은 기둥 위치.
            y = round(play_res_y * (0.833 - r.stack * (0.6 / max(1, n_stack - 1))))
            if _region_usable(tr):
                x = int(round(min(_FRAME_HI * play_res_x,
                                  max(_FRAME_LO * play_res_x, tr.cx * play_res_x))))
        if tr is not None and tr.sampled and tr.confidence >= _REGION_DARK_MIN_CONF:
            dark = bool(tr.dark_text)
        else:
            dark = bool(v is not None and v.sampled
                        and v.brightness > _DARK_BRIGHTNESS)
        dx = dy = 0
        if rp is not None:
            d = rp.hint.get("drift") if rp.hint else None
            if r.stack < 0 and d is not None:
                dxf, dyf = _fit_drift(x, y, d, play_res_x, play_res_y)
                if (dxf ** 2 + dyf ** 2) ** 0.5 >= _REGION_MOVE_PX:
                    dx, dy = int(round(dxf)), int(round(dyf))
        else:
            drift = (abs(v.gx1 - v.gx0) + abs(v.gy1 - v.gy0)
                     if v is not None and v.sampled and v.salient > 0.003 else 0.0)
            if r.stack < 0 and drift > _MOVE_DRIFT:
                dxf, dyf = _fit_drift(x, y, ((v.gx1 - v.gx0) * play_res_x,
                                             (v.gy1 - v.gy0) * play_res_y),
                                      play_res_x, play_res_y)
                dx, dy = round(dxf), round(dyf)
        out.append(_Placed(index=i, row=r, visual=v, text=text,
                           x=x, y=y, dark=dark, dx=dx, dy=dy,
                           pinned=rp is not None,
                           bounds=rp.bounds if rp is not None else None,
                           hint=rp.hint if rp is not None else None,
                           role=_role_of(pairs, i, r, tr)))
    return out


def compose_lines(
    pairs: list[LyricPair],
    rows: list[_Row],
    visuals: list[LineVisual],
    play_res_x: int = 1920,
    play_res_y: int = 1080,
    regions: Optional[list[Optional[TextRegion]]] = None,
    groups: Optional[list[int]] = None,
) -> list[PlannedLine]:
    """계획 + 시각 분석(+ 텍스트 영역) → 태그 붙은 최종 줄. visuals·regions 는 시간 있는 줄 순서.

    같은 자리(같은 영역 bbox 를 공유하는 구들 등)에 동시에 뜨는 줄은
    _spread_collisions 로 벌린다 — 영역 좌표는 고정(pinned)이라 서로 떨어진
    영역 줄끼리는 움직이지 않는다.
    """
    placed = _place_lines(pairs, rows, visuals, play_res_x, play_res_y, regions, groups)
    # 글자 스택(꼬리)은 세로 기둥이라 벌리기에서 제외 — 글자 간격(<220px)이 충돌로 보인다.
    body = [pl for pl in placed if pl.row.stack < 0]
    pts = [_XY(pl.text, int(pl.row.start), int(pl.row.end), pl.x, pl.y) for pl in body]
    _spread_collisions(pts, play_res_x, play_res_y,
                       fixed={k for k, pl in enumerate(body) if pl.role == "title"},
                       pinned={k for k, pl in enumerate(body) if pl.pinned},
                       bounds={k: pl.bounds for k, pl in enumerate(body)
                               if pl.bounds is not None})
    for pl, pt in zip(body, pts):
        if (pl.x, pl.y) != (pt.x, pt.y) and (pl.dx or pl.dy):
            # 벌리기로 옮겨진 줄의 \move 끝점도 프레임 안에 (영역 줄은 pinned 라 안 옮겨짐)
            dxf, dyf = _fit_drift(pt.x, pt.y, (pl.dx, pl.dy), play_res_x, play_res_y)
            pl.dx, pl.dy = int(round(dxf)), int(round(dyf))
        pl.x, pl.y = pt.x, pt.y
    out: list[PlannedLine] = []
    for pl in placed:
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

def _role_of(pairs: list[LyricPair], i: int, r: _Row,
             region: Optional[TextRegion] = None) -> str:
    """줄의 역할 — 디렉터가 fx 를 고르는 사전정보.

    tail: 꼬리 글자 스택 / title: 제목 카드 / prologue: 원문이 비일본어(영어
    머리말 등)이고 번역이 여러 행 / verse: 그 외.

    title 은 첫 줄이 '노래 구가 아니라는 근거' 가 있을 때만: 다른 쌍에는 독음이
    있는데 이 줄만 없고(3행 형식에서 제목 카드는 독음이 없다), 보컬/그래픽
    정렬 근거 없이 gap 으로 시간이 잡힌 경우. 2행(원문/번역)·원문만 형식은
    모든 쌍이 독음이 없으므로 첫 가사 줄을 세로 제목으로 오판하지 않는다.
    제목 텍스트를 다시 부르는 가사 줄(星空へと続く長い坂道は, 유사도 ≥0.75)은
    화면 텍스트 영역(region)이 세로 배치일 때만 제목으로 본다 — 레퍼런스는 그
    줄을 세로 제목 카드로 다시 그린다.
    """
    from ai.lyric_normalize import detect_language
    p = pairs[i]
    if r.stack >= 0:
        return "tail"
    if (i == 0 and not p.reading and r.via in ("gap", "-", "vocal0")
            and any(q.reading for q in pairs)):
        return "title"
    if (i > 0 and region is not None and region.layout == "vertical"
            and p.source and pairs[0].source and not pairs[0].reading
            and any(q.reading for q in pairs)
            and _letters_similarity(pairs[0].source, p.source) >= 0.75):
        return "title"
    tr = p.translation or ""
    if (p.source and detect_language(p.source) != "ja"
            and ("\\N" in tr or "\\n" in tr or "\n" in tr)):
        return "prologue"
    return "verse"


def _letters_similarity(a: str, b: str) -> float:
    """글자(L/N)만 남긴 두 문자열의 SequenceMatcher 비율 (0..1)."""
    from difflib import SequenceMatcher
    la = "".join(ch for ch in a if unicodedata.category(ch)[0] in ("L", "N"))
    lb = "".join(ch for ch in b if unicodedata.category(ch)[0] in ("L", "N"))
    if not la or not lb:
        return 0.0
    return SequenceMatcher(None, la, lb).ratio()


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
_PINNED_APART_PX = 100.0  # 화면 텍스트에 고정된 두 줄이 이만큼 떨어져 있으면 충돌 아님 (원문 행 간격 실측 ~134px)


@dataclass(slots=True)
class _XY:
    """_spread_collisions 입력 최소 형태 (FxLine 과 같은 속성)."""
    text: str
    start_ms: int
    end_ms: int
    x: int
    y: int


def _spread_collisions(lines: list, rx: int, ry: int,
                       fixed: Optional[set[int]] = None,
                       pinned: Optional[set[int]] = None,
                       bounds: Optional[dict[int, tuple[int, int, int, int]]] = None) -> int:
    """동시에(≥1s) 보이면서 중심이 가까운(<220px) 줄 무리를 좌우/상하로 벌린다.

    같은 절의 구들(블록 페이드)과 인트로의 gap 줄들은 같은 교체 이벤트 중심을
    받아 한 자리에 쌓인다. 레퍼런스는 그런 쌍을 좌우로(x 650/1445), 서너 줄은
    아래로 흐르는 계단으로 배치한다: 2줄은 두 줄의 추정 폭이 열 간격에 들어가면
    2열(레퍼런스 '아무것도 바라지 않으려고'/'그렇지만' 650/1445), 아니면 1열 계단;
    3~6줄은 1열 계단; 7줄 이상은 2~4열 격자(행 간격 220px). 무리 전체를 추정
    폭까지 포함해 프레임 안(8~92%)으로 민 뒤 클램프. fixed 에 든 줄(세로 제목 —
    프레임 높이 대부분을 차지하는 기둥)은 움직이지 않고, 같은 무리의 나머지를
    기둥에서 0.30W 떨어진 쪽(기둥이 오른쪽이면 왼쪽)에 배치한다.

    pinned 에 든 줄은 좌표가 화면 텍스트 영역에서 온 줄: 둘 다 pinned 이고
    100px 이상 떨어져 있으면(원문의 다른 행) 충돌로 보지 않는다. 무리에
    pinned 와 아닌 줄이 섞이면 pinned 는 그대로 두고 나머지를 그 아래(자리가
    없으면 위)에 둔다. 같은 자리를 공유하는 pinned 들(덩어리가 모자라 bbox
    중심을 나눠 가진 구들)은 bounds 의 합집합 중심에서 벌리되, 2열 간격은
    bbox 폭의 절반 이상으로 한다. 반환: 좌표가 바뀐 줄 수. 결정적.
    """
    fixed = set(fixed or ())
    pinned = set(pinned or ()) - fixed
    bounds = dict(bounds or {})
    n = len(lines)
    parent = list(range(n))

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def _dist(i: int, j: int) -> float:
        a, b = lines[i], lines[j]
        return ((a.x - b.x) ** 2 + (a.y - b.y) ** 2) ** 0.5

    for i in range(n):
        a = lines[i]
        for j in range(i + 1, n):
            b = lines[j]
            if min(a.end_ms, b.end_ms) - max(a.start_ms, b.start_ms) < _COLLIDE_MS:
                continue
            d = _dist(i, j)
            if d >= _COLLIDE_PX:
                continue
            if i in pinned and j in pinned and d >= _PINNED_APART_PX:
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
        below = False
        if not anchors:
            pins = [t for t in idxs if t in pinned]
            if (pins and len(pins) < len(idxs)
                    and all(_dist(pins[a], pins[b]) >= _PINNED_APART_PX
                            for a in range(len(pins)) for b in range(a + 1, len(pins)))):
                # 화면 텍스트에 고정된 줄은 그대로, 나머지만 그 아래로
                anchors = pins
                idxs = [t for t in idxs if t not in pinned]
                below = True
        k = len(idxs)
        if k < 1:
            continue
        idxs.sort(key=lambda t: (lines[t].start_ms, t))
        cy = sum(lines[t].y for t in idxs) / k
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
        col_x = _COLLIDE_COL_X * rx
        if anchors and below:
            ax = sum(lines[t].x for t in anchors) / len(anchors)
            ay = sum(lines[t].y for t in anchors) / len(anchors)
            cx = ax
            top = ay + _COLLIDE_GRID_ROW
            if top + (nrows - 1) * dy > hi_y and ay - _COLLIDE_GRID_ROW >= lo_y:
                top = ay - _COLLIDE_GRID_ROW - (nrows - 1) * dy
            cy = top + (nrows - 1) * dy / 2.0
        elif anchors:
            ax = sum(lines[t].x for t in anchors) / len(anchors)
            cx = ax - _COLLIDE_ANCHOR_X * rx if ax >= rx / 2.0 else ax + _COLLIDE_ANCHOR_X * rx
        elif all(t in bounds for t in idxs):
            # 같은 영역 bbox 를 나눠 가진 구들 — 그 bbox 안(폭이 모자라면 겹치지 않을 만큼)에서
            bx0 = min(bounds[t][0] for t in idxs)
            bx1 = max(bounds[t][2] for t in idxs)
            by0 = min(bounds[t][1] for t in idxs)
            by1 = max(bounds[t][3] for t in idxs)
            cx, cy = (bx0 + bx1) / 2.0, (by0 + by1) / 2.0
            if ncols == 2:
                col_x = max((bx1 - bx0) / 4.0,
                            (widths[0] + widths[1]) / 4.0 + _COLLIDE_GAP_PX / 2.0)
        else:
            cx = sum(lines[t].x for t in idxs) / k
        pts: list[tuple[float, float]] = []
        for s in range(k):
            col, row = s % ncols, s // ncols
            if k == 1:
                ox = 0.0
            elif ncols == 1:
                ox = (_COLLIDE_WOBBLE_X if s % 2 else -_COLLIDE_WOBBLE_X) * rx
            elif ncols == 2:
                ox = col_x if col else -col_x
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


def place_fx_lines(
    pairs: list[LyricPair],
    rows: list[_Row],
    visuals: list[LineVisual],
    play_res: tuple[int, int] = (1920, 1080),
    regions: Optional[list[Optional[TextRegion]]] = None,
    groups: Optional[list[int]] = None,
) -> "tuple[list, list[str], list[int], list[Optional[dict]]]":
    """계획 + 시각 분석(+ 텍스트 영역) → 태그 없는 FxLine 목록 (AI 연출 디렉터 입력).

    compose_lines 와 같은 좌표/텍스트/스타일/흑백 결정을 공유하되, 동시에 같은
    자리에 뜨는 줄들은 _spread_collisions 로 좌우/계단 배치한다 (디렉터·확장기
    전 단계 — 글자별/잔상/막대 연출이 한 점에 쌓이지 않게). 텍스트 영역에서
    온 좌표는 pinned(서로 떨어진 영역 줄은 안 움직임), 대각선 영역 줄의 (x,y)
    는 첫 글자 덩어리 중심(char_diagonal 의 시작점).

    Returns:
        (fx_lines, roles, row_indices, hints) — 병렬 리스트. roles 는
        title|prologue|verse|tail, row_indices 는 각 FxLine 의 pairs 인덱스,
        hints 는 typeset_director.hint_fx 가 읽는 화면 측정 힌트(없으면 None).

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
    placed = _place_lines(pairs, rows, visuals, rx, ry, regions, groups)
    fx_lines: list = []
    roles: list[str] = []
    row_indices: list[int] = []
    hints: list[Optional[dict]] = []
    pinned: set[int] = set()
    bounds: dict[int, tuple[int, int, int, int]] = {}
    tail: list[_Placed] = []
    for pl in placed:
        r = pl.row
        if r.stack >= 0:
            tail.append(pl)
            continue
        x, y = pl.x, pl.y
        if pl.hint and pl.hint.get("layout") == "diagonal" and pl.hint.get("diag_start"):
            sx, sy = pl.hint["diag_start"]
            x, y = int(round(sx)), int(round(sy))
        if pl.pinned:
            pinned.add(len(fx_lines))
        if pl.bounds is not None:
            bounds[len(fx_lines)] = pl.bounds
        fx_lines.append(FxLine(
            text=pl.text, start_ms=int(r.start), end_ms=int(r.end),
            style=DARK_STYLE if pl.dark else LIGHT_STYLE,
            x=x, y=y, dark=pl.dark))
        roles.append(pl.role)
        row_indices.append(pl.index)
        hints.append(pl.hint)
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
        hints.append(None)
    _spread_collisions(fx_lines, rx, ry,
                       fixed={k for k, role in enumerate(roles) if role == "title"},
                       pinned=pinned, bounds=bounds)
    # 시작 시간 순서 유지 (꼬리 합본은 원래도 마지막이지만 안전하게)
    order = sorted(range(len(fx_lines)),
                   key=lambda k: (fx_lines[k].start_ms, row_indices[k]))
    return ([fx_lines[k] for k in order], [roles[k] for k in order],
            [row_indices[k] for k in order], [hints[k] for k in order])


def to_fx_lines(
    pairs: list[LyricPair],
    rows: list[_Row],
    visuals: list[LineVisual],
    play_res: tuple[int, int] = (1920, 1080),
    regions: Optional[list[Optional[TextRegion]]] = None,
    groups: Optional[list[int]] = None,
) -> "tuple[list, list[str], list[int]]":
    """place_fx_lines 의 (fx_lines, roles, row_indices) — 힌트가 필요 없는 호출자용."""
    fx_lines, roles, row_indices, _hints = place_fx_lines(
        pairs, rows, visuals, play_res, regions, groups)
    return fx_lines, roles, row_indices


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
