"""DTW 기반 가사 ↔ transcript 정렬.

핵심 흐름:
    1. 모든 라인 가사를 토큰 시퀀스로 변환 (라인별 토큰 인덱스 기록)
    2. transcript 의 모든 단어/문자를 토큰 시퀀스로 변환 (단어별 인덱스 기록)
    3. DTW 로 두 시퀀스를 정렬 (cost: 토큰 동일성)
    4. 라인별 토큰 인덱스 범위에 매칭된 transcript 단어들의 첫·마지막 시간을
       해당 라인의 start/end 로 사용

LOCKED 라인은 anchor 로 사용되어 시간이 보존되며,
anchor 사이 구간만 DTW 로 정렬한다.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import numpy as np

from ai.lyric_normalize import tokenize, tokens_match
from ai.transcription import TranscriptionResult, Word

log = logging.getLogger(__name__)


def _preview(text: str, n: int = 40) -> str:
    s = text.replace("\n", " ").replace("\r", "").strip()
    return s if len(s) <= n else s[:n] + "..."


@dataclass(slots=True)
class LineAlignment:
    """단일 라인의 정렬 결과."""
    event_id: str
    start_ms: int
    end_ms: int
    matched_token_count: int
    total_token_count: int
    avg_word_prob: float

    @property
    def match_ratio(self) -> float:
        if self.total_token_count == 0:
            return 0.0
        return self.matched_token_count / self.total_token_count


@dataclass(slots=True)
class _LineSpec:
    event_id: str
    text: str
    locked: bool
    locked_start_ms: int
    locked_end_ms: int


def _tokenize_lines(specs: list[_LineSpec], language: str) -> tuple[list[str], list[tuple[int, int]]]:
    """모든 라인을 합쳐 토큰 시퀀스 + 라인별 [start, end) 인덱스 범위."""
    all_tokens: list[str] = []
    spans: list[tuple[int, int]] = []
    for sp in specs:
        toks = tokenize(sp.text, language)
        start = len(all_tokens)
        all_tokens.extend(toks)
        end = len(all_tokens)
        spans.append((start, end))
    return all_tokens, spans


def _tokenize_words(words: list[Word], language: str) -> tuple[list[str], list[int]]:
    """transcript 단어 시퀀스 → 토큰 시퀀스 + 토큰→단어인덱스 맵.

    문자 기반 언어(ja/ko)는 한 단어가 여러 토큰이 될 수 있으므로 단어 인덱스 추적.
    """
    tokens: list[str] = []
    word_idx: list[int] = []
    for wi, w in enumerate(words):
        toks = tokenize(w.text, language)
        for t in toks:
            tokens.append(t)
            word_idx.append(wi)
    return tokens, word_idx


def _dtw_path(a: list[str], b: list[str]) -> list[tuple[int, int]]:
    """단순 DTW: 비용 = 0(매치)/1(미스), 출력은 매치된 (i, j) 페어 리스트.

    a 가 가사 토큰, b 가 transcript 토큰. 둘 다 빈 경우 빈 리스트.
    너무 큰 경우(> 4000x4000) 메모리 보호로 빈 리스트 반환.
    """
    n, m = len(a), len(b)
    if n == 0 or m == 0:
        return []
    if n * m > 16_000_000:
        log.warning("DTW 매트릭스 너무 큼 (%dx%d) — 매칭 생략", n, m)
        return []

    INF = np.float32(1e9)
    cost = np.empty((n + 1, m + 1), dtype=np.float32)
    cost.fill(INF)
    cost[0, 0] = 0.0
    # back-pointer: 0=diag, 1=up(skip a), 2=left(skip b)
    back = np.zeros((n + 1, m + 1), dtype=np.int8)

    a_norm = a
    b_norm = b
    for i in range(1, n + 1):
        ai = a_norm[i - 1]
        for j in range(1, m + 1):
            local = 0.0 if ai == b_norm[j - 1] else 1.0
            d = cost[i - 1, j - 1] + local
            u = cost[i - 1, j] + 1.0  # gap in transcript
            l = cost[i, j - 1] + 1.0  # gap in lyric
            best = d
            bi = 0
            if u < best:
                best = u
                bi = 1
            if l < best:
                best = l
                bi = 2
            cost[i, j] = best
            back[i, j] = bi

    # 백트래킹: lyric 토큰 i 가 매칭된 transcript 토큰 j 들의 페어만 보존
    pairs: list[tuple[int, int]] = []
    i, j = n, m
    while i > 0 and j > 0:
        bi = back[i, j]
        if bi == 0:
            if a_norm[i - 1] == b_norm[j - 1]:
                pairs.append((i - 1, j - 1))
            i -= 1
            j -= 1
        elif bi == 1:
            i -= 1
        else:
            j -= 1
    pairs.reverse()
    return pairs


def _align_segment(
    specs: list[_LineSpec],
    words: list[Word],
    language: str,
    fallback_start_ms: int,
    fallback_end_ms: int,
) -> list[LineAlignment]:
    """anchor 사이의 한 segment 만 DTW. specs 는 모두 unlocked."""
    if not specs:
        return []

    lyric_tokens, lyric_spans = _tokenize_lines(specs, language)
    word_tokens, word_idx_map = _tokenize_words(words, language)

    pairs = _dtw_path(lyric_tokens, word_tokens)
    log.info(
        "DTW segment: 라인=%d, 가사 토큰=%d, transcript 토큰=%d, 매칭 페어=%d, 구간=%d~%dms",
        len(specs), len(lyric_tokens), len(word_tokens), len(pairs),
        fallback_start_ms, fallback_end_ms,
    )
    if log.isEnabledFor(logging.DEBUG):
        log.debug("  가사 토큰 첫 30: %s", lyric_tokens[:30])
        log.debug("  transcript 토큰 첫 30: %s", word_tokens[:30])

    # lyric_token_idx -> word_idx
    lyric_to_word: dict[int, int] = {p[0]: word_idx_map[p[1]] for p in pairs}

    out: list[LineAlignment] = []
    for spec, (ls, le) in zip(specs, lyric_spans):
        matched_word_indices: list[int] = []
        for li in range(ls, le):
            wi = lyric_to_word.get(li)
            if wi is not None:
                matched_word_indices.append(wi)

        matched_count = sum(1 for li in range(ls, le) if li in lyric_to_word)
        total_count = le - ls

        if matched_word_indices:
            first_w = words[min(matched_word_indices)]
            last_w = words[max(matched_word_indices)]
            start_ms = first_w.start_ms
            end_ms = max(last_w.end_ms, start_ms + 100)
            avg_prob = float(np.mean([words[wi].prob for wi in matched_word_indices]))
            log.debug(
                "  매칭 %d/%d: id=%s %d→%dms (avg_prob=%.2f) text='%s'",
                matched_count, total_count, spec.event_id[:8],
                start_ms, end_ms, avg_prob, _preview(spec.text),
            )
        else:
            # 매칭 0 — segment 의 비례 추정값 사용
            n_lines = len(specs)
            idx = specs.index(spec)
            seg_dur = max(0, fallback_end_ms - fallback_start_ms)
            start_ms = fallback_start_ms + seg_dur * idx // max(1, n_lines)
            end_ms = fallback_start_ms + seg_dur * (idx + 1) // max(1, n_lines)
            avg_prob = 0.0
            log.warning(
                "  매칭 0/%d (fallback): id=%s %d→%dms text='%s'",
                total_count, spec.event_id[:8],
                start_ms, end_ms, _preview(spec.text),
            )

        out.append(LineAlignment(
            event_id=spec.event_id,
            start_ms=start_ms,
            end_ms=end_ms,
            matched_token_count=matched_count,
            total_token_count=total_count,
            avg_word_prob=avg_prob,
        ))
    return out


@dataclass(slots=True)
class _Anchor:
    """LOCKED 라인 — 시간이 고정된 hard anchor."""
    event_id: str
    start_ms: int
    end_ms: int


def align_lines_to_transcript(
    lines: list[tuple[str, str, bool, int, int]],
    transcript: TranscriptionResult,
    language: Optional[str] = None,
    audio_duration_ms: int = 0,
) -> list[LineAlignment]:
    """가사 라인들을 transcript 에 정렬.

    Args:
        lines: (event_id, text, locked, start_ms, end_ms) 튜플 리스트.
               순서대로 가사 진행 순서를 의미. locked=True 면 anchor.
        transcript: faster-whisper 결과.
        language: ISO 언어 코드. None 이면 transcript.language 사용.
        audio_duration_ms: 오디오 전체 길이 (anchor 외삽용).

    Returns:
        LineAlignment 리스트. unlocked 라인만 포함 (locked 는 그대로 두면 됨).
    """
    if not lines:
        return []
    lang = language or transcript.language or ""
    words = transcript.all_words()

    specs = [_LineSpec(eid, text, lock, ls, le) for (eid, text, lock, ls, le) in lines]
    locked_count = sum(1 for s in specs if s.locked)
    log.info(
        "정렬 시작: 라인=%d (locked=%d), transcript words=%d, language=%s, audio=%dms",
        len(specs), locked_count, len(words), lang, audio_duration_ms,
    )
    if log.isEnabledFor(logging.DEBUG) and words:
        log.debug(
            "transcript 첫 20단어: %s",
            " ".join(w.text for w in words[:20]),
        )

    # anchor 위치 분리: locked 라인 사이의 unlocked 묶음 단위로 DTW
    results: list[LineAlignment] = []
    n = len(specs)
    i = 0
    # 시작 anchor: 가장 첫 locked 라인. 없으면 0.
    prev_anchor_ms = 0
    prev_anchor_idx = -1
    # 미리 anchor 위치 수집
    anchor_positions = [k for k, sp in enumerate(specs) if sp.locked]

    if not anchor_positions:
        # 전체 한 segment
        end_anchor_ms = audio_duration_ms or (words[-1].end_ms if words else 0)
        results = _align_segment(specs, words, lang, 0, end_anchor_ms)
        return results

    # 첫 anchor 이전의 unlocked 묶음
    first_anchor = anchor_positions[0]
    if first_anchor > 0:
        unlocked = specs[:first_anchor]
        seg_words = [w for w in words if w.end_ms <= specs[first_anchor].locked_start_ms]
        results.extend(_align_segment(
            unlocked, seg_words, lang,
            0, specs[first_anchor].locked_start_ms,
        ))

    # anchor 사이의 unlocked 묶음
    for ai in range(len(anchor_positions) - 1):
        a = anchor_positions[ai]
        b = anchor_positions[ai + 1]
        if b - a <= 1:
            continue
        unlocked = specs[a + 1: b]
        a_end = specs[a].locked_end_ms
        b_start = specs[b].locked_start_ms
        seg_words = [w for w in words if w.start_ms >= a_end and w.end_ms <= b_start]
        results.extend(_align_segment(unlocked, seg_words, lang, a_end, b_start))

    # 마지막 anchor 이후의 unlocked 묶음
    last_anchor = anchor_positions[-1]
    if last_anchor < n - 1:
        unlocked = specs[last_anchor + 1:]
        a_end = specs[last_anchor].locked_end_ms
        end_ms = audio_duration_ms or (words[-1].end_ms if words else a_end)
        seg_words = [w for w in words if w.start_ms >= a_end]
        results.extend(_align_segment(unlocked, seg_words, lang, a_end, end_ms))

    return results
