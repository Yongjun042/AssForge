"""자동 효과 연출 다이얼로그 — 모션그래픽풍 효과를 전체/선택 줄에 자동 배정.

두 모드:
  · 테마(오프라인): effects.director 의 결정적 사이클 — LLM 없이 즉시.
  · LLM 연출: ai.effect_director — 가사 분위기를 보고 줄별 배정 (백그라운드).

미리보기 표에서 결과를 확인한 뒤 '적용'하면 result_updates() 로
[(event_id, new_text)] 를 돌려주고, MainWindow 가 BulkUpdateTextsCommand
(단일 undo)로 반영한다.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup, QComboBox, QDialog, QDialogButtonBox, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QPushButton, QRadioButton, QSlider,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from app.ui._llm_worker import LLMTaskRunner
from core.ass.tag_tokenizer import strip_tags
from effects import EffectContext, apply_specs
from effects.director import DirectedLine, LineInput, direct_effects, theme_names


class AutoFxDialog(QDialog):
    def __init__(self, lines: list[LineInput], selected_ids: set[str],
                 play_res: tuple[int, int],
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("자동 효과 연출 (모션그래픽)")
        self.resize(760, 620)
        self._lines = lines
        self._selected_ids = selected_ids
        self._play_res = play_res
        self._updates: list[tuple[str, str]] = []
        self._runner = LLMTaskRunner(self)
        self._runner.done.connect(self._on_llm_done)

        root = QVBoxLayout(self)

        # -- 범위 --
        scope_row = QHBoxLayout()
        scope_row.addWidget(QLabel("범위:"))
        self._scope_all = QRadioButton(f"전체 ({len(lines)}줄)")
        self._scope_sel = QRadioButton(f"선택한 줄 ({len(selected_ids)}줄)")
        self._scope_all.setChecked(True)
        if not selected_ids:
            self._scope_sel.setEnabled(False)
        g1 = QButtonGroup(self)
        g1.addButton(self._scope_all)
        g1.addButton(self._scope_sel)
        scope_row.addWidget(self._scope_all)
        scope_row.addWidget(self._scope_sel)
        scope_row.addStretch(1)
        root.addLayout(scope_row)

        # -- 모드 --
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("모드:"))
        self._mode_theme = QRadioButton("테마 (오프라인·즉시)")
        self._mode_llm = QRadioButton("LLM 연출 (가사 분위기 분석)")
        self._mode_theme.setChecked(True)
        g2 = QButtonGroup(self)
        g2.addButton(self._mode_theme)
        g2.addButton(self._mode_llm)
        mode_row.addWidget(self._mode_theme)
        mode_row.addWidget(self._mode_llm)
        mode_row.addStretch(1)
        root.addLayout(mode_row)

        # -- 테마 옵션 --
        theme_row = QHBoxLayout()
        theme_row.addWidget(QLabel("테마:"))
        self._theme = QComboBox()
        for key, label in theme_names():
            self._theme.addItem(label, key)
        theme_row.addWidget(self._theme, 1)
        theme_row.addWidget(QLabel("강도:"))
        self._intensity = QSlider(Qt.Orientation.Horizontal)
        self._intensity.setRange(50, 150)
        self._intensity.setValue(100)
        self._intensity.setFixedWidth(140)
        theme_row.addWidget(self._intensity)
        self._intensity_lbl = QLabel("100%")
        self._intensity.valueChanged.connect(
            lambda v: self._intensity_lbl.setText(f"{v}%"))
        theme_row.addWidget(self._intensity_lbl)
        root.addLayout(theme_row)

        # -- LLM 옵션 --
        llm_row = QHBoxLayout()
        llm_row.addWidget(QLabel("연출 지시(선택):"))
        self._mood = QLineEdit()
        self._mood.setPlaceholderText(
            "예: 후렴은 화려하게, 조용한 구간은 은은하게 / 네온 사이버펑크 느낌")
        llm_row.addWidget(self._mood, 1)
        root.addLayout(llm_row)

        def _sync_mode() -> None:
            theme_on = self._mode_theme.isChecked()
            self._theme.setEnabled(theme_on)
            self._intensity.setEnabled(theme_on)
            self._mood.setEnabled(not theme_on)
        self._mode_theme.toggled.connect(_sync_mode)
        _sync_mode()

        # -- 미리보기 --
        pv_row = QHBoxLayout()
        self._preview_btn = QPushButton("미리보기 생성")
        self._preview_btn.clicked.connect(self._on_preview)
        pv_row.addWidget(self._preview_btn)
        self._status = QLabel("")
        self._status.setStyleSheet("color:#9aa0a6;")
        pv_row.addWidget(self._status, 1)
        root.addLayout(pv_row)

        self._table = QTableWidget(0, 3)
        self._table.setHorizontalHeaderLabels(["줄 텍스트", "연출", "결과 태그(앞부분)"])
        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        root.addWidget(self._table, 1)

        hint = QLabel(
            "효과는 각 줄 맨 앞에 태그 블록으로 추가됩니다(기존 태그/텍스트 보존). "
            "카라오케(\\k) 줄은 은은한 효과만 적용됩니다. 적용 후 Ctrl+Z 한 번으로 전체 취소.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#9aa0a6;")
        root.addWidget(hint)

        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self._ok_btn = bb.button(QDialogButtonBox.StandardButton.Ok)
        self._ok_btn.setText("적용")
        self._ok_btn.setEnabled(False)
        bb.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        root.addWidget(bb)

    # -- 대상 줄 --
    def _target_lines(self) -> list[LineInput]:
        if self._scope_sel.isChecked() and self._selected_ids:
            return [ln for ln in self._lines if ln.event_id in self._selected_ids]
        return list(self._lines)

    # -- 미리보기 --
    def _on_preview(self) -> None:
        targets = self._target_lines()
        if not targets:
            self._status.setText("대상 줄이 없습니다.")
            return
        if self._mode_theme.isChecked():
            directed = direct_effects(
                targets, self._theme.currentData(),
                play_res=self._play_res,
                intensity=self._intensity.value() / 100.0,
            )
            self._fill_preview(directed)
        else:
            self._preview_btn.setEnabled(False)
            self._status.setText("LLM 연출 생성 중... (수십 초 걸릴 수 있음)")
            mood = self._mood.text()
            res = self._play_res

            def _job(targets=targets, mood=mood, res=res):
                from ai.effect_director import direct_with_llm
                return direct_with_llm(targets, mood=mood, play_res=res)

            self._runner.start(_job)

    def _on_llm_done(self, result, error: str) -> None:
        self._preview_btn.setEnabled(True)
        if error:
            self._status.setText(f"실패: {error}")
            return
        if result is None or result.errors:
            msg = "; ".join(result.errors) if result is not None else "결과 없음"
            self._status.setText(f"실패: {msg}")
            return
        if result.notes:
            self._status.setText(" · ".join(result.notes[:3]))
        self._fill_preview(result.directed)

    def _fill_preview(self, directed: list[DirectedLine]) -> None:
        by_id = {ln.event_id: ln for ln in self._lines}
        self._updates = []
        rows: list[tuple[str, str, str]] = []
        skipped: list[str] = []
        for d in directed:
            src = by_id.get(d.event_id)
            if src is None:
                continue
            ctx = EffectContext(
                duration_ms=src.duration_ms,
                play_res_x=self._play_res[0], play_res_y=self._play_res[1],
                plain_text=strip_tags(src.text),
            )
            new_text, _notes, errors = apply_specs(src.text, d.specs, ctx)
            if errors or new_text == src.text:
                if errors:
                    skipped.append(strip_tags(src.text)[:20])
                continue
            self._updates.append((d.event_id, new_text))
            plain = strip_tags(src.text).strip()
            rows.append((plain[:60], d.summary, new_text[:70]))

        self._table.setRowCount(len(rows))
        for r, (a, b, c) in enumerate(rows):
            for col, val in enumerate((a, b, c)):
                self._table.setItem(r, col, QTableWidgetItem(val))
        status = f"{len(rows)}줄에 효과 배정됨"
        if skipped:
            status += f" · {len(skipped)}줄 건너뜀"
        self._status.setText(status)
        self._ok_btn.setEnabled(bool(self._updates))

    def accept(self) -> None:
        if not self._updates:
            return
        super().accept()

    def reject(self) -> None:
        self._runner.cancel()
        self._runner.release()
        super().reject()

    def result_updates(self) -> list[tuple[str, str]]:
        """적용 확정된 [(event_id, new_text)] — MainWindow 가 커맨드로 반영."""
        return list(self._updates)
