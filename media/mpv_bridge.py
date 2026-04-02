"""mpv bridge — embed libmpv for video playback in PySide6.

Handles: play/pause, seek, frame step, position/duration tracking,
subtitle display toggle. Falls back gracefully when mpv is missing.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QSlider, QPushButton,
    QVBoxLayout, QWidget, QComboBox, QSizePolicy,
)

log = logging.getLogger(__name__)

try:
    import mpv as _mpv
    MPV_AVAILABLE = True
except Exception:
    MPV_AVAILABLE = False
    _mpv = None


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

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._mpv = None
        self._duration_ms = 0
        self._position_ms = 0
        self._seeking = False

        self._build_ui()

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

        self._slider = QSlider(Qt.Orientation.Horizontal)
        self._slider.setRange(0, 1000)
        self._slider.sliderPressed.connect(lambda: setattr(self, '_seeking', True))
        self._slider.sliderReleased.connect(self._on_slider_released)
        root.addWidget(self._slider)

        transport = QHBoxLayout()
        transport.setContentsMargins(4, 0, 4, 2)

        self._btn_play = QPushButton("\u25b6")
        self._btn_play.setFixedWidth(36)
        self._btn_play.clicked.connect(self.toggle_play)
        transport.addWidget(self._btn_play)

        btn_prev = QPushButton("\u23ee")
        btn_prev.setFixedWidth(30)
        btn_prev.clicked.connect(self.frame_back)
        transport.addWidget(btn_prev)

        btn_next = QPushButton("\u23ed")
        btn_next.setFixedWidth(30)
        btn_next.clicked.connect(self.frame_step)
        transport.addWidget(btn_next)

        self._lbl_time = QLabel("00:00:00.00 / 00:00:00.00")
        self._lbl_time.setMinimumWidth(180)
        transport.addWidget(self._lbl_time)

        transport.addStretch()

        transport.addWidget(QLabel("\uc74c\ub7c9"))
        self._vol = QSlider(Qt.Orientation.Horizontal)
        self._vol.setRange(0, 150)
        self._vol.setValue(100)
        self._vol.setFixedWidth(80)
        self._vol.valueChanged.connect(self._set_volume)
        transport.addWidget(self._vol)

        transport.addWidget(QLabel("\uc18d\ub3c4"))
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
            self._mpv.observe_property("duration", self._on_duration)
            self._mpv.observe_property("pause", self._on_pause)
        except Exception:
            log.exception("Failed to create mpv")
            self._mpv = None
            self._show_missing()

    def _show_missing(self) -> None:
        lbl = QLabel("mpv\ub97c \uc0ac\uc6a9\ud560 \uc218 \uc5c6\uc2b5\ub2c8\ub2e4.\npython-mpv\uc640 libmpv\ub97c \uc124\uce58\ud558\uc138\uc694.", self._container)
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setStyleSheet("color: #aaa; font-size: 13px;")
        QVBoxLayout(self._container).addWidget(lbl)

    def _on_duration(self, _n: str, val: float | None) -> None:
        if val is not None:
            self._duration_ms = int(val * 1000)
            QTimer.singleShot(0, lambda: self.duration_changed.emit(self._duration_ms))

    def _on_pause(self, _n: str, val: bool | None) -> None:
        state = "idle" if val is None else ("paused" if val else "playing")
        QTimer.singleShot(0, lambda s=state: self._apply_state(s))

    def _apply_state(self, state: str) -> None:
        self._btn_play.setText("\u23f8" if state == "playing" else "\u25b6")
        self.state_changed.emit(state)
        if state == "playing":
            self._poll.start()
        else:
            self._poll.stop()
            self._poll_position()

    def _poll_position(self) -> None:
        if self._mpv is None:
            return
        try:
            pos = self._mpv.time_pos
        except Exception:
            return
        if pos is None:
            return
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
        self._mpv.play(str(path))
        self._mpv.pause = True

    def toggle_play(self) -> None:
        if self._mpv:
            try:
                self._mpv.pause = not self._mpv.pause
            except Exception:
                pass

    def seek(self, ms: int) -> None:
        if self._mpv:
            self._mpv.seek(ms / 1000.0, reference="absolute")

    def frame_step(self) -> None:
        if self._mpv:
            self._mpv.command("frame-step")

    def frame_back(self) -> None:
        if self._mpv:
            self._mpv.command("frame-back-step")

    def get_position_ms(self) -> int:
        return self._position_ms

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
