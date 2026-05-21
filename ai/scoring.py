"""라인별 정렬 신뢰도 계산.

매칭 비율(가사 토큰 중 transcript 와 매칭된 비율)과
매칭된 단어들의 Whisper probability 평균을 결합한다.
"""
from __future__ import annotations

from ai.alignment_song import LineAlignment


def line_confidence(alignment: LineAlignment) -> float:
    """0.0 ~ 1.0 사이의 신뢰도.

    매칭 비율이 가장 큰 가중치를 차지한다 — 매칭이 거의 없는데
    Whisper 가 자신 있게 잘못 들은 경우를 페널티로 받게 함.
    """
    ratio = alignment.match_ratio
    prob = max(0.0, min(1.0, alignment.avg_word_prob))
    # 매칭 비율 70%, prob 30%
    score = 0.7 * ratio + 0.3 * prob
    # 매칭이 없으면 prob 도 의미 없음
    if alignment.matched_token_count == 0:
        score = 0.0
    return max(0.0, min(1.0, score))
