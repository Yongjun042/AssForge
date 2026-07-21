"""LLM 자동 연출 — 자막 전체(또는 선택)를 보고 줄별 효과를 배정한다.

ai/effect_author.py 가 '한 줄 + 자연어 요청'이라면, 이 모듈은 '여러 줄 + 분위기'
를 받아 LLM 이 줄마다 EffectSpec 묶음을 고르게 한다. LLM 은 *무엇을* 만 정하고
(화이트리스트 프리미티브), ASS 태그 생성은 effects.compiler 가 결정적으로 한다.

출력 계약:
    {"lines": [{"index": <입력 순번>, "effects": [{"primitive": ..., "params": {...}}]}]}
- index 는 요청에 표기된 줄 번호. 목록에 없는 줄은 효과 없음.
- slide 의 x/y 는 모델이 알 수 없으므로 생략(-1)하게 하고, 여기서
  core.typeset.effective_position 으로 채워 실제 이동이 되게 한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ai.llm import LLMError, LLMProvider, active_provider
from core.ass.tag_tokenizer import strip_tags
from effects import PRIMITIVES, EffectSpec, validate_spec
from effects.director import DirectedLine, LineInput, _pos_of
from ai.effect_author import _param_line


@dataclass(slots=True)
class DirectorProposal:
    """LLM 연출 제안 — errors 가 비어야 적용 가능."""
    directed: list[DirectedLine] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    provider: str = ""
    model: str = ""
    raw: Any = None

    @property
    def ok(self) -> bool:
        return not self.errors and bool(self.directed)


def _build_system_prompt() -> str:
    lines = [
        "당신은 뮤직비디오 자막의 모션그래픽 연출가입니다.",
        "자막 줄 목록(번호·텍스트·길이)을 보고, 각 줄에 어울리는 효과 조합을 고릅니다.",
        "가사의 분위기 흐름(도입/고조/후렴/마무리)에 맞게 변화를 주되,",
        "과하지 않게 — 한 줄에 효과 1~3개, 인접 줄과 살짝 다르게 리듬을 만드세요.",
        "색상은 #RRGGBB. slide 의 x/y 는 알 수 없으므로 넣지 마세요(자동 계산됨).",
        "",
        "사용 가능한 프리미티브(화이트리스트 — 이 외는 무시됨):",
    ]
    for prim, meta in PRIMITIVES.items():
        lines.append(f"  {prim} — {meta['label']}")
        for name, p in meta["params"].items():
            lines.append(_param_line(name, p))
    lines += [
        "",
        "출력은 반드시 다음 JSON 만, 설명 없이:",
        '  {"lines": [{"index": <번호>, "effects": [{"primitive": "<이름>", "params": {...}}]}]}',
        "효과를 주지 않을 줄은 목록에서 빼도 됩니다.",
    ]
    return "\n".join(lines)


def _build_user_prompt(lines: list[LineInput], mood: str) -> str:
    rows = []
    for i, ln in enumerate(lines):
        plain = strip_tags(ln.text).strip().replace("\n", " ")
        rows.append(f"{i}. [{ln.duration_ms}ms] {plain[:80]}")
    mood_part = f"연출 지시: {mood}\n" if mood.strip() else ""
    return (
        f"{mood_part}자막 줄 목록 ({len(lines)}줄):\n" + "\n".join(rows)
        + "\n\n위 줄들에 대한 효과 배정 JSON 을 만드세요."
    )


def direct_with_llm(
    lines: list[LineInput],
    mood: str = "",
    play_res: tuple[int, int] = (1920, 1080),
    provider: LLMProvider | None = None,
) -> DirectorProposal:
    """LLM 에게 줄별 연출을 받아 DirectedLine 목록으로. 적용은 호출자 몫."""
    provider = provider or active_provider()
    info = provider.info()
    proposal = DirectorProposal(provider=info.name, model=info.model)

    usable = [ln for ln in lines
              if not ln.is_comment and strip_tags(ln.text).strip()]
    if not usable:
        proposal.errors = ["연출할 줄이 없습니다 (주석/빈 줄 제외)"]
        return proposal

    available, why = provider.is_available()
    if not available:
        proposal.errors = [f"LLM 사용 불가({info.label}): {why}"]
        return proposal

    try:
        data = provider.complete_json(
            _build_system_prompt(), _build_user_prompt(usable, mood),
            max_tokens=4096,
        )
    except LLMError as e:
        proposal.errors = [f"LLM 호출 실패: {e}"]
        return proposal
    proposal.raw = data

    entries = data.get("lines") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        proposal.errors = ["LLM 응답에서 lines 배열을 찾지 못함"]
        return proposal

    by_index: dict[int, list[EffectSpec]] = {}
    for item in entries:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("index"))
        except (TypeError, ValueError):
            continue
        if not (0 <= idx < len(usable)):
            proposal.notes.append(f"범위 밖 줄 번호 무시: {idx}")
            continue
        raw_fx = item.get("effects")
        if not isinstance(raw_fx, list):
            continue
        specs: list[EffectSpec] = []
        for fx in raw_fx:
            if not isinstance(fx, dict) or "primitive" not in fx:
                continue
            spec = EffectSpec.from_dict(fx)
            errs = validate_spec(spec)
            if errs:
                proposal.notes.append(f"{idx}번 줄 효과 탈락: " + "; ".join(errs))
                continue
            specs.append(spec)
        if specs:
            by_index[idx] = specs

    if not by_index:
        proposal.errors = ["유효한 효과 배정이 없습니다 (검증 통과 0건)"]
        return proposal

    for idx, specs in sorted(by_index.items()):
        line = usable[idx]
        # slide 좌표 자동 주입 — 모델은 해상도/정렬을 모른다.
        for spec in specs:
            if spec.primitive == "slide":
                p = spec.params
                if int(p.get("x", -1)) < 0 or int(p.get("y", -1)) < 0:
                    x, y = _pos_of(line, play_res)
                    p["x"], p["y"] = x, y
        labels = [PRIMITIVES[s.primitive]["label"] for s in specs]
        proposal.directed.append(DirectedLine(
            event_id=line.event_id, specs=specs, summary=" + ".join(labels)))
    return proposal
