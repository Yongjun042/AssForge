r"""완성본 스타일 타이프셋 연출 확장기 — FxDirective → 여러 FxEvent (결정적).

effects.typeset_fx_schema 의 TYPESET_FX 화이트리스트를 그대로 따른다. 디렉터
(LLM/규칙)는 fx 이름 + 파라미터만 정하고, ASS 태그 문자열은 이 모듈이 만든다.
난수는 쓰지 않는다 — 글자별 변주는 글자 순번 기반 고정 테이블.

레퍼런스(수작업 완성본)에서 본뜬 패턴:
  - plain           {\an5\pos\fs\fad}텍스트{\fsp-5}...
  - drift_scale     {\an5\move(...)\fs\fad\t(0,dur,\fscx80\fscy80)}
  - char_scatter    글자별 {\an5\move\fs\fad\t(0,dur,\fscx\fscy\frz\frx\fry)}요
  - char_diagonal   글자별 {\an5\pos\fs\fad} — (x,y)→(x1,y1) 선형
  - char_stack      글자별 {\an5\pos\fs} — 아래→위, 시작 시차, 공통 끝
  - ghost_trail     같은 텍스트 N 겹, 회색조 \c + \move 오프셋 + \blur
  - shadow_bar      (extras 전용) ■■■ 블러 막대, layer 0
  - vertical_title  \fn@세로폰트\frz270 — 머리(블러 등장 + 별 자리 \iclip 구멍)
                    + 몸통(\clip 위→아래 드러내기) + 회전 ★
  - partial_color   span 만 \1c 변경 (+ \1a 드러내기)

글자별 배치(char_*)는 행/대각선 상자가 프레임을 넘치면 행 전체를 안쪽으로 민다
(글자마다 가장자리 좌표로 뭉개지지 않게). _cx/_cy 클램프는 마지막 안전장치.

레이어 규칙 (ASS 는 layer 가 클수록 위): 장식=0, 별=1, 본문=2.
ghost_trail 은 잔상 겹을 0..n-2, 원색 본문을 n-1 에 둔다.
"""
from __future__ import annotations

import math
import re
from typing import Any, Callable

from core.ass.tag_tokenizer import alpha_to_ass, hex_to_ass_color
from effects.spec import ParamSpec
from effects.typeset_fx_schema import (
    EXTRA_ONLY_FX,
    TYPESET_FX,
    FxDirective,
    FxEvent,
    FxLine,
)

# ---- 상수 --------------------------------------------------------------

DECOR_LAYER = 0     # shadow_bar 등 장식
STAR_LAYER = 1      # vertical_title 의 ★
TEXT_LAYER = 2      # 본문 텍스트 (최상단)

_HEX_RE = re.compile(r"^#?[0-9a-fA-F]{6}$")
_ELLIPSIS_RE = re.compile(r"(\.{2,}|…+)$")
_MAX_STR_LEN = 200

# 회색조 잔상 팔레트 (레퍼런스 순서) — #RRGGBB
_GHOST_PALETTE: tuple[str, ...] = (
    "#4E4E4E", "#615953", "#67564F", "#B7B7B7", "#5C5D59",
)

# char_scatter 변주 테이블 — 글자 순번 % 8. 열: (frz, frx, fry, sx, sy, mx, my)
# 앞 다섯은 [-1, 1] 단위값(rot_max / scale_var 에 곱함), 뒤 둘은 \move 오프셋(px).
_SCATTER_TABLE: tuple[tuple[float, float, float, float, float, int, int], ...] = (
    ( 0.10,  0.47, -0.50,  1.00,  0.30, -30,   7),
    (-0.35, -0.60,  0.40,  0.55,  0.80,  25,  -5),
    ( 0.60,  0.20,  0.70, -0.40,  0.20, -15, -10),
    (-0.15,  0.80, -0.30,  0.75, -0.35,  20,   8),
    ( 0.45, -0.40, -0.75, -0.20,  0.60, -25,  -6),
    (-0.70,  0.30,  0.55,  0.35, -0.50,  15,  10),
    ( 0.25, -0.75,  0.15,  0.90,  0.45, -10,  -8),
    (-0.50,  0.55, -0.60, -0.60,  0.70,  30,   4),
)

_SHADOW_BAR_FADE_IN = 660   # 레퍼런스 \fad(660,0)
_STAR_FS = 20
_STAR_GAP = 30
_STAR_HOLE = 12             # 머리 이벤트에 뚫는 별 자리 \iclip 구멍 반폭(px)

# 세로쓰기 폰트 — 레퍼런스 \fn@서울한강체 B. 가사 스타일(ai.lyric_typeset.
# lyric_style_props)과 같은 폰트이며 '@' 접두는 글리프를 미리 90° 눕혀 두어
# \frz270 뒤에 글자가 바로 선다 (없으면 글자가 옆으로 눕는다).
_VERTICAL_FONT = "서울한강체 B"
_TITLE_HEAD_MIN = 6         # 이 글자 수 이상이면 머리 3글자를 별도 이벤트로 (레퍼런스 '밤하늘')
_TITLE_HEAD_N = 3
_TITLE_FS_STEP = 5          # 글자별 크기 증분 (레퍼런스 70→105)
_TITLE_FS_GAIN_MAX = 35     # 마지막 글자까지의 총 증분 캡 — 12글자도 105 에서 멈춤
_TITLE_ADV = 0.9            # 세로 기둥에서 글자 1개의 진행량(em) — 한글 전각 실측 0.78~0.9


# ---- 검증 --------------------------------------------------------------

def _validate_param(fx: str, name: str, value: Any, pspec: ParamSpec) -> list[str]:
    """ParamSpec kind 별 검증 — int/float/color/bool/choice/str."""
    kind = pspec.kind
    if kind == "color":
        if not (isinstance(value, str) and _HEX_RE.match(value.strip())):
            return [f"{fx}.{name}: 색은 '#RRGGBB' 형식이어야 함 (현재 {value!r})"]
        return []
    if kind == "choice":
        if value not in pspec.choices:
            return [f"{fx}.{name}: {pspec.choices} 중 하나여야 함 (현재 {value!r})"]
        return []
    if kind == "bool":
        if not isinstance(value, bool):
            return [f"{fx}.{name}: 불리언이어야 함 (현재 {value!r})"]
        return []
    if kind == "str":
        if not isinstance(value, str):
            return [f"{fx}.{name}: 문자열이어야 함 (현재 {value!r})"]
        if len(value) > _MAX_STR_LEN:
            return [f"{fx}.{name}: {_MAX_STR_LEN}자 이하여야 함 (현재 {len(value)}자)"]
        return []
    if kind in ("int", "float"):
        if isinstance(value, bool):
            return [f"{fx}.{name}: 숫자여야 함 (현재 {value!r})"]
        try:
            num = float(value)
        except (TypeError, ValueError):
            return [f"{fx}.{name}: 숫자여야 함 (현재 {value!r})"]
        if not math.isfinite(num):
            return [f"{fx}.{name}: 유한한 숫자여야 함 (현재 {value!r})"]
        if kind == "int" and num != int(num):
            return [f"{fx}.{name}: 정수여야 함 (현재 {value!r})"]
        if pspec.minimum is not None and num < pspec.minimum:
            return [f"{fx}.{name}: {pspec.minimum} 이상이어야 함 (현재 {num:g})"]
        if pspec.maximum is not None and num > pspec.maximum:
            return [f"{fx}.{name}: {pspec.maximum} 이하여야 함 (현재 {num:g})"]
        return []
    return [f"{fx}.{name}: 알 수 없는 파라미터 종류 '{kind}'"]


def _validate_params(fx: str, params: Any) -> list[str]:
    """fx 의 params 딕셔너리를 화이트리스트 + 범위 검증."""
    if not isinstance(params, dict):
        return [f"'{fx}' 의 params 는 딕셔너리여야 함 (현재 {type(params).__name__})"]
    errors: list[str] = []
    meta: dict[str, ParamSpec] = TYPESET_FX[fx]["params"]
    for key in params:
        if key not in meta:
            errors.append(f"'{fx}' 에 없는 파라미터: '{key}'")
    for name, pspec in meta.items():
        if name in params:
            errors += _validate_param(fx, name, params[name], pspec)
    return errors


def validate_directive(d: FxDirective) -> list[str]:
    """오류 메시지 목록(빈 리스트 = 통과). 던지지 않음.

    검사: 본문 fx 가 TYPESET_FX 에 있고 EXTRA_ONLY_FX 가 아닌지, extras 의
    fx 가 모두 EXTRA_ONLY_FX 인지, 파라미터가 화이트리스트/범위 안인지.
    """
    errors: list[str] = []
    fx = d.fx
    if not isinstance(fx, str) or fx not in TYPESET_FX:
        errors.append(f"알 수 없는 fx: {fx!r}")
    elif fx in EXTRA_ONLY_FX:
        errors.append(f"'{fx}' 는 extras 전용 — 본문 fx 로 쓸 수 없음")
    else:
        errors += _validate_params(fx, d.params)

    extras = d.extras
    if not isinstance(extras, (list, tuple)):
        return errors + [f"extras 는 리스트여야 함 (현재 {type(extras).__name__})"]
    for i, item in enumerate(extras):
        if not (isinstance(item, (list, tuple)) and len(item) == 2):
            errors.append(f"extras[{i}]: (fx, params) 쌍이어야 함")
            continue
        efx, eparams = item
        if not isinstance(efx, str) or efx not in TYPESET_FX:
            errors.append(f"extras[{i}]: 알 수 없는 fx: {efx!r}")
        elif efx not in EXTRA_ONLY_FX:
            errors.append(f"extras[{i}]: '{efx}' 는 extras 에 쓸 수 없음 "
                          f"(허용: {sorted(EXTRA_ONLY_FX)})")
        else:
            errors += [f"extras[{i}] {e}" for e in _validate_params(efx, eparams)]
    return errors


# ---- 공용 헬퍼 ---------------------------------------------------------

def _merged(fx: str, params: dict[str, Any]) -> dict[str, Any]:
    """기본값 + 지정값. int/float 는 형 변환까지."""
    meta: dict[str, ParamSpec] = TYPESET_FX[fx]["params"]
    out: dict[str, Any] = {}
    for name, pspec in meta.items():
        v = params.get(name, pspec.default)
        if pspec.kind == "int":
            v = int(float(v))
        elif pspec.kind == "float":
            v = float(v)
        elif pspec.kind == "color":
            v = str(v).strip()
            if not v.startswith("#"):
                v = "#" + v
        out[name] = v
    return out


def _num(x: Any) -> str:
    """정수값이면 정수로, 아니면 소수 둘째자리까지(불필요한 0 제거)."""
    f = float(x)
    if f == int(f):
        return str(int(f))
    return f"{f:.2f}".rstrip("0").rstrip(".")


def _cx(v: float, res: tuple[int, int]) -> int:
    return max(0, min(int(res[0]), int(round(v))))


def _cy(v: float, res: tuple[int, int]) -> int:
    return max(0, min(int(res[1]), int(round(v))))


def _fit_shift(lo: float, hi: float, size: int, margin: float) -> float:
    """[lo-margin, hi+margin] 상자가 [0, size] 안에 들도록 옮길 오프셋.

    상자가 프레임보다 크면 가운데 맞춤. 글자별 배치가 프레임을 넘칠 때 글자마다
    가장자리 좌표로 뭉개지는 대신 행 전체를 안쪽으로 민다.
    """
    a, b = lo - margin, hi + margin
    if b - a > size:
        return size / 2.0 - (a + b) / 2.0
    if a < 0:
        return -a
    if b > size:
        return size - b
    return 0.0


def _font_tag() -> str:
    """세로쓰기 폰트 태그 — \\fn 인자는 원문 그대로라 중괄호/역슬래시만 제거."""
    name = _VERTICAL_FONT.replace("{", "").replace("}", "").replace("\\", "")
    return f"\\fn@{name}"


def _find_span(text: str, span: str) -> tuple[int, int] | None:
    """\\N 을 공백 하나로 본 정규화 문자열에서 span 을 찾아 원문 [start, end) 반환.

    디렉터(ai.typeset_director)는 \\N→공백 평문으로 span 을 검증하므로, 줄바꿈을
    가로지르는 span 도 같은 기준으로 찾아 원문 구간(\\N 포함)에 색을 입힌다.
    오버라이드 색은 \\N 을 넘어 유지되므로 구간을 나눌 필요가 없다. 없으면 None.
    """
    def _norm(s: str) -> tuple[str, list[tuple[int, int]]]:
        chars: list[str] = []
        spans: list[tuple[int, int]] = []
        i = 0
        while i < len(s):
            if s[i] == "\\" and i + 1 < len(s) and s[i + 1] in "Nn":
                chars.append(" ")
                spans.append((i, i + 2))
                i += 2
            else:
                chars.append(s[i])
                spans.append((i, i + 1))
                i += 1
        return "".join(chars), spans

    if not span:
        return None
    nt, idx = _norm(text)
    ns, _ = _norm(span)
    if not ns:
        return None
    pos = nt.find(ns)
    if pos < 0:
        return None
    return idx[pos][0], idx[pos + len(ns) - 1][1]


def _safe_text(text: str) -> str:
    """평문에서 오버라이드 블록을 만들 수 있는 중괄호를 제거."""
    return text.replace("{", "").replace("}", "")


def _tighten(text: str) -> str:
    """끝의 '...' / '…' 앞에 {\\fsp-5} 삽입 (레퍼런스: {\\fsp-5}...)."""
    m = _ELLIPSIS_RE.search(text)
    if not m or m.start() == 0:
        return text
    return text[:m.start()] + "{\\fsp-5}" + text[m.start():]


def _split_ellipsis(text: str) -> tuple[str, str]:
    """(본체, 말줄임) — 말줄임이 없으면 ('본체', '')."""
    m = _ELLIPSIS_RE.search(text)
    if not m or m.start() == 0:
        return text, ""
    return text[:m.start()], text[m.start():]


def _rows_of_chars(text: str) -> list[list[str]]:
    """\\N 기준 행 분할, 각 행은 공백 제외 글자 목록. 빈 행은 버린다."""
    rows: list[list[str]] = []
    for row in text.replace("\\n", "\\N").split("\\N"):
        chars = [ch for ch in row if not ch.isspace()]
        if chars:
            rows.append(chars)
    return rows


def _flat_chars(text: str) -> list[str]:
    """\\N 과 공백을 뺀 글자 목록."""
    return [ch for row in _rows_of_chars(text) for ch in row]


def _fad(fi: int, fo: int) -> str:
    return f"\\fad({int(fi)},{int(fo)})"


def _line_dur(line: FxLine) -> int:
    return max(1, int(line.end_ms) - int(line.start_ms))


def _style_color_tag(dark: bool) -> str:
    """스타일 기본색 복원 태그 (하양 &HFFFFFF& / 검정 &H000000&)."""
    return "\\1c&H000000&" if dark else "\\1c&HFFFFFF&"


def _event(line: FxLine, text: str, layer: int = TEXT_LAYER,
           start: int | None = None, end: int | None = None) -> FxEvent:
    return FxEvent(
        text=text,
        start_ms=int(line.start_ms if start is None else start),
        end_ms=int(line.end_ms if end is None else end),
        style=line.style,
        layer=int(layer),
    )


# ---- fx 별 확장 ------------------------------------------------------

def _x_plain(line: FxLine, p: dict[str, Any], res: tuple[int, int]) -> list[FxEvent]:
    x, y = _cx(line.x, res), _cy(line.y, res)
    body = _safe_text(line.text)
    if p["tighten_ellipsis"]:
        body = _tighten(body)
    block = f"{{\\an5\\pos({x},{y})\\fs{p['fs']}{_fad(p['fade_in'], p['fade_out'])}}}"
    return [_event(line, block + body)]


def _x_drift_scale(line: FxLine, p: dict[str, Any], res: tuple[int, int]) -> list[FxEvent]:
    x, y = _cx(line.x, res), _cy(line.y, res)
    x1, y1 = _cx(line.x + p["dx"], res), _cy(line.y + p["dy"], res)
    dur = _line_dur(line)
    s = _num(p["scale_to"])
    block = (
        f"{{\\an5\\move({x},{y},{x1},{y1})\\fs{p['fs']}"
        f"{_fad(p['fade_in'], p['fade_out'])}"
        f"\\t(0,{dur},\\fscx{s}\\fscy{s})}}"
    )
    return [_event(line, block + _tighten(_safe_text(line.text)))]


def _x_char_scatter(line: FxLine, p: dict[str, Any], res: tuple[int, int]) -> list[FxEvent]:
    body, ell = _split_ellipsis(_safe_text(line.text))
    rows = _rows_of_chars(body)
    if not rows:
        return _x_plain(line, _merged("plain", {"fs": p["fs"]}), res)
    fs = int(p["fs"])
    spread = int(p["spread"])
    rot_max = float(p["rot_max"])
    scale_var = float(p["scale_var"])
    dur = _line_dur(line)
    fad = _fad(p["fade_in"], p["fade_out"])
    # 행 전체 상자(글자 반폭 + 확대 여유 + \move 오프셋)가 프레임에 들도록 먼저 민다.
    max_n = max(len(r) for r in rows)
    half_row = (max_n - 1) * spread / 2.0
    glyph = fs * 0.7
    mx_max = max(abs(t[5]) for t in _SCATTER_TABLE)
    my_max = max(abs(t[6]) for t in _SCATTER_TABLE)
    ox = _fit_shift(line.x - half_row, line.x + half_row, res[0], glyph + mx_max)
    oy = _fit_shift(line.y, line.y + (len(rows) - 1) * fs * 1.1, res[1], glyph + my_max)
    out: list[FxEvent] = []
    idx = 0
    total = sum(len(r) for r in rows)
    for r, chars in enumerate(rows):
        n = len(chars)
        row_y = line.y + oy + r * fs * 1.1
        left = line.x + ox - (n - 1) * spread / 2.0
        for i, ch in enumerate(chars):
            frz_u, frx_u, fry_u, sx_u, sy_u, mx, my = _SCATTER_TABLE[idx % len(_SCATTER_TABLE)]
            cx, cy = left + i * spread, row_y
            x0, y0 = _cx(cx, res), _cy(cy, res)
            x1, y1 = _cx(cx + mx, res), _cy(cy + my, res)
            sx = _num(100.0 + scale_var * sx_u)
            sy = _num(100.0 + scale_var * sy_u)
            frz = _num(rot_max * frz_u)
            frx = _num(rot_max * frx_u)
            fry = _num(rot_max * fry_u)
            block = (
                f"{{\\an5\\move({x0},{y0},{x1},{y1})\\fs{fs}{fad}"
                f"\\t(0,{dur},\\fscx{sx}\\fscy{sy}\\frz{frz}\\frx{frx}\\fry{fry})}}"
            )
            text = ch
            if ell and idx == total - 1:
                text = ch + "{\\fsp-5}" + ell
            out.append(_event(line, block + text))
            idx += 1
    return out


def _x_char_diagonal(line: FxLine, p: dict[str, Any], res: tuple[int, int]) -> list[FxEvent]:
    body, ell = _split_ellipsis(_safe_text(line.text))
    chars = _flat_chars(body)
    if not chars:
        return _x_plain(line, _merged("plain", {"fs": p["fs"]}), res)
    fs = int(p["fs"])
    n = len(chars)
    fad = _fad(p["fade_in"], p["fade_out"])
    margin = fs * 0.6
    if p["x1"] > 0 and p["y1"] > 0:
        # 명시 끝점: (x,y) 가 시작점.
        sx, sy = float(line.x), float(line.y)
        ex, ey = float(p["x1"]), float(p["y1"])
    else:
        # 자동: 오른쪽 아래 대각선, (x,y) 를 중심으로. 프레임(여백 제외)보다
        # 길면 간격을 줄인다 — 끝 글자들이 가장자리에 뭉치지 않게.
        w, h = (n - 1) * fs * 1.1, (n - 1) * fs * 0.75
        avail_w, avail_h = res[0] - 2 * margin, res[1] - 2 * margin
        k = min(1.0, avail_w / w if w > 0 else 1.0, avail_h / h if h > 0 else 1.0)
        w, h = w * max(0.0, k), h * max(0.0, k)
        sx, sy = line.x - w / 2.0, line.y - h / 2.0
        ex, ey = line.x + w / 2.0, line.y + h / 2.0
    # 대각선 상자 전체를 프레임 안으로 (명시 끝점도 화면 밖이면 같이 민다)
    ox = _fit_shift(min(sx, ex), max(sx, ex), res[0], margin)
    oy = _fit_shift(min(sy, ey), max(sy, ey), res[1], margin)
    sx, ex, sy, ey = sx + ox, ex + ox, sy + oy, ey + oy
    out: list[FxEvent] = []
    for i, ch in enumerate(chars):
        t = i / (n - 1) if n > 1 else 0.0
        x = _cx(sx + (ex - sx) * t, res)
        y = _cy(sy + (ey - sy) * t, res)
        text = ch
        if ell and i == n - 1:
            text = ch + "{\\fsp-5}" + ell
        out.append(_event(line, f"{{\\an5\\pos({x},{y})\\fs{fs}{fad}}}" + text))
    return out


def _x_char_stack(line: FxLine, p: dict[str, Any], res: tuple[int, int]) -> list[FxEvent]:
    body, ell = _split_ellipsis(_safe_text(line.text))
    chars = _flat_chars(body)
    if not chars:
        return _x_plain(line, _merged("plain", {"fs": p["fs"]}), res)
    fs = int(p["fs"])
    rise = int(p["rise"])
    stagger = int(p["stagger_ms"])
    n = len(chars)
    start, end = int(line.start_ms), int(line.end_ms)
    dur = max(1, end - start)
    # 마지막 글자도 최소 dur/(n+1) 은 보이도록 시차를 클램프.
    latest = start + max(0, dur - max(1, dur // (n + 1)))
    out: list[FxEvent] = []
    for i, ch in enumerate(chars):
        y = _cy(line.y - (rise * i / (n - 1) if n > 1 else 0), res)
        x = _cx(line.x, res)
        fs_i = max(40, fs - 4 * i)
        s_i = min(start + i * stagger, latest)
        text = ch
        if ell and i == n - 1:
            text = ch + "{\\fsp-5}" + ell
        out.append(_event(line, f"{{\\an5\\pos({x},{y})\\fs{fs_i}}}" + text,
                          start=s_i, end=end))
    return out


def _x_ghost_trail(line: FxLine, p: dict[str, Any], res: tuple[int, int]) -> list[FxEvent]:
    x, y = _cx(line.x, res), _cy(line.y, res)
    fs = int(p["fs"])
    layers = int(p["layers"])
    spread = int(p["spread"])
    blur = _num(p["blur"])
    scale = _num(p["scale_to"])
    dur = _line_dur(line)
    t1 = int(dur * 0.8)
    t2 = dur
    body = _tighten(_safe_text(line.text))
    ghosts = layers - 1
    out: list[FxEvent] = []
    for j in range(ghosts):
        # 잔상이 여럿이면 -spread..+spread 대칭, 하나뿐이면 +spread 로 벗어나게
        # (-spread + spread = 0 이 되어 본문 밑에 숨던 버그 방지).
        off = (-spread + 2 * spread * j / (ghosts - 1)) if ghosts > 1 else spread
        x1 = _cx(x + off, res)
        col = hex_to_ass_color(_GHOST_PALETTE[j % len(_GHOST_PALETTE)])
        block = (
            f"{{\\an5\\move({x},{y},{x1},{y},{t1},{t2})\\fs{fs}\\bord0"
            f"\\t({t1},{t2},\\blur{blur}\\fscx{scale}\\c{col})}}"
        )
        out.append(_event(line, block + body, layer=j))
    top = f"{{\\an5\\pos({x},{y})\\fs{fs}{_fad(0, p['fade_out'])}}}"
    out.append(_event(line, top + body, layer=ghosts))
    return out


def _x_shadow_bar(line: FxLine, p: dict[str, Any], res: tuple[int, int]) -> list[FxEvent]:
    x, y = _cx(line.x, res), _cy(line.y + p["offset_y"], res)
    col = hex_to_ass_color(p["color"])
    alpha = alpha_to_ass(p["alpha"])
    block = (
        f"{{\\an5\\pos({x},{y})\\1c{col}\\1a{alpha}\\3a&H00&\\bord0"
        f"\\blur{_num(p['blur'])}\\fsp-35\\fscx110\\fscy{_num(p['scale_y'])}"
        f"{_fad(_SHADOW_BAR_FADE_IN, 0)}}}"
    )
    return [_event(line, block + "■" * int(p["width_chars"]), layer=DECOR_LAYER)]


def _x_vertical_title(line: FxLine, p: dict[str, Any], res: tuple[int, int]) -> list[FxEvent]:
    chars = _flat_chars(_safe_text(line.text))
    if not chars:
        return _x_plain(line, _merged("plain", {"fs": p["fs"]}), res)
    fs = int(p["fs"])
    n = len(chars)
    reveal = int(p["reveal_ms"])
    fade_out = int(p["fade_out"])
    dur = _line_dur(line)
    # 글자 크기: 순번마다 +5 하되 총 증분은 캡 (레퍼런스 8글자 70→105, 12글자도 105).
    step = min(float(_TITLE_FS_STEP), _TITLE_FS_GAIN_MAX / (n - 1)) if n > 1 else 0.0
    sizes = [fs + int(round(step * i)) for i in range(n)]
    adv = [s * _TITLE_ADV for s in sizes]
    # 레퍼런스 꼴: 머리('밤하늘') 는 블러로 즉시, 몸통('로이어지는언덕길') 은 클립 드러내기.
    head_n = _TITLE_HEAD_N if n >= _TITLE_HEAD_MIN else 0
    len_head = sum(adv[:head_n])
    len_body = sum(adv[head_n:])
    top = line.y - (len_head + len_body) / 2.0
    half_w = max(sizes) * 0.75            # 가장 큰 글자까지 덮는 클립 반폭
    margin_y = fs * 0.35                  # 글리프 여백 — 첫/끝 글자가 잘리지 않게
    x = _cx(line.x, res)
    cx0, cx1 = _cx(line.x - half_w, res), _cx(line.x + half_w, res)
    font = _font_tag()
    fad = _fad(0, fade_out)
    star_x, star_y = _cx(line.x, res), _cy(top - _STAR_GAP, res)

    def _glyphs(lo: int, hi: int) -> str:
        parts = [chars[lo]]
        for i in range(lo + 1, hi):
            parts.append(f"{{\\fs{sizes[i]}}}{chars[i]}")
        return "".join(parts)

    out: list[FxEvent] = []
    if head_n:
        hy = _cy(top + len_head / 2.0, res)
        hole = ""
        if p["star"]:
            # 별 자리에 \iclip 구멍 — 블러 번짐이 별을 덮지 않게 (레퍼런스 '밤하늘')
            hole = (f"\\iclip({star_x - _STAR_HOLE},{star_y - _STAR_HOLE},"
                    f"{star_x + _STAR_HOLE},{star_y + _STAR_HOLE})")
        head = (
            f"{{\\an5\\pos({x},{hy}){font}\\frz270\\fs{sizes[0]}"
            f"\\bord0\\blur20\\t(0,600,\\blur0){hole}{fad}}}"
        )
        out.append(_event(line, head + _glyphs(0, head_n)))
    by = top + len_head + len_body / 2.0
    y = _cy(by, res)
    ctop = _cy(by - len_body / 2.0 - margin_y, res)
    cbot = _cy(by + len_body / 2.0 + margin_y, res)
    body = (
        f"{{\\an5\\pos({x},{y}){font}\\frz270\\fs{sizes[head_n]}"
        f"\\clip({cx0},{ctop},{cx1},{ctop})"
        f"\\t(0,{reveal},\\clip({cx0},{ctop},{cx1},{cbot}))"
        f"{fad}}}"
    )
    out.append(_event(line, body + _glyphs(head_n, n)))
    if p["star"]:
        star = (
            f"{{\\an5{font}\\fs{_STAR_FS}\\bord0\\blur20\\t(0,600,\\blur0)"
            f"\\t(0,{dur},\\frz-720)\\pos({star_x},{star_y})\\org({star_x},{star_y})"
            f"{fad}}}★"
        )
        out.append(_event(line, star, layer=STAR_LAYER))
    return out


def _x_partial_color(line: FxLine, p: dict[str, Any], res: tuple[int, int]) -> list[FxEvent]:
    x, y = _cx(line.x, res), _cy(line.y, res)
    lead = f"{{\\an5\\pos({x},{y})\\fs{p['fs']}{_fad(p['fade_in'], p['fade_out'])}}}"
    text = _safe_text(line.text)
    span = _safe_text(str(p["span"]))
    found = _find_span(text, span)
    if found is None:
        return [_event(line, lead + _tighten(text))]
    t0, t1 = found
    before, span, after = text[:t0], text[t0:t1], text[t1:]
    col = hex_to_ass_color(p["color"])
    reveal = int(p["reveal_ms"])
    restore = _style_color_tag(line.dark)
    if reveal > 0:
        open_blk = f"{{\\1c{col}\\1a&HFF&\\t(0,{reveal},\\1a&H00&)}}"
        close_blk = f"{{{restore}\\1a&H00&}}"
    else:
        open_blk = f"{{\\1c{col}}}"
        close_blk = f"{{{restore}}}"
    body = before + open_blk + span + close_blk + _tighten(after)
    return [_event(line, lead + body)]


_EXPANDERS: dict[str, Callable[[FxLine, dict[str, Any], tuple[int, int]], list[FxEvent]]] = {
    "plain": _x_plain,
    "drift_scale": _x_drift_scale,
    "char_scatter": _x_char_scatter,
    "char_diagonal": _x_char_diagonal,
    "char_stack": _x_char_stack,
    "ghost_trail": _x_ghost_trail,
    "shadow_bar": _x_shadow_bar,
    "vertical_title": _x_vertical_title,
    "partial_color": _x_partial_color,
}


# ---- 공개 API ----------------------------------------------------------

def expand_line(
    line: FxLine,
    d: FxDirective,
    play_res: tuple[int, int] = (1920, 1080),
) -> list[FxEvent]:
    """검증 통과한 디렉티브를 이벤트 목록으로 확장 (결정적).

    순서: extras(장식, layer 0) → 본문 fx 이벤트들. 검증은 호출측 책임 —
    미검증 입력은 expand_safe 를 쓴다.
    """
    res = (int(play_res[0]), int(play_res[1]))
    out: list[FxEvent] = []
    for efx, eparams in d.extras:
        out += _EXPANDERS[efx](line, _merged(efx, dict(eparams or {})), res)
    out += _EXPANDERS[d.fx](line, _merged(d.fx, dict(d.params or {})), res)
    return out


def expand_safe(
    line: FxLine,
    d: FxDirective,
    play_res: tuple[int, int] = (1920, 1080),
) -> tuple[list[FxEvent], list[str]]:
    """(이벤트, 오류). 검증 실패·예외 시 plain 으로 폴백. 절대 던지지 않는다."""
    errors: list[str] = []
    try:
        errors = validate_directive(d)
    except Exception as e:  # noqa: BLE001 — 방어적
        errors = [f"검증 중 예외: {e}"]
    if not errors:
        try:
            return expand_line(line, d, play_res), []
        except Exception as e:  # noqa: BLE001
            errors = [f"'{d.fx}' 확장 중 예외: {e} — plain 으로 폴백"]
    try:
        return expand_line(line, FxDirective("plain"), play_res), errors
    except Exception as e:  # noqa: BLE001
        return [], errors + [f"plain 폴백도 실패: {e}"]


def fx_catalog_text() -> str:
    """LLM 프롬프트용 카탈로그 — TYPESET_FX 에서 자동 생성."""
    lines: list[str] = []
    for name, meta in TYPESET_FX.items():
        tag = " [extras 전용]" if name in EXTRA_ONLY_FX else ""
        lines.append(f"- {name} ({meta['label']}){tag}")
        params: dict[str, ParamSpec] = meta["params"]
        if not params:
            lines.append("    (파라미터 없음)")
        for pname, ps in params.items():
            if ps.kind == "choice":
                rng = "/".join(ps.choices)
            elif ps.kind in ("int", "float"):
                lo = _num(ps.minimum) if ps.minimum is not None else "-"
                hi = _num(ps.maximum) if ps.maximum is not None else "-"
                rng = f"{ps.kind} {lo}~{hi}"
            elif ps.kind == "color":
                rng = "#RRGGBB"
            else:
                rng = ps.kind
            default = ps.default if ps.default != "" else '""'
            lines.append(f"    {pname}: {ps.label} [{rng}, 기본 {default}]")
    lines.append(
        f"extras 에는 {sorted(EXTRA_ONLY_FX)} 만 쓸 수 있고, 본문 fx 로는 쓸 수 없다."
    )
    return "\n".join(lines)


__all__ = [
    "DECOR_LAYER",
    "STAR_LAYER",
    "TEXT_LAYER",
    "expand_line",
    "expand_safe",
    "fx_catalog_text",
    "validate_directive",
]
