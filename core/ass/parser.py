"""Parser — extract structured data from ShadowDocument lines.

Does NOT own the lines. The ShadowDocument owns the raw text;
this module only reads it to populate the project model.
"""
from __future__ import annotations
from dataclasses import dataclass, field

# ASS time format: H:MM:SS.cc
def ass_time_to_ms(text: str) -> int:
    """Parse 'H:MM:SS.cc' to milliseconds."""
    text = text.strip()
    try:
        parts = text.split(":")
        if len(parts) != 3:
            return 0
        h = int(parts[0])
        m = int(parts[1])
        sec_parts = parts[2].split(".")
        s = int(sec_parts[0])
        cs = int(sec_parts[1]) if len(sec_parts) > 1 else 0
        if len(sec_parts) > 1 and len(sec_parts[1]) == 1:
            cs *= 10
        return h * 3_600_000 + m * 60_000 + s * 1_000 + cs * 10
    except (ValueError, IndexError):
        return 0


def ms_to_ass_time(ms: int) -> str:
    """Format milliseconds to 'H:MM:SS.cc'."""
    if ms < 0:
        ms = 0
    cs = (ms // 10) % 100
    s = (ms // 1000) % 60
    m = (ms // 60_000) % 60
    h = ms // 3_600_000
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


@dataclass(slots=True)
class ParsedStyle:
    """Structured representation of one ASS style."""
    name: str = "Default"
    fontname: str = "Arial"
    fontsize: int = 48
    primary_colour: str = "&H00FFFFFF"
    secondary_colour: str = "&H000000FF"
    outline_colour: str = "&H00000000"
    back_colour: str = "&H00000000"
    bold: int = -1
    italic: int = 0
    underline: int = 0
    strikeout: int = 0
    scale_x: float = 100.0
    scale_y: float = 100.0
    spacing: float = 0.0
    angle: float = 0.0
    border_style: int = 1
    outline: float = 2.0
    shadow: float = 2.0
    alignment: int = 2
    margin_l: int = 10
    margin_r: int = 10
    margin_v: int = 10
    encoding: int = 1
    # Shadow tracking
    shadow_line_idx: int = -1


@dataclass(slots=True)
class ParsedEvent:
    """Structured representation of one ASS dialogue/comment."""
    layer: int = 0
    start_ms: int = 0
    end_ms: int = 0
    style: str = "Default"
    name: str = ""      # speaker/actor
    margin_l: int = 0
    margin_r: int = 0
    margin_v: int = 0
    effect: str = ""
    text: str = ""
    is_comment: bool = False
    # Shadow tracking
    shadow_line_idx: int = -1


def parse_style_line(format_fields: list[str], line_text: str) -> ParsedStyle:
    """Parse 'Style: val,val,...' using the Format field order."""
    raw = line_text.split(":", 1)[1] if ":" in line_text else line_text
    parts = [p.strip() for p in raw.split(",")]

    style = ParsedStyle()
    field_map = {
        "Name": "name", "Fontname": "fontname", "Fontsize": "fontsize",
        "PrimaryColour": "primary_colour", "SecondaryColour": "secondary_colour",
        "OutlineColour": "outline_colour", "BackColour": "back_colour",
        "Bold": "bold", "Italic": "italic", "Underline": "underline",
        "StrikeOut": "strikeout", "ScaleX": "scale_x", "ScaleY": "scale_y",
        "Spacing": "spacing", "Angle": "angle", "BorderStyle": "border_style",
        "Outline": "outline", "Shadow": "shadow", "Alignment": "alignment",
        "MarginL": "margin_l", "MarginR": "margin_r", "MarginV": "margin_v",
        "Encoding": "encoding",
    }
    int_fields = {"Fontsize", "Bold", "Italic", "Underline", "StrikeOut",
                  "BorderStyle", "Alignment", "MarginL", "MarginR", "MarginV", "Encoding"}
    float_fields = {"ScaleX", "ScaleY", "Spacing", "Angle", "Outline", "Shadow"}

    for i, fname in enumerate(format_fields):
        if i >= len(parts):
            break
        attr = field_map.get(fname)
        if attr is None:
            continue
        val = parts[i]
        try:
            if fname in int_fields:
                setattr(style, attr, int(val))
            elif fname in float_fields:
                setattr(style, attr, float(val))
            else:
                setattr(style, attr, val)
        except (ValueError, TypeError):
            pass
    return style


def serialize_style_line(style: ParsedStyle, format_fields: list[str]) -> str:
    """Serialize a ParsedStyle back to 'Style: val,val,...'."""
    field_map = {
        "Name": style.name, "Fontname": style.fontname, "Fontsize": style.fontsize,
        "PrimaryColour": style.primary_colour, "SecondaryColour": style.secondary_colour,
        "OutlineColour": style.outline_colour, "BackColour": style.back_colour,
        "Bold": style.bold, "Italic": style.italic, "Underline": style.underline,
        "StrikeOut": style.strikeout, "ScaleX": style.scale_x, "ScaleY": style.scale_y,
        "Spacing": style.spacing, "Angle": style.angle, "BorderStyle": style.border_style,
        "Outline": style.outline, "Shadow": style.shadow, "Alignment": style.alignment,
        "MarginL": style.margin_l, "MarginR": style.margin_r, "MarginV": style.margin_v,
        "Encoding": style.encoding,
    }
    parts = []
    for fname in format_fields:
        val = field_map.get(fname, "")
        # Format floats: emit as int if value is whole, else as minimal decimal
        if isinstance(val, float):
            parts.append(str(int(val)) if val == int(val) else str(val))
        else:
            parts.append(str(val))
    return "Style: " + ",".join(parts)


def parse_event_line(format_fields: list[str], line_text: str) -> ParsedEvent:
    """Parse 'Dialogue: val,val,...' or 'Comment: val,val,...'."""
    is_comment = line_text.strip().lower().startswith("comment:")
    raw = line_text.split(":", 1)[1] if ":" in line_text else line_text
    parts = raw.split(",", len(format_fields) - 1)

    event = ParsedEvent(is_comment=is_comment)
    for i, fname in enumerate(format_fields):
        if i >= len(parts):
            break
        val = parts[i].strip() if fname != "Text" else parts[i]
        try:
            if fname == "Layer":
                event.layer = int(val)
            elif fname == "Start":
                event.start_ms = ass_time_to_ms(val)
            elif fname == "End":
                event.end_ms = ass_time_to_ms(val)
            elif fname == "Style":
                event.style = val
            elif fname == "Name":
                event.name = val
            elif fname == "MarginL":
                event.margin_l = int(val)
            elif fname == "MarginR":
                event.margin_r = int(val)
            elif fname == "MarginV":
                event.margin_v = int(val)
            elif fname == "Effect":
                event.effect = val
            elif fname == "Text":
                event.text = val
        except (ValueError, TypeError):
            pass
    return event


def serialize_event_line(event: ParsedEvent, format_fields: list[str]) -> str:
    """Serialize a ParsedEvent back to 'Dialogue: ...' or 'Comment: ...'."""
    prefix = "Comment" if event.is_comment else "Dialogue"
    field_map = {
        "Layer": str(event.layer),
        "Start": ms_to_ass_time(event.start_ms),
        "End": ms_to_ass_time(event.end_ms),
        "Style": event.style,
        "Name": event.name,
        "MarginL": str(event.margin_l),
        "MarginR": str(event.margin_r),
        "MarginV": str(event.margin_v),
        "Effect": event.effect,
        "Text": event.text,
    }
    parts = []
    for fname in format_fields:
        parts.append(field_map.get(fname, ""))
    return f"{prefix}: " + ",".join(parts)


def extract_format_fields(format_line_text: str) -> list[str]:
    """Extract field names from a 'Format: ...' line."""
    raw = format_line_text.split(":", 1)[1] if ":" in format_line_text else format_line_text
    return [f.strip() for f in raw.split(",")]


def extract_script_info(kv_lines: list) -> dict[str, str]:
    """Extract key-value pairs from Script Info lines."""
    info = {}
    for rl in kv_lines:
        text = rl.text.strip()
        if ":" in text:
            key, _, value = text.partition(":")
            info[key.strip()] = value.strip()
    return info
