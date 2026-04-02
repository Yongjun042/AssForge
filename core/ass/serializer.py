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

    # Override modified events
    shadow_event_lines = [
        rl for rl in shadow.lines
        if rl.line_type in (LineType.DIALOGUE, LineType.COMMENT) and not rl.deleted
    ]
    event_by_shadow = {e.shadow_line_idx: e for e in events if e.shadow_line_idx >= 0}

    for rl in shadow_event_lines:
        if rl.index in event_by_shadow:
            e = event_by_shadow[rl.index]
            overrides[rl.index] = serialize_event_line(e, event_fmt)

    # New events (no shadow_line_idx)
    new_events = [e for e in events if e.shadow_line_idx < 0]
    if new_events:
        if shadow_event_lines:
            last_event_idx = shadow_event_lines[-1].index
        elif event_format_lines:
            last_event_idx = event_format_lines[-1].index
        else:
            last_event_idx = shadow.lines[-1].index if shadow.lines else -1
        inserts.setdefault(last_event_idx, []).extend(
            serialize_event_line(e, event_fmt) for e in new_events
        )

    # Handle deleted events
    existing_shadow_idxs = {e.shadow_line_idx for e in events if e.shadow_line_idx >= 0}
    for rl in shadow_event_lines:
        if rl.index not in existing_shadow_idxs and rl.index not in overrides:
            shadow.mark_deleted(rl.index)

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

    return shadow.export(overrides, inserts)


def save_ass_file(
    filepath: str,
    shadow: ShadowDocument,
    styles: list[ParsedStyle],
    events: list[ParsedEvent],
    script_info: dict[str, str] | None = None,
) -> None:
    """Write .ass file preserving original encoding and BOM."""
    content = export_ass(shadow, styles, events, script_info)
    encoding = shadow.encoding if shadow.encoding != "utf-8-sig" else "utf-8-sig"
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
