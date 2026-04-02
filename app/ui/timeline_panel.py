"""Timeline panel — waveform + subtitle blocks + position indicator."""
from __future__ import annotations

import math
from typing import Optional, Sequence

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QBrush, QColor, QPainter, QPainterPath, QPen,
    QWheelEvent, QMouseEvent, QPaintEvent, QResizeEvent, QFont,
)
from PySide6.QtWidgets import QWidget, QScrollBar, QVBoxLayout

from core.project.project_db import EventRow


class TimelinePanel(QWidget):
    """Waveform timeline with subtitle event blocks.

    Signals:
        position_clicked(int): seek to ms
        event_time_changed(str, str, int): (event_id, "start"/"end", new_ms)
    """

    position_clicked = Signal(int)
    event_time_changed = Signal(str, str, int)

    _RULER_H = 22
    _WAVE_H = 60
    _BLOCK_H = 24
    _BLOCK_Y_OFFSET = 4
    _EDGE_GRAB = 5
    _MIN_PPS = 2.0
    _MAX_PPS = 500.0

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(self._RULER_H + self._WAVE_H + self._BLOCK_H + 30)

        self._events: list[EventRow] = []
        self._peaks: np.ndarray | None = None
        self._peaks_per_sec = 100
        self._duration_ms = 10_000
        self._position_ms = 0
        self._selected: set[str] = set()
        self._keyframes: list[int] = []

        self._pps: float = 50.0  # pixels per second
        self._scroll_off: float = 0.0

        self._drag: tuple[str, str] | None = None  # (event_id, "start"/"end")
        self._drag_start_x = 0.0
        self._drag_orig_ms = 0

        self._hbar = QScrollBar(Qt.Orientation.Horizontal, self)
        self._hbar.valueChanged.connect(self._on_scroll)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addStretch()
        lay.addWidget(self._hbar)

        self.setMouseTracking(True)

    # -- Public --
    def set_events(self, events: list[EventRow]) -> None:
        self._events = list(events)
        self.update()

    def set_waveform(self, peaks: np.ndarray, pps: int = 100) -> None:
        self._peaks = peaks
        self._peaks_per_sec = pps
        self.update()

    def set_duration(self, ms: int) -> None:
        self._duration_ms = max(ms, 1)
        self._sync_scrollbar()
        self.update()

    def set_position(self, ms: int) -> None:
        self._position_ms = ms
        self.update()

    def set_selected(self, ids: set[str]) -> None:
        self._selected = ids
        self.update()

    def set_keyframes(self, keyframes: list[int]) -> None:
        self._keyframes = keyframes
        self.update()

    # -- Coordinates --
    def _ms_to_x(self, ms: int) -> float:
        return ms / 1000.0 * self._pps - self._scroll_off

    def _x_to_ms(self, x: float) -> int:
        return max(0, int((x + self._scroll_off) / self._pps * 1000))

    def _total_w(self) -> float:
        return self._duration_ms / 1000.0 * self._pps

    # -- Scrollbar --
    def _sync_scrollbar(self) -> None:
        tw = self._total_w()
        vw = self.width()
        self._hbar.blockSignals(True)
        if tw <= vw:
            self._hbar.setRange(0, 0)
        else:
            self._hbar.setRange(0, int(tw - vw))
            self._hbar.setPageStep(vw)
            self._hbar.setValue(int(self._scroll_off))
        self._hbar.blockSignals(False)

    def _on_scroll(self, val: int) -> None:
        self._scroll_off = float(val)
        self.update()

    # -- Paint --
    def paintEvent(self, ev: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        w = self.width()
        h = self.height() - self._hbar.height()

        p.fillRect(0, 0, w, h, QColor("#1A1A1A"))

        # Ruler
        self._draw_ruler(p, w)

        # Waveform
        wave_top = self._RULER_H
        wave_h = self._WAVE_H
        self._draw_waveform(p, w, wave_top, wave_h)

        # Keyframe markers
        if self._keyframes:
            p.setPen(QPen(QColor(255, 200, 50, 80), 1))
            kf_y_top = self._RULER_H
            kf_y_bot = self._RULER_H + self._WAVE_H
            for kf_ms in self._keyframes:
                kx = self._ms_to_x(kf_ms)
                if 0 <= kx <= w:
                    p.drawLine(QPointF(kx, kf_y_top), QPointF(kx, kf_y_bot))

        # Event blocks
        block_top = wave_top + wave_h + self._BLOCK_Y_OFFSET
        for ev_row in self._events:
            x1 = self._ms_to_x(ev_row.start_ms)
            x2 = self._ms_to_x(ev_row.end_ms)
            if x2 < 0 or x1 > w:
                continue
            rect = QRectF(x1, block_top, max(x2 - x1, 2), self._BLOCK_H)
            sel = ev_row.id in self._selected
            if ev_row.is_comment:
                fill = QColor("#777733") if not sel else QColor("#AAAA44")
            else:
                fill = QColor("#264F78") if not sel else QColor("#3A7FCF")
            p.fillRect(rect, fill)
            p.setPen(QPen(QColor("#88AABB"), 1))
            p.drawRect(rect)

            if rect.width() > 20:
                p.setPen(QColor("#CCC"))
                p.setFont(QFont("Segoe UI", 8))
                txt = ev_row.text[:40].replace("\\N", " ").replace("\n", " ")
                p.drawText(rect.adjusted(3, 1, -3, -1),
                          Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, txt)

        # Position line
        px = self._ms_to_x(self._position_ms)
        if 0 <= px <= w:
            p.setPen(QPen(QColor("#FF3333"), 2))
            p.drawLine(QPointF(px, 0), QPointF(px, h))

        p.end()

    def _draw_ruler(self, p: QPainter, w: int) -> None:
        p.fillRect(0, 0, w, self._RULER_H, QColor("#2D2D2D"))
        p.setFont(QFont("Segoe UI", 8))
        p.setPen(QColor("#999"))

        target_px = 100.0
        interval = target_px / self._pps
        nice = [0.1, 0.25, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600]
        for n in nice:
            if n >= interval:
                interval = n
                break
        else:
            interval = nice[-1]

        start = max(0.0, self._scroll_off / self._pps)
        t = math.floor(start / interval) * interval
        end = (self._scroll_off + w) / self._pps

        while t <= end:
            x = self._ms_to_x(int(t * 1000))
            if 0 <= x <= w:
                p.drawLine(QPointF(x, self._RULER_H - 6), QPointF(x, self._RULER_H))
                m_int, s_frac = divmod(t, 60)
                h_int, m_int = divmod(m_int, 60)
                if h_int:
                    lbl = f"{int(h_int)}:{int(m_int):02d}:{s_frac:05.2f}"
                elif m_int:
                    lbl = f"{int(m_int)}:{s_frac:05.2f}"
                else:
                    lbl = f"{s_frac:.2f}s"
                p.drawText(QPointF(x + 3, self._RULER_H - 7), lbl)
            t += interval

    def _draw_waveform(self, p: QPainter, w: int, top: int, h: int) -> None:
        if self._peaks is None or len(self._peaks) == 0:
            mid = top + h // 2
            p.setPen(QPen(QColor("#333"), 1))
            p.drawLine(0, mid, w, mid)
            return

        mid_y = top + h / 2.0
        half_h = h / 2.0

        # Build filled waveform path
        top_points = []
        bot_points = []

        for px_x in range(w):
            ms = self._x_to_ms(px_x)
            peak_idx = int(ms / 1000.0 * self._peaks_per_sec)
            if 0 <= peak_idx < len(self._peaks):
                amp = float(self._peaks[peak_idx])
            else:
                amp = 0.0
            top_points.append(QPointF(px_x, mid_y - amp * half_h))
            bot_points.append(QPointF(px_x, mid_y + amp * half_h))

        if top_points:
            path = QPainterPath()
            path.moveTo(top_points[0])
            for pt in top_points[1:]:
                path.lineTo(pt)
            for pt in reversed(bot_points):
                path.lineTo(pt)
            path.closeSubpath()

            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(QColor(60, 140, 200, 120)))
            p.drawPath(path)

        # Center line
        p.setPen(QPen(QColor("#444"), 1))
        p.drawLine(QPointF(0, mid_y), QPointF(w, mid_y))

    # -- Mouse --
    def mousePressEvent(self, ev: QMouseEvent) -> None:
        if ev.button() != Qt.MouseButton.LeftButton:
            return
        x = ev.position().x()
        y = ev.position().y()
        block_top = self._RULER_H + self._WAVE_H + self._BLOCK_Y_OFFSET

        for er in self._events:
            x1 = self._ms_to_x(er.start_ms)
            x2 = self._ms_to_x(er.end_ms)
            if block_top <= y <= block_top + self._BLOCK_H:
                if abs(x - x1) <= self._EDGE_GRAB:
                    self._drag = (er.id, "start")
                    self._drag_start_x = x
                    self._drag_orig_ms = er.start_ms
                    return
                if abs(x - x2) <= self._EDGE_GRAB:
                    self._drag = (er.id, "end")
                    self._drag_start_x = x
                    self._drag_orig_ms = er.end_ms
                    return

        self.position_clicked.emit(self._x_to_ms(x))

    def mouseMoveEvent(self, ev: QMouseEvent) -> None:
        if self._drag:
            dx = ev.position().x() - self._drag_start_x
            delta_ms = int(dx / self._pps * 1000)
            new_ms = max(0, self._drag_orig_ms + delta_ms)
            eid, edge = self._drag
            self.event_time_changed.emit(eid, edge, new_ms)
        else:
            x = ev.position().x()
            y = ev.position().y()
            block_top = self._RULER_H + self._WAVE_H + self._BLOCK_Y_OFFSET
            on_edge = False
            for er in self._events:
                x1 = self._ms_to_x(er.start_ms)
                x2 = self._ms_to_x(er.end_ms)
                if block_top <= y <= block_top + self._BLOCK_H:
                    if abs(x - x1) <= self._EDGE_GRAB or abs(x - x2) <= self._EDGE_GRAB:
                        on_edge = True
                        break
            self.setCursor(Qt.CursorShape.SizeHorCursor if on_edge else Qt.CursorShape.ArrowCursor)

    def mouseReleaseEvent(self, ev: QMouseEvent) -> None:
        self._drag = None

    def wheelEvent(self, ev: QWheelEvent) -> None:
        delta = ev.angleDelta().y()
        if ev.modifiers() & Qt.KeyboardModifier.ControlModifier:
            factor = 1.15 if delta > 0 else 1 / 1.15
            anchor_x = ev.position().x()
            old_ms = self._x_to_ms(anchor_x)
            self._pps = max(self._MIN_PPS, min(self._pps * factor, self._MAX_PPS))
            self._scroll_off = old_ms / 1000.0 * self._pps - anchor_x
            self._scroll_off = max(0, min(self._scroll_off, max(0, self._total_w() - self.width())))
            self._sync_scrollbar()
        else:
            self._scroll_off -= delta * 0.5
            self._scroll_off = max(0, min(self._scroll_off, max(0, self._total_w() - self.width())))
            self._sync_scrollbar()
        self.update()

    def resizeEvent(self, ev: QResizeEvent) -> None:
        super().resizeEvent(ev)
        self._sync_scrollbar()
