"""영상 위 비주얼 자막 편집 — 메인 화면에 끼우는 인라인 패널 (Aegisub 스타일 핸들).

mpv 는 자체 네이티브 윈도우(wid 임베딩)로 렌더링해 그 위에 Qt 위젯을 겹치기가
Windows 에서 까다롭다. 그래서 편집 모드에 들어가면 영상 영역을 이 패널(현재
프레임 스냅샷 + 핸들)로 전환하고, 적용/닫기 하면 다시 라이브 영상으로 돌아간다:
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

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QBrush, QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPen, QPixmap,
)
from PySide6.QtWidgets import (
    QColorDialog, QDoubleSpinBox, QGraphicsItem, QGraphicsObject,
    QGraphicsScene, QGraphicsView, QHBoxLayout, QLabel, QPushButton,
    QVBoxLayout, QWidget,
)

from app.commands.edit_commands import UpdateEventCommand
from core.ass.tag_tokenizer import (
    ass_color_to_rgb, find_tag, remove_tag, rgb_to_ass_color, strip_tags,
    upsert_tag,
)
from core.project.project_db import ProjectDB
from core.typeset import (
    clear_clip, effective_position, get_rotation, set_clip_rect, set_position,
    set_rotation,
)

# 태그 읽기는 전부 tag_tokenizer 기반 — 정규식으로 원문을 훑으면 \t(...) 인자
# 속 태그를 정적 값으로 오독한다 (CLAUDE.md: ASS 태그 작업은 토크나이저에 근거).

# SSA 레거시 \a → \an 넘패드 변환 (docs/ass-format-reference.md: 1–3=하단,
# 5–7=상단, 9–11=중단)
_LEGACY_A_TO_AN = {1: 1, 2: 2, 3: 3, 5: 7, 6: 8, 7: 9, 9: 4, 10: 5, 11: 6}


def _plain(text: str) -> str:
    return strip_tags(text).strip()


def _tag_num(text: str, name: str, default: float) -> float:
    t = find_tag(text, name)
    if t is None:
        return default
    try:
        return float(t.args.strip() or default)
    except (TypeError, ValueError):
        return default


def _alignment(text: str) -> int:
    an = find_tag(text, "an")
    if an is not None:
        try:
            v = int(an.args.strip())
            if 1 <= v <= 9:
                return v
        except (TypeError, ValueError):
            pass
    a = find_tag(text, "a")
    if a is not None:
        try:
            return _LEGACY_A_TO_AN.get(int(a.args.strip()), 2)
        except (TypeError, ValueError):
            pass
    return 2


def _initial_color(text: str) -> QColor:
    t = find_tag(text, "1c") or find_tag(text, "c")
    if t is not None and t.args.strip():
        r, g, b = ass_color_to_rgb(t.args)
        return QColor(r, g, b)
    return QColor("#FFFFFF")


def _fmt_num(v: float) -> str:
    return str(int(v)) if v == int(v) else f"{v:.2f}".rstrip("0").rstrip(".")


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
        col = (self._align - 1) % 3
        rowg = (self._align - 1) // 3
        x = {0: 0.0, 1: -w / 2, 2: -w}[col]
        y = {0: -h, 1: -h / 2, 2: 0.0}[rowg]
        return QRectF(x, y, w, h)

    def boundingRect(self) -> QRectF:
        r = self._text_rect()
        w = r.width() * self.scale_x / 100.0
        h = r.height() * self.scale_y / 100.0
        m = math.hypot(w, h) / 2 + 18
        return QRectF(-m, -m, 2 * m, 2 * m)

    def refresh(self) -> None:
        self.prepareGeometryChange()
        self.update()

    def paint(self, p: QPainter, _opt, _w=None) -> None:
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.save()
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


class VideoEditPanel(QWidget):
    """메인 화면 영상 영역에 끼우는 인라인 비주얼 편집 패널.

    load() 로 특정 이벤트/프레임을 올리고, 적용 시 committed(Command) 을,
    닫기 시 closed() 를 emit 한다. MainWindow 가 영상 스택과 전환한다.
    """

    committed = Signal(object)  # UpdateEventCommand
    closed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._db: ProjectDB | None = None
        self._event_id: str | None = None
        self._rx, self._ry = 1920, 1080
        self._orig_text = ""
        self._anchor: _AnchorItem | None = None
        self._handle: _RotateHandle | None = None
        self._clip_item = None
        self._clip_rect_img: QRectF | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        self._scene = QGraphicsScene(self)
        self._view = _FitView(self._scene)
        self._view.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        self._view.clip_drawn.connect(self._on_clip_drawn)
        root.addWidget(self._view, 1)

        # 컨트롤 행 1: 좌표/회전/배율
        ctl = QHBoxLayout()
        self._coord = QLabel("")
        ctl.addWidget(self._coord)
        ctl.addStretch(1)
        ctl.addWidget(QLabel("회전°:"))
        self._rot = self._spin(-360, 360, 0, 2)
        self._rot.valueChanged.connect(self._on_rot_spin)
        ctl.addWidget(self._rot)
        ctl.addWidget(QLabel("배율X%:"))
        self._sx = self._spin(1, 1000, 100, 1)
        self._sx.valueChanged.connect(self._on_scale_changed)
        ctl.addWidget(self._sx)
        ctl.addWidget(QLabel("Y%:"))
        self._sy = self._spin(1, 1000, 100, 1)
        self._sy.valueChanged.connect(self._on_scale_changed)
        ctl.addWidget(self._sy)
        root.addLayout(ctl)

        # 컨트롤 행 2: 색상/클립/적용/닫기
        ctl2 = QHBoxLayout()
        cbtn = QPushButton("색상...")
        cbtn.clicked.connect(self._on_pick_color)
        ctl2.addWidget(cbtn)
        self._color_sw = QLabel()
        self._color_sw.setFixedSize(28, 18)
        ctl2.addWidget(self._color_sw)
        ctl2.addSpacing(10)
        clip_btn = QPushButton("사각 클립 그리기")
        clip_btn.clicked.connect(self._on_clip_mode)
        ctl2.addWidget(clip_btn)
        clip_clear = QPushButton("클립 해제")
        clip_clear.clicked.connect(self._on_clip_clear)
        ctl2.addWidget(clip_clear)
        reset = QPushButton("위치 지우기")
        reset.clicked.connect(self._on_clear_pos)
        ctl2.addWidget(reset)
        ctl2.addStretch(1)
        self._warn = QLabel("")
        self._warn.setStyleSheet("color:#E0B040;")
        ctl2.addWidget(self._warn)
        apply_btn = QPushButton("적용")
        apply_btn.setStyleSheet("background:#2A7A35;")
        apply_btn.clicked.connect(self._on_apply)
        ctl2.addWidget(apply_btn)
        close_btn = QPushButton("닫기")
        close_btn.clicked.connect(lambda: self.closed.emit())
        ctl2.addWidget(close_btn)
        root.addLayout(ctl2)

    def current_event_id(self) -> str | None:
        """지금 편집 중인 이벤트 id — MainWindow 가 커밋 후 UI 갱신에 사용."""
        return self._event_id

    @staticmethod
    def _spin(lo, hi, val, dec) -> QDoubleSpinBox:
        s = QDoubleSpinBox()
        s.setRange(lo, hi)
        s.setDecimals(dec)
        s.setValue(val)
        return s

    # -- 특정 이벤트/프레임 로드 --
    def load(self, db: ProjectDB, event_id: str, frame_path: str | None,
             play_res: tuple[int, int]) -> None:
        self._db = db
        self._event_id = event_id
        self._rx, self._ry = play_res
        ev = db.get_event(event_id)
        self._orig_text = ev.text if ev else ""
        # 이전 세션에서 '클립 그리기'만 누르고 닫았을 때 armed 상태가 새 세션의
        # 첫 드래그를 클립으로 오인하지 않도록 리셋.
        self._view.clip_mode = False
        self._view.setCursor(Qt.CursorShape.ArrowCursor)

        pix = QPixmap(frame_path) if frame_path else QPixmap()
        if pix.isNull():
            pix = QPixmap(self._rx, self._ry)
            pix.fill(QColor("#222"))
        # 0 나눗셈 방어 — 손상된 프레임/비정상 PlayRes 에도 좌표 환산이 죽지 않게.
        self._img_w, self._img_h = max(1, pix.width()), max(1, pix.height())

        self._scene.clear()
        self._clip_item = None
        self._clip_rect_img = None
        self._scene.setSceneRect(0, 0, self._img_w, self._img_h)
        self._scene.addPixmap(pix)

        align = _alignment(self._orig_text)
        vx, vy = effective_position(self._orig_text, align, self._rx, self._ry)
        self._anchor = _AnchorItem(_plain(self._orig_text), align, self._img_h * 0.045)
        self._anchor.rotation_deg = get_rotation(self._orig_text)
        self._anchor.scale_x = _tag_num(self._orig_text, "fscx", 100.0)
        self._anchor.scale_y = _tag_num(self._orig_text, "fscy", 100.0)
        self._anchor.color = _initial_color(self._orig_text)
        self._anchor.setPos(self._vid_to_img(vx, vy))
        self._anchor.moved.connect(self._on_moved)
        self._scene.addItem(self._anchor)

        self._handle = _RotateHandle()
        self._handle.rotated.connect(self._on_handle_rotated)
        self._scene.addItem(self._handle)
        self._reposition_handle()

        # 컨트롤 값 세팅 (신호 막고 — dirty 로 잡히지 않게)
        for w, v in ((self._rot, self._anchor.rotation_deg),
                     (self._sx, self._anchor.scale_x), (self._sy, self._anchor.scale_y)):
            w.blockSignals(True)
            w.setValue(v)
            w.blockSignals(False)
        self._update_swatch()

        self._clear_pos = False
        self._pos_dirty = False
        self._rot_dirty = False
        self._scale_dirty = False
        self._color_dirty = False
        self._clip_dirty = False
        has_anim = bool(find_tag(self._orig_text, "move")
                        or find_tag(self._orig_text, "t"))
        self._warn.setText("⚠ \\move/\\t 애니메이션 — 드래그 적용 시 고정 위치로 대체됨"
                           if has_anim else "")
        self._view.fitInView(self._scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)
        self._update_label()

    # -- 좌표 --
    def _vid_to_img(self, vx: float, vy: float) -> QPointF:
        return QPointF(vx / self._rx * self._img_w, vy / self._ry * self._img_h)

    def _img_to_vid(self, pt: QPointF) -> tuple[float, float]:
        return (pt.x() / self._img_w * self._rx, pt.y() / self._img_h * self._ry)

    def _reposition_handle(self) -> None:
        if self._anchor and self._handle:
            self._handle.set_geometry(self._anchor.pos(), self._img_h * 0.14,
                                      self._anchor.rotation_deg)

    def _update_label(self) -> None:
        if not self._anchor:
            return
        vx, vy = self._img_to_vid(self._anchor.pos())
        self._coord.setText(
            f"\\pos({vx:.0f}, {vy:.0f})  \\frz{self._anchor.rotation_deg:.0f}")

    def _update_swatch(self) -> None:
        if self._anchor:
            self._color_sw.setStyleSheet(
                f"background:{self._anchor.color.name()}; border:1px solid #888;")

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
        if not self._anchor:
            return
        self._anchor.rotation_deg = val
        self._rot_dirty = True
        self._anchor.refresh()
        self._reposition_handle()
        self._update_label()

    def _on_scale_changed(self, _v: float) -> None:
        if not self._anchor:
            return
        self._anchor.scale_x = self._sx.value()
        self._anchor.scale_y = self._sy.value()
        self._scale_dirty = True
        self._anchor.refresh()

    def _on_pick_color(self) -> None:
        if not self._anchor:
            return
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

    def _on_apply(self) -> None:
        if self._db is None or self._anchor is None:
            self.closed.emit()
            return
        # 패널이 열려 있는 동안 메인 창은 계속 조작 가능하다(undo/바꾸기/인스펙터).
        # load() 시점 스냅샷이 아니라 지금 DB 의 텍스트를 베이스로 태그를 얹어야
        # 그 사이의 편집을 덮어쓰지 않는다. 줄이 삭제됐으면 조용히 닫는다.
        try:
            cur = self._db.get_event(self._event_id) if self._event_id else None
        except Exception:  # 닫힌/교체된 DB — 프로젝트가 바뀐 경우
            cur = None
        if cur is None:
            self._warn.setText("⚠ 편집하던 줄이 삭제되어 적용할 수 없습니다.")
            self.closed.emit()
            return
        base_text = cur.text
        text = base_text
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
        if text != base_text:
            self.committed.emit(
                UpdateEventCommand(self._db, self._event_id, {"text": text}))
        else:
            self.closed.emit()
