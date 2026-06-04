"""core.style — ASS 스타일 스키마/검증(순수 코어)."""
from __future__ import annotations

from core.style.schema import (
    STYLE_FIELDS,
    FieldMeta,
    default_style_props,
    validate_style_props,
)

__all__ = ["STYLE_FIELDS", "FieldMeta", "default_style_props", "validate_style_props"]
