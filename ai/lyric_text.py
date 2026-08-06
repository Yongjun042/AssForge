"""사용자가 붙여넣은 가사 텍스트(원문/한국어 번역) 파싱 + 이벤트 매핑.

자막 텍스트가 한국어뿐이면 일본어 노래의 transcript 와 매칭이 거의 안 된다.
사용자가 실제 불리는 원문 가사(일본어 등)를 붙여넣으면, 그 텍스트를 해당
줄의 정렬 기준(ref)으로 써서 발음 공간에서 정확히 매칭할 수 있다.

붙여넣기 형식은 자유:
  · 원문만 — 한 줄에 하나. 대상 줄에 순서대로 1:1 연결.
  · 원문 + 한국어 번역 교차 — 한국어 줄이 직전 원문 블록을 닫는다.
    번역 텍스트 유사도로 기존 자막 줄을 자동으로 찾아 연결한다.
  · 원문 + 독음 + 한국어 번역 3줄 — 독음(요루노 카루마가…)도 한글이므로,
    원문 뒤 연속된 한글 2줄은 발음 유사도로 확인해 첫 줄=독음, 둘째 줄=번역.
    원문+독음 2줄 형식도 발음 유사도로 독음을 가려내 번역으로 오인하지 않는다.

분류 규칙: 한국어(한글 포함) 줄 = 번역 후보, 그 외 모든 줄 = 원문.
(일본 노래에 영어 소절이 흔하므로 ja 로 한정하지 않는다.)
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from ai.lyric_normalize import detect_language, strip_ass_text, to_romaji, tokenize


@dataclass(slots=True)
class LyricPair:
    """가사 한 줄 단위 — 원문(불리는 텍스트) / 한국어 번역 / 한글 독음."""
    source: str | None
    translation: str | None
    reading: str | None = None


@dataclass(slots=True)
class RefMapResult:
    """build_ref_map 결과. matched_ids 의 모든 이벤트는 ref_texts 에 원문이
    있다 — 원문 없는 줄은 동기화 대상에 넣지 않는다 (ref 없이 한국어 텍스트로
    일본어 transcript 에 정렬하면 무의미한 fallback 제안만 생긴다)."""
    ref_texts: dict[str, str]     # event_id -> 정렬 기준 원문
    matched_ids: list[str]        # 원문이 연결된 이벤트 (표시 순서)
    n_pairs: int                  # 파싱된 가사 쌍 수
    n_unmatched: int              # 원문을 연결하지 못한 쌍 수
    by_translation: bool          # True=번역 유사도 매칭, False=순서 1:1


# ── 발음 비교 (독음 판별) ────────────────────────────────────────
# 한글 음절 → 로마자 (개정 로마자 표기 근사 — 발음 비교용이라 정밀할 필요 없음)
_CHO = ("g", "kk", "n", "d", "tt", "r", "m", "b", "pp", "s", "ss", "",
        "j", "jj", "ch", "k", "t", "p", "h")
_JUNG = ("a", "ae", "ya", "yae", "eo", "e", "yeo", "ye", "o", "wa", "wae",
         "oe", "yo", "u", "wo", "we", "wi", "yu", "eu", "ui", "i")
_JONG = ("", "k", "k", "k", "n", "n", "n", "t", "l", "k", "m", "l", "l",
         "l", "p", "l", "m", "p", "p", "t", "t", "ng", "t", "t", "k", "t",
         "p", "t")


def _hangul_to_roman(text: str) -> str:
    out: list[str] = []
    for ch in text:
        cp = ord(ch)
        if 0xAC00 <= cp <= 0xD7A3:
            i = cp - 0xAC00
            out.append(_CHO[i // 588] + _JUNG[(i % 588) // 28] + _JONG[i % 28])
        elif ch.isascii() and ch.isalpha():
            out.append(ch.lower())
    return "".join(out)


def _fold_pron(s: str) -> str:
    """언어별 표기 차이를 발음 근사로 접는다 — 츠(cheu)≈つ(tsu), l≈r, ざ(za)≈자(ja)."""
    s = re.sub(r"[^a-z]", "", s.lower())
    for a, b in (("sh", "s"), ("ch", "c"), ("ts", "c"), ("l", "r"),
                 ("f", "h"), ("wo", "o"), ("eu", "u"), ("eo", "o"), ("z", "j")):
        s = s.replace(a, b)
    return re.sub(r"(.)\1+", r"\1", s)


def _letters_only(s: str) -> str:
    """문자/숫자만 남긴다 — ･･･ 같은 기호를 pykakasi 가 반복 부호로
    오해석해 romaji 를 오염시키는 것을 막는다 (실측: 遠ざかる･･･ →
    'toozakarukarukarukaru')."""
    import unicodedata
    return "".join(ch for ch in s if unicodedata.category(ch)[0] in ("L", "N"))


# 2줄(원문/한글) 형식에서 한글 줄을 독음으로 판정하는 임계값. 번역이 외래어를
# 공유해도(카르마 등) 0.5 부근에 그치고, 실제 독음은 0.8+ 로 갈린다.
_READING_SIM = 0.65


def _sounds_like(source: str, hangul: str) -> float | None:
    """한글 줄이 source(일본어 등)의 독음처럼 들리는지 — 유사도 0~1.

    로마자 변환 불가(pykakasi 미설치 등)거나 비교할 발음이 너무 짧아
    (folded 4자 미만 — 단일 한자는 kakasi 가 문맥 없이 다른 독음을 고를 수
    있다: 駈→ku vs 카, 上→ue vs 아) 판단이 무의미하면 None.
    """
    rom_src = to_romaji(_letters_only(source))
    if not rom_src:
        return None
    a = _fold_pron(rom_src)
    b = _fold_pron(_hangul_to_roman(hangul))
    if not a or not b or len(a) < 4 or len(b) < 4:
        return None
    return SequenceMatcher(None, a, b).ratio()


def parse_lyric_pairs(raw: str) -> list[LyricPair]:
    """붙여넣은 텍스트를 (원문, 번역, 독음) 쌍 리스트로.

    한국어 줄이 하나도 없으면 각 줄이 독립 원문. 있으면 한국어 줄이 직전까지
    쌓인 원문 블록을 닫는다 (원문 2줄 + 번역 1줄도 한 쌍). 원문 뒤 연속된
    한글 2줄은 가사/독음/한국어 3줄 형식 — 발음이 원문과 비슷한 첫 줄을
    독음으로 옮기고 둘째 줄을 번역으로 삼는다. 한글 1줄뿐이어도 발음이
    원문과 거의 같으면 번역이 아니라 독음으로 재분류한다.

    같은 블록(빈 줄 없이 이어짐) 안에서 그 뒤로 더 나오는 한글 줄은 여러 줄
    번역으로 보고 \\N 으로 이어 붙인다 — 영어 프롤로그 3줄 + 한국어 번역
    4줄 같은 화면 텍스트 블록이 단독 쌍으로 흩어지지 않는다.

    빈 줄은 절(verse) 경계 — 한글 줄 없이 빈 줄로 끝난 원문 블록(무대 지시문,
    영어 머리말 등)은 원문 단독 쌍으로 닫아서 다음 절과 섞이지 않게 한다.
    """
    all_lines = [ln.strip() for ln in raw.replace("\r", "").split("\n")]
    nonempty = [ln for ln in all_lines if ln]
    if not nonempty:
        return []
    if not any(detect_language(ln) == "ko" for ln in nonempty):
        return [LyricPair(ln, None) for ln in nonempty]

    pairs: list[LyricPair] = []
    pending: list[str] = []
    open_pair: LyricPair | None = None  # 직전 원문 블록을 닫은 쌍 (한글 줄 수용 중)
    open_ko = 0                         # open_pair 에 붙은 한글 줄 수

    def _flush_pending() -> None:
        nonlocal pending
        if pending:
            pairs.append(LyricPair(" ".join(pending), None))
            pending = []

    for ln in all_lines:
        if not ln:
            # 절 경계 — 한글 없이 끝난 원문 블록을 닫고 한글 수집도 종료.
            _flush_pending()
            open_pair = None
            continue
        if detect_language(ln) != "ko":
            open_pair = None
            pending.append(ln)
            continue
        if pending:
            open_pair = LyricPair(" ".join(pending), ln)
            open_ko = 1
            pairs.append(open_pair)
            pending = []
        elif open_pair is not None:
            if open_ko == 1 and open_pair.reading is None and open_pair.source:
                # 원문 뒤 두 번째 연속 한글 줄 — 3줄 형식이면 앞 줄이 독음.
                # 발음으로 확인하고(실측: 독음 0.9+, 번역 0.55 이하), 판단
                # 불가(초단문·pykakasi 미설치)면 형식 순서를 믿는다.
                first = open_pair.translation
                sim = _sounds_like(open_pair.source, first)
                if sim is None or sim >= _READING_SIM:
                    open_pair.reading = first
                    open_pair.translation = ln
                    open_ko = 2
                    continue
            # 같은 블록의 추가 한글 줄 — 여러 줄 번역으로 이어 붙인다.
            open_pair.translation = (
                open_pair.translation + r"\N" + ln
                if open_pair.translation else ln)
            open_ko += 1
        else:
            pairs.append(LyricPair(None, ln))
    _flush_pending()

    # 원문+한글 1줄 쌍 — 한글이 번역이 아니라 독음인 2줄 형식 가려내기.
    # 독음을 번역으로 두면 자막 텍스트와 유사도 매칭이 어긋나므로 중요하다.
    for p in pairs:
        if p.source and p.translation and p.reading is None:
            sim = _sounds_like(p.source, p.translation)
            if sim is not None and sim >= _READING_SIM:
                p.reading = p.translation
                p.translation = None
    return pairs


def _norm_ko(text: str) -> str:
    """유사도 비교용 정규화 — 문자/숫자만 남긴 연속 문자열."""
    return "".join(tokenize(text, "ko"))


def _similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _split_phrases(text: str) -> list[str]:
    """말줄임(… 또는 .. 이상)이 구 경계인 가사 줄을 구 단위로 나눈다.

    '胸を刺す...気配の中で...' → ['胸を刺す...', '気配の中で...']
    끝에만 말줄임이 있으면 나뉘지 않는다.
    """
    out: list[str] = []
    last = 0
    for m in re.finditer(r"(?:…|\.{2,})+\s*", text):
        if m.end() < len(text):
            seg = text[last:m.end()].strip()
            if seg:
                out.append(seg)
            last = m.end()
    rest = text[last:].strip()
    if rest:
        out.append(rest)
    return out


def split_phrase_pairs(pairs: list[LyricPair]) -> list[LyricPair]:
    """절 내부의 말줄임 구를 독립 쌍으로 분할한다.

    화면 가사 그래픽은 구 단위로 따로 뜨는 경우가 많다 (실측: 수작업
    완성본은 '가슴을 찌르는…'과 '냉기 속에서…'가 각각 다른 시간·위치의
    이벤트다). 원문과 번역의 구 수가 같을 때만 나눈다 — 다르면 어느 구가
    어느 번역인지 알 수 없으므로 통째로 둔다. 독음은 수가 맞으면 같이
    나누고, 아니면 버린다(표시/정렬 어디에도 필수가 아니다).
    """
    out: list[LyricPair] = []
    for p in pairs:
        if not (p.source and p.translation):
            out.append(p)
            continue
        src = _split_phrases(p.source)
        tr = _split_phrases(p.translation)
        if len(src) < 2 or len(src) != len(tr):
            out.append(p)
            continue
        rd = _split_phrases(p.reading) if p.reading else []
        if len(rd) != len(src):
            rd = [None] * len(src)
        for s, t, r in zip(src, tr, rd):
            out.append(LyricPair(s, t, r))
    return out


def creation_sync_targets(pairs: list[LyricPair]) -> list[LyricPair]:
    """새 줄 생성 시 동기화 대상이 될 쌍만 고른다.

    원문이 있어야 한다. 독음/번역이 섞인 형식에서 한글 짝 없는 원문 블록은
    둘로 갈린다: 일본어면 번역을 안 붙인 소절(반복 후렴 등)로 보고 대상에
    넣고, 그 외(영어 머리말·무대 지시문)는 불리는 가사가 아니므로 뺀다 —
    정렬 대상이 되면 쓰레기 fallback 제안만 생긴다.
    원문만 있는 형식(한글이 전혀 없음)이면 모든 원문 쌍이 대상이다.
    """
    mixed = any(p.reading or p.translation for p in pairs)
    out: list[LyricPair] = []
    for p in pairs:
        if not p.source:
            continue
        if (not mixed or p.reading or p.translation
                or detect_language(p.source) == "ja"):
            out.append(p)
    return out


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
        cursor = idx + 1
        if not pair.source:
            # 원문 없는 번역 단독 줄 — 단조 진행(cursor)에만 반영하고 동기화
            # 대상에서는 뺀다. ref 없이 재정렬하면 매칭 0 fallback 만 생긴다.
            n_unmatched += 1
            continue
        eid = ev_norm[idx][0]
        matched_ids.append(eid)
        ref_texts[eid] = pair.source
    return RefMapResult(ref_texts, matched_ids, len(pairs), n_unmatched, True)
