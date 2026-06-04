"""effects — EffectSpec 기반 결정적 효과 컴파일러.

순수 코어 모듈(app/ 에 의존하지 않음). 사용 예:

    from effects import EffectSpec, EffectContext, compile_and_apply
    spec = EffectSpec("emphasis", {"scale": 130})
    new_text, notes = compile_and_apply(spec, ev.text, EffectContext(duration_ms=2000))
    # UI: UpdateEventCommand(db, ev.id, {"text": new_text}) → 단일 undo
"""
from __future__ import annotations

from effects.compiler import (
    EffectValidationError,
    apply_specs,
    apply_to_text,
    compile_and_apply,
    compile_effect,
    plain_text_of,
    validate_spec,
)
from effects.presets import PRESETS, get_preset, preset_names
from effects.spec import (
    PRIMITIVES,
    CompiledEffect,
    EffectContext,
    EffectSpec,
    ParamSpec,
)

__all__ = [
    "EffectSpec",
    "EffectContext",
    "CompiledEffect",
    "ParamSpec",
    "PRIMITIVES",
    "compile_effect",
    "compile_and_apply",
    "apply_specs",
    "apply_to_text",
    "validate_spec",
    "plain_text_of",
    "EffectValidationError",
    "PRESETS",
    "get_preset",
    "preset_names",
]
