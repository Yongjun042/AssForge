"""Serializer — export project back to .ass using Shadow Document."""
from __future__ import annotations

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

    # New styles (no shadow_line_idx)
    new_styles = [s for s in styles if s.shadow_line_idx < 0]
    if new_styles and shadow_style_lines:
        last_style_idx = shadow_style_lines[-1].index
        inserts[last_style_idx] = [serialize_style_line(s, style_fmt) for s in new_styles]

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

    # Override script info if changed
    if script_info:
        for rl in shadow.get_lines_by_type(LineType.SCRIPT_INFO_KV):
            text = rl.text.strip()
            if ":" in text:
                key = text.partition(":")[0].strip()
                if key in script_info:
                    new_val = script_info[key]
                    expected = f"{key}: {new_val}"
                    if rl.text.strip() != expected:
                        overrides[rl.index] = expected

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
