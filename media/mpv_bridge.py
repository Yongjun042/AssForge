"""mpv bridge — embed libmpv for video playback in PySide6.

Handles: play/pause, seek, frame step, position/duration tracking,
subtitle display toggle. Falls back gracefully when mpv is missing.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QTimer, Qt, Signal, Slot, QMetaObject, Q_ARG, Qt as QtNS
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QSlider, QPushButton, QStyleOptionSlider, QStyle,
    QVBoxLayout, QWidget, QComboBox, QSizePolicy,
)

log = logging.getLogger(__name__)

try:
    import mpv as _mpv
    MPV_AVAILABLE = True
except Exception:
    MPV_AVAILABLE = False
    _mpv = None


class _ClickSlider(QSlider):
    """QSlider that jumps directly to the clicked position."""

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            opt = QStyleOptionSlider()
            self.initStyleOption(opt)
            groove = self.style().subControlRect(
                QStyle.ComplexControl.CC_Slider, opt,
                QStyle.SubControl.SC_SliderGroove, self,
            )
            if self.orientation() == Qt.Orientation.Horizontal:
                val = QStyle.sliderValueFromPosition(
                    self.minimum(), self.maximum(),
                    event.position().toPoint().x() - groove.x(), groove.width(),
                )
            else:
                val = QStyle.sliderValueFromPosition(
                    self.minimum(), self.maximum(),
                    event.position().toPoint().y() - groove.y(), groove.height(),
                    upsideDown=True,
                )
            self.setValue(val)
            self.sliderMoved.emit(val)
            event.accept()
        super().mousePressEvent(event)


class MpvPlayer(QWidget):
    """mpv-backed video player widget.

    Signals:
        position_changed(int): current position in ms
        duration_changed(int): total duration in ms
        state_changed(str): "playing" | "paused" | "idle"
    """

    position_changed = Signal(int)
    duration_changed = Signal(int)
    state_changed = Signal(str)

    # Internal cross-thread signals (mpv thread → Qt main thread)
    _sig_duration = Signal(int)
    _sig_pause_state = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._mpv = None
        self._duration_ms = 0
        self._position_ms = 0
        self._seeking = False

        self._build_ui()

        # Connect internal cross-thread signals to slots
        self._sig_duration.connect(self._handle_duration)
        self._sig_pause_state.connect(self._apply_state)

        if MPV_AVAILABLE:
            QTimer.singleShot(0, self._init_mpv)
        else:
            self._show_missing()

        self._poll = QTimer(self)
        self._poll.setInterval(40)
        self._poll.timeout.connect(self._poll_position)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(2)

        self._container = QWidget(self)
        self._container.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._container.setStyleSheet("background-color: black;")
        self._container.setMinimumHeight(120)
        root.addWidget(self._container, stretch=1)

        self._slider = _ClickSlider(Qt.Orientation.Horizontal)
        self._slider.setRange(0, 1000)
        self._slider.sliderPressed.connect(self._on_slider_pressed)
        self._slider.sliderReleased.connect(self._on_slider_released)
        root.addWidget(self._slider)

        transport = QHBoxLayout()
        transport.setContentsMargins(4, 0, 4, 2)

        self._btn_play = QPushButton("▶")
        self._btn_play.setFixedWidth(36)
        self._btn_play.setToolTip("재생/일시정지")
        self._btn_play.clicked.connect(self.toggle_play)
        transport.addWidget(self._btn_play)

        btn_prev = QPushButton("⏮")
        btn_prev.setFixedWidth(30)
        btn_prev.setToolTip("이전 프레임")
        btn_prev.clicked.connect(self.frame_back)
        transport.addWidget(btn_prev)

        btn_next = QPushButton("⏭")
        btn_next.setFixedWidth(30)
        btn_next.setToolTip("다음 프레임")
        btn_next.clicked.connect(self.frame_step)
        transport.addWidget(btn_next)

        self._lbl_time = QLabel("00:00:00.00 / 00:00:00.00")
        self._lbl_time.setMinimumWidth(180)
        transport.addWidget(self._lbl_time)

        transport.addStretch()

        transport.addWidget(QLabel("음량"))
        self._vol = QSlider(Qt.Orientation.Horizontal)
        self._vol.setRange(0, 150)
        self._vol.setValue(100)
        self._vol.setFixedWidth(80)
        self._vol.valueChanged.connect(self._set_volume)
        transport.addWidget(self._vol)

        transport.addWidget(QLabel("속도"))
        self._speed = QComboBox()
        for s in ("0.25", "0.5", "0.75", "1.0", "1.25", "1.5", "2.0"):
            self._speed.addItem(f"{s}x", float(s))
        self._speed.setCurrentIndex(3)
        self._speed.currentIndexChanged.connect(self._set_speed)
        transport.addWidget(self._speed)

        root.addLayout(transport)

    def _init_mpv(self) -> None:
        if not MPV_AVAILABLE:
            return
        try:
            wid = int(self._container.winId())
            self._mpv = _mpv.MPV(
                wid=str(wid),
                log_handler=lambda *a: None,
                ytdl=False,
                input_default_bindings=False,
                input_vo_keyboard=False,
                osc=False,
                osd_level=0,
                keep_open="yes",
                idle="yes",
            )
            # observe_property callbacks run on mpv's thread.
            # We use Qt signals (thread-safe) to marshal to the GUI thread.
            self._mpv.observe_property("duration", self._on_mpv_duration)
            self._mpv.observe_property("pause", self._on_mpv_pause)
        except Exception:
            log.exception("Failed to create mpv")
            self._mpv = None
            self._show_missing()

    def _show_missing(self) -> None:
        lbl = QLabel(
            "mpv를 사용할 수 없습니다.\npython-mpv와 libmpv를 설치하세요.\n\npython setup.py 를 실행하세요.",
            self._container,
        )
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setStyleSheet("color: #aaa; font-size: 13px;")
        QVBoxLayout(self._container).addWidget(lbl)

    # ------------------------------------------------------------------
    # mpv property observers (called from mpv thread!)
    # These MUST NOT touch Qt widgets directly. Use signals instead.
    # ------------------------------------------------------------------

    def _on_mpv_duration(self, _name: str, value: float | None) -> None:
        if value is not None:
            self._sig_duration.emit(int(value * 1000))

    def _on_mpv_pause(self, _name: str, value: bool | None) -> None:
        if value is None:
            self._sig_pause_state.emit("idle")
        elif value:
            self._sig_pause_state.emit("paused")
        else:
            self._sig_pause_state.emit("playing")

    # ------------------------------------------------------------------
    # Qt main thread handlers
    # ------------------------------------------------------------------

    @Slot(int)
    def _handle_duration(self, ms: int) -> None:
        self._duration_ms = ms
        # Refresh the in-widget time label immediately. The observer can fire
        # well after load_video's one-shot poll on a cold first load, so
        # without this the label would stay "… / 00:00:00.00" until the next
        # poll (which only happens on play/seek).
        self._lbl_time.setText(
            f"{_fmt(self._position_ms)} / {_fmt(self._duration_ms)}"
        )
        self.duration_changed.emit(ms)

    @Slot(str)
    def _apply_state(self, state: str) -> None:
        self._btn_play.setText("⏸" if state == "playing" else "▶")
        self.state_changed.emit(state)
        if state == "playing":
            if not self._poll.isActive():
                self._poll.start()
        else:
            self._poll.stop()
            self._poll_position()  # one final update

    def _poll_position(self) -> None:
        if self._mpv is None:
            return
        # Read duration directly too — don't rely solely on the observer,
        # which may not have fired yet right after a cold first load.
        try:
            dur = self._mpv.duration
        except Exception:
            dur = None
        if dur is not None:
            d_ms = int(dur * 1000)
            if d_ms != self._duration_ms:
                self._duration_ms = d_ms
                self.duration_changed.emit(d_ms)

        try:
            pos = self._mpv.time_pos
        except Exception:
            pos = None
        if pos is not None:
            self._position_ms = int(pos * 1000)
            self.position_changed.emit(self._position_ms)

        if not self._seeking:
            dur = max(self._duration_ms, 1)
            self._slider.blockSignals(True)
            self._slider.setValue(int(self._position_ms / dur * 1000))
            self._slider.blockSignals(False)

        self._lbl_time.setText(
            f"{_fmt(self._position_ms)} / {_fmt(self._duration_ms)}"
        )

    # -- Public API --
    def load_video(self, path: str) -> None:
        if self._mpv is None:
            return
        self._duration_ms = 0
        self._position_ms = 0
        self._lbl_time.setText(f"{_fmt(0)} / {_fmt(0)}")
        try:
            self._mpv.play(str(path))
            self._mpv.pause = True
        except Exception:
            log.exception("Failed to load video: %s", path)
            return
        # Duration is only known once the demuxer has parsed the file, which
        # on a cold first load can take longer than a single delayed poll.
        # Retry until duration is known (or give up after ~3s).
        self._post_load_attempts = 0
        QTimer.singleShot(100, self._poll_until_duration)

    def _poll_until_duration(self) -> None:
        if self._mpv is None:
            return
        self._poll_position()  # reads position + duration, updates label/slider
        self._post_load_attempts += 1
        if self._duration_ms <= 0 and self._post_load_attempts < 30:
            QTimer.singleShot(100, self._poll_until_duration)

    def stop(self) -> None:
        """Unload the current video and reset transport UI to zero."""
        self._poll.stop()
        self._duration_ms = 0
        self._position_ms = 0
        self._lbl_time.setText(f"{_fmt(0)} / {_fmt(0)}")
        self._slider.blockSignals(True)
        self._slider.setValue(0)
        self._slider.blockSignals(False)
        if self._mpv is not None:
            try:
                self._mpv.command("stop")
            except Exception:
                pass
        self.duration_changed.emit(0)
        self.position_changed.emit(0)

    def load_subtitle(self, path: str) -> None:
        """Load (or reload) a subtitle file into mpv."""
        if self._mpv is None:
            return
        try:
            # Remove existing subtitle tracks first
            self._mpv.command("sub-remove")
        except Exception:
            pass
        try:
            self._mpv.command("sub-add", str(path), "select")
        except Exception:
            log.exception("Failed to load subtitle: %s", path)

    def toggle_play(self) -> None:
        if self._mpv:
            try:
                self._mpv.pause = not self._mpv.pause
            except Exception:
                pass

    def seek(self, ms: int) -> None:
        if not self._mpv:
            return
        try:
            # precision="exact": 키프레임으로 스냅하지 않고 정확한 위치로 — 파형
            # 클릭/스크럽이 커서 위치와 어긋나지 않게 한다.
            self._mpv.seek(ms / 1000.0, reference="absolute", precision="exact")
        except Exception:
            # No file loaded / not seekable yet — mpv raises MPV_ERROR_COMMAND.
            return
        # Poll immediately to update UI
        QTimer.singleShot(50, self._poll_position)

    def frame_step(self) -> None:
        if not self._mpv:
            return
        try:
            self._mpv.command("frame-step")
        except Exception:
            return
        QTimer.singleShot(50, self._poll_position)

    def frame_back(self) -> None:
        if not self._mpv:
            return
        try:
            self._mpv.command("frame-back-step")
        except Exception:
            return
        QTimer.singleShot(50, self._poll_position)

    def get_position_ms(self) -> int:
        return self._position_ms

    def _on_slider_pressed(self) -> None:
        self._seeking = True
        ms = int(self._slider.value() / 1000 * self._duration_ms)
        self.seek(ms)

    def _on_slider_released(self) -> None:
        self._seeking = False
        ms = int(self._slider.value() / 1000 * self._duration_ms)
        self.seek(ms)

    def _set_volume(self, val: int) -> None:
        if self._mpv:
            self._mpv.volume = val

    def _set_speed(self, idx: int) -> None:
        speed = self._speed.itemData(idx)
        if self._mpv and speed:
            self._mpv.speed = speed

    def closeEvent(self, event) -> None:
        self._poll.stop()
        if self._mpv:
            try:
                self._mpv.terminate()
            except Exception:
                pass
            self._mpv = None
        super().closeEvent(event)


def _fmt(ms: int) -> str:
    if ms < 0:
        ms = 0
    cs = (ms // 10) % 100
    s = (ms // 1000) % 60
    m = (ms // 60_000) % 60
    h = ms // 3_600_000
    return f"{h:02d}:{m:02d}:{s:02d}.{cs:02d}"
