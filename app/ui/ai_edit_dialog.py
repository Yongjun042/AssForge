"""AI 자막 편집 다이얼로그 — 자연어 명령 + .ass 효과 생성 (3 프로바이더 공용).

두 탭:
  · 자연어 명령 : 한국어/영어 지시 → ai.nl_commands.interpret_command 로 편집 연산을
                  해석 → app.commands.nl_apply.plan_to_command 로 단일 Command 화.
  · 효과 생성   : 프리셋 버튼(LLM 불필요) 또는 자연어 프롬프트 →
                  ai.effect_author.author_effects 로 EffectSpec 생성 → 결정적 컴파일.

LLM 호출은 LLMTaskRunner 로 백그라운드에서 돌려 UI 가 멈추지 않게 한다. 결과는
항상 사용자가 '적용' 을 눌러야만 result_command 로 확정되고, MainWindow 가
cmd_bus 로 실행한다(suggestion-only 원칙, 단일 undo).
"""
from __future__ import annotations

from functools import partial
from typing import Any

from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QGridLayout, QGroupBox, QHBoxLayout, QLabel,
    QListWidget, QPlainTextEdit, QPushButton, QTabWidget, QVBoxLayout, QWidget,
)

from ai.effect_author import EffectProposal, author_effects
from ai.nl_commands import CommandPlan, interpret_command
from app.commands.bus import Command
from app.commands.edit_commands import CompositeCommand, UpdateEventCommand
from app.commands.nl_apply import plan_to_command
from app.ui._llm_worker import LLMTaskRunner
from core.project.project_db import ProjectDB
from effects import (
    PRESETS, EffectContext, EffectSpec, apply_specs, get_preset, plain_text_of,
)


class AiEditDialog(QDialog):
    def __init__(
        self,
        db: ProjectDB,
        events: list[Any],
        play_res: tuple[int, int] = (1920, 1080),
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("AI 편집 — 자연어 명령 / 효과 생성")
        self.resize(640, 600)
        self._db = db
        self._events = events
        self._event_ids = [e.id for e in events]
        self._play_res = play_res
        self.result_command: Command | None = None

        self._runner = LLMTaskRunner(self)
        self._runner.done.connect(self._on_llm_done)

        self._plan: CommandPlan | None = None
        self._effect_specs: list[EffectSpec] = []

        root = QVBoxLayout(self)
        sel = QLabel(f"선택된 줄: {len(events)}개")
        if not events:
            sel.setText("선택된 줄이 없습니다 — 먼저 자막 줄을 선택하세요.")
            sel.setStyleSheet("color: #c33;")
        root.addWidget(sel)

        self._tabs = QTabWidget()
        self._tabs.addTab(self._build_nl_tab(), "자연어 명령")
        self._tabs.addTab(self._build_fx_tab(), "효과 생성")
        root.addWidget(self._tabs, 1)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        root.addWidget(self._status)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.button(QDialogButtonBox.StandardButton.Close).setText("닫기")
        bb.rejected.connect(self.reject)
        root.addWidget(bb)

        self._set_enabled(bool(events))

    # ============================================================
    # 자연어 명령 탭
    # ============================================================

    def _build_nl_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(QLabel(
            "예: '모든 줄을 0.5초 뒤로', '쉼표를 마침표로 바꿔', '이 줄들 주석 처리'"
        ))
        self._nl_prompt = QPlainTextEdit()
        self._nl_prompt.setPlaceholderText("선택한 줄에 적용할 편집 지시를 입력...")
        self._nl_prompt.setFixedHeight(90)
        lay.addWidget(self._nl_prompt)

        row = QHBoxLayout()
        self._nl_interpret_btn = QPushButton("해석")
        self._nl_interpret_btn.clicked.connect(self._on_interpret)
        row.addWidget(self._nl_interpret_btn)
        row.addStretch(1)
        self._nl_apply_btn = QPushButton("적용")
        self._nl_apply_btn.setEnabled(False)
        self._nl_apply_btn.clicked.connect(self._on_apply_nl)
        row.addWidget(self._nl_apply_btn)
        lay.addLayout(row)

        self._nl_summary = QLabel("")
        self._nl_summary.setWordWrap(True)
        lay.addWidget(self._nl_summary)
        self._nl_ops = QListWidget()
        lay.addWidget(self._nl_ops, 1)
        return w

    def _on_interpret(self) -> None:
        prompt = self._nl_prompt.toPlainText().strip()
        if not prompt or not self._events:
            return
        self._begin_llm("자연어 명령 해석 중...")
        events = list(self._events)
        self._runner.start(lambda: ("nl", interpret_command(prompt, events)))

    def _show_plan(self, plan: CommandPlan) -> None:
        self._plan = plan
        self._nl_ops.clear()
        for op in plan.ops:
            self._nl_ops.addItem(op.describe())
        if plan.errors:
            self._nl_summary.setText(
                "<span style='color:#c33'>해석 실패: "
                + "; ".join(plan.errors) + "</span>"
            )
            self._nl_apply_btn.setEnabled(False)
        else:
            self._nl_summary.setText(
                f"[{plan.provider}/{plan.model}] {plan.summary}"
            )
            self._nl_apply_btn.setEnabled(plan.ok)

    def _on_apply_nl(self) -> None:
        if self._plan is None or not self._plan.ok:
            return
        cmd = plan_to_command(self._db, self._plan, self._event_ids)
        if cmd is None:
            self._status.setText("적용할 변경이 없습니다.")
            return
        self.result_command = cmd
        self.accept()

    # ============================================================
    # 효과 생성 탭
    # ============================================================

    def _build_fx_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)

        presets_box = QGroupBox("프리셋 (LLM 불필요 — 즉시 미리보기)")
        grid = QGridLayout(presets_box)
        for i, (name, (label, _spec)) in enumerate(PRESETS.items()):
            btn = QPushButton(label)
            btn.clicked.connect(partial(self._on_preset, name))
            grid.addWidget(btn, i // 3, i % 3)
        lay.addWidget(presets_box)

        lay.addWidget(QLabel("또는 자연어로 효과 요청 (예: '글자가 커지면서 흔들리게'):"))
        self._fx_prompt = QPlainTextEdit()
        self._fx_prompt.setFixedHeight(70)
        lay.addWidget(self._fx_prompt)
        row = QHBoxLayout()
        self._fx_gen_btn = QPushButton("효과 생성")
        self._fx_gen_btn.clicked.connect(self._on_generate_fx)
        row.addWidget(self._fx_gen_btn)
        row.addStretch(1)
        self._fx_apply_btn = QPushButton("적용")
        self._fx_apply_btn.setEnabled(False)
        self._fx_apply_btn.clicked.connect(self._on_apply_fx)
        row.addWidget(self._fx_apply_btn)
        lay.addLayout(row)

        prev_box = QGroupBox("미리보기 (첫 번째 선택 줄 기준)")
        pv = QVBoxLayout(prev_box)
        pv.addWidget(QLabel("변경 전:"))
        self._fx_old = QPlainTextEdit()
        self._fx_old.setReadOnly(True)
        self._fx_old.setFixedHeight(60)
        pv.addWidget(self._fx_old)
        pv.addWidget(QLabel("변경 후:"))
        self._fx_new = QPlainTextEdit()
        self._fx_new.setReadOnly(True)
        self._fx_new.setFixedHeight(60)
        pv.addWidget(self._fx_new)
        self._fx_notes = QLabel("")
        self._fx_notes.setWordWrap(True)
        pv.addWidget(self._fx_notes)
        lay.addWidget(prev_box, 1)
        return w

    def _first_ctx(self) -> tuple[str, EffectContext]:
        ev = self._events[0]
        rx, ry = self._play_res
        ctx = EffectContext(
            duration_ms=max(0, ev.end_ms - ev.start_ms),
            play_res_x=rx, play_res_y=ry, plain_text=plain_text_of(ev.text),
        )
        return ev.text, ctx

    def _preview_specs(self, specs: list[EffectSpec], source: str) -> None:
        self._effect_specs = specs
        text, ctx = self._first_ctx()
        new_text, notes, errors = apply_specs(text, specs, ctx)
        self._fx_old.setPlainText(text)
        if errors:
            self._fx_new.setPlainText("")
            self._fx_notes.setText(
                "<span style='color:#c33'>" + "; ".join(errors) + "</span>"
            )
            self._fx_apply_btn.setEnabled(False)
            return
        self._fx_new.setPlainText(new_text)
        note_txt = f"[{source}] " + ("; ".join(notes) if notes else "적용 준비됨")
        self._fx_notes.setText(note_txt)
        self._fx_apply_btn.setEnabled(True)

    def _on_preset(self, name: str) -> None:
        if not self._events:
            return
        spec = get_preset(name)
        if spec is not None:
            self._preview_specs([spec], f"프리셋:{name}")

    def _on_generate_fx(self) -> None:
        prompt = self._fx_prompt.toPlainText().strip()
        if not prompt or not self._events:
            return
        text, ctx = self._first_ctx()
        self._begin_llm("효과 생성 중...")
        self._runner.start(lambda: ("fx", author_effects(prompt, text, ctx)))

    def _show_proposal(self, proposal: EffectProposal) -> None:
        if proposal.errors:
            self._fx_old.setPlainText(proposal.preview_old)
            self._fx_new.setPlainText("")
            self._fx_notes.setText(
                "<span style='color:#c33'>" + "; ".join(proposal.errors) + "</span>"
            )
            self._fx_apply_btn.setEnabled(False)
            return
        self._preview_specs(proposal.specs, f"{proposal.provider}/{proposal.model}")

    def _on_apply_fx(self) -> None:
        if not self._effect_specs or not self._events:
            return
        rx, ry = self._play_res
        cmds: list[Command] = []
        skipped = 0
        for ev in self._events:
            ctx = EffectContext(
                duration_ms=max(0, ev.end_ms - ev.start_ms),
                play_res_x=rx, play_res_y=ry, plain_text=plain_text_of(ev.text),
            )
            new_text, _notes, errors = apply_specs(ev.text, self._effect_specs, ctx)
            if errors:
                skipped += 1
                continue
            if new_text != ev.text:
                cmds.append(UpdateEventCommand(self._db, ev.id, {"text": new_text}))
        if not cmds:
            self._status.setText("적용할 변경이 없습니다.")
            return
        self.result_command = CompositeCommand(cmds, f"효과 적용: {len(cmds)}줄")
        self.accept()

    # ============================================================
    # LLM 작업 공통
    # ============================================================

    def _set_enabled(self, on: bool) -> None:
        for b in (self._nl_interpret_btn, self._fx_gen_btn):
            b.setEnabled(on)

    def _begin_llm(self, msg: str) -> None:
        self._status.setText(msg)
        self._set_enabled(False)

    def _on_llm_done(self, result: object, error: str) -> None:
        self._set_enabled(bool(self._events))
        if error:
            self._status.setText(f"실패: {error}")
            return
        self._status.setText("")
        kind, payload = result  # type: ignore[misc]
        if kind == "nl":
            self._show_plan(payload)
        elif kind == "fx":
            self._show_proposal(payload)

    def reject(self) -> None:
        # CLI 백엔드는 cancel() 로 프로세스가 죽는다. Ollama 처럼 HTTP 블로킹
        # 중이라 취소가 안 통하는 경우도 있으므로 wait() 대신 release() —
        # 스레드를 고아로 넘기고 즉시 닫아 GUI 가 얼지 않게 한다.
        self._runner.cancel()
        self._runner.release()
        super().reject()
