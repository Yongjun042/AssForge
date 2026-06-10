"""Inspector panel — edit a single event's properties."""
from __future__ import annotations

import re
from typing import Any, Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QButtonGroup, QComboBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QPlainTextEdit, QPushButton, QRadioButton, QSpinBox,
    QVBoxLayout, QWidget,
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
        lock_state_changed(str, str): (event_id, new_state_value)
        accept_suggestion(str): (event_id)
        reject_suggestion(str): (event_id)
    """

    event_edited = Signal(str, dict)
    lock_state_changed = Signal(str, str)
    accept_suggestion = Signal(str)
    reject_suggestion = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._event_id: str | None = None
        self._updating = False
        # 텍스트 편집 디바운스 — 키스트로크마다 undo 커맨드가 쌓이지 않도록
        # 입력이 멎은 뒤 한 번만 event_edited 를 보낸다.
        self._text_dirty = False
        self._text_timer = QTimer(self)
        self._text_timer.setSingleShot(True)
        self._text_timer.setInterval(500)
        self._text_timer.timeout.connect(self._flush_text)
        self._build_ui()

    def load_event(self, ev: EventRow) -> None:
        # 이전 줄의 미커밋 텍스트를 먼저 흘려보낸다 — 선택이 바뀌어도
        # 마지막 입력이 유실되거나 엉뚱한 줄에 적용되지 않게.
        self._flush_text()
        self._updating = True
        self._event_id = ev.id
        self._start.setText(_fmt(ev.start_ms))
        self._end.setText(_fmt(ev.end_ms))
        self._dur.setText(_fmt(max(0, ev.end_ms - ev.start_ms)))
        self._style.setCurrentText(ev.style_id)
        self._speaker.setText(ev.speaker)
        self._layer.setValue(ev.layer)
        self._text.setPlainText(ev.text)

        # LockState 라디오 동기화
        self._lock_radios[ev.lock_state].setChecked(True)

        if ev.ai_confidence > 0:
            self._conf_label.setText(f"신뢰도: {ev.ai_confidence:.2f}")
        else:
            self._conf_label.setText("신뢰도: —")

        # AI 제안값 표시 + Accept/Reject 버튼 활성화
        has_suggestion = (
            ev.suggested_start_ms is not None and ev.suggested_end_ms is not None
            and ev.lock_state != LockState.LOCKED
        )
        if has_suggestion:
            self._sugg_label.setText(
                f"제안: {_fmt(ev.suggested_start_ms)}  →  {_fmt(ev.suggested_end_ms)}"
            )
            self._sugg_label.setVisible(True)
            self._btn_accept.setVisible(True)
            self._btn_reject.setVisible(True)
        else:
            self._sugg_label.setVisible(False)
            self._btn_accept.setVisible(False)
            self._btn_reject.setVisible(False)

        self._updating = False

    def clear(self) -> None:
        # 미커밋 텍스트는 버린다 — clear 는 보통 행 삭제/선택 해제 직후라
        # 이미 사라진 줄에 편집을 보내면 무의미한 undo 항목만 쌓인다.
        self._text_timer.stop()
        self._text_dirty = False
        self._updating = True
        self._event_id = None
        self._start.clear()
        self._end.clear()
        self._dur.setText("—")
        self._speaker.clear()
        self._layer.setValue(0)
        self._text.clear()
        self._lock_radios[LockState.UNLOCKED].setChecked(True)
        self._conf_label.setText("신뢰도: —")
        self._sugg_label.setVisible(False)
        self._btn_accept.setVisible(False)
        self._btn_reject.setVisible(False)
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

        # LockState 라디오
        lock_row = QHBoxLayout()
        lock_row.addWidget(QLabel("잠금:"))
        self._lock_radios: dict[LockState, QRadioButton] = {}
        self._lock_group = QButtonGroup(self)
        for state, label in (
            (LockState.UNLOCKED, "열림"),
            (LockState.AI_SUGGESTED, "AI 제안"),
            (LockState.CONFIRMED, "확인"),
            (LockState.LOCKED, "잠금"),
        ):
            rb = QRadioButton(label)
            rb.toggled.connect(lambda checked, st=state: self._on_lock_radio(checked, st))
            self._lock_radios[state] = rb
            self._lock_group.addButton(rb)
            lock_row.addWidget(rb)
        lock_row.addStretch()
        sl.addLayout(lock_row)

        self._conf_label = QLabel("신뢰도: —")
        sl.addWidget(self._conf_label)

        # 제안 시간 표시 + Accept/Reject
        self._sugg_label = QLabel("")
        self._sugg_label.setStyleSheet("color: #FFD66B;")
        self._sugg_label.setVisible(False)
        sl.addWidget(self._sugg_label)

        btn_row = QHBoxLayout()
        self._btn_accept = QPushButton("✓ 수락")
        self._btn_accept.setStyleSheet("background: #2A7A35;")
        self._btn_accept.clicked.connect(self._on_accept)
        self._btn_accept.setVisible(False)
        btn_row.addWidget(self._btn_accept)
        self._btn_reject = QPushButton("✗ 거부")
        self._btn_reject.setStyleSheet("background: #7A2A2A;")
        self._btn_reject.clicked.connect(self._on_reject)
        self._btn_reject.setVisible(False)
        btn_row.addWidget(self._btn_reject)
        btn_row.addStretch()
        sl.addLayout(btn_row)

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
        if self._updating or not self._event_id:
            return
        self._text_dirty = True
        self._text_timer.start()  # 재시작 — 입력이 멎으면 1회 flush

    def _flush_text(self) -> None:
        if not self._text_dirty or not self._event_id:
            return
        self._text_timer.stop()
        self._text_dirty = False
        self._emit({"text": self._text.toPlainText()})

    def flush_pending(self) -> None:
        """디바운스 대기 중인 텍스트 편집을 즉시 커밋 — 저장 직전에 호출해
        마지막 키 입력(<500ms)이 저장에서 빠지지 않게 한다."""
        self._flush_text()

    def _on_lock_radio(self, checked: bool, state: LockState) -> None:
        if not checked or self._updating or not self._event_id:
            return
        self.lock_state_changed.emit(self._event_id, state.value)

    def _on_accept(self) -> None:
        if self._event_id:
            self.accept_suggestion.emit(self._event_id)

    def _on_reject(self) -> None:
        if self._event_id:
            self.reject_suggestion.emit(self._event_id)
