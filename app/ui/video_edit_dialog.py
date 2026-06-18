"""영상 위 비주얼 자막 편집 (Aegisub 스타일 위치/회전 핸들).

mpv 는 자체 네이티브 윈도우(wid 임베딩)로 렌더링해 그 위에 Qt 위젯을 겹치기가
Windows 에서 까다롭다. 대신 현재 프레임을 스냅샷으로 떠서 QGraphicsView 에
올리고, 그 위에서 자막 앵커를 드래그해 \\pos 를(회전은 \\frz) 잡는다. 좌표는
이미지 픽셀 → PlayRes 로 환산한다. 적용은 core.typeset 으로 \\pos/\\frz 를 써서
UpdateEventCommand(단일 undo)로 처리한다.
"""
from __future__ import annotations

import re

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QBrush, QColor, QFont, QPainter, QPainterPath, QPen, QPixmap,
)
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QDoubleSpinBox, QGraphicsItem, QGraphicsObject,
    QGraphicsScene, QGraphicsView, QHBoxLayout, QLabel, QPushButton, QVBoxLayout,
    QWidget,
)

from app.commands.bus import Command
from app.commands.edit_commands import UpdateEventCommand
from core.ass.tag_tokenizer import remove_tag
from core.project.project_db import ProjectDB
from core.typeset import (
    effective_position, get_position, get_rotation, set_position, set_rotation,
)

_AN_RE = re.compile(r"\\an([1-9])")
_TAG_RE = re.compile(r"\{[^}]*\}")
_MOVE_RE = re.compile(r"\\move\s*\(")
_ANIM_RE = re.compile(r"\\t\s*\(")


def _plain(text: str) -> str:
    return _TAG_RE.sub("", text).replace("\\N", " ").replace("\\n", " ").strip()


def _alignment(text: str) -> int:
    m = _AN_RE.search(text)
    return int(m.group(1)) if m else 2


class _AnchorItem(QGraphicsObject):
    """드래그 가능한 자막 앵커 — 십자선 + 정렬에 맞춰 배치된 미리보기 텍스트."""

    moved = Signal()

    def __init__(self, text: str, alignment: int, font_px: float) -> None:
        super().__init__()
        self._text = text or "자막"
        self._align = alignment
        self._font = QFont("Malgun Gothic", max(8, int(font_px)))
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsScenePositionChanges, True)
        self.setZValue(10)

    def _text_rect(self) -> QRectF:
        from PySide6.QtGui import QFontMetricsF
        fm = QFontMetricsF(self._font)
        w = fm.horizontalAdvance(self._text)
        h = fm.height()
        # 앵커(0,0) 기준 정렬별 텍스트 박스 위치 (libass \an 규약)
        col = (self._align - 1) % 3      # 0=left,1=center,2=right
        rowg = (self._align - 1) // 3    # 0=bottom,1=middle,2=top
        x = {0: 0.0, 1: -w / 2, 2: -w}[col]
        y = {0: -h, 1: -h / 2, 2: 0.0}[rowg]
        return QRectF(x, y, w, h)

    def boundingRect(self) -> QRectF:
        return self._text_rect().adjusted(-14, -14, 14, 14)

    def paint(self, p: QPainter, _opt, _w=None) -> None:
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        # 미리보기 텍스트 (외곽선 + 밝은 글자)
        rect = self._text_rect()
        path = QPainterPath()
        path.addText(rect.bottomLeft() + QPointF(0, -p.fontMetrics().descent()),
                     self._font, self._text)
        p.setFont(self._font)
        p.setPen(QPen(QColor(0, 0, 0, 220), 4))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)
        p.fillPath(path, QBrush(QColor("#FFFFFF")))
        # 앵커 십자선
        p.setPen(QPen(QColor("#FF4444"), 2))
        p.drawLine(QPointF(-9, 0), QPointF(9, 0))
        p.drawLine(QPointF(0, -9), QPointF(0, 9))
        p.setPen(QPen(QColor("#FFCC00"), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QPointF(0, 0), 4, 4)

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemScenePositionHasChanged:
            self.moved.emit()
        return super().itemChange(change, value)


class _FitView(QGraphicsView):
    """씬을 항상 맞춰 보여주는 뷰 (리사이즈 시 fit)."""

    def resizeEvent(self, ev) -> None:
        super().resizeEvent(ev)
        if self.scene() is not None:
            self.fitInView(self.scene().sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)


class VideoEditDialog(QDialog):
    def __init__(
        self,
        db: ProjectDB,
        event_id: str,
        frame_path: str | None,
        play_res: tuple[int, int],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("영상 위에서 위치 편집 — 드래그로 \\pos, 회전은 \\frz")
        self.resize(900, 640)
        self._db = db
        self._event_id = event_id
        self._rx, self._ry = play_res
        self.result_command: Command | None = None

        row = db.conn.execute("SELECT text FROM events WHERE id=?", (event_id,)).fetchone()
        self._orig_text = row["text"] if row else ""

        # 배경 프레임 — 없으면 PlayRes 비율의 회색 캔버스
        self._pix = QPixmap(frame_path) if frame_path else QPixmap()
        if self._pix.isNull():
            self._pix = QPixmap(self._rx, self._ry)
            self._pix.fill(QColor("#222"))
        self._img_w = self._pix.width()
        self._img_h = self._pix.height()

        root = QVBoxLayout(self)

        self._scene = QGraphicsScene(self)
        self._scene.setSceneRect(0, 0, self._img_w, self._img_h)
        self._scene.addPixmap(self._pix)
        self._view = _FitView(self._scene)
        self._view.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        root.addWidget(self._view, 1)

        # 초기 앵커: \pos 가 있으면 그 값, 없으면 정렬 기준 기본 위치
        align = _alignment(self._orig_text)
        vx, vy = effective_position(self._orig_text, align, self._rx, self._ry)
        font_px = self._img_h * 0.045
        self._anchor = _AnchorItem(_plain(self._orig_text), align, font_px)
        self._anchor.setPos(self._vid_to_img(vx, vy))
        self._anchor.moved.connect(self._on_moved)
        self._scene.addItem(self._anchor)

        # 컨트롤 행
        ctl = QHBoxLayout()
        self._coord = QLabel("")
        ctl.addWidget(self._coord)
        ctl.addStretch(1)
        ctl.addWidget(QLabel("회전°:"))
        self._rot = QDoubleSpinBox()
        self._rot.setRange(-360, 360)
        self._rot.setDecimals(2)
        self._rot.setValue(get_rotation(self._orig_text))
        # 사용자가 실제로 바꿨을 때만 기록 — 스핀박스 반올림이 원본 \frz 정밀도를
        # 무음으로 깎지 않도록 (connect 는 초기 setValue 이후라 그건 무시된다).
        self._rot_dirty = False
        self._rot.valueChanged.connect(self._on_rot_changed)
        ctl.addWidget(self._rot)
        btn_reset = QPushButton("위치 지우기")
        btn_reset.setToolTip("\\pos 를 제거해 스타일 기본 위치로 되돌림")
        btn_reset.clicked.connect(self._on_clear_pos)
        ctl.addWidget(btn_reset)
        root.addLayout(ctl)

        hint = QLabel(
            "자막 앵커를 드래그해 위치를 잡으세요. "
            f"좌표는 PlayRes {self._rx}×{self._ry} 기준으로 저장됩니다."
        )
        hint.setStyleSheet("color:#888;")
        root.addWidget(hint)

        # 이 줄이 이미 모션/애니메이션 위치를 갖고 있으면 경고 — 드래그 적용이
        # \move 를 고정 \pos 로 대체하므로 사용자가 모르게 데이터를 잃지 않도록.
        self._has_move = bool(_MOVE_RE.search(self._orig_text))
        if self._has_move or _ANIM_RE.search(self._orig_text):
            kind = "\\move 모션" if self._has_move else "\\t 애니메이션"
            warn = QLabel(
                f"⚠ 이 줄은 {kind}을 사용합니다. 앵커를 드래그해 적용하면 "
                "고정 위치(\\pos)로 대체되어 모션이 사라집니다. "
                "위치를 바꾸지 않으려면 드래그하지 말고 적용하세요."
            )
            warn.setWordWrap(True)
            warn.setStyleSheet("color:#E0B040;")
            root.addWidget(warn)

        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("적용")
        bb.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        bb.accepted.connect(self._on_accept)
        bb.rejected.connect(self.reject)
        root.addWidget(bb)

        # 사용자가 실제로 앵커를 드래그했을 때만 \pos 를 쓴다 — 그냥 열어보고
        # 적용해도 정렬/모션 기반 위치가 고정 좌표로 바뀌지 않도록.
        self._clear_pos = False
        self._pos_dirty = False
        self._rot_dirty = False
        self._update_label()

    # -- 좌표 변환 (이미지 픽셀 ↔ PlayRes) --
    def _vid_to_img(self, vx: float, vy: float) -> QPointF:
        return QPointF(vx / self._rx * self._img_w, vy / self._ry * self._img_h)

    def _img_to_vid(self, pt: QPointF) -> tuple[float, float]:
        return (pt.x() / self._img_w * self._rx, pt.y() / self._img_h * self._ry)

    def _update_label(self) -> None:
        vx, vy = self._img_to_vid(self._anchor.pos())
        self._coord.setText(f"\\pos({vx:.0f}, {vy:.0f})")

    def _on_moved(self) -> None:
        self._pos_dirty = True   # 사용자가 실제로 드래그함
        self._clear_pos = False
        self._update_label()

    def _on_clear_pos(self) -> None:
        self._clear_pos = True
        self._pos_dirty = False
        self._coord.setText("\\pos 제거 (스타일 기본 위치)")

    def _on_rot_changed(self, _val: float) -> None:
        self._rot_dirty = True

    def _on_accept(self) -> None:
        text = self._orig_text
        # 위치는 명시적 의도가 있을 때만 건드린다: 드래그했거나(=_pos_dirty)
        # '위치 지우기'를 눌렀을 때만. 안 그러면 \move/정렬 위치를 보존.
        if self._clear_pos:
            text = remove_tag(text, "pos")
            text = remove_tag(text, "move")
        elif self._pos_dirty:
            vx, vy = self._img_to_vid(self._anchor.pos())
            text = set_position(text, round(vx), round(vy))
        # 회전은 사용자가 스핀박스를 실제로 바꿨을 때만 — 안 그러면 반올림된
        # 값이 원본 \frz 를 무음으로 덮어쓴다. 애니메이션 \t(\frz) 도 보존.
        if self._rot_dirty:
            text = set_rotation(text, self._rot.value())
        if text != self._orig_text:
            self.result_command = UpdateEventCommand(
                self._db, self._event_id, {"text": text}
            )
        self.accept()
