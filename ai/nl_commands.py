"""자연어 명령 → 구조화된 편집 연산(EditOp). LLM 은 의도만, 적용은 app/ 가.

레이어링: ai/ 는 core/ 에만 의존(app/ 의 Command 클래스에 의존하지 않음). 이 모듈은
선택된 줄들과 사용자의 한국어/영어 지시를 받아, 화이트리스트로 제한된 편집 연산
리스트를 돌려준다. app/ 는 각 EditOp 를 기존 Command(ShiftTimes/UpdateEvent/
ToggleComment) 로 변환해 단일 배치로 실행한다(= 하나의 undo). AI 결과는 항상
사용자 Accept 후에만 적용된다(suggestion-only 원칙).

LLM 출력 계약:
    {"ops": [{"action": "...", "event_id": "...|null", ...}], "summary": "..."}
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ai.llm import LLMError, LLMProvider, active_provider

# set_field 로 바꿀 수 있는 필드와 기대 타입 — 구조 필드(id/track_id/order 등)는 제외.
_FIELD_TYPES: dict[str, type] = {
    "start_ms": int,
    "end_ms": int,
    "layer": int,
    "margin_l": int,
    "margin_r": int,
    "margin_v": int,
    "style_id": str,
    "speaker": str,
}

_ACTIONS = frozenset({"shift_time", "set_field", "replace_text", "toggle_comment"})


@dataclass(slots=True)
class EditOp:
    """단일 편집 연산. event_id=None 이면 제공된 모든 줄에 적용."""
    action: str
    event_id: str | None = None
    field_name: str = ""
    value: Any = None
    find: str = ""
    replace: str = ""
    new_text: str | None = None

    def describe(self) -> str:
        scope = f"줄 {self.event_id}" if self.event_id else "선택 전체"
        if self.action == "shift_time":
            return f"{scope}: 시간 {int(self.value):+d}ms"
        if self.action == "set_field":
            return f"{scope}: {self.field_name} → {self.value!r}"
        if self.action == "replace_text":
            if self.new_text is not None:
                return f"{scope}: 텍스트 교체"
            return f"{scope}: '{self.find}' → '{self.replace}'"
        if self.action == "toggle_comment":
            return f"{scope}: 주석 전환"
        return f"{scope}: {self.action}"


@dataclass(slots=True)
class CommandPlan:
    """해석 결과. errors 가 비어야 적용 가능."""
    ops: list[EditOp] = field(default_factory=list)
    summary: str = ""
    errors: list[str] = field(default_factory=list)
    provider: str = ""
    model: str = ""
    raw: Any = None

    @property
    def ok(self) -> bool:
        return not self.errors and bool(self.ops)


def build_system_prompt() -> str:
    fields = ", ".join(f"{k}({v.__name__})" for k, v in _FIELD_TYPES.items())
    return "\n".join([
        "당신은 자막 편집 명령 해석기입니다. 사용자의 자연어 지시를 보고,",
        "선택된 줄들에 적용할 편집 연산 목록(JSON)을 만듭니다.",
        "",
        "가능한 action:",
        "  shift_time   — 시작/끝을 함께 이동. value=정수 ms(+뒤로/-앞으로)",
        "  set_field    — 한 필드를 설정. field 와 value 필요",
        f"     설정 가능한 field: {fields}",
        "  replace_text — 텍스트 치환. find/replace 또는 new_text 로 전체 교체",
        "  toggle_comment — 주석/해제 전환",
        "",
        "각 연산의 event_id 는 적용 대상 줄의 id 입니다. null 이면 선택된 모든 줄.",
        "시간 단위는 밀리초(ms). '0.5초 뒤로' = value 500.",
        "",
        "출력은 설명 없이 다음 JSON 만:",
        '  {"ops": [{"action": "...", "event_id": "...|null", "field": "...",',
        '           "value": ..., "find": "...", "replace": "...", "new_text": null}],',
        '   "summary": "<무엇을 할지 한 줄 한국어 요약>"}',
        "맞는 연산이 없으면 ops 를 빈 배열로 두세요.",
    ])


def _event_brief(events: list[Any]) -> str:
    """LLM 에 줄 컨텍스트 제공 — id/시간/텍스트."""
    out: list[str] = []
    for ev in events:
        if isinstance(ev, dict):
            eid, s, e, t = ev.get("id"), ev.get("start_ms"), ev.get("end_ms"), ev.get("text")
        else:
            eid, s, e, t = ev.id, ev.start_ms, ev.end_ms, ev.text
        out.append(f"  id={eid} [{s}~{e}ms] {t!r}")
    return "\n".join(out)


def _coerce_value(field_name: str, value: Any) -> tuple[Any, str | None]:
    expected = _FIELD_TYPES[field_name]
    if expected is int:
        try:
            return int(value), None
        except (TypeError, ValueError):
            return None, f"{field_name}: 정수여야 함 (현재 {value!r})"
    return str(value), None


def _validate_op(data: dict[str, Any], valid_ids: set[str]) -> tuple[EditOp | None, list[str]]:
    action = data.get("action")
    if action not in _ACTIONS:
        return None, [f"알 수 없는 action: {action!r}"]
    eid = data.get("event_id")
    if eid is not None:
        eid = str(eid)
        if eid not in valid_ids:
            return None, [f"존재하지 않는 event_id: {eid!r}"]
    errors: list[str] = []
    op = EditOp(action=action, event_id=eid)

    if action == "shift_time":
        try:
            op.value = int(data.get("value"))
        except (TypeError, ValueError):
            errors.append(f"shift_time.value: 정수 ms 여야 함 (현재 {data.get('value')!r})")
    elif action == "set_field":
        fname = data.get("field")
        if fname not in _FIELD_TYPES:
            errors.append(f"set_field.field: 허용되지 않은 필드 {fname!r}")
        else:
            op.field_name = fname
            coerced, err = _coerce_value(fname, data.get("value"))
            if err:
                errors.append(err)
            else:
                op.value = coerced
    elif action == "replace_text":
        if data.get("new_text") is not None:
            op.new_text = str(data["new_text"])
        elif data.get("find"):
            op.find = str(data["find"])
            op.replace = str(data.get("replace", ""))
        else:
            errors.append("replace_text: find 또는 new_text 가 필요")
    # toggle_comment: 추가 인자 없음

    if errors:
        return None, errors
    return op, []


def interpret_command(
    prompt: str,
    events: list[Any],
    provider: LLMProvider | None = None,
) -> CommandPlan:
    """자연어 prompt 를 선택된 events 에 대한 EditOp 목록으로 해석한다(적용 안 함)."""
    provider = provider or active_provider()
    info = provider.info()
    plan = CommandPlan(provider=info.name, model=info.model)

    available, why = provider.is_available()
    if not available:
        plan.errors = [f"LLM 사용 불가({info.label}): {why}"]
        return plan
    if not events:
        plan.errors = ["선택된 줄이 없음"]
        return plan

    valid_ids = {
        str(ev["id"] if isinstance(ev, dict) else ev.id) for ev in events
    }
    system = build_system_prompt()
    user = (
        f"선택된 줄:\n{_event_brief(events)}\n\n"
        f"지시: {prompt}\n"
        "위 줄들에 적용할 편집 연산 JSON 을 만드세요."
    )
    try:
        data = provider.complete_json(system, user)
    except LLMError as e:
        plan.errors = [f"LLM 호출 실패: {e}"]
        return plan

    plan.raw = data
    if not isinstance(data, dict):
        plan.errors = ["LLM 응답 형식 오류(객체 아님)"]
        return plan
    raw_ops = data.get("ops", [])
    if not isinstance(raw_ops, list):
        plan.errors = ["ops 가 배열이 아님"]
        return plan

    plan.summary = str(data.get("summary", ""))
    for item in raw_ops:
        if not isinstance(item, dict):
            plan.errors.append(f"연산 형식 오류: {item!r}")
            continue
        op, errs = _validate_op(item, valid_ids)
        if errs:
            plan.errors += errs
        elif op is not None:
            plan.ops.append(op)

    if not plan.ops and not plan.errors:
        plan.errors = ["해석된 편집 연산이 없음"]
    if not plan.summary and plan.ops:
        plan.summary = "; ".join(op.describe() for op in plan.ops)
    return plan
