"""EffectSpec → ASS 태그 결정적 컴파일러.

`compile_effect(spec, ctx)` 는 검증된 스펙을 받아 CompiledEffect 를 돌려준다.
`apply_to_text(text, compiled)` 는 그 결과를 기존 줄 텍스트에 합쳐 새 텍스트를 만든다.
편집 적용은 UI 에서 UpdateEventCommand(db, id, {"text": new_text}) 한 번으로 = 단일 undo.

검증 실패 시 EffectValidationError 를 던진다(화이트리스트 + 범위). 컴파일러는
검증을 통과한 값만 문자열에 끼워 넣으므로 임의 태그 주입이 불가능하다.
"""
from __future__ import annotations

import re
from typing import Any, Callable

from core.ass.tag_tokenizer import (
    OverrideBlock,
    hex_to_ass_color,
    tokenize,
)
from effects.spec import (
    PRIMITIVES,
    CompiledEffect,
    EffectContext,
    EffectSpec,
    ParamSpec,
)

_HEX_RE = re.compile(r"^#?[0-9a-fA-F]{6}$")


class EffectValidationError(ValueError):
    """효과 스펙이 화이트리스트/범위 검증을 통과하지 못함."""


# ---- 검증 -------------------------------------------------------------

def validate_spec(spec: EffectSpec) -> list[str]:
    """오류 메시지 리스트를 돌려준다(빈 리스트 = 통과). 던지지 않음."""
    errors: list[str] = []
    meta = PRIMITIVES.get(spec.primitive)
    if meta is None:
        return [f"알 수 없는 효과: '{spec.primitive}'"]
    params_meta: dict[str, ParamSpec] = meta["params"]
    for key in spec.params:
        if key not in params_meta:
            errors.append(f"'{spec.primitive}' 에 없는 파라미터: '{key}'")
    for name, pspec in params_meta.items():
        if name not in spec.params:
            continue  # 누락은 with_defaults 가 채움
        errors += _validate_param(spec.primitive, name, spec.params[name], pspec)
    return errors


def _validate_param(prim: str, name: str, value: Any, pspec: ParamSpec) -> list[str]:
    if pspec.kind == "color":
        if not (isinstance(value, str) and _HEX_RE.match(value.strip())):
            return [f"{prim}.{name}: 색은 '#RRGGBB' 형식이어야 함 (현재 {value!r})"]
        return []
    if pspec.kind == "choice":
        if value not in pspec.choices:
            return [f"{prim}.{name}: {pspec.choices} 중 하나여야 함 (현재 {value!r})"]
        return []
    if pspec.kind == "bool":
        if not isinstance(value, bool):
            return [f"{prim}.{name}: 불리언이어야 함 (현재 {value!r})"]
        return []
    # int / float
    try:
        num = float(value)
    except (TypeError, ValueError):
        return [f"{prim}.{name}: 숫자여야 함 (현재 {value!r})"]
    if pspec.kind == "int" and float(value) != int(num):
        return [f"{prim}.{name}: 정수여야 함 (현재 {value!r})"]
    if pspec.minimum is not None and num < pspec.minimum:
        return [f"{prim}.{name}: {pspec.minimum} 이상이어야 함 (현재 {num:g})"]
    if pspec.maximum is not None and num > pspec.maximum:
        return [f"{prim}.{name}: {pspec.maximum} 이하여야 함 (현재 {num:g})"]
    return []


# ---- 숫자 포맷 --------------------------------------------------------

def _num(x: Any) -> str:
    """정수값이면 정수로, 아니면 소수 둘째자리까지(불필요한 0 제거)."""
    f = float(x)
    if f == int(f):
        return str(int(f))
    return f"{f:.2f}".rstrip("0").rstrip(".")


# ---- 프리미티브별 컴파일 ---------------------------------------------

def _c_fade(p: dict[str, Any], ctx: EffectContext) -> CompiledEffect:
    fi, fo = int(p["fade_in_ms"]), int(p["fade_out_ms"])
    return CompiledEffect(lead_block=f"{{\\fad({fi},{fo})}}")


def _c_emphasis(p: dict[str, Any], ctx: EffectContext) -> CompiledEffect:
    s, a = _num(p["scale"]), int(p["attack_ms"])
    block = (
        f"{{\\fscx100\\fscy100"
        f"\\t(0,{a},\\fscx{s}\\fscy{s})"
        f"\\t({a},{a * 2},\\fscx100\\fscy100)}}"
    )
    return CompiledEffect(lead_block=block)


def _c_glow(p: dict[str, Any], ctx: EffectContext) -> CompiledEffect:
    color = hex_to_ass_color(p["color"])
    block = f"{{\\bord{_num(p['bord'])}\\blur{_num(p['blur'])}\\3c{color}\\3a&H00&}}"
    return CompiledEffect(lead_block=block)


def _osc(tag: str, amp: float, cycles: int, duration: int, center: float = 0.0) -> str:
    """center±amp 로 진동하다 마지막에 center 로 복귀하는 \\t 시퀀스."""
    seg = max(1, duration // cycles)
    parts = [f"\\{tag}{_num(center + amp)}"]
    sign = -1.0
    for k in range(cycles):
        t0, t1 = k * seg, (k + 1) * seg
        val = center if k == cycles - 1 else center + amp * sign
        parts.append(f"\\t({t0},{t1},\\{tag}{_num(val)})")
        sign = -sign
    return "".join(parts)


def _c_shake(p: dict[str, Any], ctx: EffectContext) -> CompiledEffect:
    block = "{" + _osc("frz", float(p["amplitude"]), int(p["cycles"]),
                        int(p["duration_ms"])) + "}"
    return CompiledEffect(lead_block=block)


def _c_bounce(p: dict[str, Any], ctx: EffectContext) -> CompiledEffect:
    # fscy 100 을 중심으로 진동 — 위로 늘어났다 정착하는 바운스 느낌.
    block = "{" + _osc("fscy", float(p["amplitude"]), int(p["cycles"]),
                       int(p["duration_ms"]), center=100.0) + "}"
    return CompiledEffect(lead_block=block)


def _c_karaoke_fill(p: dict[str, Any], ctx: EffectContext) -> CompiledEffect:
    dur = int(p["duration_ms"]) or ctx.duration_ms
    c0, c1 = hex_to_ass_color(p["from_color"]), hex_to_ass_color(p["to_color"])
    notes: list[str] = []
    if dur <= 0:
        dur = 1000
        notes.append("줄 길이를 알 수 없어 색 스윕을 1000ms 로 가정")
    block = f"{{\\1c{c0}\\t(0,{dur},\\1c{c1})}}"
    return CompiledEffect(lead_block=block, notes=notes)


def _c_slide(p: dict[str, Any], ctx: EffectContext) -> CompiledEffect:
    x, y = int(p["x"]), int(p["y"])
    dur = int(p["duration_ms"])
    dist = int(p["distance"])
    mode = p["mode"]
    if x < 0 or y < 0:
        # 정착 좌표 없음 → 페이드로 강등.
        fade = f"{{\\fad({dur},0)}}" if mode == "in" else f"{{\\fad(0,{dur})}}"
        return CompiledEffect(
            lead_block=fade,
            notes=["정착 좌표(x,y) 가 없어 슬라이드를 페이드로 대체"],
        )
    dx, dy = 0, 0
    direction = p["direction"]
    if direction == "left":
        dx = -dist
    elif direction == "right":
        dx = dist
    elif direction == "up":
        dy = -dist
    elif direction == "down":
        dy = dist
    off_x, off_y = x + dx, y + dy
    if mode == "in":
        move = f"\\move({off_x},{off_y},{x},{y},0,{dur})\\fad({dur},0)"
    else:
        move = f"\\move({x},{y},{off_x},{off_y},0,{dur})\\fad(0,{dur})"
    return CompiledEffect(lead_block="{" + move + "}")


def _c_typewriter(p: dict[str, Any], ctx: EffectContext) -> CompiledEffect:
    total = int(p["total_ms"]) or ctx.duration_ms
    notes: list[str] = []
    if total <= 0:
        total = 1500
        notes.append("줄 길이를 알 수 없어 타자기를 1500ms 로 가정")
    chars = list(ctx.plain_text)
    if not chars:
        return CompiledEffect(new_text="", notes=["평문이 비어 타자기 적용 안 함"])
    n = len(chars)
    out: list[str] = []
    for i, ch in enumerate(chars):
        ti = round(i / n * total)
        safe = ch.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")
        out.append(f"{{\\alpha&HFF&\\t({ti},{ti},\\alpha&H00&)}}{safe}")
    if ctx.plain_text:
        notes.append("타자기는 평문만 보존 — 기존 인라인 태그는 제거됨")
    return CompiledEffect(new_text="".join(out), notes=notes)


def _c_fade_complex(p: dict[str, Any], ctx: EffectContext) -> CompiledEffect:
    a1, a2, a3 = int(p["start_alpha"]), int(p["mid_alpha"]), int(p["end_alpha"])
    fin, fout = int(p["fade_in_ms"]), int(p["fade_out_ms"])
    notes: list[str] = []
    dur = ctx.duration_ms
    if dur <= 0:
        dur = 2000
        notes.append("줄 길이를 알 수 없어 복합 페이드를 2000ms 로 가정")
    # 시간은 단조 증가해야 한다: t1 <= t2 <= t3 <= t4.
    t1 = 0
    t2 = min(fin, dur)
    t4 = dur
    t3 = max(t2, dur - fout)
    block = f"{{\\fade({a1},{a2},{a3},{t1},{t2},{t3},{t4})}}"
    return CompiledEffect(lead_block=block, notes=notes)


def _c_perspective(p: dict[str, Any], ctx: EffectContext) -> CompiledEffect:
    parts: list[str] = []
    for tag in ("frx", "fry", "frz"):
        v = float(p[tag])
        if v != 0.0:
            parts.append(f"\\{tag}{_num(v)}")
    if not parts:
        return CompiledEffect(notes=["회전값이 모두 0 — 변화 없음"])
    return CompiledEffect(lead_block="{" + "".join(parts) + "}")


def _c_outline_only(p: dict[str, Any], ctx: EffectContext) -> CompiledEffect:
    # 채움(primary) 알파를 완전 투명으로 → 외곽선+그림자만 보인다.
    return CompiledEffect(lead_block="{\\1a&HFF&}")


def _c_spin(p: dict[str, Any], ctx: EffectContext) -> CompiledEffect:
    ang = float(p["angle"])
    dur = int(p["duration_ms"])
    if ang == 0.0:
        return CompiledEffect(notes=["시작 각도가 0 — 스핀 없음"])
    parts = [f"\\frz{_num(ang)}", f"\\t(0,{dur},\\frz0)"]
    if p["fade"]:
        parts.append(f"\\fad({dur},0)")
    return CompiledEffect(lead_block="{" + "".join(parts) + "}")


_COMPILERS: dict[str, Callable[[dict[str, Any], EffectContext], CompiledEffect]] = {
    "fade": _c_fade,
    "emphasis": _c_emphasis,
    "glow": _c_glow,
    "shake": _c_shake,
    "bounce": _c_bounce,
    "slide": _c_slide,
    "karaoke_fill": _c_karaoke_fill,
    "typewriter": _c_typewriter,
    "fade_complex": _c_fade_complex,
    "perspective": _c_perspective,
    "outline_only": _c_outline_only,
    "spin": _c_spin,
}


# ---- 공개 API ---------------------------------------------------------

def compile_effect(spec: EffectSpec, ctx: EffectContext | None = None) -> CompiledEffect:
    """검증 → 기본값 채움 → 프리미티브 컴파일. 검증 실패 시 예외."""
    errors = validate_spec(spec)
    if errors:
        raise EffectValidationError("; ".join(errors))
    ctx = ctx or EffectContext()
    full = spec.with_defaults()
    return _COMPILERS[full.primitive](full.params, ctx)


def apply_to_text(text: str, compiled: CompiledEffect) -> str:
    """컴파일 결과를 기존 줄 텍스트에 합쳐 새 텍스트를 만든다.

    new_text 가 있으면(타자기 등) 평문을 전부 치환한다. 아니면 lead_block 을
    줄 맨 앞에 덧댄다 — 인접 '{a}{b}' 블록은 libass 에서 '{ab}' 와 동일하게
    렌더되고 round-trip 도 보존된다.
    """
    if compiled.new_text is not None:
        return compiled.new_text
    if not compiled.lead_block:
        return text
    return compiled.lead_block + text


def compile_and_apply(
    spec: EffectSpec,
    text: str,
    ctx: EffectContext | None = None,
) -> tuple[str, list[str]]:
    """편의 함수: (새 텍스트, 경고노트). UI/LLM 적용 경로의 단일 진입점."""
    compiled = compile_effect(spec, ctx)
    return apply_to_text(text, compiled), compiled.notes


def apply_specs(
    text: str,
    specs: list[EffectSpec],
    ctx: EffectContext | None = None,
) -> tuple[str, list[str], list[str]]:
    """여러 효과를 순서대로 합성. (새 텍스트, 노트, 오류) — 예외를 던지지 않는다.

    오류 난 스펙은 건너뛰고 errors 에 모은다. LLM 이 만든 스펙 묶음을 안전하게
    적용하기 위한 경로. 검증 실패가 하나라도 있으면 호출측이 적용을 보류할 수 있다.
    """
    notes: list[str] = []
    errors: list[str] = []
    cur = text
    for spec in specs:
        errs = validate_spec(spec)
        if errs:
            errors += errs
            continue
        compiled = compile_effect(spec, ctx)
        cur = apply_to_text(cur, compiled)
        notes += compiled.notes
    return cur, notes, errors


def plain_text_of(text: str) -> str:
    """줄 텍스트에서 보이는 평문만 — 타자기 등 컨텍스트 구성용."""
    return "".join(
        seg.text for seg in tokenize(text)
        if not isinstance(seg, OverrideBlock)
    )
