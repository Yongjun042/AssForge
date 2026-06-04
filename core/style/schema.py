"""ASS V4+ 스타일 필드 메타데이터 — 에디터 UI 자동생성 + 검증의 단일 출처.

DB 컬럼(core.project.project_db 의 styles 테이블)과 1:1 대응한다. UI 는 이 표를
introspect 해 색 피커/체크박스/스핀박스를 만든다. bold/italic/underline/strikeout 는
ASS 규약상 -1=참, 0=거짓으로 저장된다(kind='bool_ass').
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class FieldMeta:
    kind: str               # str|int|float|bool_ass|color|choice
    label: str
    default: Any
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[int, ...] = ()


# 표시 순서 = 에디터 배치 순서.
STYLE_FIELDS: dict[str, FieldMeta] = {
    "fontname": FieldMeta("str", "글꼴", "Arial"),
    "fontsize": FieldMeta("int", "크기", 48, 1, 2000),
    "primary_colour": FieldMeta("color", "주 색상", "&H00FFFFFF"),
    "secondary_colour": FieldMeta("color", "보조 색상", "&H000000FF"),
    "outline_colour": FieldMeta("color", "외곽선 색", "&H00000000"),
    "back_colour": FieldMeta("color", "그림자 색", "&H00000000"),
    "bold": FieldMeta("bool_ass", "굵게", -1),
    "italic": FieldMeta("bool_ass", "기울임", 0),
    "underline": FieldMeta("bool_ass", "밑줄", 0),
    "strikeout": FieldMeta("bool_ass", "취소선", 0),
    "scale_x": FieldMeta("float", "가로 비율(%)", 100.0, 1, 1000),
    "scale_y": FieldMeta("float", "세로 비율(%)", 100.0, 1, 1000),
    "spacing": FieldMeta("float", "자간", 0.0, -100, 100),
    "angle": FieldMeta("float", "회전각", 0.0, -360, 360),
    "border_style": FieldMeta("choice", "테두리 방식", 1, choices=(1, 3)),
    "outline": FieldMeta("float", "외곽선 두께", 2.0, 0, 100),
    "shadow": FieldMeta("float", "그림자 거리", 2.0, 0, 100),
    "alignment": FieldMeta("choice", "정렬", 2, choices=(1, 2, 3, 4, 5, 6, 7, 8, 9)),
    "margin_l": FieldMeta("int", "왼쪽 여백", 10, 0, 4000),
    "margin_r": FieldMeta("int", "오른쪽 여백", 10, 0, 4000),
    "margin_v": FieldMeta("int", "세로 여백", 10, 0, 4000),
    "encoding": FieldMeta("int", "인코딩", 1, 0, 255),
}


def default_style_props() -> dict[str, Any]:
    """모든 필드를 기본값으로 채운 새 스타일 속성 dict."""
    return {name: meta.default for name, meta in STYLE_FIELDS.items()}


def validate_style_props(props: dict[str, Any]) -> list[str]:
    """알 수 없는 키/범위 위반을 오류 리스트로. 빈 리스트 = 통과."""
    errors: list[str] = []
    for key, value in props.items():
        meta = STYLE_FIELDS.get(key)
        if meta is None:
            errors.append(f"알 수 없는 스타일 필드: '{key}'")
            continue
        if meta.kind in ("int", "float"):
            try:
                num = float(value)
            except (TypeError, ValueError):
                errors.append(f"{key}: 숫자여야 함 (현재 {value!r})")
                continue
            if meta.minimum is not None and num < meta.minimum:
                errors.append(f"{key}: {meta.minimum:g} 이상 (현재 {num:g})")
            if meta.maximum is not None and num > meta.maximum:
                errors.append(f"{key}: {meta.maximum:g} 이하 (현재 {num:g})")
        elif meta.kind == "choice":
            try:
                if int(value) not in meta.choices:
                    errors.append(f"{key}: {meta.choices} 중 하나 (현재 {value!r})")
            except (TypeError, ValueError):
                errors.append(f"{key}: 정수여야 함 (현재 {value!r})")
        elif meta.kind == "bool_ass":
            try:
                ival = int(value)
            except (TypeError, ValueError):
                ival = None
            if ival not in (-1, 0):
                errors.append(f"{key}: -1(참) 또는 0(거짓) (현재 {value!r})")
    return errors
