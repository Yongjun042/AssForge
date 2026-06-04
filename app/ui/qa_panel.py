"""QA 검사 다이얼로그 — core.qa.checks 를 돌려 문제 목록을 보여주고 해당 줄로 점프.

비모달로 띄워 검사 결과를 보면서 그리드의 줄을 선택할 수 있게 한다. 더블클릭하면
jump_to(event_id) 시그널을 내고, MainWindow 가 그 줄을 그리드에서 선택한다.
'다시 검사' 는 생성 시 받은 provider 콜백으로 현재 상태를 다시 읽어 재검사한다.
"""
from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton,
    QVBoxLayout, QWidget,
)

from core.qa.checks import QAOptions, run_checks, summarize

_SEV_COLOR = {"error": "#e05555", "warning": "#d8a23a", "info": "#6aa0d8"}
_SEV_LABEL = {"error": "오류", "warning": "경고", "info": "정보"}


class QaDialog(QDialog):
    jump_to = Signal(str)  # event_id

    def __init__(
        self,
        provider: Callable[[], tuple[list[Any], list[Any]]],
        options: QAOptions | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("QA 검사")
        self.resize(560, 480)
        self._provider = provider
        self._options = options or QAOptions()

        root = QVBoxLayout(self)
        self._summary = QLabel("")
        root.addWidget(self._summary)

        self._list = QListWidget()
        self._list.itemDoubleClicked.connect(self._on_double_click)
        root.addWidget(self._list, 1)

        btn_row = QHBoxLayout()
        self._recheck_btn = QPushButton("다시 검사")
        self._recheck_btn.clicked.connect(self.run)
        btn_row.addWidget(self._recheck_btn)
        btn_row.addStretch(1)
        close_btn = QPushButton("닫기")
        close_btn.clicked.connect(self.close)
        btn_row.addWidget(close_btn)
        root.addLayout(btn_row)

        self.run()

    def run(self) -> None:
        events, styles = self._provider()
        issues = run_checks(events, styles, self._options)
        self._summary.setText(summarize(issues))
        self._list.clear()
        for iss in issues:
            tag = _SEV_LABEL.get(iss.severity, iss.severity)
            item = QListWidgetItem(f"[{tag}] {iss.message}")
            item.setForeground(QColor(_SEV_COLOR.get(iss.severity, "#ccc")))
            item.setData(Qt.ItemDataRole.UserRole, iss.event_id)
            if iss.event_id:
                item.setToolTip("더블클릭하면 해당 줄로 이동")
            self._list.addItem(item)
        if not issues:
            self._list.addItem(QListWidgetItem("문제가 발견되지 않았습니다. ✓"))

    def _on_double_click(self, item: QListWidgetItem) -> None:
        event_id = item.data(Qt.ItemDataRole.UserRole)
        if event_id:
            self.jump_to.emit(str(event_id))
