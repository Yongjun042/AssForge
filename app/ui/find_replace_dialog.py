"""찾기/바꾸기 다이얼로그 — 자막 텍스트 대상. 비모달(편집하며 띄워둠).

검색/치환 로직은 MainWindow 가 갖고, 이 다이얼로그는 입력만 모아 시그널로
넘긴다(find_next / replace_one / replace_all). 정규식·대소문자·선택영역 옵션.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QVBoxLayout, QWidget,
)


class FindReplaceDialog(QDialog):
    find_next = Signal(dict)
    replace_one = Signal(dict)
    replace_all = Signal(dict)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("찾기 / 바꾸기")
        self.setWindowFlag(Qt.WindowType.Tool, True)  # 항상 위, 비모달
        self.resize(440, 200)

        root = QVBoxLayout(self)
        grid = QGridLayout()
        grid.addWidget(QLabel("찾을 내용:"), 0, 0)
        self._find = QLineEdit()
        self._find.returnPressed.connect(self._emit_find)
        grid.addWidget(self._find, 0, 1)
        grid.addWidget(QLabel("바꿀 내용:"), 1, 0)
        self._replace = QLineEdit()
        grid.addWidget(self._replace, 1, 1)
        root.addLayout(grid)

        opts = QHBoxLayout()
        self._regex = QCheckBox("정규식")
        self._case = QCheckBox("대소문자 구분")
        self._sel_only = QCheckBox("선택 줄만")
        opts.addWidget(self._regex)
        opts.addWidget(self._case)
        opts.addWidget(self._sel_only)
        opts.addStretch(1)
        root.addLayout(opts)

        btns = QHBoxLayout()
        b_find = QPushButton("다음 찾기")
        b_find.clicked.connect(self._emit_find)
        b_one = QPushButton("바꾸기")
        b_one.clicked.connect(lambda: self.replace_one.emit(self._opts()))
        b_all = QPushButton("모두 바꾸기")
        b_all.clicked.connect(lambda: self.replace_all.emit(self._opts()))
        b_close = QPushButton("닫기")
        b_close.clicked.connect(self.close)
        for b in (b_find, b_one, b_all):
            btns.addWidget(b)
        btns.addStretch(1)
        btns.addWidget(b_close)
        root.addLayout(btns)

    def _opts(self) -> dict:
        return {
            "find": self._find.text(),
            "replace": self._replace.text(),
            "regex": self._regex.isChecked(),
            "case": self._case.isChecked(),
            "selected_only": self._sel_only.isChecked(),
        }

    def _emit_find(self) -> None:
        self.find_next.emit(self._opts())

    def showEvent(self, ev) -> None:
        super().showEvent(ev)
        self._find.setFocus()
        self._find.selectAll()
