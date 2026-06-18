"""영상 위 비주얼 자막 편집 (Aegisub 스타일 핸들).

mpv 는 자체 네이티브 윈도우(wid 임베딩)로 렌더링해 그 위에 Qt 위젯을 겹치기가
Windows 에서 까다롭다. 대신 현재 프레임을 스냅샷으로 떠서 QGraphicsView 에
올리고, 그 위에서 편집한다:
  · 앵커 드래그 → \\pos
  · 회전 핸들 드래그 / 스핀박스 → \\frz
  · 배율 스핀박스 → \\fscx / \\fscy
  · 색상 버튼 → \\1c (아이드로퍼 포함 QColorDialog)
  · 클립 그리기 → \\clip(x1,y1,x2,y2)
좌표는 이미지 픽셀 → PlayRes 로 환산. 적용은 변경된 속성만 써서
UpdateEventCommand(단일 undo)로 처리한다(원본 태그/애니메이션 보존).
"""
from __future__ import annotations

import math
import re

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QBrush, QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPen, QPixmap,
)
from PySide6.QtWidgets import (
    QColorDialog, QDialog, QDialogButtonBox, QDoubleSpinBox, QGraphicsItem,
    QGraphicsObject, QGraphicsScene, QGraphicsView, QHBoxLayout, QLabel,
    QPushButton, QVBoxLayout, QWidget,
)

from app.commands.bus import Command
from app.commands.edit_commands import UpdateEventCommand
from core.ass.tag_tokenizer import remove_tag, rgb_to_ass_color, upsert_tag
from core.project.project_db import ProjectDB
from core.typeset import (
    clear_clip, effective_position, get_rotation, set_clip_rect, set_position,
    set_rotation,
)

_AN_RE = re.compile(r"\\an([1-9])")
_TAG_RE = re.compile(r"\{[^}]*\}")
_MOVE_RE = re.compile(r"\\move\s*\(")
_ANIM_RE = re.compile(r"\\t\s*\(")
_FSCX_RE = re.compile(r"\\fscx(-?\d+(?:\.\d+)?)")
_FSCY_RE = re.compile(r"\\fscy(-?\d+(?:\.\d+)?)")
_C1_RE = re.compile(r"\\1?c&H([0-9A-Fa-f]{6})&")


def _plain(text: str) -> str:
    return _TAG_RE.sub("", text).replace("\\N", " ").replace("\\n", " ").strip()


def _alignment(text: str) -> int:
    m = _AN_RE.search(text)
    return int(m.group(1)) if m else 2


def _num(rx, text, default):
    m = rx.search(text)
    return float(m.group(1)) if m else default


def _initial_color(text: str) -> QColor:
    m = _C1_RE.search(text)
    if m:
        bb, gg, rr = m.group(1)[0:2], m.group(1)[2:4], m.group(1)[4:6]
        return QColor(int(rr, 16), int(gg, 16), int(bb, 16))
    return QColor("#FFFFFF")


class _AnchorItem(QGraphicsObject):
    """드래그 가능한 자막 앵커 — 회전/배율/색이 적용된 미리보기 텍스트 + 십자선."""

    moved = Signal()

    def __init__(self, text: str, alignment: int, font_px: float) -> None:
        super().__init__()
        self._text = text or "자막"
        self._align = alignment
        self._font = QFont("Malgun Gothic", max(8, int(font_px)))
        self.rotation_deg = 0.0
        self.scale_x = 100.0
        self.scale_y = 100.0
        self.color = QColor("#FFFFFF")
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsScenePositionChanges, True)
        self.setZValue(10)

    def _text_rect(self) -> QRectF:
        fm = QFontMetricsF(self._font)
        w = fm.horizontalAdvance(self._text)
        h = fm.height()
        col = (self._align - 1) % 3      # 0=left,1=center,2=right
        rowg = (self._align - 1) // 3    # 0=bottom,1=middle,2=top
        x = {0: 0.0, 1: -w / 2, 2: -w}[col]
        y = {0: -h, 1: -h / 2, 2: 0.0}[rowg]
        return QRectF(x, y, w, h)

    def boundingRect(self) -> QRectF:
        r = self._text_rect()
        w = r.width() * self.scale_x / 100.0
        h = r.height() * self.scale_y / 100.0
        diag = math.hypot(w, h)
        m = diag / 2 + 18
        return QRectF(-m, -m, 2 * m, 2 * m)

    def refresh(self) -> None:
        self.prepareGeometryChange()
        self.update()

    def paint(self, p: QPainter, _opt, _w=None) -> None:
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.save()
        # \frz 는 반시계(스크린 y 아래이므로 음각), \fscx/y 는 % 배율
        p.rotate(-self.rotation_deg)
        p.scale(self.scale_x / 100.0, self.scale_y / 100.0)
        rect = self._text_rect()
        path = QPainterPath()
        path.addText(rect.bottomLeft() + QPointF(0, -QFontMetricsF(self._font).descent()),
                     self._font, self._text)
        p.setPen(QPen(QColor(0, 0, 0, 220), 4))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)
        p.fillPath(path, QBrush(self.color))
        p.restore()
        # 앵커 십자선 (회전/배율 영향 안 받게 restore 후)
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


class _RotateHandle(QGraphicsObject):
    """앵커 둘레를 도는 회전 핸들. 드래그하면 각도를 emit."""

    rotated = Signal(float)

    def __init__(self) -> None:
        super().__init__()
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsScenePositionChanges, True)
        self.setZValue(12)
        self._center = QPointF(0, 0)
        self._radius = 60.0
        self._suppress = False

    def set_geometry(self, center: QPointF, radius: float, frz_deg: float) -> None:
        self._center = center
        self._radius = radius
        self._suppress = True
        a = math.radians(-frz_deg)
        self.setPos(center + QPointF(radius * math.cos(a), radius * math.sin(a)))
        self._suppress = False

    def boundingRect(self) -> QRectF:
        return QRectF(-8, -8, 16, 16)

    def paint(self, p: QPainter, _opt, _w=None) -> None:
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(QPen(QColor("#33CCFF"), 2))
        p.setBrush(QBrush(QColor(30, 30, 30)))
        p.drawEllipse(QPointF(0, 0), 6, 6)

    def itemChange(self, change, value):
        if (change == QGraphicsItem.GraphicsItemChange.ItemScenePositionHasChanged
                and not self._suppress):
            d = self.scenePos() - self._center
            ang = math.degrees(math.atan2(-d.y(), d.x()))
            self.rotated.emit(ang)
        return super().itemChange(change, value)


class _FitView(QGraphicsView):
    """씬을 맞춰 보여주는 뷰. 클립 모드일 땐 드래그로 사각형을 그린다."""

    clip_drawn = Signal(QRectF)

    def __init__(self, scene) -> None:
        super().__init__(scene)
        self.clip_mode = False
        self._rubber: QRectF | None = None
        self._start = QPointF()

    def resizeEvent(self, ev) -> None:
        super().resizeEvent(ev)
        if self.scene() is not None:
            self.fitInView(self.scene().sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def mousePressEvent(self, ev) -> None:
        if self.clip_mode and ev.button() == Qt.MouseButton.LeftButton:
            self._start = self.mapToScene(ev.position().toPoint())
            self._rubber = QRectF(self._start, self._start)
            return
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev) -> None:
        if self.clip_mode and self._rubber is not None:
            cur = self.mapToScene(ev.position().toPoint())
            self._rubber = QRectF(self._start, cur).normalized()
            self.viewport().update()
            return
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev) -> None:
        if self.clip_mode and self._rubber is not None:
            r = self._rubber.normalized()
            self._rubber = None
            self.clip_mode = False
            self.setCursor(Qt.CursorShape.ArrowCursor)
            if r.width() > 3 and r.height() > 3:
                self.clip_drawn.emit(r)
            self.viewport().update()
            return
        super().mouseReleaseEvent(ev)

    def drawForeground(self, p: QPainter, _rect) -> None:
        if self._rubber is not None:
            p.setPen(QPen(QColor("#33CCFF"), 0, Qt.PenStyle.DashLine))
            p.setBrush(QBrush(QColor(60, 160, 255, 40)))
            p.drawRect(self._rubber)


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
        self.setWindowTitle("영상 위에서 비주얼 편집 — 위치/회전/배율/색/클립")
        self.resize(940, 720)
        self._db = db
        self._event_id = event_id
        self._rx, self._ry = play_res
        self.result_command: Command | None = None

        row = db.conn.execute("SELECT text FROM events WHERE id=?", (event_id,)).fetchone()
        self._orig_text = row["text"] if row else ""

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
        self._view.clip_drawn.connect(self._on_clip_drawn)
        root.addWidget(self._view, 1)

        align = _alignment(self._orig_text)
        vx, vy = effective_position(self._orig_text, align, self._rx, self._ry)
        font_px = self._img_h * 0.045
        self._anchor = _AnchorItem(_plain(self._orig_text), align, font_px)
        self._anchor.rotation_deg = get_rotation(self._orig_text)
        self._anchor.scale_x = _num(_FSCX_RE, self._orig_text, 100.0)
        self._anchor.scale_y = _num(_FSCY_RE, self._orig_text, 100.0)
        self._anchor.color = _initial_color(self._orig_text)
        self._anchor.setPos(self._vid_to_img(vx, vy))
        self._anchor.moved.connect(self._on_moved)
        self._scene.addItem(self._anchor)

        self._handle = _RotateHandle()
        self._handle.rotated.connect(self._on_handle_rotated)
        self._scene.addItem(self._handle)
        self._reposition_handle()

        # 클립 미리보기 사각형
        self._clip_item = None
        self._clip_rect_img: QRectF | None = None

        # 컨트롤 행 1: 좌표/회전/배율
        ctl = QHBoxLayout()
        self._coord = QLabel("")
        ctl.addWidget(self._coord)
        ctl.addStretch(1)
        ctl.addWidget(QLabel("회전°:"))
        self._rot = self._spin(-360, 360, self._anchor.rotation_deg, 2)
        self._rot.valueChanged.connect(self._on_rot_spin)
        ctl.addWidget(self._rot)
        ctl.addWidget(QLabel("배율X%:"))
        self._sx = self._spin(1, 1000, self._anchor.scale_x, 1)
        self._sx.valueChanged.connect(self._on_scale_changed)
        ctl.addWidget(self._sx)
        ctl.addWidget(QLabel("Y%:"))
        self._sy = self._spin(1, 1000, self._anchor.scale_y, 1)
        self._sy.valueChanged.connect(self._on_scale_changed)
        ctl.addWidget(self._sy)
        root.addLayout(ctl)

        # 컨트롤 행 2: 색상/클립/위치지우기
        ctl2 = QHBoxLayout()
        self._color_btn = QPushButton("색상...")
        self._color_btn.clicked.connect(self._on_pick_color)
        ctl2.addWidget(self._color_btn)
        self._color_sw = QLabel()
        self._color_sw.setFixedSize(28, 18)
        self._update_swatch()
        ctl2.addWidget(self._color_sw)
        ctl2.addSpacing(12)
        self._clip_btn = QPushButton("사각 클립 그리기")
        self._clip_btn.clicked.connect(self._on_clip_mode)
        ctl2.addWidget(self._clip_btn)
        self._clip_clear_btn = QPushButton("클립 해제")
        self._clip_clear_btn.clicked.connect(self._on_clip_clear)
        ctl2.addWidget(self._clip_clear_btn)
        ctl2.addStretch(1)
        btn_reset = QPushButton("위치 지우기")
        btn_reset.setToolTip("\\pos 를 제거해 스타일 기본 위치로")
        btn_reset.clicked.connect(self._on_clear_pos)
        ctl2.addWidget(btn_reset)
        root.addLayout(ctl2)

        hint = QLabel(
            "앵커=위치, 파란 핸들=회전 드래그. 배율·색·클립은 컨트롤로. "
            f"좌표는 PlayRes {self._rx}×{self._ry} 기준."
        )
        hint.setStyleSheet("color:#888;")
        root.addWidget(hint)

        self._has_move = bool(_MOVE_RE.search(self._orig_text))
        if self._has_move or _ANIM_RE.search(self._orig_text):
            kind = "\\move 모션" if self._has_move else "\\t 애니메이션"
            warn = QLabel(
                f"⚠ 이 줄은 {kind}을 사용합니다. 앵커를 드래그해 적용하면 고정 "
                "위치(\\pos)로 대체됩니다. 위치를 안 바꾸려면 드래그하지 마세요."
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

        # 변경 추적 — 실제로 만진 속성만 기록(원본 태그/애니메이션 보존)
        self._clear_pos = False
        self._pos_dirty = False
        self._rot_dirty = False
        self._scale_dirty = False
        self._color_dirty = False
        self._clip_dirty = False
        self._update_label()

    # -- helpers --
    @staticmethod
    def _spin(lo, hi, val, dec) -> QDoubleSpinBox:
        s = QDoubleSpinBox()
        s.setRange(lo, hi)
        s.setDecimals(dec)
        s.setValue(val)
        return s

    def _vid_to_img(self, vx: float, vy: float) -> QPointF:
        return QPointF(vx / self._rx * self._img_w, vy / self._ry * self._img_h)

    def _img_to_vid(self, pt: QPointF) -> tuple[float, float]:
        return (pt.x() / self._img_w * self._rx, pt.y() / self._img_h * self._ry)

    def _reposition_handle(self) -> None:
        r = self._img_h * 0.14
        self._handle.set_geometry(self._anchor.pos(), r, self._anchor.rotation_deg)

    def _update_label(self) -> None:
        vx, vy = self._img_to_vid(self._anchor.pos())
        self._coord.setText(f"\\pos({vx:.0f}, {vy:.0f})  \\frz{self._anchor.rotation_deg:.0f}")

    def _update_swatch(self) -> None:
        c = self._anchor.color
        self._color_sw.setStyleSheet(
            f"background:{c.name()}; border:1px solid #888;")

    # -- 이벤트 --
    def _on_moved(self) -> None:
        self._pos_dirty = True
        self._clear_pos = False
        self._reposition_handle()
        self._update_label()

    def _on_handle_rotated(self, deg: float) -> None:
        self._anchor.rotation_deg = deg
        self._rot_dirty = True
        self._rot.blockSignals(True)
        self._rot.setValue(deg)
        self._rot.blockSignals(False)
        self._anchor.refresh()
        self._update_label()

    def _on_rot_spin(self, val: float) -> None:
        self._anchor.rotation_deg = val
        self._rot_dirty = True
        self._anchor.refresh()
        self._reposition_handle()
        self._update_label()

    def _on_scale_changed(self, _v: float) -> None:
        self._anchor.scale_x = self._sx.value()
        self._anchor.scale_y = self._sy.value()
        self._scale_dirty = True
        self._anchor.refresh()

    def _on_pick_color(self) -> None:
        c = QColorDialog.getColor(self._anchor.color, self, "글자 색상 (\\1c)")
        if c.isValid():
            self._anchor.color = c
            self._color_dirty = True
            self._update_swatch()
            self._anchor.refresh()

    def _on_clip_mode(self) -> None:
        self._view.clip_mode = True
        self._view.setCursor(Qt.CursorShape.CrossCursor)

    def _on_clip_drawn(self, rect_scene: QRectF) -> None:
        self._clip_rect_img = rect_scene
        self._clip_dirty = True
        self._draw_clip_item(rect_scene)

    def _draw_clip_item(self, rect_scene: QRectF | None) -> None:
        if self._clip_item is not None:
            self._scene.removeItem(self._clip_item)
            self._clip_item = None
        if rect_scene is not None:
            self._clip_item = self._scene.addRect(
                rect_scene, QPen(QColor("#33CCFF"), 0, Qt.PenStyle.DashLine),
                QBrush(QColor(60, 160, 255, 30)))
            self._clip_item.setZValue(5)

    def _on_clip_clear(self) -> None:
        self._clip_rect_img = None
        self._clip_dirty = True
        self._draw_clip_item(None)

    def _on_clear_pos(self) -> None:
        self._clear_pos = True
        self._pos_dirty = False
        self._coord.setText("\\pos 제거 (스타일 기본 위치)")

    def _on_accept(self) -> None:
        text = self._orig_text
        if self._clear_pos:
            text = remove_tag(text, "pos")
            text = remove_tag(text, "move")
        elif self._pos_dirty:
            vx, vy = self._img_to_vid(self._anchor.pos())
            text = set_position(text, round(vx), round(vy))
        if self._rot_dirty:
            text = set_rotation(text, round(self._anchor.rotation_deg, 2))
        if self._scale_dirty:
            text = upsert_tag(text, "fscx", _fmt_num(self._sx.value()))
            text = upsert_tag(text, "fscy", _fmt_num(self._sy.value()))
        if self._color_dirty:
            c = self._anchor.color
            text = upsert_tag(text, "1c", rgb_to_ass_color(c.red(), c.green(), c.blue()))
        if self._clip_dirty:
            if self._clip_rect_img is None:
                text = clear_clip(text)
            else:
                r = self._clip_rect_img
                x1, y1 = self._img_to_vid(r.topLeft())
                x2, y2 = self._img_to_vid(r.bottomRight())
                text = set_clip_rect(text, round(x1), round(y1), round(x2), round(y2))
        if text != self._orig_text:
            self.result_command = UpdateEventCommand(
                self._db, self._event_id, {"text": text})
        self.accept()


def _fmt_num(v: float) -> str:
    return str(int(v)) if v == int(v) else f"{v:.2f}".rstrip("0").rstrip(".")
