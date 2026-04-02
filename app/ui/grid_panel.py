"""Subtitle grid — QTableView backed by EventRow data from ProjectDB."""
from __future__ import annotations

import re
from typing import Any, Optional, Sequence

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QTableView, QVBoxLayout, QWidget,
)

from core.project.project_db import EventRow

_TAG_RE = re.compile(r"\{[^}]*\}")
_COLS = ("#", "시작", "종료", "길이", "스타일", "텍스트")


def _fmt(ms: int) -> str:
    if ms < 0: ms = 0
    cs = (ms // 10) % 100
    s = (ms // 1000) % 60
    m = (ms // 60_000) % 60
    h = ms // 3_600_000
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _strip(text: str) -> str:
    return _TAG_RE.sub("", text).replace("\\N", " ").replace("\\n", " ")


class _Model(QAbstractTableModel):
    def __init__(self) -> None:
        super().__init__()
        self._events: list[EventRow] = []

    def set_events(self, events: list[EventRow]) -> None:
        self.beginResetModel()
        self._events = list(events)
        self.endResetModel()

    def update_single(self, event: EventRow) -> None:
        for i, ev in enumerate(self._events):
            if ev.id == event.id:
                self._events[i] = event
                tl = self.index(i, 0)
                br = self.index(i, len(_COLS) - 1)
                self.dataChanged.emit(tl, br)
                return

    def get_event(self, row: int) -> EventRow | None:
        return self._events[row] if 0 <= row < len(self._events) else None

    def rowCount(self, parent=QModelIndex()) -> int:
        return len(self._events)

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(_COLS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return _COLS[section]
        return None

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        r, c = index.row(), index.column()
        if r >= len(self._events):
            return None
        ev = self._events[r]

        if role == Qt.ItemDataRole.DisplayRole:
            if c == 0: return r + 1
            if c == 1: return _fmt(ev.start_ms)
            if c == 2: return _fmt(ev.end_ms)
            if c == 3: return _fmt(max(0, ev.end_ms - ev.start_ms))
            if c == 4: return ev.style_id
            if c == 5: return _strip(ev.text)

        if role == Qt.ItemDataRole.BackgroundRole and ev.is_comment:
            return QBrush(QColor("#2D2D1A"))
        if role == Qt.ItemDataRole.ForegroundRole:
            return QBrush(QColor("#AAA") if ev.is_comment else QColor("#DDD"))
        if role == Qt.ItemDataRole.FontRole and ev.is_comment:
            f = QFont(); f.setItalic(True); return f

        return None

    def flags(self, index):
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable


class GridPanel(QWidget):
    """Subtitle event list.

    Signals:
        selection_changed(list[str]): event IDs selected
        line_activated(str): double-click event ID
    """

    selection_changed = Signal(list)
    line_activated = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._model = _Model()
        self._build_ui()

    def set_events(self, events: list[EventRow]) -> None:
        self._model.set_events(events)

    def update_event_row(self, event_id: str) -> None:
        for i, ev in enumerate(self._model._events):
            if ev.id == event_id:
                tl = self._model.index(i, 0)
                br = self._model.index(i, len(_COLS) - 1)
                self._model.dataChanged.emit(tl, br)
                return

    def selected_event_ids(self) -> list[str]:
        indices = self._table.selectionModel().selectedRows()
        ids = []
        for idx in indices:
            ev = self._model.get_event(idx.row())
            if ev:
                ids.append(ev.id)
        return ids

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        search = QHBoxLayout()
        search.addWidget(QLabel("검색:"))
        self._search = QLineEdit()
        self._search.setPlaceholderText("텍스트로 필터링...")
        self._search.setClearButtonEnabled(True)
        search.addWidget(self._search)
        root.addLayout(search)

        self._table = QTableView()
        self._table.setModel(self._model)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.doubleClicked.connect(self._on_double_click)

        for col, w in ((0, 35), (1, 90), (2, 90), (3, 65), (4, 70)):
            self._table.setColumnWidth(col, w)

        root.addWidget(self._table)
        self._table.selectionModel().selectionChanged.connect(self._on_sel)

    def _on_sel(self) -> None:
        self.selection_changed.emit(self.selected_event_ids())

    def _on_double_click(self, idx: QModelIndex) -> None:
        ev = self._model.get_event(idx.row())
        if ev:
            self.line_activated.emit(ev.id)
