"""레퍼런스 스타일 다이제스트 — 수작업 완성본(.ass)의 연출 패턴을 요약한다.

사용자가 손으로 만든 완성본 자막을 읽어, 어떤 연출(effects.typeset_fx_schema 의
fx 이름)이 얼마나 자주 쓰였는지·전형적인 값(글자 크기, \\fad, 스타일 이름)이
무엇인지 휴리스틱으로 분류한다. 결과는 LLM 타이프셋 디렉터(ai.typeset_director)
의 프롬프트에 압축 텍스트로 들어가거나, UI 에서 '참고한 스타일' 표시에 쓰인다.

분류 규칙(휴리스틱, 우선순위 순):
  1. 연속된 1글자 줄 묶음(≥3)
       - 같은 시작·끝 + \\t 에 frx/fry/frz     → char_scatter
       - 같은 시작·끝 + \\pos 만, x·y 단조 증가 → char_diagonal
       - 시작이 계단식, x 동일, 끝 동일         → char_stack
  2. 같은 평문이 같은 시간에 N≥3 겹                 → ghost_trail
  3. 평문이 ■●♣◆○ 류 기호로만                        → shadow_bar
  4. \\frz270 + \\clip/\\iclip, 또는 \\fn@(세로 폰트)  → vertical_title
  5. 본문 중간 블록에 \\1c/\\c 전환                    → partial_color
  6. \\move + \\t(...\\fscx...)                          → drift_scale
  7. \\move + \\t(...\\fr/\\frz...) (크기 변화 없음)      → fly_rotate
  8. 그 외                                              → plain

파일 없음/파싱 실패는 예외 없이 빈 다이제스트를 돌려준다.
"""
from __future__ import annotations

import os
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from core.ass.tag_tokenizer import OverrideBlock, OverrideTag, TextRun, strip_tags, tokenize

# 장식 막대에 쓰이는 기호들 (레퍼런스: ♣■●■○◆■■■ / ●●●●●●●)
_BAR_CHARS = frozenset("■●♣◆○□◇▲△▼▽◎")   # ★ 는 세로 제목의 장식이므로 제외
_ELLIPSIS_MARK = "{\\fsp-5}..."
_TIME_RE = re.compile(r"^\s*(\d+):(\d{1,2}):(\d{1,2})[.:](\d{1,2})\s*$")
_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")

# 다이제스트에 실리는 카테고리 순서 (스키마와 동일 이름)
CATEGORY_ORDER: tuple[str, ...] = (
    "char_scatter", "char_diagonal", "char_stack", "ghost_trail", "shadow_bar",
    "vertical_title", "partial_color", "drift_scale", "fly_rotate", "plain",
)
_FR_IN_T_RE = re.compile(r"\\frz?-?\d")   # \t 안의 \fr / \frz (frx/fry 제외)


@dataclass(slots=True)
class StyleDigest:
    """레퍼런스 완성본의 연출 요약."""
    source_path: str = ""
    n_lines: int = 0
    categories: dict[str, int] = field(default_factory=dict)   # fx 이름 → 빈도
    examples: dict[str, list[str]] = field(default_factory=dict)  # fx 이름 → 예시 ≤2
    typical: dict[str, Any] = field(default_factory=dict)      # fs 중앙값, fad, 스타일 등

    @property
    def empty(self) -> bool:
        return self.n_lines == 0

    def summary_text(self, limit: int = 2500) -> str:
        """LLM 프롬프트용 압축 요약 (한국어, limit 자 이내)."""
        if self.empty:
            return ""
        out: list[str] = []
        name = os.path.basename(self.source_path) if self.source_path else "(레퍼런스)"
        out.append(f"[참고 완성본 스타일] {name} — 이벤트 {self.n_lines}줄")
        t = self.typical
        bits: list[str] = []
        if t.get("fs_median"):
            bits.append(f"글자 크기 중앙값 {t['fs_median']}")
        if t.get("fad_common"):
            bits.append("흔한 \\fad " + ", ".join(
                f"({a},{b})" for a, b in t["fad_common"]))
        if t.get("styles"):
            bits.append("스타일 " + "/".join(t["styles"]))
        if t.get("ellipsis_lines"):
            bits.append(f"말줄임 '{_ELLIPSIS_MARK}' {t['ellipsis_lines']}줄")
        if t.get("chars_per_line"):
            bits.append(f"줄당 평균 {t['chars_per_line']}글자")
        if bits:
            out.append("전형: " + "; ".join(bits))
        if self.categories:
            ordered = sorted(self.categories.items(), key=lambda kv: -kv[1])
            out.append("연출 빈도: " + ", ".join(f"{k} {v}" for k, v in ordered))
        for cat in CATEGORY_ORDER:
            exs = self.examples.get(cat)
            if not exs:
                continue
            out.append(f"- {cat}:")
            for ex in exs[:2]:
                out.append(f"    {ex}")
        text = "\n".join(out)
        if len(text) > limit:
            text = text[: max(0, limit - 1)].rstrip() + "…"
        return text


# ---- 파싱 ---------------------------------------------------------------

@dataclass(slots=True)
class _Rec:
    """분류용 Dialogue 1줄."""
    idx: int
    start_ms: int
    end_ms: int
    style: str
    text: str                       # 태그 포함 원문
    plain: str                      # 태그 제거 평문
    head: list[OverrideTag]         # 첫 텍스트 앞 블록들의 태그
    mid: list[OverrideTag]          # 텍스트 이후 블록들의 태그
    category: str = ""


def _parse_time(s: str) -> int | None:
    m = _TIME_RE.match(s)
    if not m:
        return None
    h, mi, se, cs = (int(g) for g in m.groups())
    cs = cs * 10 if len(m.group(4)) == 1 else cs
    return ((h * 60 + mi) * 60 + se) * 1000 + cs * 10


def _split_tags(text: str) -> tuple[list[OverrideTag], list[OverrideTag]]:
    head: list[OverrideTag] = []
    mid: list[OverrideTag] = []
    seen_text = False
    for seg in tokenize(text):
        if isinstance(seg, TextRun):
            if seg.text.strip():
                seen_text = True
        elif isinstance(seg, OverrideBlock):
            (mid if seen_text else head).extend(t for t in seg.tags if t.name)
    return head, mid


def _read_dialogues(path: str) -> list[_Rec]:
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as fh:
            raw = fh.read()
    except OSError:
        return []
    recs: list[_Rec] = []
    for line in raw.splitlines():
        if not line.startswith("Dialogue:"):
            continue
        body = line[len("Dialogue:"):].strip()
        parts = body.split(",", 9)
        if len(parts) < 10:
            continue
        st, en = _parse_time(parts[1]), _parse_time(parts[2])
        if st is None or en is None:
            continue
        text = parts[9]
        head, mid = _split_tags(text)
        recs.append(_Rec(len(recs), st, en, parts[3].strip(), text,
                         strip_tags(text).strip(), head, mid))
    return recs


# ---- 태그 헬퍼 ----------------------------------------------------------

def _tag(tags: list[OverrideTag], name: str) -> OverrideTag | None:
    for t in tags:
        if t.name == name:
            return t
    return None


def _nums(args: str) -> list[float]:
    return [float(x) for x in _NUM_RE.findall(args)]


def _pos_xy(tags: list[OverrideTag]) -> tuple[float, float] | None:
    p = _tag(tags, "pos")
    if p is None:
        p = _tag(tags, "move")
    if p is None:
        return None
    n = _nums(p.args)
    if len(n) < 2:
        return None
    return n[0], n[1]


def _t_bodies(tags: list[OverrideTag]) -> str:
    """모든 \\t(...) 인자 문자열을 이어 붙인다 (내부 태그 검사용)."""
    return " ".join(t.args for t in tags if t.name == "t")


def _nletters(s: str) -> int:
    return sum(1 for ch in s if unicodedata.category(ch)[0] in ("L", "N"))


def _is_single_glyph(plain: str) -> bool:
    core = plain.replace(" ", "")
    if not core:
        return False
    if len(core) == 1:
        return True
    # 흩뿌리기 안의 말줄임 조각 ("...", "…", ".")
    return all(ch in ".…" for ch in core)


def _is_bar(plain: str) -> bool:
    core = plain.replace(" ", "")
    return bool(core) and all(ch in _BAR_CHARS for ch in core)


def _monotone(vals: list[float]) -> bool:
    if len(vals) < 2:
        return False
    inc = all(b >= a for a, b in zip(vals, vals[1:]))
    dec = all(b <= a for a, b in zip(vals, vals[1:]))
    return (inc or dec) and vals[0] != vals[-1]


# ---- 분류 -----------------------------------------------------------------

def _classify_single(r: _Rec) -> str:
    tags = r.head + r.mid
    if _is_bar(r.plain):
        return "shadow_bar"
    frz = _tag(r.head, "frz")
    has_clip = _tag(tags, "clip") is not None or _tag(tags, "iclip") is not None
    fn = _tag(r.head, "fn")
    if frz is not None and has_clip:
        n = _nums(frz.args)
        if n and abs(n[0] % 360 - 270) < 1:
            return "vertical_title"
    if fn is not None and fn.args.strip().startswith("@"):
        return "vertical_title"
    if any(t.name in ("1c", "c") for t in r.mid):
        return "partial_color"
    if _tag(r.head, "move") is not None:
        bodies = _t_bodies(r.head)
        if "fscx" in bodies:
            return "drift_scale"
        if _FR_IN_T_RE.search(bodies):
            return "fly_rotate"
    return "plain"


def _classify_groups(recs: list[_Rec]) -> None:
    """연속 1글자 묶음과 겹침(고스트)을 먼저 분류한다."""
    n = len(recs)
    i = 0
    while i < n:
        r = recs[i]
        if r.category or not _is_single_glyph(r.plain):
            i += 1
            continue
        j = i
        while j + 1 < n and not recs[j + 1].category and _is_single_glyph(recs[j + 1].plain):
            j += 1
        run = recs[i:j + 1]
        if len(run) >= 3:
            _label_glyph_run(run)
        i = j + 1

    # 고스트: 같은 평문·같은 시작·끝이 3겹 이상
    buckets: dict[tuple[str, int, int], list[_Rec]] = {}
    for r in recs:
        if r.category or not r.plain:
            continue
        buckets.setdefault((r.plain, r.start_ms, r.end_ms), []).append(r)
    for group in buckets.values():
        if len(group) >= 3:
            for r in group:
                r.category = "ghost_trail"


def _label_glyph_run(run: list[_Rec]) -> None:
    """1글자 줄 묶음 → scatter / diagonal / stack. 시간이 다른 소묶음은 나눠 본다."""
    # 같은 (start,end) 별 소묶음
    by_time: dict[tuple[int, int], list[_Rec]] = {}
    for r in run:
        by_time.setdefault((r.start_ms, r.end_ms), []).append(r)
    labeled = 0
    for sub in by_time.values():
        if len(sub) < 3:
            continue
        rot = any(any(k in _t_bodies(r.head) for k in ("frx", "fry", "frz"))
                  for r in sub)
        if rot:
            for r in sub:
                r.category = "char_scatter"
            labeled += len(sub)
            continue
        pts = [_pos_xy(r.head) for r in sub]
        if all(p is not None for p in pts):
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            if _monotone(xs) and _monotone(ys):
                for r in sub:
                    r.category = "char_diagonal"
                labeled += len(sub)
    if labeled:
        return
    # 스택: 시작 계단식, 끝 동일, x 동일
    ends = {r.end_ms for r in run}
    starts = [r.start_ms for r in run]
    pts = [_pos_xy(r.head) for r in run]
    if (len(ends) == 1 and all(b > a for a, b in zip(starts, starts[1:]))
            and all(p is not None for p in pts)
            and len({round(p[0]) for p in pts}) == 1):
        for r in run:
            r.category = "char_stack"


# ---- 예시/전형값 ----------------------------------------------------------

def _tag_summary(r: _Rec, max_len: int = 140) -> str:
    parts: list[str] = []
    for t in r.head:
        a = t.args
        if len(a) > 40:
            a = a[:37] + "…"
        parts.append(f"\\{t.name}{a}")
    if r.mid:
        parts.append("… " + " ".join(f"\\{t.name}{t.args[:16]}" for t in r.mid[:3]))
    s = " ".join(parts)
    if len(s) > max_len:
        s = s[: max_len - 1] + "…"
    plain = r.plain if len(r.plain) <= 24 else r.plain[:23] + "…"
    dur = (r.end_ms - r.start_ms) / 1000
    return f"{{{s}}} \"{plain}\" ({dur:.1f}s)"


def _collect_examples(recs: list[_Rec]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    seen_plain: dict[str, set[str]] = {}
    for r in recs:
        cat = r.category
        bucket = out.setdefault(cat, [])
        if len(bucket) >= 2:
            continue
        key = r.plain
        # 글자별 묶음은 같은 묶음에서 2개가 아니라 서로 다른 묶음에서 뽑는다.
        if cat in ("char_scatter", "char_diagonal", "char_stack", "ghost_trail"):
            key = f"{r.start_ms}"
            if cat == "ghost_trail":
                key = r.plain
        if key in seen_plain.setdefault(cat, set()):
            continue
        seen_plain[cat].add(key)
        bucket.append(_tag_summary(r))
    return {k: v for k, v in out.items() if v}


def _typical(recs: list[_Rec]) -> dict[str, Any]:
    fs_vals: list[float] = []
    fads: Counter[tuple[int, int]] = Counter()
    styles: Counter[str] = Counter()
    ellipsis = 0
    letters: list[int] = []
    for r in recs:
        styles[r.style] += 1
        fs = _tag(r.head, "fs")
        if fs is not None:
            n = _nums(fs.args)
            if n:
                fs_vals.append(n[0])
        fad = _tag(r.head, "fad")
        if fad is not None:
            n = _nums(fad.args)
            if len(n) >= 2:
                fads[(int(n[0]), int(n[1]))] += 1
        if r.text.endswith(_ELLIPSIS_MARK):
            ellipsis += 1
        if r.plain and not _is_bar(r.plain):
            letters.append(_nletters(r.plain))
    typical: dict[str, Any] = {}
    if fs_vals:
        s = sorted(fs_vals)
        typical["fs_median"] = int(s[len(s) // 2])
    typical["fad_common"] = [k for k, _ in fads.most_common(4)]
    typical["styles"] = [k for k, _ in styles.most_common(3)]
    typical["ellipsis_lines"] = ellipsis
    if letters:
        typical["chars_per_line"] = round(sum(letters) / len(letters), 1)
    return typical


# ---- 공개 API -------------------------------------------------------------

def build_style_digest(ass_path: str) -> StyleDigest:
    """레퍼런스 .ass → StyleDigest. 파일 없음/파싱 실패면 빈 다이제스트."""
    digest = StyleDigest(source_path=ass_path or "")
    if not ass_path or not os.path.isfile(ass_path):
        return digest
    try:
        recs = _read_dialogues(ass_path)
        if not recs:
            return digest
        _classify_groups(recs)
        for r in recs:
            if not r.category:
                r.category = _classify_single(r)
        cats: Counter[str] = Counter(r.category for r in recs)
        digest.n_lines = len(recs)
        digest.categories = {k: cats[k] for k in CATEGORY_ORDER if cats.get(k)}
        digest.examples = _collect_examples(recs)
        digest.typical = _typical(recs)
    except Exception:  # noqa: BLE001 — 다이제스트는 절대 예외를 내지 않는다
        return StyleDigest(source_path=ass_path or "")
    return digest
