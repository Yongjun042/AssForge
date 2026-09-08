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

from difflib import SequenceMatcher

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
    """부분 정렬 DTW(subsequence DTW): 비용 = 0(매치)/1(미스), 출력은 매치된
    (i, j) 페어 리스트.

    a 가 가사 토큰, b 가 transcript 토큰. 둘 다 빈 경우 빈 리스트.
    너무 큰 경우(> 4000x4000) 메모리 보호로 빈 리스트 반환.

    transcript 쪽의 앞·뒤 gap 은 비용 0 이다(가사가 곡의 일부만 덮어도 됨).
    전역 DTW 는 남은 transcript 토큰을 전부 '건너뛰기(비용 1)' 로 소비해야
    해서, 가사가 곡 앞부분만일 때 마지막 줄들을 곡 끝 쪽으로 끌어당긴다
    (실측: 가사 일부만 붙여넣자 30초 줄이 82초로, 이후 줄은 96초로 밀렸다).
    가사 쪽 gap(가사 토큰 건너뛰기)은 여전히 비용 1 — 가사 토큰은 모두
    소비해야 하므로 특정 구간으로 붕괴하지 않는다.
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
    cost[0, :] = 0.0   # 자유 시작: transcript 어디서든 가사가 시작할 수 있다
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

    # 자유 끝: 마지막 가사 토큰 뒤의 transcript 는 비용 없이 버린다 —
    # cost[n, j] 가 최소인 j 에서 끝낸다 (동률이면 앞쪽, 즉 이른 위치).
    j_end = int(np.argmin(cost[n, 1:])) + 1

    # 백트래킹: lyric 토큰 i 가 매칭된 transcript 토큰 j 들의 페어만 보존
    pairs: list[tuple[int, int]] = []
    i, j = n, j_end
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


# ── 구절(세그먼트) 앵커 ──────────────────────────────────────────
# 글자 단위 DTW 는 히라가나 우연 일치가 흔해, 가사가 곡의 일부만 덮거나
# 전사의 일부 구간이 환청이면 가사 전체가 엉뚱한 구간으로 흘러간다
# (실측: 가사 14절만 붙여넣자 30초 줄이 74~96초로 밀림). 사람이 하듯
# 먼저 '이 줄은 이 세그먼트' 를 구절 유사도로 확정해 시간 앵커로 삼고,
# 나머지 줄만 앵커 사이에서 DTW 한다.
_ANCHOR_MIN_COVER = 0.6      # 가사 토큰 중 세그먼트에 (순서대로) 담긴 비율
_ANCHOR_MIN_BLOCK = 3        # 연속 일치 토큰 최소 길이 (우연 일치 배제)


def _seg_tokens(seg, language: str) -> list[str]:
    text = seg.text or " ".join(w.text for w in seg.words)
    return tokenize(text, language)


def _refine_in_segment(lt: list[str], seg, language: str) -> tuple[int, int, int, float]:
    """가사 토큰이 세그먼트 안 어느 단어들에 놓이는지 — (start, end, matched, prob)."""
    wt, widx = _tokenize_words(seg.words, language)
    if wt:
        pairs = _dtw_path(lt, wt)   # 부분 정렬 DTW — 세그먼트 안에서 구절 위치를 찾는다
        if pairs:
            wis = sorted({widx[j] for _, j in pairs})
            first, last = seg.words[wis[0]], seg.words[wis[-1]]
            probs = [seg.words[w].prob for w in wis]
            return (int(first.start_ms), int(max(last.end_ms, first.start_ms + 100)),
                    len({i for i, _ in pairs}), float(np.mean(probs)))
    return int(seg.start_ms), int(seg.end_ms), 0, 1.0


def _segment_anchors(
    specs: list["_LineSpec"], transcript: TranscriptionResult, language: str,
) -> dict[int, tuple[int, int, int, int, float]]:
    """가사 줄 → 세그먼트 구절 매칭 앵커. {줄 인덱스: (start, end, matched, total, prob)}.

    후보: 가사 토큰의 60% 이상이 세그먼트 토큰에 순서대로 담기고, 그중 연속
    일치 블록이 3토큰(6토큰 이상 줄은 4토큰) 이상. 점수 내림차순으로 고르되
    가사 순서 = 시간 순서(같은 세그먼트는 허용)를 깨는 후보는 버린다.
    """
    segs = [(seg, _seg_tokens(seg, language)) for seg in transcript.segments]
    cands: list[tuple[float, int, int]] = []
    line_toks: dict[int, list[str]] = {}
    for li, sp in enumerate(specs):
        if sp.locked:
            continue
        lt = tokenize(sp.text, language)
        if len(lt) < _ANCHOR_MIN_BLOCK:
            continue
        line_toks[li] = lt
        need_block = _ANCHOR_MIN_BLOCK + (1 if len(lt) >= 6 else 0)
        for si, (seg, st) in enumerate(segs):
            if not st:
                continue
            m = SequenceMatcher(None, lt, st, autojunk=False)
            blocks = [b for b in m.get_matching_blocks() if b.size]
            if not blocks:
                continue
            matched = sum(b.size for b in blocks)
            longest = max(b.size for b in blocks)
            cover = matched / len(lt)
            if cover >= _ANCHOR_MIN_COVER and longest >= need_block:
                cands.append((cover + 0.01 * longest, li, si))
    chosen = _select_monotonic(cands)
    out: dict[int, tuple[int, int, int, int, float]] = {}
    for li, si in chosen.items():
        seg = segs[si][0]
        lt = line_toks[li]
        s_ms, e_ms, matched, prob = _refine_in_segment(lt, seg, language)
        if matched < _ANCHOR_MIN_BLOCK:
            # 단어 시간이 없거나 국소 정렬이 빈약하면 세그먼트 시간 전체로
            s_ms, e_ms = int(seg.start_ms), int(seg.end_ms)
            matched = max(matched, _ANCHOR_MIN_BLOCK)
        out[li] = (s_ms, e_ms, matched, len(lt), prob)
    if out:
        log.info("구절 앵커 %d줄: %s", len(out),
                 ", ".join(f"{specs[li].event_id[:6]}@{v[0]/1000:.1f}s" for li, v in sorted(out.items())))
    return out


def _select_monotonic(cands: list[tuple[float, int, int]]) -> dict[int, int]:
    """후보 (score, line, seg) 중 총점 최대의 단조 부분집합을 고른다 (DP).

    제약: 줄 순서 = 세그먼트 순서. 같은 세그먼트를 두 줄이 공유하는 것은
    가까운 줄(2줄 이내 — 한 절을 나눈 구들)만 허용한다. 반복 후렴처럼 같은
    텍스트가 곡에 두 번 나오면 그리디는 둘 다 첫 세그먼트에 붙여 그 사이
    줄들을 짓누른다(실측: 5줄이 한 시각으로 붕괴). DP 는 두 번째 등장을 뒤의
    세그먼트로 보내는 쪽이 총점이 높아 자연히 그렇게 고른다.
    """
    # 줄마다 후보를 점수 순으로 정리 (같은 줄의 여러 세그먼트 후보 허용)
    items = sorted(cands, key=lambda c: (c[1], c[2]))
    n = len(items)
    best = [0.0] * n
    prev = [-1] * n
    for i in range(n):
        sc, li, si = items[i]
        best[i] = sc
        for j in range(i):
            _, lj, sj = items[j]
            if lj >= li:
                continue
            if sj > si:
                continue
            if sj == si and li - lj > 2:
                continue
            if best[j] + sc > best[i]:
                best[i] = best[j] + sc
                prev[i] = j
    if n == 0:
        return {}
    end = max(range(n), key=lambda k: best[k])
    chosen: dict[int, int] = {}
    k = end
    while k >= 0:
        _, li, si = items[k]
        chosen.setdefault(li, si)
        k = prev[k]
    return chosen


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

    # 구절 앵커 — 확실히 매칭된 줄은 LOCKED 처럼 시간을 고정하고 결과에도 싣는다.
    seg_anchors = _segment_anchors(specs, transcript, lang)
    extra: list[LineAlignment] = []
    for li, (s_ms, e_ms, matched, total, prob) in seg_anchors.items():
        sp = specs[li]
        sp.locked = True
        sp.locked_start_ms, sp.locked_end_ms = s_ms, e_ms
        extra.append(LineAlignment(sp.event_id, s_ms, e_ms, matched, total, prob))

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
        return results + extra

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

    return results + extra
