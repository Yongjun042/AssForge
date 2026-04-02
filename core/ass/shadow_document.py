"""Shadow Document — line-based lossless round-trip for .ass files.

Instead of building a perfect AST, we store the original file line by line.
On export, unmodified lines are emitted as-is; only edited lines are
re-serialized from the structured model.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Literal

class LineType(Enum):
    SECTION_HEADER = auto()
    SCRIPT_INFO_KV = auto()      # key: value
    SCRIPT_INFO_COMMENT = auto()  # ; comment or blank
    STYLE_FORMAT = auto()
    STYLE = auto()
    EVENT_FORMAT = auto()
    DIALOGUE = auto()
    COMMENT = auto()              # Comment: ...
    UNKNOWN = auto()

@dataclass(slots=True)
class RawLine:
    """One line from the original .ass file with type annotation."""
    index: int                     # 0-based line number in original file
    text: str                      # exact original text (no line ending)
    line_type: LineType
    section: str = ""              # which section this line belongs to
    modified: bool = False         # set True when model overwrites this line
    deleted: bool = False          # set True when line is removed

class ShadowDocument:
    """Stores the complete original file for lossless round-trip."""

    __slots__ = ("_lines", "_encoding", "_has_bom", "_line_ending", "_extra_info")

    def __init__(self) -> None:
        self._lines: list[RawLine] = []
        self._encoding: str = "utf-8"
        self._has_bom: bool = False
        self._line_ending: str = "\r\n"
        self._extra_info: dict = {}

    @property
    def lines(self) -> list[RawLine]:
        return self._lines

    @property
    def encoding(self) -> str:
        return self._encoding

    @property
    def has_bom(self) -> bool:
        return self._has_bom

    @property
    def line_ending(self) -> str:
        return self._line_ending

    def load_from_file(self, filepath: str) -> None:
        """Read a .ass file and classify each line."""
        # Detect BOM from raw bytes first so BOM-less UTF-8 stays BOM-less on save.
        raw = open(filepath, "rb").read()
        self._has_bom = raw.startswith(b"\xef\xbb\xbf")

        if self._has_bom:
            text = raw[3:].decode("utf-8")
            self._encoding = "utf-8"
        else:
            for enc in ("utf-8", "latin-1"):
                try:
                    text = raw.decode(enc)
                    self._encoding = enc
                    break
                except (UnicodeDecodeError, UnicodeError):
                    continue
            else:
                text = raw.decode("utf-8", errors="replace")
                self._encoding = "utf-8"

        # Detect line ending
        if "\r\n" in text:
            self._line_ending = "\r\n"
        elif "\r" in text:
            self._line_ending = "\r"
        else:
            self._line_ending = "\n"

        content = text.replace("\r\n", "\n").replace("\r", "\n")
        raw_lines = content.split("\n")

        # Remove trailing empty line from split
        if raw_lines and raw_lines[-1] == "":
            raw_lines.pop()

        current_section = ""
        self._lines = []

        for i, line in enumerate(raw_lines):
            stripped = line.strip()

            # Section header
            if stripped.startswith("[") and "]" in stripped:
                section_name = stripped[stripped.index("[") + 1: stripped.index("]")]
                current_section = section_name
                self._lines.append(RawLine(i, line, LineType.SECTION_HEADER, current_section))
                continue

            normalized_section = current_section.lower().replace(" ", "")

            if normalized_section == "scriptinfo":
                if not stripped or stripped.startswith(";") or stripped.startswith("!:"):
                    self._lines.append(RawLine(i, line, LineType.SCRIPT_INFO_COMMENT, current_section))
                elif ":" in stripped:
                    self._lines.append(RawLine(i, line, LineType.SCRIPT_INFO_KV, current_section))
                else:
                    self._lines.append(RawLine(i, line, LineType.UNKNOWN, current_section))

            elif normalized_section in ("v4+styles", "v4styles", "v4+styles+"):
                lower = stripped.lower()
                if lower.startswith("format:"):
                    self._lines.append(RawLine(i, line, LineType.STYLE_FORMAT, current_section))
                elif lower.startswith("style:"):
                    self._lines.append(RawLine(i, line, LineType.STYLE, current_section))
                else:
                    self._lines.append(RawLine(i, line, LineType.UNKNOWN, current_section))

            elif normalized_section == "events":
                lower = stripped.lower()
                if lower.startswith("format:"):
                    self._lines.append(RawLine(i, line, LineType.EVENT_FORMAT, current_section))
                elif lower.startswith("dialogue:"):
                    self._lines.append(RawLine(i, line, LineType.DIALOGUE, current_section))
                elif lower.startswith("comment:"):
                    self._lines.append(RawLine(i, line, LineType.COMMENT, current_section))
                else:
                    self._lines.append(RawLine(i, line, LineType.UNKNOWN, current_section))

            else:
                self._lines.append(RawLine(i, line, LineType.UNKNOWN, current_section))

    def load_from_string(self, content: str) -> None:
        """Parse from string (for testing)."""
        import tempfile, os
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".ass",
                                          encoding="utf-8", delete=False, newline="")
        try:
            tmp.write(content)
            tmp.close()
            self.load_from_file(tmp.name)
        finally:
            os.unlink(tmp.name)

    def mark_modified(self, line_index: int) -> None:
        """Mark a shadow line as modified (will be re-serialized on export)."""
        for rl in self._lines:
            if rl.index == line_index:
                rl.modified = True
                return

    def mark_deleted(self, line_index: int) -> None:
        """Mark a shadow line as deleted."""
        for rl in self._lines:
            if rl.index == line_index:
                rl.deleted = True
                return

    def export(self, overrides: dict[int, str] | None = None,
               inserts: dict[int, list[str]] | None = None,
               deleted_indices: set[int] | None = None) -> str:
        """Export the document back to string.

        Args:
            overrides: {shadow_line_index: new_text} for modified lines
            inserts: {after_shadow_line_index: [new_lines]} for new lines
                     Use -1 to insert before the first line
            deleted_indices: shadow line indices to omit for this export only
        """
        overrides = overrides or {}
        inserts = inserts or {}
        deleted_indices = deleted_indices or set()

        result_lines: list[str] = []

        # Insert before first line if needed
        if -1 in inserts:
            result_lines.extend(inserts[-1])

        for rl in self._lines:
            if rl.deleted or rl.index in deleted_indices:
                continue
            if rl.index in overrides:
                result_lines.append(overrides[rl.index])
            else:
                result_lines.append(rl.text)

            # Insert after this line
            if rl.index in inserts:
                result_lines.extend(inserts[rl.index])

        return self._line_ending.join(result_lines) + self._line_ending

    def get_lines_by_type(self, line_type: LineType) -> list[RawLine]:
        return [rl for rl in self._lines if rl.line_type == line_type and not rl.deleted]

    def get_section_lines(self, section: str) -> list[RawLine]:
        return [rl for rl in self._lines if rl.section == section and not rl.deleted]

    @staticmethod
    def create_empty() -> ShadowDocument:
        """Create a minimal empty .ass shadow document."""
        doc = ShadowDocument()
        minimal = (
            "[Script Info]\n"
            "; Script generated by AssForge\n"
            "ScriptType: v4.00+\n"
            "PlayResX: 1920\n"
            "PlayResY: 1080\n"
            "WrapStyle: 0\n"
            "\n"
            "[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
            "Style: Default,Arial,48,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,2,2,2,10,10,10,1\n"
            "\n"
            "[Events]\n"
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        )
        doc.load_from_string(minimal)
        return doc
