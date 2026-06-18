"""조건으로 줄 선택 — 텍스트/스타일/시간범위/종류로 매칭해 그리드 선택을 만든다.

매칭 자체는 MainWindow 가 수행하고, 이 다이얼로그는 기준(criteria)만 모은다.
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QRadioButton, QVBoxLayout, QWidget,
)


class SelectLinesDialog(QDialog):
    def __init__(self, styles: list[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("줄 선택")
        self.resize(420, 360)

        root = QVBoxLayout(self)
        form = QFormLayout()

        self._text = QLineEdit()
        self._text.setPlaceholderText("비우면 텍스트 조건 없음")
        form.addRow("텍스트 포함:", self._text)
        opt = QHBoxLayout()
        self._regex = QCheckBox("정규식")
        self._case = QCheckBox("대소문자 구분")
        opt.addWidget(self._regex)
        opt.addWidget(self._case)
        opt.addStretch(1)
        form.addRow("", self._wrap(opt))

        self._style = QComboBox()
        self._style.addItem("— 무관 —", "")
        for s in styles:
            self._style.addItem(s, s)
        form.addRow("스타일:", self._style)

        self._kind = QComboBox()
        self._kind.addItem("전체", "any")
        self._kind.addItem("대사만", "dialogue")
        self._kind.addItem("주석만", "comment")
        form.addRow("종류:", self._kind)
        root.addLayout(form)

        # 시간 범위
        self._time_on = QCheckBox("시간 범위로 제한 (초)")
        root.addWidget(self._time_on)
        tbox = QGroupBox("")
        tf = QFormLayout(tbox)
        self._tmin = QDoubleSpinBox()
        self._tmin.setRange(0, 360000)
        self._tmin.setDecimals(2)
        self._tmax = QDoubleSpinBox()
        self._tmax.setRange(0, 360000)
        self._tmax.setDecimals(2)
        self._tmax.setValue(360000)
        tf.addRow("시작 ≥:", self._tmin)
        tf.addRow("종료 ≤:", self._tmax)
        root.addWidget(tbox)
        self._time_on.toggled.connect(tbox.setEnabled)
        tbox.setEnabled(False)

        # 선택 방식
        mbox = QGroupBox("선택 방식")
        ml = QHBoxLayout(mbox)
        self._mode = QButtonGroup(self)
        self._rb_new = QRadioButton("새 선택")
        self._rb_add = QRadioButton("선택에 추가")
        self._rb_sub = QRadioButton("선택에서 빼기")
        self._rb_new.setChecked(True)
        for rb in (self._rb_new, self._rb_add, self._rb_sub):
            self._mode.addButton(rb)
            ml.addWidget(rb)
        root.addWidget(mbox)

        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("선택")
        bb.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        root.addWidget(bb)

    @staticmethod
    def _wrap(layout) -> QWidget:
        w = QWidget()
        w.setLayout(layout)
        return w

    def criteria(self) -> dict:
        mode = "new"
        if self._rb_add.isChecked():
            mode = "add"
        elif self._rb_sub.isChecked():
            mode = "subtract"
        crit = {
            "text": self._text.text().strip(),
            "regex": self._regex.isChecked(),
            "case": self._case.isChecked(),
            "style": self._style.currentData() or "",
            "kind": self._kind.currentData(),
            "mode": mode,
            "min_ms": None,
            "max_ms": None,
        }
        if self._time_on.isChecked():
            crit["min_ms"] = int(self._tmin.value() * 1000)
            crit["max_ms"] = int(self._tmax.value() * 1000)
        return crit
