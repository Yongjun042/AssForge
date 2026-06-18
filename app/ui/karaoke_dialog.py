"""가라오케 타이밍 다이얼로그 — 음절 \\k 타이밍을 잡는다.

흐름:
  1. 줄을 음절로 분리(기존 \\k 가 있으면 그대로 읽어옴).
  2. 각 음절 길이(센티초)를 잡는다 — 균등 분배하거나, '재생하며 탭'으로
     오디오를 들으며 각 음절 시작점에서 Space/탭을 눌러 타이밍한다.
  3. 미리보기를 보고 적용 → '{\\kfNN}가{\\kfMM}사...' 텍스트.

core.karaoke 가 분리/분배/렌더를 담당하고 여기선 UI 만 한다.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView, QLabel,
    QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from core.karaoke.toolkit import (
    Syllable, distribute_durations, parse_karaoke, render_karaoke,
    split_syllables, total_duration_cs,
)

import re

_TAG_RE = re.compile(r"\{[^}]*\}")
_LEAD_RE = re.compile(r"^\s*\{([^}]*)\}")
_K_IN_TAG = re.compile(r"\\k[fo]?\d*|\\K\d*")


def _plain(text: str) -> str:
    return _TAG_RE.sub("", text).replace("\\N", " ").replace("\\n", " ")


def _extract_lead(text: str) -> tuple[str, bool]:
    """선두 오버라이드 블록에서 \\k 계열을 뺀 나머지 태그와, 중간(선두 외)에
    의미있는 태그가 더 있는지 여부를 반환."""
    m = _LEAD_RE.match(text)
    lead = _K_IN_TAG.sub("", m.group(1)) if m else ""
    rest = text[m.end():] if m else text
    # 중간 블록 중 \k 만 있는 게 아니면 보존되지 않음 → 경고용 플래그
    has_mid = any(_K_IN_TAG.sub("", b.strip("{}")).strip()
                  for b in _TAG_RE.findall(rest))
    return lead, has_mid


class KaraokeDialog(QDialog):
    def __init__(self, text: str, start_ms: int, end_ms: int, video_player,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("가라오케 타이밍 (\\k)")
        self.resize(520, 560)
        self._start = start_ms
        self._end = end_ms
        self._dur_ms = max(0, end_ms - start_ms)
        self._player = video_player
        self._result: str | None = None

        # 선두 오버라이드 태그(\pos/색상/굵기 등)는 보존한다 — 카라오케 렌더가
        # \k 만 내보내서 원본 태그를 잃지 않도록.
        self._lead, has_mid = _extract_lead(text)

        # 기존 카라오케가 있으면 읽고, 없으면 평문을 음절로 분리
        existing = parse_karaoke(text)
        if existing and total_duration_cs(existing) > 0:
            self._sylls = existing
        else:
            toks = split_syllables(_plain(text)) or [_plain(text) or "음절"]
            self._sylls = distribute_durations(toks, self._dur_ms, "kf")

        # 탭 상태
        self._tapping = False
        self._tap_i = 0
        self._tap_prev = start_ms

        root = QVBoxLayout(self)
        root.addWidget(QLabel(
            f"줄 길이 {self._dur_ms} ms · 음절 {len(self._sylls)}개. "
            "표에서 직접 입력하거나, 아래 '재생하며 탭'으로 들으며 타이밍하세요."
        ))
        if has_mid:
            warn = QLabel("⚠ 줄 중간의 오버라이드 태그는 카라오케 적용 시 보존되지 "
                          "않습니다 (선두 태그만 유지).")
            warn.setWordWrap(True)
            warn.setStyleSheet("color:#E0B040;")
            root.addWidget(warn)

        self._table = QTableWidget(len(self._sylls), 2)
        self._table.setHorizontalHeaderLabels(["음절", "길이(cs)"])
        self._table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch)
        self._table.verticalHeader().setVisible(False)
        self._fill_table()
        root.addWidget(self._table, 1)

        kind_row = QHBoxLayout()
        kind_row.addWidget(QLabel("종류:"))
        self._kind = QComboBox()
        self._kind.addItem("\\kf 채움 스윕", "kf")
        self._kind.addItem("\\k 즉시", "k")
        self._kind.addItem("\\ko 외곽선", "ko")
        cur_kind = self._sylls[0].kind if self._sylls else "kf"
        idx = {"kf": 0, "k": 1, "ko": 2}.get(cur_kind, 0)
        self._kind.setCurrentIndex(idx)
        self._kind.currentIndexChanged.connect(self._update_preview)
        kind_row.addWidget(self._kind)
        kind_row.addStretch(1)
        btn_even = QPushButton("균등 분배")
        btn_even.clicked.connect(self._on_even)
        kind_row.addWidget(btn_even)
        root.addLayout(kind_row)

        tap_row = QHBoxLayout()
        self._tap_start_btn = QPushButton("재생하며 탭 시작")
        self._tap_start_btn.clicked.connect(self._on_tap_start)
        tap_row.addWidget(self._tap_start_btn)
        self._tap_btn = QPushButton("탭 (Space)")
        self._tap_btn.clicked.connect(self._on_tap)
        self._tap_btn.setEnabled(False)
        tap_row.addWidget(self._tap_btn)
        self._tap_stop_btn = QPushButton("타이밍 종료")
        self._tap_stop_btn.clicked.connect(self._on_tap_stop)
        self._tap_stop_btn.setEnabled(False)
        tap_row.addWidget(self._tap_stop_btn)
        root.addLayout(tap_row)

        self._preview = QLabel()
        self._preview.setWordWrap(True)
        self._preview.setStyleSheet("color:#9cf; font-family:Consolas;")
        root.addWidget(self._preview)
        self._total_lbl = QLabel()
        root.addWidget(self._total_lbl)

        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("적용")
        bb.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        bb.accepted.connect(self._on_accept)
        bb.rejected.connect(self.reject)
        root.addWidget(bb)
        self._update_preview()

    # -- 표 --
    def _fill_table(self) -> None:
        for i, s in enumerate(self._sylls):
            item = QTableWidgetItem(s.visible or s.text)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._table.setItem(i, 0, item)
            sp = QSpinBox()
            sp.setRange(0, 100000)
            sp.setValue(s.duration_cs)
            sp.valueChanged.connect(lambda v, idx=i: self._on_dur_changed(idx, v))
            self._table.setCellWidget(i, 1, sp)

    def _on_dur_changed(self, idx: int, v: int) -> None:
        if 0 <= idx < len(self._sylls):
            self._sylls[idx].duration_cs = v
            self._update_preview()

    def _sync_spins(self) -> None:
        for i, s in enumerate(self._sylls):
            w = self._table.cellWidget(i, 1)
            if w is not None:
                w.blockSignals(True)
                w.setValue(s.duration_cs)
                w.blockSignals(False)

    # -- 동작 --
    def _on_even(self) -> None:
        toks = [s.text for s in self._sylls]
        self._sylls = distribute_durations(toks, self._dur_ms, self._kind.currentData())
        self._sync_spins()
        self._update_preview()

    def _on_tap_start(self) -> None:
        self._tapping = True
        self._tap_i = 0
        self._tap_prev = self._start
        self._tap_btn.setEnabled(True)
        self._tap_stop_btn.setEnabled(True)
        self._tap_start_btn.setEnabled(False)
        self._table.setCurrentCell(0, 0)
        if self._player is not None:
            # 끝에서 좀 더 듣도록 약간 여유
            self._player.play_range(self._start, self._end + 400)

    def _on_tap(self) -> None:
        """현재 음절의 끝을 현재 재생 위치로 — 길이 = 직전 경계~현재."""
        if not self._tapping or self._tap_i >= len(self._sylls):
            return
        pos = self._player.get_position_ms() if self._player is not None else self._end
        pos = max(self._tap_prev, pos)
        self._sylls[self._tap_i].duration_cs = max(0, round((pos - self._tap_prev) / 10))
        self._tap_prev = pos
        self._tap_i += 1
        self._sync_spins()
        self._update_preview()
        if self._tap_i < len(self._sylls):
            self._table.setCurrentCell(self._tap_i, 0)
        else:
            self._on_tap_stop()

    def _on_tap_stop(self) -> None:
        # 마지막 음절은 줄 끝까지 채운다.
        if self._tapping and self._tap_i < len(self._sylls):
            remain = max(0, round((self._end - self._tap_prev) / 10))
            self._sylls[self._tap_i].duration_cs = remain
            for s in self._sylls[self._tap_i + 1:]:
                s.duration_cs = 0
        self._tapping = False
        self._tap_btn.setEnabled(False)
        self._tap_stop_btn.setEnabled(False)
        self._tap_start_btn.setEnabled(True)
        self._sync_spins()
        self._update_preview()

    def keyPressEvent(self, ev) -> None:
        if self._tapping and ev.key() == Qt.Key.Key_Space:
            self._on_tap()
            ev.accept()
            return
        super().keyPressEvent(ev)

    def _render(self) -> str:
        """카라오케 텍스트 — 선두 오버라이드 태그를 첫 \\k 블록에 합쳐 보존."""
        rendered = render_karaoke(self._sylls, self._kind.currentData())
        if self._lead and rendered.startswith("{"):
            rendered = "{" + self._lead + rendered[1:]
        return rendered

    def _update_preview(self) -> None:
        rendered = self._render()
        self._preview.setText(rendered)
        tot = total_duration_cs(self._sylls) * 10
        self._total_lbl.setText(
            f"합계 {tot} ms / 줄 {self._dur_ms} ms"
            + ("  ✓" if abs(tot - self._dur_ms) <= 30 else "  (차이 있음)")
        )

    def _on_accept(self) -> None:
        self._result = self._render()
        self.accept()

    def result_text(self) -> str | None:
        return self._result
