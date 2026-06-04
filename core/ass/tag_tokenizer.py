r"""ASS override tag 토크나이저 — lazy parsing, round-trip 보존.

Dialogue 텍스트를 평문 런(TextRun)과 오버라이드 블록(OverrideBlock)으로 쪼개고,
블록 안의 개별 태그를 OverrideTag 로 파싱한다. 다시 render() 하면 원본과 동일.

ASS 태그 파싱의 난점:
  - 태그명은 단순 알파벳 최대 런이 아니다. `\fnArial Black` 의 이름은 `fn`,
    `\rStyle` 의 이름은 `r` 이어야 한다 → known-tag 최장일치로 해결.
  - 색상/알파 태그는 숫자로 시작 (`\1c`, `\3a`).
  - 괄호 인자는 중첩 가능 (`\t(\clip(...))`) → 균형 괄호 읽기.

효과 컴파일러(effects/), QA(core/qa/), 구문 하이라이팅이 이 모듈을 공유한다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Union

# 알려진 태그명 — 최장일치를 위해 길이 내림차순 정렬해 사용.
KNOWN_TAGS: frozenset[str] = frozenset({
    # 위치/이동/원점/클립
    "pos", "move", "org", "clip", "iclip",
    # 페이드/트랜지션
    "fad", "fade", "t",
    # 폰트/크기/자간
    "fn", "fs", "fsp", "fscx", "fscy", "fe",
    # 회전/시어
    "fr", "frx", "fry", "frz", "fax", "fay",
    # 외형
    "bord", "xbord", "ybord", "shad", "xshad", "yshad", "be", "blur",
    # 색/알파
    "c", "1c", "2c", "3c", "4c", "alpha", "1a", "2a", "3a", "4a",
    # 스타일 토글
    "b", "i", "u", "s",
    # 정렬/줄바꿈
    "an", "a", "q",
    # 카라오케
    "k", "kf", "ko", "kt", "K",
    # 리셋/드로잉
    "r", "p", "pbo",
})

_KNOWN_SORTED: list[str] = sorted(KNOWN_TAGS, key=len, reverse=True)
_ALPHA_RE = re.compile(r"\d*[a-zA-Z]+")


def _match_known(rest: str) -> str | None:
    for t in _KNOWN_SORTED:
        if rest.startswith(t):
            return t
    return None


def _read_parens(body: str, i: int) -> tuple[str, int]:
    """body[i] == '(' 에서 균형 괄호를 읽어 (args_including_parens, new_i)."""
    depth = 0
    start = i
    n = len(body)
    while i < n:
        ch = body[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                i += 1
                break
        i += 1
    return body[start:i], i


@dataclass(slots=True)
class OverrideTag:
    """단일 오버라이드 태그. name='' 은 블록 내 코멘트 텍스트/잡음(round-trip용)."""
    name: str
    args: str = ""

    def render(self) -> str:
        if not self.name:
            return self.args
        return f"\\{self.name}{self.args}"


@dataclass(slots=True)
class OverrideBlock:
    tags: list[OverrideTag] = field(default_factory=list)

    def render(self) -> str:
        return "{" + "".join(t.render() for t in self.tags) + "}"

    def find(self, name: str) -> OverrideTag | None:
        for t in self.tags:
            if t.name == name:
                return t
        return None


@dataclass(slots=True)
class TextRun:
    text: str

    def render(self) -> str:
        return self.text


Segment = Union[OverrideBlock, TextRun]


def parse_tags(body: str) -> list[OverrideTag]:
    """오버라이드 블록 내부 문자열을 태그 리스트로."""
    tags: list[OverrideTag] = []
    i, n = 0, len(body)
    while i < n:
        if body[i] != "\\":
            j = body.find("\\", i)
            if j == -1:
                tags.append(OverrideTag("", body[i:]))
                break
            tags.append(OverrideTag("", body[i:j]))
            i = j
            continue

        i += 1  # consume backslash
        rest = body[i:]
        name = _match_known(rest)
        if name is None:
            m = _ALPHA_RE.match(rest)
            name = m.group(0) if m else ""
        if name == "":
            tags.append(OverrideTag("", "\\"))
            continue
        i += len(name)

        if i < n and body[i] == "(":
            args, i = _read_parens(body, i)
        else:
            j = body.find("\\", i)
            if j == -1:
                args, i = body[i:], n
            else:
                args, i = body[i:j], j
        tags.append(OverrideTag(name, args))
    return tags


def tokenize(text: str) -> list[Segment]:
    """Dialogue 텍스트 → [TextRun | OverrideBlock] 리스트. round-trip 보존."""
    segs: list[Segment] = []
    i, n = 0, len(text)
    while i < n:
        if text[i] == "{":
            j = text.find("}", i + 1)
            if j == -1:
                segs.append(TextRun(text[i:]))
                break
            segs.append(OverrideBlock(parse_tags(text[i + 1:j])))
            i = j + 1
        else:
            j = text.find("{", i)
            if j == -1:
                segs.append(TextRun(text[i:]))
                break
            segs.append(TextRun(text[i:j]))
            i = j
    return segs


def serialize(segs: list[Segment]) -> str:
    return "".join(s.render() for s in segs)


def strip_tags(text: str, newlines: bool = False) -> str:
    """오버라이드 블록을 제거한 평문. newlines=True 면 \\N/\\n 을 실제 줄바꿈으로."""
    out = "".join(s.text for s in tokenize(text) if isinstance(s, TextRun))
    if newlines:
        out = out.replace("\\N", "\n").replace("\\n", "\n")
    else:
        out = out.replace("\\N", " ").replace("\\n", " ")
    return out.replace("\\h", " ")


def visible_length(text: str) -> int:
    """CPS 계산용 — 태그/줄바꿈 마커를 제외한 보이는 글자 수."""
    return len(strip_tags(text, newlines=False).replace(" ", "")) + \
        strip_tags(text).count(" ")


def upsert_tag(text: str, name: str, args: str) -> str:
    """첫 오버라이드 블록에 \\name 을 설정(있으면 교체). 블록이 없으면 맨 앞에 생성."""
    segs = tokenize(text)
    for seg in segs:
        if isinstance(seg, OverrideBlock):
            existing = seg.find(name)
            if existing is not None:
                existing.args = args
            else:
                seg.tags.append(OverrideTag(name, args))
            return serialize(segs)
    return serialize([OverrideBlock([OverrideTag(name, args)]), *segs])


def find_tag(text: str, name: str) -> OverrideTag | None:
    for seg in tokenize(text):
        if isinstance(seg, OverrideBlock):
            t = seg.find(name)
            if t is not None:
                return t
    return None


def remove_tag(text: str, name: str) -> str:
    """모든 오버라이드 블록에서 \\name 태그를 제거. 빈 블록은 함께 정리."""
    segs = tokenize(text)
    out: list[Segment] = []
    for seg in segs:
        if isinstance(seg, OverrideBlock):
            seg.tags = [t for t in seg.tags if t.name != name]
            if not seg.tags:
                continue  # 빈 '{}' 블록은 버린다
        out.append(seg)
    return serialize(out)


# ---- 색상 변환: ASS 는 BGR, UI/LLM 은 RGB ----

def ass_color_to_rgb(s: str) -> tuple[int, int, int]:
    """'&HBBGGRR&' 또는 '&HAABBGGRR' → (r, g, b)."""
    h = re.sub(r"[&Hh]", "", s.strip())
    if not h:
        return (255, 255, 255)
    h = h.zfill(6)
    if len(h) >= 8:
        h = h[-8:]
        b, g, r = int(h[2:4], 16), int(h[4:6], 16), int(h[6:8], 16)
    else:
        h = h[-6:]
        b, g, r = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return (r, g, b)


def rgb_to_ass_color(r: int, g: int, b: int) -> str:
    """(r, g, b) → '&HBBGGRR&'."""
    clamp = lambda v: max(0, min(255, int(v)))
    return f"&H{clamp(b):02X}{clamp(g):02X}{clamp(r):02X}&"


def hex_to_rgb(s: str) -> tuple[int, int, int]:
    """'#RRGGBB' 또는 'RRGGBB' → (r, g, b)."""
    h = s.strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def hex_to_ass_color(s: str) -> str:
    """'#RRGGBB' → '&HBBGGRR&'. LLM 효과 컴파일에서 색 변환의 단일 경로."""
    r, g, b = hex_to_rgb(s)
    return rgb_to_ass_color(r, g, b)


def alpha_to_ass(a: int) -> str:
    """0~255 알파 → '&HAA&'. 0=불투명, 255=완전 투명 (ASS 규약)."""
    return f"&H{max(0, min(255, int(a))):02X}&"
