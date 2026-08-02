"""사용자가 붙여넣은 가사 텍스트(원문/한국어 번역) 파싱 + 이벤트 매핑.

자막 텍스트가 한국어뿐이면 일본어 노래의 transcript 와 매칭이 거의 안 된다.
사용자가 실제 불리는 원문 가사(일본어 등)를 붙여넣으면, 그 텍스트를 해당
줄의 정렬 기준(ref)으로 써서 발음 공간에서 정확히 매칭할 수 있다.

붙여넣기 형식은 자유:
  · 원문만 — 한 줄에 하나. 대상 줄에 순서대로 1:1 연결.
  · 원문 + 한국어 번역 교차 — 한국어 줄이 직전 원문 블록을 닫는다.
    번역 텍스트 유사도로 기존 자막 줄을 자동으로 찾아 연결한다.

분류 규칙: 한국어(한글 포함) 줄 = 번역, 그 외 모든 줄 = 원문.
(일본 노래에 영어 소절이 흔하므로 ja 로 한정하지 않는다.)
"""
from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher

from ai.lyric_normalize import detect_language, strip_ass_text, tokenize


@dataclass(slots=True)
class LyricPair:
    """가사 한 줄 단위 — 원문(불리는 텍스트)과 한국어 번역."""
    source: str | None
    translation: str | None


@dataclass(slots=True)
class RefMapResult:
    """build_ref_map 결과."""
    ref_texts: dict[str, str]     # event_id -> 정렬 기준 원문
    matched_ids: list[str]        # 가사와 연결된 이벤트 (표시 순서)
    n_pairs: int                  # 파싱된 가사 쌍 수
    n_unmatched: int              # 이벤트를 못 찾은 쌍 수
    by_translation: bool          # True=번역 유사도 매칭, False=순서 1:1


def parse_lyric_pairs(raw: str) -> list[LyricPair]:
    """붙여넣은 텍스트를 (원문, 번역) 쌍 리스트로.

    한국어 줄이 하나도 없으면 각 줄이 독립 원문. 있으면 한국어 줄이
    직전까지 쌓인 원문 블록을 닫는다 (원문 2줄 + 번역 1줄도 한 쌍).
    """
    lines = [ln.strip() for ln in raw.replace("\r", "").split("\n")]
    lines = [ln for ln in lines if ln]
    if not lines:
        return []
    is_ko = [detect_language(ln) == "ko" for ln in lines]
    if not any(is_ko):
        return [LyricPair(ln, None) for ln in lines]

    pairs: list[LyricPair] = []
    pending: list[str] = []
    for ln, ko in zip(lines, is_ko):
        if not ko:
            pending.append(ln)
            continue
        pairs.append(LyricPair(" ".join(pending) if pending else None, ln))
        pending = []
    if pending:
        pairs.append(LyricPair(" ".join(pending), None))
    return pairs


def _norm_ko(text: str) -> str:
    """유사도 비교용 정규화 — 문자/숫자만 남긴 연속 문자열."""
    return "".join(tokenize(text, "ko"))


def _similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def build_ref_map(
    raw: str,
    events: list[tuple[str, str]],
) -> RefMapResult:
    """가사 텍스트를 이벤트에 연결해 정렬 ref 맵을 만든다.

    Args:
        raw: 붙여넣은 가사 텍스트.
        events: (event_id, 이벤트 텍스트) — 표시(시간) 순서. 주석 제외.

    Returns:
        RefMapResult. 번역이 있으면 유사도 매칭(순서 단조 증가),
        원문만이면 앞에서부터 순서대로 1:1.
    """
    pairs = parse_lyric_pairs(raw)
    if not pairs or not events:
        return RefMapResult({}, [], len(pairs), len(pairs), False)

    has_translation = any(p.translation for p in pairs)
    ref_texts: dict[str, str] = {}
    matched_ids: list[str] = []
    n_unmatched = 0

    if not has_translation:
        for pair, (eid, _txt) in zip(pairs, events):
            if pair.source:
                ref_texts[eid] = pair.source
                matched_ids.append(eid)
        n_unmatched = max(0, len(pairs) - len(events))
        return RefMapResult(ref_texts, matched_ids, len(pairs), n_unmatched, False)

    # 번역 유사도 매칭 — 가사 순서와 자막 순서는 같다고 보고 단조 진행.
    # 거의 같은 텍스트(자막이 이 번역으로 만들어진 경우)가 보통이므로
    # 높은 유사도를 먼저 찾고, 없으면 남은 것 중 최고를 느슨하게 허용한다.
    ev_norm = [(eid, _norm_ko(strip_ass_text(txt))) for eid, txt in events]
    cursor = 0
    for pair in pairs:
        if not pair.translation:
            n_unmatched += 1
            continue
        tnorm = _norm_ko(pair.translation)
        found = -1
        best_i, best_sim = -1, 0.0
        for i in range(cursor, len(ev_norm)):
            sim = _similarity(tnorm, ev_norm[i][1])
            if sim >= 0.75:
                found = i
                break
            if sim > best_sim:
                best_sim, best_i = sim, i
        idx = found if found >= 0 else (best_i if best_sim >= 0.5 else -1)
        if idx < 0:
            n_unmatched += 1
            continue
        eid = ev_norm[idx][0]
        matched_ids.append(eid)
        if pair.source:
            ref_texts[eid] = pair.source
        cursor = idx + 1
    return RefMapResult(ref_texts, matched_ids, len(pairs), n_unmatched, True)
