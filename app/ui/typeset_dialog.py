"""Visual Typesetting 다이얼로그 (수치 입력 v1).

core.typeset 지오메트리 위에 올린 얇은 UI — 선택한 한 줄의 \\pos / \\frz / \\org /
\\clip(사각) 을 수치로 편집해 UpdateEventCommand 로 적용한다(단일 undo). mpv 위
드래그/회전 핸들 오버레이는 다음 단계(WORK_STATUS 참고).
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
    QGroupBox, QVBoxLayout, QWidget,
)

from app.commands.bus import Command
from app.commands.edit_commands import UpdateEventCommand
from core.ass.tag_tokenizer import remove_tag
from core.project.project_db import ProjectDB
from core.typeset import (
    clear_clip, get_clip_rect, get_org, get_position, get_rotation,
    set_clip_rect, set_org, set_position, set_rotation,
)


def _spin(lo: float, hi: float, val: float) -> QDoubleSpinBox:
    s = QDoubleSpinBox()
    s.setRange(lo, hi)
    s.setDecimals(2)
    s.setValue(val)
    return s


class TypesetDialog(QDialog):
    def __init__(
        self,
        db: ProjectDB,
        event_id: str,
        play_res: tuple[int, int] = (1920, 1080),
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("타이프세팅 (위치/회전/클립)")
        self.resize(380, 460)
        self._db = db
        self._event_id = event_id
        self.result_command: Command | None = None

        row = db.conn.execute(
            "SELECT text FROM events WHERE id=?", (event_id,)
        ).fetchone()
        self._orig_text = row["text"] if row else ""
        rx, ry = play_res

        root = QVBoxLayout(self)

        # 위치
        pos_box = QGroupBox("위치 \\pos")
        pf = QFormLayout(pos_box)
        pos = get_position(self._orig_text) or (rx / 2, ry / 2)
        self._pos_x = _spin(-100000, 100000, pos[0])
        self._pos_y = _spin(-100000, 100000, pos[1])
        pf.addRow("X:", self._pos_x)
        pf.addRow("Y:", self._pos_y)
        root.addWidget(pos_box)

        # 회전
        rot_box = QGroupBox("회전 \\frz")
        rf = QFormLayout(rot_box)
        self._rot = _spin(-3600, 3600, get_rotation(self._orig_text))
        rf.addRow("각도(°):", self._rot)
        root.addWidget(rot_box)

        # 원점
        org = get_org(self._orig_text)
        self._org_on = QCheckBox("회전 원점 \\org 사용")
        self._org_on.setChecked(org is not None)
        root.addWidget(self._org_on)
        org_box = QGroupBox("")
        of = QFormLayout(org_box)
        self._org_x = _spin(-100000, 100000, org[0] if org else rx / 2)
        self._org_y = _spin(-100000, 100000, org[1] if org else ry / 2)
        of.addRow("원점 X:", self._org_x)
        of.addRow("원점 Y:", self._org_y)
        root.addWidget(org_box)

        # 사각 클립
        clip = get_clip_rect(self._orig_text)
        self._clip_on = QCheckBox("사각 클립 \\clip 사용")
        self._clip_on.setChecked(clip is not None)
        root.addWidget(self._clip_on)
        clip_box = QGroupBox("")
        cf = QFormLayout(clip_box)
        self._cx1 = _spin(-100000, 100000, clip[0] if clip else 0)
        self._cy1 = _spin(-100000, 100000, clip[1] if clip else 0)
        self._cx2 = _spin(-100000, 100000, clip[2] if clip else rx)
        self._cy2 = _spin(-100000, 100000, clip[3] if clip else ry)
        cf.addRow("좌상 X:", self._cx1)
        cf.addRow("좌상 Y:", self._cy1)
        cf.addRow("우하 X:", self._cx2)
        cf.addRow("우하 Y:", self._cy2)
        root.addWidget(clip_box)

        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("적용")
        bb.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        bb.accepted.connect(self._on_accept)
        bb.rejected.connect(self.reject)
        root.addWidget(bb)

    def _build_text(self) -> str:
        text = set_position(self._orig_text, self._pos_x.value(), self._pos_y.value())
        if self._rot.value() != 0 or get_rotation(self._orig_text) != 0:
            text = set_rotation(text, self._rot.value())
        if self._org_on.isChecked():
            text = set_org(text, self._org_x.value(), self._org_y.value())
        else:
            text = remove_tag(text, "org")
        if self._clip_on.isChecked():
            text = set_clip_rect(
                text, self._cx1.value(), self._cy1.value(),
                self._cx2.value(), self._cy2.value(),
            )
        else:
            text = clear_clip(text)
        return text

    def _on_accept(self) -> None:
        new_text = self._build_text()
        if new_text != self._orig_text:
            self.result_command = UpdateEventCommand(
                self._db, self._event_id, {"text": new_text}
            )
        self.accept()
