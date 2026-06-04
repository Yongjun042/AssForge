"""Karaoke Toolkit — 줄을 음절로 쪼개고 \\k/\\kf/\\ko 타이밍을 생성·편집.

순수 코어. UI 는 결과 문자열을 UpdateEventCommand(db, id, {"text": ...}) 로 적용한다.

음절 분리 규칙(v1):
  - 한글 음절 블록(가~힣)은 글자 하나가 음절 하나.
  - 라틴/숫자/문장부호 런은 통째로 한 음절(모음 분리는 오류가 많아 보류).
  - 공백은 직전 음절에 붙여 round-trip 을 보존한다 → 음절 텍스트를 모두
    이어붙이면 원래 평문과 정확히 같다.

타이밍 단위는 ASS 규약대로 센티초(centisecond, 1/100s). \\k=즉시, \\kf(=\\K)=
채움 스윕, \\ko=외곽선 채움.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from core.ass.tag_tokenizer import OverrideBlock, TextRun, tokenize

_K_TAGS = ("kf", "ko", "kt", "k", "K")  # 최장일치 순


def _is_hangul(ch: str) -> bool:
    return "가" <= ch <= "힣"


@dataclass(slots=True)
class Syllable:
    text: str               # 표시 텍스트(후행 공백 포함 가능)
    duration_cs: int = 0    # 센티초
    kind: str = "kf"        # "k" | "kf" | "ko"

    @property
    def visible(self) -> str:
        return self.text.strip()


def split_syllables(plain: str) -> list[str]:
    """평문 → 음절 토큰 리스트. 토큰을 이어붙이면 원본과 동일."""
    tokens: list[str] = []
    i, n = 0, len(plain)
    while i < n:
        ch = plain[i]
        if _is_hangul(ch):
            tokens.append(ch)
            i += 1
        elif ch.isspace():
            if tokens:
                tokens[-1] += ch
            else:
                tokens.append(ch)
            i += 1
        else:
            j = i
            while j < n and not _is_hangul(plain[j]) and not plain[j].isspace():
                j += 1
            tokens.append(plain[i:j])
            i = j
    return tokens


def _weight(token: str) -> int:
    """음절 가중치 — 보이는 글자 수(최소 1). 균등 분배의 기준."""
    return max(1, len(token.strip()))


def distribute_durations(
    tokens: list[str],
    total_ms: int,
    kind: str = "kf",
) -> list[Syllable]:
    """총 길이를 음절 가중치에 비례해 센티초로 분배. 반올림 오차는 마지막에 흡수."""
    sylls = [Syllable(t, 0, kind) for t in tokens]
    if not sylls:
        return sylls
    total_cs = max(0, round(total_ms / 10))
    weights = [_weight(t) for t in tokens]
    wsum = sum(weights) or 1
    acc = 0
    for s, w in zip(sylls[:-1], weights[:-1]):
        cs = round(total_cs * w / wsum)
        s.duration_cs = cs
        acc += cs
    sylls[-1].duration_cs = max(0, total_cs - acc)
    return sylls


def render_karaoke(sylls: list[Syllable], kind: str | None = None) -> str:
    """음절 리스트 → '{\\kfNN}가{\\kfMM}사...' 텍스트. kind 로 전체 종류 덮어쓰기."""
    out: list[str] = []
    for s in sylls:
        k = kind or s.kind
        out.append(f"{{\\{k}{s.duration_cs}}}{s.text}")
    return "".join(out)


def parse_karaoke(text: str) -> list[Syllable]:
    """기존 카라오케 텍스트 → 음절 리스트. \\k 계열 태그와 뒤따르는 평문을 묶는다."""
    sylls: list[Syllable] = []
    pending_k: tuple[str, int] | None = None  # (kind, cs)
    leading_seen = False
    for seg in tokenize(text):
        if isinstance(seg, OverrideBlock):
            ktag = None
            for tag in seg.tags:
                if tag.name in _K_TAGS:
                    ktag = tag
            if ktag is not None:
                try:
                    cs = int(float(ktag.args.strip() or "0"))
                except ValueError:
                    cs = 0
                kind = "kf" if ktag.name == "K" else ktag.name
                pending_k = (kind, cs)
                leading_seen = True
        elif isinstance(seg, TextRun):
            if pending_k is not None:
                kind, cs = pending_k
                sylls.append(Syllable(seg.text, cs, kind))
                pending_k = None
            elif sylls:
                sylls[-1].text += seg.text  # k 없는 후행 텍스트는 직전 음절에 붙임
            elif seg.text and not leading_seen:
                sylls.append(Syllable(seg.text, 0, "k"))  # k 없는 선두 평문
    return sylls


def total_duration_cs(sylls: list[Syllable]) -> int:
    return sum(s.duration_cs for s in sylls)


def rescale(sylls: list[Syllable], new_total_ms: int) -> list[Syllable]:
    """전체 길이를 new_total_ms 에 맞춰 비례 조정(음절 비율 유지)."""
    cur = total_duration_cs(sylls)
    target = max(0, round(new_total_ms / 10))
    if cur == 0 or not sylls:
        return distribute_durations([s.text for s in sylls], new_total_ms,
                                    sylls[0].kind if sylls else "kf")
    acc = 0
    out = [Syllable(s.text, s.duration_cs, s.kind) for s in sylls]
    for s in out[:-1]:
        s.duration_cs = round(s.duration_cs * target / cur)
        acc += s.duration_cs
    out[-1].duration_cs = max(0, target - acc)
    return out


def set_kind(sylls: list[Syllable], kind: str) -> list[Syllable]:
    return [Syllable(s.text, s.duration_cs, kind) for s in sylls]


def shift_boundary(sylls: list[Syllable], index: int, delta_cs: int) -> list[Syllable]:
    """음절 index 와 index+1 사이 경계를 delta_cs 만큼 이동(이웃에서 가져옴).

    delta_cs>0 이면 index 음절이 길어지고 index+1 이 짧아진다. 총합은 보존.
    """
    out = [Syllable(s.text, s.duration_cs, s.kind) for s in sylls]
    if not (0 <= index < len(out) - 1):
        return out
    left, right = out[index], out[index + 1]
    move = max(-left.duration_cs, min(right.duration_cs, delta_cs))
    left.duration_cs += move
    right.duration_cs -= move
    return out


def auto_karaoke(plain: str, total_ms: int, kind: str = "kf") -> str:
    """평문 → 자동 카라오케 텍스트(분리 + 균등 분배 + 렌더). 단일 진입점."""
    return render_karaoke(distribute_durations(split_syllables(plain), total_ms, kind))
