"""스타일 매니저 다이얼로그 — V4+ 스타일 CRUD 편집기.

STYLE_FIELDS(core.style.schema)를 introspect 해 에디터를 자동 생성한다. ParsedStyle
의 속성명이 STYLE_FIELDS 키와 1:1 이라 getattr/setattr 로 직접 읽고 쓴다.

이 다이얼로그는 순수 에디터다 — self._styles 의 deepcopy 작업본을 편집하고, OK 시
result_styles()(새 리스트)와 rename_map()(원래이름→새이름)을 노출한다. MainWindow
가 ReplaceStylesCommand + 이벤트 재지정을 CompositeCommand 로 묶어 적용한다(단일 undo).
"""
from __future__ import annotations

import copy
import re
from typing import Any

from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox, QColorDialog, QComboBox, QDialog, QDialogButtonBox,
    QDoubleSpinBox, QFormLayout, QHBoxLayout, QInputDialog, QLabel,
    QLineEdit, QListWidget, QMessageBox, QPushButton, QScrollArea, QSpinBox,
    QVBoxLayout, QWidget,
)

from core.ass.parser import ParsedStyle
from core.style.schema import STYLE_FIELDS


def _ass_to_qcolor(s: str) -> QColor:
    """'&HAABBGGRR' 또는 '&HBBGGRR' → QColor. ASS 알파(0=불투명)를 Qt(255=불투명)로 반전."""
    h = re.sub(r"[&Hh]", "", (s or "").strip()) or "FFFFFF"
    h = h.zfill(8)[-8:]
    aa, b, g, r = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), int(h[6:8], 16)
    return QColor(r, g, b, 255 - aa)


def _qcolor_to_ass(c: QColor) -> str:
    aa = 255 - c.alpha()
    return f"&H{aa:02X}{c.blue():02X}{c.green():02X}{c.red():02X}"


class _ColorButton(QPushButton):
    def __init__(self, ass_color: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._ass = ass_color
        self.clicked.connect(self._pick)
        self._refresh()

    def _refresh(self) -> None:
        c = _ass_to_qcolor(self._ass)
        self.setText(self._ass)
        self.setStyleSheet(
            f"background-color: rgb({c.red()},{c.green()},{c.blue()});"
            f"color: {'#000' if c.lightness() > 128 else '#fff'};"
        )

    def _pick(self) -> None:
        c = QColorDialog.getColor(
            _ass_to_qcolor(self._ass), self, "색 선택",
            QColorDialog.ColorDialogOption.ShowAlphaChannel,
        )
        if c.isValid():
            self._ass = _qcolor_to_ass(c)
            self._refresh()

    def value(self) -> str:
        return self._ass

    def set_value(self, ass_color: str) -> None:
        self._ass = ass_color
        self._refresh()


class StyleManagerDialog(QDialog):
    def __init__(self, styles: list[ParsedStyle], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("스타일 매니저")
        self.resize(680, 560)

        self._styles: list[ParsedStyle] = [copy.deepcopy(s) for s in styles]
        self._original: dict[int, str] = {id(s): s.name for s in self._styles}
        self._deleted: set[str] = set()
        self._current: ParsedStyle | None = None
        self._widgets: dict[str, QWidget] = {}

        root = QHBoxLayout(self)

        # 좌측: 목록 + 버튼
        left = QVBoxLayout()
        self._list = QListWidget()
        self._list.currentRowChanged.connect(self._on_row_changed)
        left.addWidget(self._list, 1)
        for text, slot in (
            ("새로 만들기", self._on_new),
            ("복제", self._on_duplicate),
            ("이름 변경", self._on_rename),
            ("삭제", self._on_delete),
        ):
            b = QPushButton(text)
            b.clicked.connect(slot)
            left.addWidget(b)
        root.addLayout(left, 1)

        # 우측: 속성 에디터 (스크롤)
        right = QVBoxLayout()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        editor = QWidget()
        self._form = QFormLayout(editor)
        for field, meta in STYLE_FIELDS.items():
            w = self._make_widget(field, meta)
            self._widgets[field] = w
            self._form.addRow(meta.label, w)
        scroll.setWidget(editor)
        right.addWidget(scroll, 1)

        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("확인")
        bb.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        bb.accepted.connect(self._on_ok)
        bb.rejected.connect(self.reject)
        right.addWidget(bb)
        root.addLayout(right, 2)

        self._rebuild_list(select=0 if self._styles else -1)

    # -- 위젯 생성 / 값 입출력 --

    def _make_widget(self, field: str, meta: Any) -> QWidget:
        if meta.kind == "str":
            return QLineEdit()
        if meta.kind == "int":
            w = QSpinBox()
            w.setRange(int(meta.minimum if meta.minimum is not None else -100000),
                       int(meta.maximum if meta.maximum is not None else 100000))
            return w
        if meta.kind == "float":
            w = QDoubleSpinBox()
            w.setDecimals(2)
            w.setRange(float(meta.minimum if meta.minimum is not None else -100000),
                       float(meta.maximum if meta.maximum is not None else 100000))
            return w
        if meta.kind == "bool_ass":
            return QCheckBox("사용")
        if meta.kind == "color":
            return _ColorButton(str(meta.default))
        if meta.kind == "choice":
            w = QComboBox()
            for c in meta.choices:
                w.addItem(str(c), c)
            return w
        return QLineEdit()

    def _widget_get(self, field: str) -> Any:
        w = self._widgets[field]
        meta = STYLE_FIELDS[field]
        if isinstance(w, QLineEdit):
            return w.text()
        if isinstance(w, QSpinBox):
            return w.value()
        if isinstance(w, QDoubleSpinBox):
            return w.value()
        if isinstance(w, QCheckBox):
            return -1 if w.isChecked() else 0
        if isinstance(w, _ColorButton):
            return w.value()
        if isinstance(w, QComboBox):
            return w.currentData()
        return None

    def _widget_set(self, field: str, value: Any) -> None:
        w = self._widgets[field]
        if isinstance(w, QLineEdit):
            w.setText(str(value))
        elif isinstance(w, QSpinBox):
            try:
                w.setValue(int(float(value)))
            except (TypeError, ValueError):
                w.setValue(0)
        elif isinstance(w, QDoubleSpinBox):
            try:
                w.setValue(float(value))
            except (TypeError, ValueError):
                w.setValue(0.0)
        elif isinstance(w, QCheckBox):
            w.setChecked(int(value) != 0 if str(value).lstrip("-").isdigit() else False)
        elif isinstance(w, _ColorButton):
            w.set_value(str(value))
        elif isinstance(w, QComboBox):
            idx = w.findData(int(value)) if str(value).lstrip("-").isdigit() else -1
            w.setCurrentIndex(idx if idx >= 0 else 0)

    # -- 목록 / 선택 --

    def _rebuild_list(self, select: int = -1) -> None:
        self._list.blockSignals(True)
        self._list.clear()
        for s in self._styles:
            self._list.addItem(s.name)
        self._list.blockSignals(False)
        if 0 <= select < len(self._styles):
            self._list.setCurrentRow(select)
        else:
            self._current = None
            self._set_editor_enabled(False)

    def _set_editor_enabled(self, on: bool) -> None:
        for w in self._widgets.values():
            w.setEnabled(on)

    def _on_row_changed(self, row: int) -> None:
        self._flush_current()
        if 0 <= row < len(self._styles):
            self._current = self._styles[row]
            self._load_style(self._current)
            self._set_editor_enabled(True)
        else:
            self._current = None
            self._set_editor_enabled(False)

    def _load_style(self, s: ParsedStyle) -> None:
        for field, meta in STYLE_FIELDS.items():
            self._widget_set(field, getattr(s, field, meta.default))

    def _flush_current(self) -> None:
        if self._current is None:
            return
        for field in STYLE_FIELDS:
            setattr(self._current, field, self._widget_get(field))

    # -- CRUD --

    def _names(self) -> set[str]:
        return {s.name for s in self._styles}

    def _unique(self, base: str) -> str:
        name = base
        i = 1
        existing = self._names()
        while name in existing:
            i += 1
            name = f"{base} {i}"
        return name

    def _on_new(self) -> None:
        self._flush_current()
        name, ok = QInputDialog.getText(self, "새 스타일", "이름:")
        if not ok or not name.strip():
            return
        name = name.strip()
        if name in self._names():
            QMessageBox.warning(self, "중복", "이미 있는 이름입니다.")
            return
        s = ParsedStyle(name=name, shadow_line_idx=-1)
        self._styles.append(s)
        self._rebuild_list(select=len(self._styles) - 1)

    def _on_duplicate(self) -> None:
        self._flush_current()
        if self._current is None:
            return
        s = copy.deepcopy(self._current)
        s.name = self._unique(f"{s.name} copy")
        s.shadow_line_idx = -1
        self._styles.append(s)
        self._rebuild_list(select=len(self._styles) - 1)

    def _on_rename(self) -> None:
        self._flush_current()
        if self._current is None:
            return
        new, ok = QInputDialog.getText(
            self, "이름 변경", "새 이름:", text=self._current.name
        )
        if not ok or not new.strip():
            return
        new = new.strip()
        if new != self._current.name and new in self._names():
            QMessageBox.warning(self, "중복", "이미 있는 이름입니다.")
            return
        row = self._styles.index(self._current)
        self._current.name = new
        self._rebuild_list(select=row)

    def _on_delete(self) -> None:
        if self._current is None:
            return
        if QMessageBox.question(
            self, "스타일 삭제",
            f"'{self._current.name}' 스타일을 삭제할까요?\n"
            "(이 스타일을 참조하는 줄은 QA 에서 누락 스타일로 표시됩니다.)",
        ) != QMessageBox.StandardButton.Yes:
            return
        s = self._current
        if id(s) in self._original:
            self._deleted.add(self._original.pop(id(s)))
        self._styles.remove(s)
        self._current = None
        self._rebuild_list(select=min(0, len(self._styles) - 1) if self._styles else -1)

    def _on_ok(self) -> None:
        self._flush_current()
        self.accept()

    # -- 결과 노출 --

    def result_styles(self) -> list[ParsedStyle]:
        return self._styles

    def rename_map(self) -> dict[str, str]:
        return {
            self._original[id(s)]: s.name
            for s in self._styles
            if id(s) in self._original and self._original[id(s)] != s.name
        }

    def deleted_originals(self) -> set[str]:
        return set(self._deleted)
