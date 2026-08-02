"""가사 텍스트 정규화 + 토큰화.

DTW 매칭의 비교 단위가 되는 토큰을 만든다. 언어에 따라 단위가 다르다:
- 일본어/한국어/중국어: 문자(grapheme) 단위 (음절성이 있어 자모 비교가 적절)
- 영어/유럽어: 단어 단위 (공백 분리)

정규화는 비교용 토큰 텍스트만 만들고, 원본 텍스트는 따로 유지한다.
"""
from __future__ import annotations

import re
import unicodedata

# ASS override tag: {\...}
_TAG_RE = re.compile(r"\{[^}]*\}")
# Hard newline: \N \n \h
_HARDNL_RE = re.compile(r"\\[Nnh]")
# 일본어 한자/히라가나/가타카나
_JP_RANGES = (
    (0x3040, 0x309F),  # Hiragana
    (0x30A0, 0x30FF),  # Katakana
    (0x4E00, 0x9FFF),  # CJK Unified
    (0xFF66, 0xFF9F),  # Halfwidth Katakana
)
# 한글
_KO_RANGES = (
    (0xAC00, 0xD7AF),  # Hangul Syllables
    (0x1100, 0x11FF),  # Hangul Jamo
    (0x3130, 0x318F),  # Hangul Compat Jamo
)
# 한자(중국어 공유)
_CN_RANGES = ((0x4E00, 0x9FFF),)

# 루비 표기: 漢字（かな/カナ）— 가사엔 표기+읽기가 같이 있지만 가수는 읽기만
# 부르므로(Whisper 전사엔 읽기만 존재) 읽기 쪽만 남긴다.
_RUBY_RE = re.compile(
    r"[一-鿿々〆]+[（(]([ぁ-んァ-ヶー・]+)[）)]"
)

_kakasi = None
_kakasi_failed = False


def _to_hiragana(text: str) -> str:
    """가능하면 전체를 히라가나로 (pykakasi) — 한자 이형(赦/許)과 표기 차이를
    발음 공간에서 흡수한다. pykakasi 미설치면 가타카나 폴드만 수행."""
    global _kakasi, _kakasi_failed
    if _kakasi is None and not _kakasi_failed:
        try:
            import pykakasi
            _kakasi = pykakasi.kakasi()
        except Exception:
            _kakasi_failed = True
    if _kakasi is not None:
        try:
            text = "".join(item["hira"] for item in _kakasi.convert(text))
        except Exception:
            pass
    # 가타카나 → 히라가나 폴드 (カルマ ≡ かるま)
    return "".join(
        chr(ord(ch) - 0x60) if 0x30A1 <= ord(ch) <= 0x30F6 else ch
        for ch in text
    )


def to_romaji(text: str) -> str:
    """일본어 → 헵번 로마자 (pykakasi). 미설치/실패 시 빈 문자열.

    한글 독음(예: '요루노 카루마가')과 일본어 원문을 발음 공간에서 비교할 때
    쓴다 — lyric_text._sounds_like 참고.
    """
    global _kakasi, _kakasi_failed
    if _kakasi is None and not _kakasi_failed:
        try:
            import pykakasi
            _kakasi = pykakasi.kakasi()
        except Exception:
            _kakasi_failed = True
    if _kakasi is None:
        return ""
    try:
        return "".join(item["hepburn"] for item in _kakasi.convert(text)).lower()
    except Exception:
        return ""


def strip_ass_text(text: str) -> str:
    """ASS override tag 와 hard newline 제거."""
    text = _TAG_RE.sub("", text)
    text = _HARDNL_RE.sub(" ", text)
    return text


def _in_ranges(ch: str, ranges) -> bool:
    cp = ord(ch)
    for lo, hi in ranges:
        if lo <= cp <= hi:
            return True
    return False


def detect_language(text: str) -> str:
    """텍스트로부터 대략의 언어 코드 추정.

    매우 단순한 휴리스틱이지만 ja/ko/en 분기에 충분.
    """
    jp = ko = ascii_letters = 0
    for ch in text:
        if _in_ranges(ch, _JP_RANGES):
            jp += 1
        elif _in_ranges(ch, _KO_RANGES):
            ko += 1
        elif "a" <= ch.lower() <= "z":
            ascii_letters += 1
    if ko > 0 and ko >= jp:
        return "ko"
    if jp > 0:
        return "ja"
    if ascii_letters > 0:
        return "en"
    return ""


def _normalize_token(s: str) -> str:
    """공통 정규화: NFKC, 소문자, 구두점·기호 제거.

    구두점(P*)만 지우면 ♪☆♡… 같은 기호(S*)가 토큰에 남아 transcript 와
    절대 매칭되지 않는 잡음이 된다 — 문자(L*)/숫자(N*)만 남긴다.
    """
    s = unicodedata.normalize("NFKC", s)
    s = s.lower()
    s = "".join(ch for ch in s if unicodedata.category(ch)[0] in ("L", "N"))
    s = s.strip()
    return s


def _is_separator(ch: str) -> bool:
    if ch.isspace():
        return True
    cat = unicodedata.category(ch)
    if cat.startswith("P") or cat in ("Cc", "Cf"):
        return True
    return False


def tokenize(text: str, language: str = "") -> list[str]:
    """언어에 따라 비교용 토큰 시퀀스 생성.

    빈 토큰은 결과에 포함하지 않는다. 한자/가나/한글은 문자 단위,
    그 외는 공백으로 단어 분리.
    """
    text = strip_ass_text(text)
    text = unicodedata.normalize("NFKC", text)
    text = text.lower()

    if not language:
        language = detect_language(text)

    if language == "ja":
        # 일본어는 발음(히라가나) 공간에서 매칭한다:
        #   · 루비 因業（カルマ） → 읽기(カルマ)만 (가수는 읽기만 부른다)
        #   · 한자 → 읽기 (Whisper 가 赦 대신 許 를 골라도 발음은 같다)
        #   · 가타카나 → 히라가나 폴드
        text = _RUBY_RE.sub(r"\1", text)
        text = _to_hiragana(text)
        return [ch for ch in text if unicodedata.category(ch)[0] in ("L", "N")]

    if language in ("ko", "zh"):
        # 문자 단위 — 문자(L*)/숫자(N*)만 토큰으로. 구두점(…—「」)은 물론
        # 기호(♪☆♡ 등, S*)도 제외해야 매칭 불가 잡음 토큰이 안 생긴다.
        return [ch for ch in text if unicodedata.category(ch)[0] in ("L", "N")]

    # 단어 단위
    raw = re.split(r"\s+", text)
    out: list[str] = []
    for w in raw:
        n = _normalize_token(w)
        if n:
            out.append(n)
    return out


def tokens_match(a: str, b: str) -> bool:
    """두 토큰이 매칭되는지 — 정규화 후 동일 비교."""
    return _normalize_token(a) == _normalize_token(b)
