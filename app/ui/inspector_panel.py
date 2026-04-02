"""Inspector panel — edit a single event's properties."""
from __future__ import annotations

import re
from typing import Any, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QComboBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QPlainTextEdit, QSpinBox, QVBoxLayout, QWidget,
)

from core.project.project_db import EventRow, LockState


_TIME_RE = re.compile(r"^(\d+):(\d{1,2}):(\d{1,2})\.(\d{1,2})$")


def _parse_time(text: str) -> int | None:
    m = _TIME_RE.match(text.strip())
    if not m:
        return None
    h, mi, s, cs = int(m[1]), int(m[2]), int(m[3]), int(m[4])
    if len(m[4]) == 1:
        cs *= 10
    return h * 3_600_000 + mi * 60_000 + s * 1_000 + cs * 10


def _fmt(ms: int) -> str:
    if ms < 0: ms = 0
    cs = (ms // 10) % 100
    s = (ms // 1000) % 60
    m = (ms // 60_000) % 60
    h = ms // 3_600_000
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


class InspectorPanel(QWidget):
    """Edit panel for a single subtitle event.

    Signals:
        event_edited(str, dict): (event_id, {field: value})
    """

    event_edited = Signal(str, dict)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._event_id: str | None = None
        self._updating = False
        self._build_ui()

    def load_event(self, ev: EventRow) -> None:
        self._updating = True
        self._event_id = ev.id
        self._start.setText(_fmt(ev.start_ms))
        self._end.setText(_fmt(ev.end_ms))
        self._dur.setText(_fmt(max(0, ev.end_ms - ev.start_ms)))
        self._style.setCurrentText(ev.style_id)
        self._speaker.setText(ev.speaker)
        self._layer.setValue(ev.layer)
        self._text.setPlainText(ev.text)
        self._lock_label.setText(f"상태: {ev.lock_state.value}")
        if ev.ai_confidence > 0:
            self._conf_label.setText(f"신뢰도: {ev.ai_confidence:.2f}")
        else:
            self._conf_label.setText("신뢰도: —")
        self._updating = False

    def clear(self) -> None:
        self._updating = True
        self._event_id = None
        self._start.clear()
        self._end.clear()
        self._dur.setText("—")
        self._speaker.clear()
        self._layer.setValue(0)
        self._text.clear()
        self._lock_label.setText("상태: —")
        self._conf_label.setText("신뢰도: —")
        self._updating = False

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)

        # Timing
        tg = QGroupBox("타이밍")
        tl = QHBoxLayout(tg)
        tl.addWidget(QLabel("시작:"))
        self._start = QLineEdit()
        self._start.setMaximumWidth(110)
        self._start.editingFinished.connect(self._on_time)
        tl.addWidget(self._start)
        tl.addWidget(QLabel("종료:"))
        self._end = QLineEdit()
        self._end.setMaximumWidth(110)
        self._end.editingFinished.connect(self._on_time)
        tl.addWidget(self._end)
        tl.addWidget(QLabel("길이:"))
        self._dur = QLabel("—")
        tl.addWidget(self._dur)
        tl.addStretch()
        root.addWidget(tg)

        # Meta
        mg = QGroupBox("메타데이터")
        ml = QFormLayout(mg)
        self._style = QComboBox()
        self._style.setEditable(True)
        self._style.addItem("Default")
        self._style.currentTextChanged.connect(self._on_meta)
        ml.addRow("스타일:", self._style)
        self._speaker = QLineEdit()
        self._speaker.editingFinished.connect(self._on_meta)
        ml.addRow("화자:", self._speaker)
        self._layer = QSpinBox()
        self._layer.setRange(0, 9999)
        self._layer.valueChanged.connect(self._on_meta)
        ml.addRow("레이어:", self._layer)
        root.addWidget(mg)

        # Text
        self._text = QPlainTextEdit()
        self._text.setMaximumHeight(100)
        self._text.setFont(QFont("Consolas", 11))
        self._text.textChanged.connect(self._on_text)
        root.addWidget(self._text)

        # AI status
        sg = QGroupBox("AI 상태")
        sl = QVBoxLayout(sg)
        self._lock_label = QLabel("상태: —")
        sl.addWidget(self._lock_label)
        self._conf_label = QLabel("신뢰도: —")
        sl.addWidget(self._conf_label)
        root.addWidget(sg)

        root.addStretch()

    def _emit(self, changes: dict) -> None:
        if self._updating or not self._event_id:
            return
        self.event_edited.emit(self._event_id, changes)

    def _on_time(self) -> None:
        changes = {}
        s = _parse_time(self._start.text())
        e = _parse_time(self._end.text())
        if s is not None:
            changes["start_ms"] = s
        if e is not None:
            changes["end_ms"] = e
        if s is not None and e is not None:
            self._dur.setText(_fmt(max(0, e - s)))
        if changes:
            self._emit(changes)

    def _on_meta(self) -> None:
        self._emit({
            "style_id": self._style.currentText(),
            "speaker": self._speaker.text(),
            "layer": self._layer.value(),
        })

    def _on_text(self) -> None:
        self._emit({"text": self._text.toPlainText()})
