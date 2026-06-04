"""LLM 프로바이더 설정 다이얼로그.

활성 프로바이더(Ollama/Claude/Codex)와 각 프로바이더의 모델·API 키·base_url 을
편집하고 ~/.assforge/llm.json 에 저장한다. '가용성 테스트' 는 현재 입력값으로
임시 설정을 만들어 provider.is_available() 을 호출한다.

주의: API 키는 환경변수(ANTHROPIC_API_KEY / OPENAI_API_KEY)가 파일 값보다
우선한다(config.api_key_for). 키 입력칸은 파일에 저장될 값이며, 환경변수가 있으면
실제로는 그쪽이 쓰인다는 안내를 함께 보여준다.
"""
from __future__ import annotations

import os

from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox, QLabel,
    QLineEdit, QPushButton, QVBoxLayout, QWidget,
)

from ai.llm.config import LLMConfig, _ENV_KEYS
from ai.llm.registry import PROVIDER_ORDER, build_provider

_LABELS = {
    "ollama": "Ollama (로컬, 무료)",
    "claude": "Claude (claude CLI)",
    "openai": "Codex (codex CLI)",
}

# 설치된 CLI 의 자체 인증을 쓰는 프로바이더 — API 키/base_url 입력칸 불필요.
_CLI_PROVIDERS = {"claude", "openai"}


class LLMSettingsDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("LLM 설정")
        self.resize(520, 360)

        self._config = LLMConfig.load()
        # 프로바이더별 입력값 작업 사본 (취소 시 버려짐)
        self._working: dict[str, dict[str, str]] = {
            n: {
                "model": self._config.model_for(n),
                "api_key": self._config.providers.get(n, {}).get("api_key", ""),
                "base_url": self._config.base_url_for(n),
            }
            for n in PROVIDER_ORDER
        }

        root = QVBoxLayout(self)

        # 활성 프로바이더
        active_box = QGroupBox("활성 프로바이더 (편집 기능이 사용할 LLM)")
        active_form = QFormLayout(active_box)
        self._active_combo = QComboBox()
        for n in PROVIDER_ORDER:
            self._active_combo.addItem(_LABELS[n], n)
        self._active_combo.setCurrentIndex(
            max(0, PROVIDER_ORDER.index(self._config.active_provider))
            if self._config.active_provider in PROVIDER_ORDER else 0
        )
        active_form.addRow("사용할 프로바이더:", self._active_combo)
        root.addWidget(active_box)

        # 프로바이더별 설정 편집
        edit_box = QGroupBox("프로바이더 설정")
        edit_form = QFormLayout(edit_box)
        self._edit_combo = QComboBox()
        for n in PROVIDER_ORDER:
            self._edit_combo.addItem(_LABELS[n], n)
        self._edit_combo.currentIndexChanged.connect(self._on_edit_changed)
        edit_form.addRow("설정할 프로바이더:", self._edit_combo)

        self._model_edit = QLineEdit()
        edit_form.addRow("모델:", self._model_edit)
        self._key_edit = QLineEdit()
        self._key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        edit_form.addRow("API 키:", self._key_edit)
        self._url_edit = QLineEdit()
        edit_form.addRow("base_url:", self._url_edit)

        self._env_hint = QLabel("")
        self._env_hint.setWordWrap(True)
        self._env_hint.setStyleSheet("color: #888;")
        edit_form.addRow("", self._env_hint)

        self._test_btn = QPushButton("가용성 테스트")
        self._test_btn.clicked.connect(self._on_test)
        edit_form.addRow("", self._test_btn)
        self._test_label = QLabel("")
        self._test_label.setWordWrap(True)
        edit_form.addRow("", self._test_label)

        root.addWidget(edit_box)

        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        bb.button(QDialogButtonBox.StandardButton.Save).setText("저장")
        bb.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        bb.accepted.connect(self._on_save)
        bb.rejected.connect(self.reject)
        root.addWidget(bb)

        self._current_edit = PROVIDER_ORDER[0]
        self._load_fields(self._current_edit)

    # -- 편집 대상 전환 --

    def _flush_fields(self) -> None:
        """현재 입력값을 작업 사본에 저장."""
        self._working[self._current_edit] = {
            "model": self._model_edit.text().strip(),
            "api_key": self._key_edit.text(),
            "base_url": self._url_edit.text().strip(),
        }

    def _load_fields(self, name: str) -> None:
        vals = self._working[name]
        self._model_edit.setText(vals["model"])
        self._key_edit.setText(vals["api_key"])
        self._url_edit.setText(vals["base_url"])

        is_cli = name in _CLI_PROVIDERS
        # CLI 프로바이더는 키/URL 을 쓰지 않으므로 입력칸을 비활성화한다.
        self._key_edit.setEnabled(not is_cli)
        self._url_edit.setEnabled(not is_cli)

        env = _ENV_KEYS.get(name, "")
        if is_cli:
            cli = "claude" if name == "claude" else "codex"
            self._env_hint.setText(
                f"설치된 `{cli}` CLI 의 로그인 인증 사용 — API 키/URL 불필요.\n"
                f"모델은 CLI 별칭/이름(비우면 CLI 기본값)."
            )
        elif env:
            present = " (현재 설정됨 — 이 값이 우선 사용됩니다)" if os.environ.get(env) else " (미설정)"
            self._env_hint.setText(f"환경변수 {env}{present}")
        else:
            self._env_hint.setText("로컬 서버 — API 키 불필요")
        self._test_label.setText("")

    def _on_edit_changed(self, _idx: int) -> None:
        self._flush_fields()
        self._current_edit = self._edit_combo.currentData()
        self._load_fields(self._current_edit)

    # -- 테스트 / 저장 --

    def _temp_config(self) -> LLMConfig:
        self._flush_fields()
        cfg = LLMConfig.load()
        for n in PROVIDER_ORDER:
            cfg.providers.setdefault(n, {})
            cfg.providers[n].update(self._working[n])
        cfg.active_provider = self._active_combo.currentData()
        return cfg

    def _on_test(self) -> None:
        name = self._current_edit
        self._test_label.setText("테스트 중...")
        self._test_btn.setEnabled(False)
        try:
            provider = build_provider(name, self._temp_config())
            ok, detail = provider.is_available()
        except Exception as exc:  # 빌드 자체가 실패할 수 있음
            ok, detail = False, str(exc)
        finally:
            self._test_btn.setEnabled(True)
        mark = "✓ 사용 가능" if ok else "✗ 사용 불가"
        color = "#2a2" if ok else "#c33"
        self._test_label.setText(f"<span style='color:{color}'>{mark}</span> — {detail}")

    def _on_save(self) -> None:
        cfg = self._temp_config()
        cfg.save()
        self.accept()
