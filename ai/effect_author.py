"""자연어 → 효과 생성 브리지. LLM 이 EffectSpec(JSON)만 내고, 결정적 컴파일러가 ASS 로.

레이어링: ai/ 는 core/ 와 effects/(둘 다 순수)에만 의존하고 app/ 에는 의존하지 않는다.
이 모듈은 LLM 호출 → JSON 파싱 → 화이트리스트 검증 → 컴파일 → 미리보기 diff 까지만
담당한다. 실제 적용(UpdateEventCommand)과 Accept/Reject UI 는 app/ 가 맡는다.

LLM 출력 계약:
    {"effects": [{"primitive": "<이름>", "params": {...}}, ...]}
프리미티브와 파라미터는 effects.PRIMITIVES 화이트리스트로 제한된다. 모델이 임의
태그를 내도 컴파일 경로에 닿지 않는다(검증에서 탈락).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ai.llm import LLMError, LLMProvider, active_provider
from effects import (
    PRESETS,
    PRIMITIVES,
    EffectContext,
    EffectSpec,
    ParamSpec,
    apply_specs,
)


@dataclass(slots=True)
class EffectProposal:
    """LLM 효과 제안 1건. errors 가 비어야 적용 가능."""
    specs: list[EffectSpec] = field(default_factory=list)
    preview_old: str = ""
    preview_new: str = ""
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    provider: str = ""
    model: str = ""
    raw: Any = None

    @property
    def ok(self) -> bool:
        return not self.errors and bool(self.specs)


def _param_line(name: str, p: ParamSpec) -> str:
    rng = ""
    if p.kind in ("int", "float") and p.minimum is not None:
        rng = f" [{p.minimum:g}~{p.maximum:g}]"
    elif p.kind == "choice":
        rng = f" {{{'|'.join(p.choices)}}}"
    elif p.kind == "color":
        rng = " (#RRGGBB)"
    return f"      - {name}: {p.kind}{rng} 기본={p.default!r}"


def build_system_prompt() -> str:
    """PRIMITIVES 를 introspect 해 시스템 프롬프트를 생성 — 스키마와 항상 동기화."""
    lines: list[str] = [
        "당신은 ASS 자막 효과 생성기입니다. 사용자의 자연어 요청을 보고,",
        "아래 화이트리스트에 있는 프리미티브만 사용해 효과 사양(JSON)을 만듭니다.",
        "색상은 반드시 #RRGGBB 형식입니다. 범위를 벗어나는 값은 쓰지 마세요.",
        "",
        "사용 가능한 프리미티브:",
    ]
    for prim, meta in PRIMITIVES.items():
        lines.append(f"  {prim} — {meta['label']}")
        for name, p in meta["params"].items():
            lines.append(_param_line(name, p))
    lines += [
        "",
        "프리셋 예시(참고용, 그대로 쓰거나 변형 가능):",
    ]
    for name, (label, spec) in list(PRESETS.items())[:6]:
        lines.append(f"  {label}: {spec.to_dict()}")
    lines += [
        "",
        "출력은 반드시 다음 JSON 형식만, 설명 없이:",
        '  {"effects": [{"primitive": "<이름>", "params": {<키:값>}}]}',
        "여러 효과를 겹치려면 effects 배열에 순서대로 넣으세요.",
        "요청에 맞는 효과가 없으면 effects 를 빈 배열로 두세요.",
    ]
    return "\n".join(lines)


def _coerce_specs(data: Any) -> tuple[list[EffectSpec], list[str]]:
    """LLM JSON 을 EffectSpec 리스트로. 관용적으로 여러 형태를 받아준다."""
    errors: list[str] = []
    raw_list: list[Any]
    if isinstance(data, dict) and "effects" in data:
        raw_list = data["effects"] if isinstance(data["effects"], list) else [data["effects"]]
    elif isinstance(data, list):
        raw_list = data
    elif isinstance(data, dict) and "primitive" in data:
        raw_list = [data]
    else:
        return [], ["LLM 응답에서 effects 를 찾지 못함"]
    specs: list[EffectSpec] = []
    for item in raw_list:
        if not isinstance(item, dict) or "primitive" not in item:
            errors.append(f"효과 항목 형식 오류: {item!r}")
            continue
        specs.append(EffectSpec.from_dict(item))
    return specs, errors


def author_effects(
    prompt: str,
    text: str,
    ctx: EffectContext | None = None,
    provider: LLMProvider | None = None,
) -> EffectProposal:
    """자연어 prompt 로 text 에 적용할 효과를 제안한다. 적용은 하지 않음(미리보기만)."""
    ctx = ctx or EffectContext(plain_text=text)
    provider = provider or active_provider()
    info = provider.info()
    proposal = EffectProposal(
        preview_old=text, preview_new=text,
        provider=info.name, model=info.model,
    )

    available, why = provider.is_available()
    if not available:
        proposal.errors = [f"LLM 사용 불가({info.label}): {why}"]
        return proposal

    system = build_system_prompt()
    user = (
        f"줄 텍스트: {text!r}\n"
        f"줄 길이: {ctx.duration_ms}ms\n"
        f"요청: {prompt}\n"
        "위 요청에 맞는 효과 JSON 을 만드세요."
    )
    try:
        data = provider.complete_json(system, user)
    except LLMError as e:
        proposal.errors = [f"LLM 호출 실패: {e}"]
        return proposal

    proposal.raw = data
    specs, parse_errors = _coerce_specs(data)
    proposal.specs = specs
    if parse_errors:
        proposal.errors += parse_errors
    if not specs:
        if not proposal.errors:
            proposal.errors = ["요청에 맞는 효과가 생성되지 않음"]
        return proposal

    new_text, notes, errors = apply_specs(text, specs, ctx)
    proposal.notes = notes
    if errors:
        proposal.errors += errors
        return proposal  # 검증 실패 시 미리보기는 원본 유지
    proposal.preview_new = new_text
    return proposal
