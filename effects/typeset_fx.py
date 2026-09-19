r"""완성본 스타일 타이프셋 연출 확장기 — FxDirective → 여러 FxEvent (결정적).

effects.typeset_fx_schema 의 TYPESET_FX 화이트리스트를 그대로 따른다. 디렉터
(LLM/규칙)는 fx 이름 + 파라미터만 정하고, ASS 태그 문자열은 이 모듈이 만든다.
실행마다 달라지는 난수는 쓰지 않는다 — 글자별 변주는 텍스트+순번 시드의 결정적
의사난수(md5) 또는 글자 순번 기반 고정 테이블.

레퍼런스(수작업 완성본)에서 본뜬 패턴:
  - plain           {\an5\pos\fs\fad}텍스트{\fsp-5}...
  - subtitle        {\an2\fs70}나레이션 — \pos 없이 기본 하단 중앙, 페이드 없음
  - drift_scale     {\an5\move(...)\fs\fad\t(0,dur,\fscx80\fscy80)}
  - char_scatter    글자별 {\an5\move\fs\fad\t(0,dur,\fscx\fscy\frz\frx\fry)}요 —
                    mode=wobble: 제자리(작은 \move ≤ 0.6×fs)에서 변형이 점점 커진다
  - char_diagonal   글자별 {\an5\pos\fs\fad} — (x,y)→(x1,y1) 선형, 간격 ≥ 글자 크기
  - char_stack      글자별 {\an5\pos\fs} — 아래→위, 시작 시차, 공통 끝
  - ghost_trail     t0 전: 정지한 통짜 한 줄(\pos). t0~t1: 같은 텍스트 N 겹이
                    \move 로 가로로 벌어지며 \blur·회색조 \c (화면 전환 번짐)
  - shadow_bar      (extras 전용) ■■■ 블러 막대, layer 0 — 기본으로는 안 붙는다
  - vertical_title  \fn@세로폰트\frz270 — 머리(블러 등장 + 별 자리 \iclip 구멍)
                    + 몸통(\clip 또는 \iclip 위→아래 드러내기, 글자 크기 선형 증가)
                    + 회전 ★. head_pos/body_top/body_bottom 으로 잰 자리에 맞춘다
  - partial_color   span(들)만 \1c 변경 후 원래 색 복귀 (+ \1a 드러내기)
  - eclipse         span 색 + 채색 글로우 ■ 층 → cover_ms 에 \t 로 \1a&HFF&,
                    어두운 ■ 블러 층과 회색 span 글자 층이 덮는다
  - fly_rotate      {\an5\move(x0,y0,x,y,0,mv)\fs\fad\t(0,mv,\fr-720)}마음 —
                    (x-dx,y-dy) 에서 (x,y) 로 날아오며 회전 (\fr = \frz 축약)

글자별 배치(char_*)는 행/대각선 상자가 프레임을 넘치면 행 전체를 안쪽으로 민다
(글자마다 가장자리 좌표로 뭉개지지 않게). _cx/_cy 클램프는 마지막 안전장치.

공통 모양 파라미터(모든 본문 fx): color → \c, glow=dark|color →
\3c\3a&H00&\bord{glow_size}\blur{glow_blur} (스타일의 외곽선 알파가 FF 라 \3a 로 켠다).
글자별 분할 fx 는 글로우를 아래 레이어의 별도 이벤트(채움 투명)로 낸다 — 같은
레이어면 뒤 글자의 두꺼운 외곽선이 앞 글자의 채움을 덮기 때문.

레이어 규칙 (ASS 는 layer 가 클수록 위): 장식=0, 별/글자 글로우=1, 본문=2.
ghost_trail 은 잔상 겹을 0..n-2, 원색 본문을 n-1 에 둔다. eclipse 는 글로우 도형 0,
그림자 막대 1, 본문 2, 회색 글자 3.
"""
from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from typing import Any, Callable

from core.ass.tag_tokenizer import alpha_to_ass, hex_to_ass_color
from effects.spec import ParamSpec
from effects.typeset_fx_schema import (
    EXTRA_ONLY_FX,
    LOOK_PARAMS,
    SPAN_FX,
    TYPESET_FX,
    FxDirective,
    FxEvent,
    FxLine,
)

# ---- 상수 --------------------------------------------------------------

DECOR_LAYER = 0     # shadow_bar 등 장식, eclipse 의 글로우 도형
STAR_LAYER = 1      # vertical_title 의 ★
GLOW_LAYER = 1      # 글자별 분할 fx 의 글로우 겹 / eclipse 의 그림자 막대
TEXT_LAYER = 2      # 본문 텍스트
COVER_LAYER = 3     # eclipse 의 회색 글자 (본문 위)

_HEX_RE = re.compile(r"^#?[0-9a-fA-F]{6}$")
_ELLIPSIS_RE = re.compile(r"(\.{2,}|…+)$")
_MAX_STR_LEN = 200
_MAX_SPANS = 8

# 회색조 잔상 팔레트 (레퍼런스 순서) — #RRGGBB
_GHOST_PALETTE: tuple[str, ...] = (
    "#4E4E4E", "#615953", "#67564F", "#B7B7B7", "#5C5D59",
)
_GHOST_T0_TAIL = 320        # t0 자동 = 줄 길이 − 320ms (레퍼런스 1620ms 줄의 1300)

# char_scatter mode=table 변주 테이블 — 글자 순번 % 8.
# 열: (frz, frx, fry, sx, sy, mx, my). 앞 다섯은 [-1, 1] 단위값(rot_max / scale_var 에
# 곱함), 뒤 둘은 \move 오프셋(px).
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
_WOBBLE_MOVE_CAP = 0.6      # mode=wobble: \move 이동량 ≤ 0.6×fs (제자리 흔들림)
_WOBBLE_FLOOR = 0.35        # 단위 변주의 최소 크기 — 모든 글자가 눈에 띄게 변형되도록

_DIAG_MIN_FS = 64           # char_diagonal: 간격이 모자라면 여기까지 글자를 줄인다
_DIAG_GAP = 1.02            # 이웃 글자 중심 간격 ≥ 1.0×fs (좌표 반올림 여유 2%)

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
_TITLE_ADV = 0.9            # 세로 기둥에서 글자 1개의 진행량(em) — 한글 전각 실측 0.78~0.9
_TITLE_RAMP_MIN = 1.0       # 몸통 끝/처음 글자 크기 비 클램프 — 균일하다고 잰 세로 줄(1.0)은 그대로
_TITLE_RAMP_MAX = 2.2
_TITLE_FS_MIN = 40
_TITLE_FS_FLOOR = 28        # 긴 번역이 프레임에 안 들어갈 때만 여기까지 줄인다
_TITLE_FS_GOOD = 52         # 몸통 첫 글자가 이보다 작아지면 기둥을 프레임 아래쪽까지 늘려 쓴다
_TITLE_BOTTOM_PAD = 24      # …그때 남기는 프레임 아래 여백(px)
_TITLE_FS_MAX = 160
_TITLE_HEAD_GAP = 0.3       # 머리와 몸통 사이 간격(em) — 자리를 잰 값이 없을 때

# eclipse (레퍼런스 '봄의 햇볕을...' 4개 층)
_ECLIPSE_GLOW_BLUR = 15     # \blur15 채색 글로우 도형
_ECLIPSE_GLOW_SCY = 180     # \fscy179
_ECLIPSE_SHADOW_BLUR = 40   # \blur40 어두운 막대
_ECLIPSE_SHADOW_SCY = 216   # \fscy216
_ECLIPSE_SHADOW_LEAD = 400  # 그림자 막대는 cover_ms − 400 부터 페이드인
_ECLIPSE_SHADOW_FADE = 660  # \fad(660,0)
_ECLIPSE_SHADOW_WIDEN = 1.3  # 그림자는 span 보다 넓게 덮는다
_ECLIPSE_AUTO_TAIL = 300    # cover_ms 자동 = 줄 길이 − cover_dur − 300
_BAR_OVERLAP = 0.36         # ■ 사이 틈을 메우는 음수 자간(em) — 레퍼런스 \fsp-35 @ fs96


# ---- 검증 --------------------------------------------------------------

def _span_pair(item: Any) -> tuple[str, str] | None:
    """spans 항목 1개 → (부분 문자열, 색). [span, color] 쌍 또는 {"span", "color"}."""
    if isinstance(item, dict):
        sp, col = item.get("span"), item.get("color")
    elif isinstance(item, (list, tuple)) and len(item) == 2:
        sp, col = item
    else:
        return None
    if not isinstance(sp, str) or not isinstance(col, str):
        return None
    return sp, col


def _validate_param(fx: str, name: str, value: Any, pspec: ParamSpec) -> list[str]:
    """ParamSpec kind 별 검증 — int/float/color/bool/choice/str/point/spans."""
    kind = pspec.kind
    if value is None and pspec.default is None:
        return []           # '지정 안 함' — 기본값이 None 인 선택 파라미터
    if kind == "color":
        if not (isinstance(value, str) and _HEX_RE.match(value.strip())):
            return [f"{fx}.{name}: 색은 '#RRGGBB' 형식이어야 함 (현재 {value!r})"]
        return []
    if kind == "point":
        if not (isinstance(value, (list, tuple)) and len(value) == 2):
            return [f"{fx}.{name}: [x, y] 좌표 쌍이어야 함 (현재 {value!r})"]
        errs: list[str] = []
        axis_spec = ParamSpec("float", 0.0, "", pspec.minimum, pspec.maximum)
        for axis, v in zip("xy", value):
            errs += _validate_param(fx, f"{name}.{axis}", v, axis_spec)
        return errs
    if kind == "spans":
        if not isinstance(value, (list, tuple)):
            return [f"{fx}.{name}: [[부분 문자열, 색], ...] 리스트여야 함 (현재 {value!r})"]
        if len(value) > _MAX_SPANS:
            return [f"{fx}.{name}: {_MAX_SPANS}개 이하여야 함 (현재 {len(value)}개)"]
        errs = []
        for i, item in enumerate(value):
            pair = _span_pair(item)
            if pair is None:
                errs.append(f"{fx}.{name}[{i}]: [부분 문자열, '#RRGGBB'] 쌍이어야 함 "
                            f"(현재 {item!r})")
                continue
            sp, col = pair
            if not sp or len(sp) > _MAX_STR_LEN:
                errs.append(f"{fx}.{name}[{i}]: 부분 문자열은 1~{_MAX_STR_LEN}자여야 함")
            if not _HEX_RE.match(col.strip()):
                errs.append(f"{fx}.{name}[{i}]: 색은 '#RRGGBB' 형식이어야 함 (현재 {col!r})")
        return errs
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
        except (TypeError, ValueError, OverflowError):
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


def _span_list(params: dict[str, Any], default_color: str) -> list[tuple[str, str]]:
    """span/color + spans → [(부분 문자열, 색)] (빈 span 은 뺀다, span 이 먼저)."""
    out: list[tuple[str, str]] = []
    sp = params.get("span")
    if isinstance(sp, str) and sp:
        out.append((sp, str(params.get("color") or default_color)))
    for item in params.get("spans") or ():
        pair = _span_pair(item)
        if pair is not None and pair[0]:
            out.append(pair)
    return out


def _validate_spans(fx: str, params: dict[str, Any], text: Any) -> list[str]:
    """SPAN_FX: 부분 문자열이 하나는 있어야 하고, text 를 알면 그 안에 있어야 한다."""
    spans = _span_list(params, "#000000")
    if not spans:
        return [f"{fx}.span: 색칠할 부분 문자열이 비어 있음"]
    if not isinstance(text, str):
        return []
    body = _safe_text(text)
    return [f"{fx}.span: 줄 텍스트에 없는 부분 문자열 {sp!r}"
            for sp, _ in spans if _find_span(body, _safe_text(sp)) is None]


def validate_directive(d: FxDirective, text: str | None = None) -> list[str]:
    """오류 메시지 목록(빈 리스트 = 통과). 던지지 않음.

    검사: 본문 fx 가 TYPESET_FX 에 있고 EXTRA_ONLY_FX 가 아닌지, extras 의
    fx 가 모두 EXTRA_ONLY_FX 인지, 파라미터가 화이트리스트/범위 안인지.
    partial_color/eclipse 는 span(또는 spans)이 비어 있으면 실패하고, text(줄 평문)를
    주면 그 안에 없는 span 도 실패한다 (expand_safe 는 줄 텍스트를 넘긴다).
    """
    errors: list[str] = []
    fx = d.fx
    if not isinstance(fx, str) or fx not in TYPESET_FX:
        errors.append(f"알 수 없는 fx: {fx!r}")
    elif fx in EXTRA_ONLY_FX:
        errors.append(f"'{fx}' 는 extras 전용 — 본문 fx 로 쓸 수 없음")
    else:
        perrs = _validate_params(fx, d.params)
        errors += perrs
        if not perrs and fx in SPAN_FX:
            errors += _validate_spans(fx, d.params, text)

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

def _norm_hex(v: Any) -> str:
    s = str(v).strip()
    return s if s.startswith("#") else "#" + s


def _merged(fx: str, params: dict[str, Any]) -> dict[str, Any]:
    """기본값 + 지정값. int/float 는 형 변환까지. None(지정 안 함)은 그대로 None."""
    meta: dict[str, ParamSpec] = TYPESET_FX[fx]["params"]
    out: dict[str, Any] = {}
    for name, pspec in meta.items():
        v = params.get(name)
        if v is None:
            v = pspec.default
        if v is None:
            out[name] = None
            continue
        if pspec.kind == "int":
            v = int(float(v))
        elif pspec.kind == "float":
            v = float(v)
        elif pspec.kind == "color":
            v = _norm_hex(v)
        elif pspec.kind == "point":
            v = (float(v[0]), float(v[1]))
        elif pspec.kind == "spans":
            pairs = [_span_pair(item) for item in v]
            v = [(sp, _norm_hex(col)) for sp, col in (pr for pr in pairs if pr) if sp]
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


def _clampf(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


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


def _find_span(text: str, span: str, near: int = -1) -> tuple[int, int] | None:
    """\\N 을 공백 하나로 본 정규화 문자열에서 span 을 찾아 원문 [start, end) 반환.

    near ≥ 0 이면 같은 문자열이 여러 번 나올 때 그 글자 위치에 가장 가까운 일치를 쓴다
    (반복 가사 '살아서 또 살아왔어' 의 둘째 '살'). −1 이면 첫 일치.

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
    if near >= 0:
        best, k = pos, pos
        while k >= 0:
            if abs(k - near) < abs(best - near):
                best = k
            k = nt.find(ns, k + 1)
        pos = best
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


def _tighten_tail(text: str) -> str:
    """부분색 구간 뒤의 꼬리 — 꼬리 전체가 말줄임이어도 {\\fsp-5} 를 넣는다."""
    if text and _ELLIPSIS_RE.fullmatch(text):
        return "{\\fsp-5}" + text
    return _tighten(text)


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


def _style_color_tag(dark: bool, base: str | None = None) -> str:
    """본문색 복원 태그 — base(지정 채움색) 또는 스타일색(하양 &HFFFFFF& / 검정)."""
    if base:
        return f"\\1c{hex_to_ass_color(base)}"
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


# ---- 공통 모양 (색 + 글로우) -------------------------------------------

def _fill_tag(color: str | None) -> str:
    r"""글자 채움색 태그 (레퍼런스: \c&H5a5858&). 지정이 없으면 빈 문자열."""
    return f"\\c{hex_to_ass_color(color)}" if color else ""


def _glow_tags(p: dict[str, Any], fill: str | None, dark: bool) -> str:
    r"""글로우 = 두꺼운 블러 외곽선. 스타일의 외곽선 알파가 FF 라 \3a&H00& 로 켠다.

    dark → 검은 글로우 (레퍼런스 \3a&H00&\bord30\blur10), color → glow_color
    (없으면 글자색, 그것도 없으면 스타일 색).
    """
    mode = p.get("glow")
    if mode not in ("dark", "color"):
        return ""
    size = int(p.get("glow_size") or 0)
    if size <= 0:
        return ""
    if mode == "dark":
        col = "#000000"
    else:
        col = p.get("glow_color") or fill or ("#000000" if dark else "#FFFFFF")
    return (f"\\3c{hex_to_ass_color(col)}\\3a&H00&\\bord{size}"
            f"\\blur{_num(p.get('glow_blur') or 0)}")


def _look(p: dict[str, Any], line: FxLine, fill_key: str = "color",
          glow: bool = True) -> str:
    """공통 모양 태그 — 첫 오버라이드 블록 끝에 붙인다. 지정이 없으면 빈 문자열."""
    fill = p.get(fill_key)
    return _fill_tag(fill) + (_glow_tags(p, fill, bool(line.dark)) if glow else "")


def _char_events(line: FxLine, inner: str, text: str, p: dict[str, Any],
                 start: int | None = None, end: int | None = None) -> list[FxEvent]:
    """글자 1개 → 이벤트. 글로우가 있으면 아래 레이어에 채움 투명 겹을 먼저 낸다."""
    fill = _fill_tag(p.get("color"))
    glow = _glow_tags(p, p.get("color"), bool(line.dark))
    out: list[FxEvent] = []
    if glow:
        out.append(_event(line, f"{{{inner}{fill}{glow}\\1a&HFF&}}" + text,
                          layer=GLOW_LAYER, start=start, end=end))
    out.append(_event(line, f"{{{inner}{fill}}}" + text, start=start, end=end))
    return out


def _unit(seed: str, k: int) -> float:
    """결정적 의사난수 [-1, 1] — md5(시드|k). hash() 는 실행마다 달라 쓰지 않는다."""
    h = hashlib.md5(f"{seed}|{k}".encode("utf-8")).digest()
    return int.from_bytes(h[:4], "big") / 0xFFFFFFFF * 2.0 - 1.0


def _wobble_unit(seed: str, k: int) -> float:
    """부호는 그대로, 크기는 [_WOBBLE_FLOOR, 1] — 변형이 0 근처로 죽지 않게."""
    u = _unit(seed, k)
    mag = _WOBBLE_FLOOR + (1.0 - _WOBBLE_FLOOR) * abs(u)
    return mag if u >= 0 else -mag


def _char_em(ch: str) -> float:
    """글자 1개의 대략적 진행 폭(em) — 부분 문자열 위치 추정용."""
    if ch.isspace():
        return 0.3
    if ch in ".,·'`!:;…":
        return 0.3
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 0.95
    return 0.55


def _text_em(s: str) -> float:
    return sum(_char_em(ch) for ch in s)


def _pos_tag(line: FxLine, p: dict[str, Any], res: tuple[int, int],
             x: float, y: float, offset: int = 0) -> str:
    r"""dx/dy 가 있으면 \move, 없으면 \pos. offset(ms) 뒤에 시작하는 겹은 그 시각의 자리부터."""
    dx, dy = int(p.get("dx") or 0), int(p.get("dy") or 0)
    if not dx and not dy:
        return f"\\pos({_cx(x, res)},{_cy(y, res)})"
    dur = _line_dur(line)
    f = _clampf(offset / float(dur), 0.0, 1.0)
    return (f"\\move({_cx(x + dx * f, res)},{_cy(y + dy * f, res)},"
            f"{_cx(x + dx, res)},{_cy(y + dy, res)})")


def _scale_tag(line: FxLine, p: dict[str, Any], offset: int = 0,
               sx: float = 100.0, sy: float = 100.0) -> str:
    r"""scale_to ≠ 100 이면 \t(\fscx\fscy) — 기준 배율(sx, sy)에 곱한다. 늦게 시작하는 겹은
    그 시각의 배율에서 출발한다. 100 이면 빈 문자열(기준 배율 태그는 호출자가 이미 냈다)."""
    s = float(p.get("scale_to") or 100.0)
    if abs(s - 100.0) < 0.05:
        return ""
    dur = _line_dur(line)
    f = _clampf(offset / float(dur), 0.0, 1.0)
    k0, k1 = 1.0 + (s / 100.0 - 1.0) * f, s / 100.0
    init = ""
    if offset > 0:
        init = f"\\fscx{_num(round(sx * k0, 1))}\\fscy{_num(round(sy * k0, 1))}"
    return (init + f"\\t(0,{max(1, dur - offset)},"
            f"\\fscx{_num(round(sx * k1, 1))}\\fscy{_num(round(sy * k1, 1))})")


# ---- fx 별 확장 ------------------------------------------------------

def _x_plain(line: FxLine, p: dict[str, Any], res: tuple[int, int]) -> list[FxEvent]:
    x, y = _cx(line.x, res), _cy(line.y, res)
    body = _safe_text(line.text)
    if p["tighten_ellipsis"]:
        body = _tighten(body)
    block = (f"{{\\an5\\pos({x},{y})\\fs{p['fs']}{_fad(p['fade_in'], p['fade_out'])}"
             f"{_look(p, line)}}}")
    return [_event(line, block + body)]


def _plain_fallback(line: FxLine, p: dict[str, Any], res: tuple[int, int]) -> list[FxEvent]:
    """글자가 없을 때 등의 폴백 — 같은 fs·모양의 plain."""
    keep = {k: p[k] for k in ("fs", *LOOK_PARAMS) if p.get(k) is not None}
    fs_spec: ParamSpec = TYPESET_FX["plain"]["params"]["fs"]
    if "fs" in keep:
        keep["fs"] = int(_clampf(keep["fs"], fs_spec.minimum or 1, fs_spec.maximum or 999))
    return _x_plain(line, _merged("plain", keep), res)


def _x_subtitle(line: FxLine, p: dict[str, Any], res: tuple[int, int]) -> list[FxEvent]:
    r"""나레이션 — \pos 없이 기본 하단 중앙. 레퍼런스: {\fs70}겨울날 해질녘,…"""
    fad = _fad(p["fade_in"], p["fade_out"]) if (p["fade_in"] or p["fade_out"]) else ""
    block = f"{{\\an2\\fs{p['fs']}{fad}{_look(p, line)}}}"
    return [_event(line, block + _tighten(_safe_text(line.text)))]


def _x_drift_scale(line: FxLine, p: dict[str, Any], res: tuple[int, int]) -> list[FxEvent]:
    x, y = _cx(line.x, res), _cy(line.y, res)
    x1, y1 = _cx(line.x + p["dx"], res), _cy(line.y + p["dy"], res)
    dur = _line_dur(line)
    s = _num(p["scale_to"])
    # 크기 변화가 없는 줄(scale_to=100)에는 아무 일도 안 하는 \t 를 내지 않는다
    grow = f"\\t(0,{dur},\\fscx{s}\\fscy{s})" if abs(float(p["scale_to"]) - 100.0) >= 0.05 else ""
    block = (
        f"{{\\an5\\move({x},{y},{x1},{y1})\\fs{p['fs']}"
        f"{_fad(p['fade_in'], p['fade_out'])}"
        f"{grow}{_look(p, line)}}}"
    )
    return [_event(line, block + _tighten(_safe_text(line.text)))]


def _x_char_scatter(line: FxLine, p: dict[str, Any], res: tuple[int, int]) -> list[FxEvent]:
    body, ell = _split_ellipsis(_safe_text(line.text))
    rows = _rows_of_chars(body)
    if not rows:
        return _plain_fallback(line, p, res)
    fs = int(p["fs"])
    spread = int(p["spread"])
    rot_max = float(p["rot_max"])
    scale_var = float(p["scale_var"])
    wobble = p["mode"] == "wobble"
    dur = _line_dur(line)
    fad = _fad(p["fade_in"], p["fade_out"])
    # 행 전체 상자(글자 반폭 + 확대 여유 + \move 오프셋)가 프레임에 들도록 먼저 민다.
    max_n = max(len(r) for r in rows)
    half_row = (max_n - 1) * spread / 2.0
    glyph = fs * 0.7
    if wobble:
        mv = min(float(p["move_max"]), _WOBBLE_MOVE_CAP * fs)
        mx_max = my_max = mv
    else:
        mv = 0.0
        mx_max = max(abs(t[5]) for t in _SCATTER_TABLE)
        my_max = max(abs(t[6]) for t in _SCATTER_TABLE)
    ox = _fit_shift(line.x - half_row, line.x + half_row, res[0], glyph + mx_max)
    oy = _fit_shift(line.y, line.y + (len(rows) - 1) * fs * 1.1, res[1], glyph + my_max)
    seed = "".join(ch for row in rows for ch in row)
    out: list[FxEvent] = []
    idx = 0
    total = sum(len(r) for r in rows)
    for r, chars in enumerate(rows):
        n = len(chars)
        row_y = line.y + oy + r * fs * 1.1
        left = line.x + ox - (n - 1) * spread / 2.0
        for i, ch in enumerate(chars):
            if wobble:
                key = f"{seed}|{idx}"
                frz_u, frx_u, fry_u, sx_u, sy_u = (_wobble_unit(key, k) for k in range(5))
                # 이동은 대각 합이 mv 를 넘지 않게 (|dx|,|dy| ≤ mv/√2)
                mx = _unit(key, 5) * mv * 0.7071
                my = _unit(key, 6) * mv * 0.7071
            else:
                frz_u, frx_u, fry_u, sx_u, sy_u, mx, my = \
                    _SCATTER_TABLE[idx % len(_SCATTER_TABLE)]
            cx, cy = left + i * spread, row_y
            x0, y0 = _cx(cx, res), _cy(cy, res)
            x1, y1 = _cx(cx + mx, res), _cy(cy + my, res)
            sx = _num(100.0 + scale_var * sx_u)
            sy = _num(100.0 + scale_var * sy_u)
            frz = _num(rot_max * frz_u)
            frx = _num(rot_max * frx_u)
            fry = _num(rot_max * fry_u)
            inner = (
                f"\\an5\\move({x0},{y0},{x1},{y1})\\fs{fs}{fad}"
                f"\\t(0,{dur},\\fscx{sx}\\fscy{sy}\\frz{frz}\\frx{frx}\\fry{fry})"
            )
            text = ch
            if ell and idx == total - 1:
                text = ch + "{\\fsp-5}" + ell
            out += _char_events(line, inner, text, p)
            idx += 1
    return out


def _x_char_diagonal(line: FxLine, p: dict[str, Any], res: tuple[int, int]) -> list[FxEvent]:
    body, ell = _split_ellipsis(_safe_text(line.text))
    chars = _flat_chars(body)
    if not chars:
        return _plain_fallback(line, p, res)
    fs = int(p["fs"])
    n = len(chars)
    fad = _fad(p["fade_in"], p["fade_out"])
    if p["x1"] > 0 and p["y1"] > 0:
        # 명시 끝점: (x,y) 가 시작점.
        sx, sy = float(line.x), float(line.y)
        ex, ey = float(p["x1"]), float(p["y1"])
    else:
        # 자동: 오른쪽 아래 대각선, (x,y) 를 중심으로. 프레임(여백 제외)보다
        # 길면 간격을 줄인다 — 끝 글자들이 가장자리에 뭉치지 않게.
        margin0 = fs * 0.6
        w, h = (n - 1) * fs * 1.1, (n - 1) * fs * 0.75
        avail_w, avail_h = res[0] - 2 * margin0, res[1] - 2 * margin0
        k = min(1.0, avail_w / w if w > 0 else 1.0, avail_h / h if h > 0 else 1.0)
        w, h = w * max(0.0, k), h * max(0.0, k)
        sx, sy = line.x - w / 2.0, line.y - h / 2.0
        ex, ey = line.x + w / 2.0, line.y + h / 2.0
    if n > 1:
        # 이웃 글자 중심 간격 ≥ 글자 크기: 모자라면 fs 를 _DIAG_MIN_FS 까지 줄이고,
        # 그래도 모자라면 진행 방향으로 끝점을 연장한다 (겹친 글자는 읽을 수 없다).
        dist = math.hypot(ex - sx, ey - sy)
        step = dist / (n - 1)
        if step < fs * _DIAG_GAP:
            fs = max(min(fs, _DIAG_MIN_FS), min(fs, int(step / _DIAG_GAP)))
        need = fs * _DIAG_GAP * (n - 1)
        if dist < need and (p.get("extend", True) or not (p["x1"] > 0 and p["y1"] > 0)):
            if dist > 1e-6:
                ux, uy = (ex - sx) / dist, (ey - sy) / dist
            else:
                ux, uy = 1.1 / math.hypot(1.1, 0.75), 0.75 / math.hypot(1.1, 0.75)
            ex, ey = sx + ux * need, sy + uy * need
    margin = fs * 0.6
    # 대각선 상자 전체를 프레임 안으로 (명시/연장 끝점이 화면 밖이면 같이 민다)
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
        out += _char_events(line, f"\\an5\\pos({x},{y})\\fs{fs}{fad}", text, p)
    return out


def _int_list(v: Any) -> list[float]:
    """'890,670,478' → [890.0, 670.0, 478.0]. 형식이 틀리면 빈 목록 (최대 64개)."""
    if not isinstance(v, str) or not v.strip():
        return []
    out: list[float] = []
    for tok in v.split(",")[:64]:
        try:
            f = float(tok.strip())
        except ValueError:
            return []
        if not math.isfinite(f):
            return []
        out.append(f)
    return out


def _resample(vals: list[float], n: int) -> list[float]:
    """vals 를 n 개로 선형 보간 (n 개면 그대로, 2개 미만이면 빈 목록 — 균등 배치로 폴백)."""
    if n <= 0 or len(vals) < 2:
        return [] if len(vals) != n else list(vals)
    if len(vals) == n:
        return list(vals)
    out: list[float] = []
    for i in range(n):
        t = i * (len(vals) - 1) / float(max(1, n - 1))
        k = min(len(vals) - 2, int(t))
        out.append(vals[k] + (vals[k + 1] - vals[k]) * (t - k))
    return out


def _x_char_stack(line: FxLine, p: dict[str, Any], res: tuple[int, int]) -> list[FxEvent]:
    body, ell = _split_ellipsis(_safe_text(line.text))
    chars = _flat_chars(body)
    if not chars:
        return _plain_fallback(line, p, res)
    fs = int(p["fs"])
    rise = int(p["rise"])
    stagger = int(p["stagger_ms"])
    n = len(chars)
    start, end = int(line.start_ms), int(line.end_ms)
    dur = max(1, end - start)
    # 마지막 글자도 최소 dur/(n+1) 은 보이도록 시차를 클램프.
    latest = start + max(0, dur - max(1, dur // (n + 1)))
    ys = _resample(_int_list(p.get("ys")), n)
    sts = _resample(_int_list(p.get("starts")), n)
    out: list[FxEvent] = []
    for i, ch in enumerate(chars):
        y = _cy(ys[i] if ys else line.y - (rise * i / (n - 1) if n > 1 else 0), res)
        x = _cx(line.x, res)
        fs_i = max(40, fs - 4 * i)
        s_i = min(start + (max(0, int(sts[i])) if sts else i * stagger), latest)
        text = ch
        if ell and i == n - 1:
            text = ch + "{\\fsp-5}" + ell
        out += _char_events(line, f"\\an5\\pos({x},{y})\\fs{fs_i}", text, p,
                            start=s_i, end=end)
    return out


def _x_ghost_trail(line: FxLine, p: dict[str, Any], res: tuple[int, int]) -> list[FxEvent]:
    r"""t0 전에는 정지한 통짜 한 줄, t0~t1 에 겹들이 가로로 벌어지며 흐려진다.

    레퍼런스(힘껏쥐고): \move(1099,345,989,345,1295,1800) + \t(1300,…,\blur5\fscx120\c&H4E4E4E&)
    — 1300ms 까지 완전 정지, 끝 0.3s 만 번진다. 여기서는 t0 에서 이벤트를 나눠
    앞(정지)에는 \move 가 아예 없다. t1 은 줄 끝을 넘어도 된다 (번지는 도중에 컷).
    """
    x, y = _cx(line.x, res), _cy(line.y, res)
    fs = int(p["fs"])
    layers = int(p["layers"])
    spread = int(p["spread"])
    blur = _num(p["blur"])
    scale = _num(p["scale_to"])
    dur = _line_dur(line)
    start, end = int(line.start_ms), int(line.end_ms)
    t0 = int(p["t0"]) if int(p["t0"]) >= 0 else max(0, dur - _GHOST_T0_TAIL)
    t0 = (min(t0, dur) // 10) * 10          # ASS 시각은 1/100초 — 경계를 그 격자에
    t1 = int(p["t1"]) if int(p["t1"]) > t0 else max(dur, t0 + 1)
    span = max(1, t1 - t0)
    body = _tighten(_safe_text(line.text))
    ghosts = layers - 1
    look = _look(p, line)
    out: list[FxEvent] = []
    if t0 > 0:
        static = f"{{\\an5\\pos({x},{y})\\fs{fs}{look}}}"
        out.append(_event(line, static + body, layer=ghosts,
                          end=min(end, start + t0) if t0 < dur else end))
    if t0 >= dur:
        return out
    s0 = start + t0
    fill = _fill_tag(p.get("color"))
    for j in range(ghosts):
        # 잔상이 여럿이면 -spread..+spread 대칭, 하나뿐이면 +spread 로 벗어나게
        # (-spread + spread = 0 이 되어 본문 밑에 숨던 버그 방지).
        off = (-spread + 2 * spread * j / (ghosts - 1)) if ghosts > 1 else spread
        x1 = _cx(x + off, res)
        col = hex_to_ass_color(_GHOST_PALETTE[j % len(_GHOST_PALETTE)])
        block = (
            f"{{\\an5\\move({x},{y},{x1},{y},0,{span})\\fs{fs}\\bord0{fill}"
            f"\\t(0,{span},\\blur{blur}\\fscx{scale}\\c{col})}}"
        )
        out.append(_event(line, block + body, layer=j, start=s0))
    fade_out = min(int(p["fade_out"]), end - s0)
    # \bord0: 스타일의 (투명) 외곽선이 있으면 libass 가 \blur 를 외곽선에만 걸어 본문 글자가
    # 또렷하게 남는다 — 외곽선을 없애 채움이 흐려지게 (글로우 지정이 있으면 look 이 다시 켠다)
    top = (f"{{\\an5\\pos({x},{y})\\fs{fs}\\bord0\\shad0{_fad(0, fade_out)}{look}"
           f"\\t(0,{span},\\blur{blur}\\fscx{scale})}}")
    out.append(_event(line, top + body, layer=ghosts, start=s0))
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


def _ramp_sizes(f0: float, ramp: float, m: int) -> list[int]:
    """몸통 글자 크기 — f0 에서 f0×ramp 까지 선형 증가 (m 글자)."""
    if m <= 1:
        return [max(1, int(round(f0)))]
    return [max(1, int(round(f0 * (1.0 + (ramp - 1.0) * i / (m - 1))))) for i in range(m)]


def _x_vertical_title(line: FxLine, p: dict[str, Any], res: tuple[int, int]) -> list[FxEvent]:
    r"""세로 제목 — 머리(3글자, 같은 크기) + 몸통(아래로 갈수록 커지는 기둥) + ★.

    레퍼런스: 머리 '밤하늘' \fs70 \pos(1025,144), 몸통 '로{\fs75}이…{\fs105}길'
    \pos(1020,700)\clip(…,300)→(…,1012). 원근 연출이라 몸통 글자가 선형으로 커진다.
      - head_pos 가 있으면 머리는 그 자리(원문 제목 머리 곁), 없으면 기둥 바로 위.
      - body_top/body_bottom 이 있으면 몸통 글자 크기 합(×0.9em)이 그 길이에 맞게
        첫 글자 크기를 계산한다 (40~160). 번역이 길어 첫 글자가 52 아래로 내려가면 기둥을
        프레임 아래쪽(여백 24px)까지 늘려 쓰고, 그래도 40 에 못 미치면 원근(ramp)을 1.0 까지
        줄이고, 마지막으로 글자 크기를 28 까지 줄이고 위 끝을 올린다 — 프레임 밖으로 잘리지
        않는다. 없으면 fs 에서 시작해 line.y 중심 배치. head_fs 가 있으면 머리 글자 크기.
      - 기둥은 프레임(0..PlayResY) 밖으로 나가지 않는다: 글자 1개를 1em 으로 본
        보수적 길이로도 아래 끝 ≤ PlayResY 가 되도록 줄이거나 위로 민다.
    """
    chars = _flat_chars(_safe_text(line.text))
    if not chars:
        return _plain_fallback(line, p, res)
    fs = int(p["fs"])
    n = len(chars)
    reveal = int(p["reveal_ms"])
    fade_out = int(p["fade_out"])
    dur = _line_dur(line)
    H = float(res[1])
    adv = _TITLE_ADV
    em_k = 0.5 + 0.5 / adv          # 위 끝 기준, 1em 보수 길이의 아래 끝 = top + len×em_k
    ramp = _clampf(float(p["ramp"]), _TITLE_RAMP_MIN, _TITLE_RAMP_MAX)
    # 레퍼런스 꼴: 머리('밤하늘') 는 블러로 즉시, 몸통('로이어지는언덕길') 은 클립 드러내기.
    head_n = _TITLE_HEAD_N if n >= _TITLE_HEAD_MIN else 0
    m = n - head_n
    unit = sum(1.0 + (ramp - 1.0) * i / (m - 1) for i in range(m)) if m > 1 else 1.0
    fs_min = float(min(_TITLE_FS_MIN, fs))
    head_pos = p.get("head_pos") if head_n else None
    bt_in, bb_in = float(p["body_top"]), float(p["body_bottom"])
    measured = bb_in > 0 and bb_in > bt_in

    def body_len(f: float) -> float:
        return adv * sum(_ramp_sizes(f, ramp, m))

    if measured:
        bt = _clampf(bt_in, 0.0, H)
        bb = _clampf(bb_in, 0.0, H)
        room = max(1.0, (H - _TITLE_BOTTOM_PAD - bt) / em_k)
        span = max(1.0, min(bb - bt, room))
        if span / (adv * unit) < _TITLE_FS_GOOD:
            # 번역이 원문보다 길어 글자가 작아진다 — 기둥을 프레임 아래쪽까지 늘려 쓴다
            span = max(span, min(room, _TITLE_FS_GOOD * adv * unit))
        if span / (adv * unit) < _TITLE_FS_MIN:
            # 그래도 최소 크기에 못 미치면 원근(ramp)을 줄여 길이를 맞춘다
            while ramp > _TITLE_RAMP_MIN and span / (adv * unit) < _TITLE_FS_MIN:
                ramp = max(_TITLE_RAMP_MIN, ramp - 0.05)
                unit = sum(1.0 + (ramp - 1.0) * i / (m - 1) for i in range(m)) if m > 1 else 1.0
        f0 = _clampf(span / (adv * unit),
                     _TITLE_FS_MIN if span / (adv * unit) >= _TITLE_FS_MIN else _TITLE_FS_FLOOR,
                     _TITLE_FS_MAX)
        if bt + body_len(f0) * em_k > H:
            # 마지막 수단: 기둥 위 끝을 올린다 (머리 아래까지)
            bt = max(0.0, H - body_len(f0) * em_k)
        fh = float(p.get("head_fs") or 0) or f0
        if head_pos is not None:
            hx, hy = float(head_pos[0]), _clampf(float(head_pos[1]), 0.0, H)
            # 머리는 프레임 위/몸통 위 끝을 넘지 않는 크기로 (최소 40)
            room = 2.0 * min(hy, max(1.0, bt - hy))
            fh = _clampf(min(fh, room / (head_n * adv)), _TITLE_FS_MIN, _TITLE_FS_MAX)
        elif head_n:
            # 머리 길이 + 간격이 몸통 위 공간(bt)에 들어가는 크기로 (최소 40)
            fh = _clampf(min(fh, bt / (head_n * adv + _TITLE_HEAD_GAP)),
                         _TITLE_FS_MIN, _TITLE_FS_MAX)
            hx, hy = float(line.x), bt - _TITLE_HEAD_GAP * fh - head_n * fh * adv / 2.0
            hy = max(hy, head_n * fh * adv / 2.0)
        else:
            hx, hy = float(line.x), bt
    elif head_pos is not None:
        hx, hy = float(head_pos[0]), _clampf(float(head_pos[1]), 0.0, H)
        fh = _clampf(min(float(fs), 2.0 * hy / (head_n * adv)), fs_min, _TITLE_FS_MAX)
        bt = hy + head_n * fh * adv / 2.0 + _TITLE_HEAD_GAP * fh
        f0 = float(fs)
        if bt + body_len(f0) * em_k > H:
            f0 = _clampf((H - bt) / (em_k * adv * unit), fs_min, _TITLE_FS_MAX)
    else:
        # 자동: 머리+몸통을 이어 붙인 기둥을 line.y 중심에. 프레임보다 길면 줄인다.
        f0 = fh = float(fs)
        star_room = float(_STAR_GAP + _STAR_HOLE) if p["star"] else 0.0
        total_unit = head_n + unit
        if f0 * total_unit + star_room > H:                 # 1em 보수 길이 기준
            f0 = fh = _clampf((H - star_room) / total_unit, fs_min, _TITLE_FS_MAX)
        len_head = head_n * int(round(fh)) * adv
        total = len_head + body_len(f0)
        pad = (total / adv - total) / 2.0                   # 1em 보수 여유
        top = line.y - total / 2.0
        lo, hi = top - pad - star_room, top + total + pad   # 별 자리까지 포함한 상자
        if hi - lo > H or lo < 0:
            top -= lo                                       # 위 끝(별)을 프레임에 맞춤
        elif hi > H:
            top -= hi - H
        hx, hy = float(line.x), top + len_head / 2.0
        bt = top + len_head

    sizes = _ramp_sizes(f0, ramp, m)
    fh_i = max(1, int(round(fh)))
    len_head = head_n * fh_i * adv
    len_body = adv * sum(sizes)
    by = bt + len_body / 2.0
    if by + sum(sizes) / 2.0 > H:            # 최소 크기로도 넘치면 기둥을 위로 민다
        by = max(sum(sizes) / 2.0, H - sum(sizes) / 2.0)
    col_top = (hy - len_head / 2.0) if head_n else (by - len_body / 2.0)

    half_w = max(sizes + [fh_i]) * 0.75      # 가장 큰 글자까지 덮는 클립 반폭
    x = _cx(line.x, res)
    cx0, cx1 = _cx(line.x - half_w, res), _cx(line.x + half_w, res)
    font = _font_tag()
    fad = _fad(0, fade_out)
    fill = _fill_tag(p.get("color"))
    star_cx = hx if head_n else float(line.x)
    star_x = _cx(star_cx, res)
    star_y = max(_STAR_HOLE, _cy(col_top - _STAR_GAP, res))

    out: list[FxEvent] = []
    iclip_reveal = p["reveal"] == "iclip"
    if head_n:
        margin_h = fh_i * 0.35               # 글리프 여백 — 첫/끝 글자가 잘리지 않게
        hxi, hyi = _cx(hx, res), _cy(hy, res)
        hx0, hx1 = _cx(hx - half_w, res), _cx(hx + half_w, res)
        htop = _cy(hy - len_head / 2.0 - margin_h, res)
        hbot = _cy(hy + len_head / 2.0 + margin_h, res)
        hole = ""
        if p["star"]:
            # 별 자리에 \iclip 구멍 — 블러 번짐이 별을 덮지 않게 (레퍼런스 '밤하늘')
            hole = (f"\\iclip({star_x - _STAR_HOLE},{star_y - _STAR_HOLE},"
                    f"{star_x + _STAR_HOLE},{star_y + _STAR_HOLE})")
        if iclip_reveal:
            # 레퍼런스 변형: 머리를 덮은 \iclip 사각형이 별 구멍(없으면 아래 선)으로
            # 줄어들며 드러난다. 몸통은 아래의 \clip 와이프 그대로.
            final = hole or f"\\iclip({hx0},{hbot},{hx1},{hbot})"
            hole = (f"\\iclip({hx0},{htop},{hx1},{hbot})"
                    f"\\t(0,{reveal},{final})")
        head = (
            f"{{\\an5\\pos({hxi},{hyi}){font}\\frz270\\fs{fh_i}"
            f"\\bord0\\blur20\\t(0,600,\\blur0){hole}{fad}{fill}}}"
        )
        out.append(_event(line, head + "".join(chars[:head_n])))
    y = _cy(by, res)
    ctop = _cy(by - len_body / 2.0 - sizes[0] * 0.35, res)
    cbot = _cy(by + len_body / 2.0 + sizes[-1] * 0.35, res)
    if iclip_reveal and not head_n:
        # 머리가 없으면 기둥 자체를 \iclip 으로 — 가리는 사각형이 위에서부터 줄어든다
        clip = (f"\\iclip({cx0},{ctop},{cx1},{cbot})"
                f"\\t(0,{reveal},\\iclip({cx0},{cbot},{cx1},{cbot}))")
    else:
        clip = (f"\\clip({cx0},{ctop},{cx1},{ctop})"
                f"\\t(0,{reveal},\\clip({cx0},{ctop},{cx1},{cbot}))")
    glyphs = chars[head_n] + "".join(
        f"{{\\fs{sizes[i]}}}{chars[head_n + i]}" for i in range(1, m))
    body = (
        f"{{\\an5\\pos({x},{y}){font}\\frz270\\fs{sizes[0]}"
        f"{clip}{fad}{_look(p, line)}}}"
    )
    out.append(_event(line, body + glyphs))
    if p["star"]:
        star = (
            f"{{\\an5{font}\\fs{_STAR_FS}\\bord0\\blur20\\t(0,600,\\blur0)"
            f"\\t(0,{dur},\\frz-720)\\pos({star_x},{star_y})\\org({star_x},{star_y})"
            f"{fad}{fill}}}★"
        )
        out.append(_event(line, star, layer=STAR_LAYER))
    return out


def _locate_spans(text: str, spans: list[tuple[str, str]], near0: int = -1
                  ) -> list[tuple[int, int, str]]:
    """[(start, end, 색)] — 첫 일치(첫 항목은 near0 에 가장 가까운 일치), 위치순, 앞선
    구간과 겹치는 것은 버린다."""
    found: list[tuple[int, int, str]] = []
    for k, (sp, col) in enumerate(spans):
        loc = _find_span(text, _safe_text(sp), near0 if k == 0 else -1)
        if loc is not None:
            found.append((loc[0], loc[1], col))
    found.sort(key=lambda t: (t[0], t[1]))
    out: list[tuple[int, int, str]] = []
    for a, b, col in found:
        if not out or a >= out[-1][1]:
            out.append((a, b, col))
    return out


def _x_partial_color(line: FxLine, p: dict[str, Any], res: tuple[int, int]) -> list[FxEvent]:
    r"""레퍼런스: {\fs110\pos(1480,884)\1c&HC2A954&}살아{\1c&FFFFFF&}왔어{\fsp-5}..."""
    look = _look(p, line, fill_key="base_color")
    lead = (f"{{\\an5{_pos_tag(line, p, res, line.x, line.y)}\\fs{p['fs']}"
            f"{_fad(p['fade_in'], p['fade_out'])}{_scale_tag(line, p)}{look}}}")
    text = _safe_text(line.text)
    spans = _locate_spans(text, _span_list(p, p["color"]), int(p.get("span_start", -1)))
    if not spans:
        return [_event(line, lead + _tighten(text))]
    reveal = int(p["reveal_ms"])
    restore = _style_color_tag(line.dark, p.get("base_color"))
    parts: list[str] = []
    cur = 0
    for a, b, col in spans:
        ass = hex_to_ass_color(_norm_hex(col))
        if reveal > 0:
            open_blk = f"{{\\1c{ass}\\1a&HFF&\\t(0,{reveal},\\1a&H00&)}}"
            close_blk = f"{{{restore}\\1a&H00&}}"
        else:
            open_blk = f"{{\\1c{ass}}}"
            close_blk = f"{{{restore}}}"
        parts.append(text[cur:a] + open_blk + text[a:b] + close_blk)
        cur = b
    return [_event(line, lead + "".join(parts) + _tighten_tail(text[cur:]))]


def _span_center_x(text: str, a: int, b: int, x: float, fs: int) -> tuple[float, float, int, int]:
    """\\an5 가운데 정렬 줄에서 구간 [a,b) 의 (중심 x, 폭 px, 행 번호, 행 수) 추정."""
    rows: list[tuple[int, int]] = []
    i = start = 0
    while i < len(text):
        if text[i] == "\\" and i + 1 < len(text) and text[i + 1] in "Nn":
            rows.append((start, i))
            i += 2
            start = i
        else:
            i += 1
    rows.append((start, len(text)))
    r = next((k for k, (s, e) in enumerate(rows) if s <= a <= e), 0)
    rs, re_ = rows[r]
    seg_end = max(a, min(b, re_))
    row_w = _text_em(text[rs:re_]) * fs
    before_w = _text_em(text[rs:a]) * fs
    span_w = max(fs * 0.5, _text_em(text[a:seg_end]) * fs)
    return x - row_w / 2.0 + before_w + span_w / 2.0, span_w, r, len(rows)


def _bar(width: float, fs: int) -> tuple[int, int, str]:
    """폭 width(px) 를 덮는 ■ 막대 → (개수, 음수 자간 px, \\fscx %).

    ■ 은 전각(≈1em)이고 자간은 \\fscx 와 같이 늘어난다: 폭 = ((n−1)(1−k)+1)·fs·sx.
    """
    step = (1.0 - _BAR_OVERLAP) * fs
    n = int(_clampf(round(width / step), 2, 16))
    sx = width / (((n - 1) * (1.0 - _BAR_OVERLAP) + 1.0) * fs)
    return n, int(round(_BAR_OVERLAP * fs)), _num(_clampf(sx * 100.0, 40.0, 250.0))


def _x_eclipse(line: FxLine, p: dict[str, Any], res: tuple[int, int]) -> list[FxEvent]:
    r"""그림자가 햇볕을 잠식 — 레퍼런스 '봄의 햇볕을...' 의 4개 층.

      (a) {\1c&H2954F1&\bord0\blur15\fsp-35\fscy179\t(6840,…,\1a&HFF&)}♣■●■…  채색 글로우
      (b) {\c&64C2F7&\t(6800,…,\1a&HFF&)}봄의 햇볕{\c&000000&\1a&H00&}을…       본문
      (c) 78.88s {\fad(660,0)\1c&H131313&\bord0\blur40\fscy216}■■■■■■■■■        그림자
      (d) 79.25s {\c&565558&\1a&FF&\t(0,660,\1a&00&)}봄의 햇볕{\1a&FF&}을…      회색 글자
    \t 는 (t1,t2,태그) 꼴로 쓴다. span 밖 글자는 (b) 에서 끝까지 보인다.
    """
    fs = int(p["fs"])
    fad = _fad(p["fade_in"], p["fade_out"])
    base_fill = _fill_tag(p.get("base_color"))
    lead = (f"{{\\an5{_pos_tag(line, p, res, line.x, line.y)}\\fs{fs}{fad}"
            f"{_scale_tag(line, p)}{base_fill}}}")
    text = _safe_text(line.text)
    loc = _find_span(text, _safe_text(str(p["span"])), int(p.get("span_start", -1)))
    if loc is None:
        return [_event(line, lead + _tighten(text))]
    a, b = loc
    before, span, after = text[:a], text[a:b], _tighten_tail(text[b:])
    start, end = int(line.start_ms), int(line.end_ms)
    dur = _line_dur(line)
    cdur = int(p["cover_dur"])
    cover = int(p["cover_ms"])
    if cover < 0:
        cover = dur - cdur - _ECLIPSE_AUTO_TAIL
    cover = (int(_clampf(cover, 0, max(0, dur - 100))) // 10) * 10
    t_end = cover + cdur
    col = hex_to_ass_color(p["color"])
    restore = _style_color_tag(line.dark, p.get("base_color"))

    # 도형 층의 자리: span 의 중심 (여러 행이면 span 이 시작하는 행)
    scx, sw, row, nrows = _span_center_x(text, a, b, float(line.x), fs)
    gy_f = line.y + (row - (nrows - 1) / 2.0) * fs * 1.15
    out: list[FxEvent] = []

    # (a) 채색 글로우 도형 — 줄 전체 시간, cover 에 \1a&HFF& 로 꺼진다
    n, fsp, sx = _bar(sw * 1.08, fs)
    g_alpha = int(p.get("glow_alpha") or 0)
    g_a = f"\\1a{alpha_to_ass(g_alpha)}" if g_alpha > 0 else ""
    glow = (
        f"{{\\an5{_pos_tag(line, p, res, scx, gy_f)}\\fs{fs}\\1c{hex_to_ass_color(p['glow_color'])}"
        f"{g_a}\\bord0\\blur{_ECLIPSE_GLOW_BLUR}\\fsp-{fsp}\\fscx{sx}\\fscy{_ECLIPSE_GLOW_SCY}"
        f"{_scale_tag(line, p, 0, float(sx), float(_ECLIPSE_GLOW_SCY))}"
        f"{fad}\\t({cover},{t_end},\\1a&HFF&)}}"
    )
    out.append(_event(line, glow + "■" * n, layer=DECOR_LAYER))

    # (c) 어두운 블러 막대 — cover − 400 부터 페이드인, 줄 끝까지
    s_shadow = start + max(0, cover - _ECLIPSE_SHADOW_LEAD)
    n2, fsp2, sx2 = _bar(sw * _ECLIPSE_SHADOW_WIDEN, fs)
    off_sh = s_shadow - start
    shadow = (
        f"{{\\an5{_pos_tag(line, p, res, scx, gy_f, off_sh)}\\fs{fs}"
        f"\\1c{hex_to_ass_color(p['shadow_color'])}"
        f"\\bord0\\blur{_ECLIPSE_SHADOW_BLUR}\\fsp-{fsp2}\\fscx{sx2}"
        f"\\fscy{_ECLIPSE_SHADOW_SCY}"
        f"{_scale_tag(line, p, off_sh, float(sx2), float(_ECLIPSE_SHADOW_SCY))}"
        f"{_fad(min(_ECLIPSE_SHADOW_FADE, max(0, end - s_shadow)), 0)}}}"
    )
    out.append(_event(line, shadow + "■" * n2, layer=GLOW_LAYER, start=s_shadow))

    # (b) 본문 — span 은 color, cover 에 span 의 \1a 만 FF 로 (뒤 글자는 \1a&H00& 로 복귀)
    body = (
        before + f"{{\\1c{col}\\t({cover},{t_end},\\1a&HFF&)}}" + span
        + f"{{{restore}\\1a&H00&}}" + after
    )
    out.append(_event(line, lead + body, layer=TEXT_LAYER))

    # (d) 회색 span 글자 — cover 부터 페이드인해 줄 끝까지. 같은 조판을 위해 줄 전체를
    # 넣고 span 밖은 투명하게 둔다.
    s_gray = start + cover
    g_in = max(1, min(cdur, end - s_gray))
    fo = _fad(0, p["fade_out"]) if p["fade_out"] else ""
    gray = (
        f"{{\\an5{_pos_tag(line, p, res, line.x, line.y, cover)}\\fs{fs}{fo}"
        f"{_scale_tag(line, p, cover)}\\1a&HFF&}}" + before
        + f"{{\\1c{hex_to_ass_color(p['gray_color'])}\\t(0,{g_in},\\1a&H00&)}}" + span
        + "{\\1a&HFF&}" + after
    )
    out.append(_event(line, gray, layer=COVER_LAYER, start=s_gray))
    return out


_FLY_MOVE_FRAC = 0.72   # 레퍼런스 '마음': 4900ms 이동 / 6790ms 지속


def _x_fly_rotate(line: FxLine, p: dict[str, Any], res: tuple[int, int]) -> list[FxEvent]:
    """(x-dx, y-dy) → (x, y) 로 날아오며 \\fr 회전. 도착 후 남은 시간은 머문다."""
    x1, y1 = _cx(line.x, res), _cy(line.y, res)
    x0, y0 = _cx(line.x - p["dx"], res), _cy(line.y - p["dy"], res)
    dur = _line_dur(line)
    mv = max(1, int(round(dur * _FLY_MOVE_FRAC)))
    if int(p.get("move_ms", -1) or -1) > 0:          # 화면에서 잰 도착 시각
        mv = max(1, min(dur, int(p["move_ms"])))
    deg = _num(-360.0 * float(p["turns"]))     # turns=2 → \fr-720 (레퍼런스)
    block = (
        f"{{\\an5\\move({x0},{y0},{x1},{y1},0,{mv})\\fs{p['fs']}"
        f"{_fad(p['fade_in'], p['fade_out'])}\\t(0,{mv},\\fr{deg}){_look(p, line)}}}"
    )
    return [_event(line, block + _tighten(_safe_text(line.text)))]


_EXPANDERS: dict[str, Callable[[FxLine, dict[str, Any], tuple[int, int]], list[FxEvent]]] = {
    "plain": _x_plain,
    "subtitle": _x_subtitle,
    "drift_scale": _x_drift_scale,
    "char_scatter": _x_char_scatter,
    "char_diagonal": _x_char_diagonal,
    "char_stack": _x_char_stack,
    "ghost_trail": _x_ghost_trail,
    "shadow_bar": _x_shadow_bar,
    "vertical_title": _x_vertical_title,
    "partial_color": _x_partial_color,
    "eclipse": _x_eclipse,
    "fly_rotate": _x_fly_rotate,
}


# ---- 공개 API ----------------------------------------------------------

def expand_line(
    line: FxLine,
    d: FxDirective,
    play_res: tuple[int, int] = (1920, 1080),
) -> list[FxEvent]:
    """검증 통과한 디렉티브를 이벤트 목록으로 확장 (결정적).

    순서: extras(장식, layer 0) → 본문 fx 이벤트들. 검증은 호출측 책임 —
    미검증 입력은 expand_safe 를 쓴다. extras 는 디렉티브에 적힌 것만 붙는다
    (자동으로 붙는 장식은 없다).
    """
    res = (int(play_res[0]), int(play_res[1]))
    out: list[FxEvent] = []
    for efx, eparams in d.extras:
        out += _EXPANDERS[efx](line, _merged(efx, dict(eparams or {})), res)
    out += _EXPANDERS[d.fx](line, _merged(d.fx, dict(d.params or {})), res)
    return out


def _fallback_directive(d: FxDirective) -> FxDirective:
    """검증 실패 시의 plain — 멀쩡한 fs·모양 파라미터는 살린다 (자리·크기 유지)."""
    keep: dict[str, Any] = {}
    try:
        fx = d.fx
        params = d.params
        if isinstance(fx, str) and fx in TYPESET_FX and isinstance(params, dict):
            own: dict[str, ParamSpec] = TYPESET_FX[fx]["params"]
            plain: dict[str, ParamSpec] = TYPESET_FX["plain"]["params"]
            for key in ("fs", *LOOK_PARAMS):
                src = key
                if key == "color" and own.get("color") is not LOOK_PARAMS["color"]:
                    src = "base_color"          # partial_color/eclipse 의 본문색
                if src not in own or params.get(src) is None:
                    continue
                if not _validate_param("plain", key, params[src], plain[key]):
                    keep[key] = params[src]
    except Exception:  # noqa: BLE001 — 폴백은 어떤 입력에도 실패하지 않아야 한다
        keep = {}
    return FxDirective("plain", keep)


def expand_safe(
    line: FxLine,
    d: FxDirective,
    play_res: tuple[int, int] = (1920, 1080),
) -> tuple[list[FxEvent], list[str]]:
    """(이벤트, 오류). 검증 실패·예외 시 plain 으로 폴백. 절대 던지지 않는다."""
    errors: list[str] = []
    try:
        errors = validate_directive(d, getattr(line, "text", None))
    except Exception as e:  # noqa: BLE001 — 방어적
        errors = [f"검증 중 예외: {e}"]
    if not errors:
        try:
            return expand_line(line, d, play_res), []
        except Exception as e:  # noqa: BLE001
            errors = [f"'{d.fx}' 확장 중 예외: {e} — plain 으로 폴백"]
    try:
        return expand_line(line, _fallback_directive(d), play_res), errors
    except Exception:  # noqa: BLE001
        pass
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
        shared = [k for k, ps in params.items() if LOOK_PARAMS.get(k) is ps]
        for pname, ps in params.items():
            if LOOK_PARAMS.get(pname) is ps:
                continue        # 공통 모양 파라미터는 아래에 한 번만 적는다
            if ps.kind == "choice":
                rng = "/".join(ps.choices)
            elif ps.kind in ("int", "float"):
                lo = _num(ps.minimum) if ps.minimum is not None else "-"
                hi = _num(ps.maximum) if ps.maximum is not None else "-"
                rng = f"{ps.kind} {lo}~{hi}"
            elif ps.kind == "color":
                rng = "#RRGGBB"
            elif ps.kind == "point":
                rng = "[x, y]"
            elif ps.kind == "spans":
                rng = '[["부분 문자열", "#RRGGBB"], ...]'
            else:
                rng = ps.kind
            if ps.default is None:
                default: Any = "없음"
            else:
                default = ps.default if ps.default != "" else '""'
            lines.append(f"    {pname}: {ps.label} [{rng}, 기본 {default}]")
        if shared and len(shared) < len(LOOK_PARAMS):
            lines.append(f"    (공통 모양 중 {', '.join(shared)} 만 지원)")
    lines.append("공통 모양 파라미터 — 모든 본문 fx (화면에서 잰 색·글로우가 있을 때만 지정):")
    for pname, ps in LOOK_PARAMS.items():
        if ps.kind == "choice":
            rng = "/".join(ps.choices)
        elif ps.kind == "color":
            rng = "#RRGGBB"
        else:
            rng = f"{ps.kind} {_num(ps.minimum)}~{_num(ps.maximum)}"
        default = "없음" if ps.default is None else ps.default
        lines.append(f"    {pname}: {ps.label} [{rng}, 기본 {default}]")
    lines.append(
        f"extras 에는 {sorted(EXTRA_ONLY_FX)} 만 쓸 수 있고, 본문 fx 로는 쓸 수 없다. "
        "extras 는 기본으로 아무 데도 붙지 않는다 — 화면에 실제로 그림자가 있을 때만."
    )
    lines.append(
        "회색 글자+검은 글로우 = color #58585A + glow dark. partial_color/eclipse 는 "
        "color 가 '부분 색' 이고 본문색은 base_color. span 은 줄 텍스트 안의 부분 "
        "문자열이어야 하며 비어 있으면 거부된다."
    )
    return "\n".join(lines)


__all__ = [
    "COVER_LAYER",
    "DECOR_LAYER",
    "GLOW_LAYER",
    "STAR_LAYER",
    "TEXT_LAYER",
    "expand_line",
    "expand_safe",
    "fx_catalog_text",
    "validate_directive",
]
