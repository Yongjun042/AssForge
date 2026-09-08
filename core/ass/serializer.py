"""Serializer — export project back to .ass using Shadow Document."""
from __future__ import annotations

from dataclasses import replace

from .shadow_document import ShadowDocument, LineType
from .parser import (
    ParsedStyle, ParsedEvent,
    serialize_style_line, serialize_event_line,
    extract_format_fields,
)


def export_ass(
    shadow: ShadowDocument,
    styles: list[ParsedStyle],
    events: list[ParsedEvent],
    script_info: dict[str, str] | None = None,
) -> str:
    """Export to .ass string using shadow document for round-trip.

    - Unmodified lines: emit original text from shadow
    - Modified styles/events: re-serialize from structured data
    - New styles/events: append after existing ones
    """
    # 구조 방어 — 섀도 문서에 [Events]/[V4+ Styles] Format 줄이 없으면(빈 파일이나
    # 손상 파일을 연 경우) 최소 골격 위에 다시 쓴다. 헤더 없는 Dialogue 나열은
    # 어떤 렌더러도 읽지 못한다 (실측: 저장 결과가 Dialogue 60줄뿐이라 자막이
    # 전혀 보이지 않았다). 기존 줄 참조(shadow_line_idx)는 골격에 없으므로 모두
    # '새 줄' 로 다시 배정한다.
    if (not shadow.get_lines_by_type(LineType.EVENT_FORMAT)
            or not shadow.get_lines_by_type(LineType.STYLE_FORMAT)):
        shadow = ShadowDocument.create_empty()
        styles = [replace(st, shadow_line_idx=-1) for st in styles]
        events = [replace(ev, shadow_line_idx=-1) for ev in events]

    overrides: dict[int, str] = {}
    inserts: dict[int, list[str]] = {}
    deleted_event_idxs: set[int] = set()

    # Get format fields from shadow
    style_format_lines = shadow.get_lines_by_type(LineType.STYLE_FORMAT)
    style_fmt = (
        extract_format_fields(style_format_lines[0].text)
        if style_format_lines else _DEFAULT_STYLE_FORMAT
    )

    event_format_lines = shadow.get_lines_by_type(LineType.EVENT_FORMAT)
    event_fmt = (
        extract_format_fields(event_format_lines[0].text)
        if event_format_lines else _DEFAULT_EVENT_FORMAT
    )

    # Override modified styles
    shadow_style_lines = shadow.get_lines_by_type(LineType.STYLE)
    style_by_shadow = {s.shadow_line_idx: s for s in styles if s.shadow_line_idx >= 0}

    for rl in shadow_style_lines:
        if rl.index in style_by_shadow:
            s = style_by_shadow[rl.index]
            overrides[rl.index] = serialize_style_line(s, style_fmt)

    # New styles (no shadow_line_idx). 섀도에 같은 이름의 Style 줄이 있으면
    # (골격의 Default, 파일에서 온 스타일을 DB 가 다시 내보내는 경우) 중복
    # 삽입 대신 그 줄을 덮어쓴다. 앵커는 마지막 Style 줄, Style 줄이 하나도
    # 없으면 Format 줄 — 예전엔 Style 줄이 없는 문서에서 새 스타일이 통째로
    # 사라졌다.
    shadow_style_by_name = {
        _style_name_of(rl.text): rl.index for rl in shadow_style_lines
    }
    new_style_lines: list[str] = []
    for st in styles:
        if st.shadow_line_idx >= 0:
            continue
        line = serialize_style_line(st, style_fmt)
        dup_idx = shadow_style_by_name.get(st.name)
        if dup_idx is not None and dup_idx not in overrides:
            overrides[dup_idx] = line
        else:
            new_style_lines.append(line)
    if new_style_lines:
        if shadow_style_lines:
            anchor_idx = shadow_style_lines[-1].index
        else:
            anchor_idx = style_format_lines[-1].index
        inserts.setdefault(anchor_idx, []).extend(new_style_lines)

    # Events: `events` 의 순서(= order_index 순)가 파일에 실리는 최종 순서다.
    # 기존 이벤트를 각자의 원래 shadow 줄(slot)에 되쓰면 그리드에서 재정렬한
    # 순서가 저장 시 사라지므로, 살아남은 slot 들을 문서 순서대로 모아
    # events 순서대로 "순차" 배정한다. 순서가 안 바뀐 파일은 이전과 동일하게
    # 자기 자리에 되써져 round-trip 이 유지된다.
    shadow_event_lines = [
        rl for rl in shadow.lines
        if rl.line_type in (LineType.DIALOGUE, LineType.COMMENT) and not rl.deleted
    ]
    valid_slot_idxs = {rl.index for rl in shadow_event_lines}
    existing_events = [
        e for e in events
        if e.shadow_line_idx >= 0 and e.shadow_line_idx in valid_slot_idxs
    ]
    used_slot_idxs = {e.shadow_line_idx for e in existing_events}
    slots = [rl.index for rl in shadow_event_lines if rl.index in used_slot_idxs]

    # 매칭되는 이벤트가 없는 slot 은 삭제된 이벤트 — 이번 export 에서만 제외.
    deleted_event_idxs.update(valid_slot_idxs - used_slot_idxs)

    # 새 이벤트(shadow_line_idx < 0)는 직전 이벤트의 slot 뒤에 끼워 넣어
    # 중간 삽입/재정렬 위치를 유지한다. 첫 이벤트보다 앞이면 Format 줄 뒤.
    if event_format_lines:
        lead_anchor = event_format_lines[-1].index
    else:
        lead_anchor = shadow.lines[-1].index if shadow.lines else -1

    slot_iter = iter(slots)
    anchor = lead_anchor
    for e in events:
        if e.shadow_line_idx >= 0 and e.shadow_line_idx in valid_slot_idxs:
            anchor = next(slot_iter)
            overrides[anchor] = serialize_event_line(e, event_fmt)
        else:
            inserts.setdefault(anchor, []).append(serialize_event_line(e, event_fmt))

    # Override script info if changed; 문서에 없는 키(Video File 등)는 마지막
    # KV 줄 뒤에 추가한다 — 예전엔 새 문서에서 조용히 사라졌다.
    if script_info:
        kv_lines = shadow.get_lines_by_type(LineType.SCRIPT_INFO_KV)
        seen_keys: set[str] = set()
        for rl in kv_lines:
            text = rl.text.strip()
            if ":" in text:
                key = text.partition(":")[0].strip()
                seen_keys.add(key)
                if key in script_info:
                    new_val = script_info[key]
                    expected = f"{key}: {new_val}"
                    if rl.text.strip() != expected:
                        overrides[rl.index] = expected
        missing = [k for k in script_info if k not in seen_keys]
        if missing and kv_lines:
            inserts.setdefault(kv_lines[-1].index, []).extend(
                f"{k}: {script_info[k]}" for k in missing)

    return shadow.export(overrides, inserts, deleted_indices=deleted_event_idxs)


def save_ass_file(
    filepath: str,
    shadow: ShadowDocument,
    styles: list[ParsedStyle],
    events: list[ParsedEvent],
    script_info: dict[str, str] | None = None,
) -> None:
    """Write .ass file preserving original encoding and BOM."""
    content = export_ass(shadow, styles, events, script_info)
    encoding = "utf-8-sig" if shadow.has_bom else shadow.encoding
    with open(filepath, "w", encoding=encoding, newline="") as f:
        f.write(content)


_DEFAULT_STYLE_FORMAT = [
    "Name", "Fontname", "Fontsize", "PrimaryColour", "SecondaryColour",
    "OutlineColour", "BackColour", "Bold", "Italic", "Underline", "StrikeOut",
    "ScaleX", "ScaleY", "Spacing", "Angle", "BorderStyle", "Outline", "Shadow",
    "Alignment", "MarginL", "MarginR", "MarginV", "Encoding",
]

_DEFAULT_EVENT_FORMAT = [
    "Layer", "Start", "End", "Style", "Name",
    "MarginL", "MarginR", "MarginV", "Effect", "Text",
]


def _style_name_of(style_line: str) -> str:
    """'Style: Name,...' 줄에서 이름만."""
    body = style_line.split(":", 1)[1] if ":" in style_line else style_line
    return body.split(",", 1)[0].strip()
