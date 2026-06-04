"""LLM 설정 — 프로바이더 선택, 모델, API 키 영속화.

~/.assforge/llm.json 에 저장. 환경변수(ANTHROPIC_API_KEY / OPENAI_API_KEY)가
파일에 저장된 키보다 우선하므로 비밀키를 파일 밖에 둘 수 있다.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONFIG_DIR = Path.home() / ".assforge"
CONFIG_PATH = CONFIG_DIR / "llm.json"

_ENV_KEYS = {
    "claude": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "ollama": "",
}

_DEFAULTS: dict[str, dict[str, Any]] = {
    "claude": {"model": "claude-sonnet-4-6", "api_key": "", "base_url": ""},
    "openai": {"model": "gpt-4o-mini", "api_key": "", "base_url": ""},
    # 로컬에 설치된 모델로 기본값을 맞춰 즉시 동작하게 한다. 설정에서 변경 가능.
    "ollama": {
        "model": "hf.co/unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF:UD-Q4_K_XL",
        "api_key": "",
        "base_url": "http://localhost:11434",
    },
}


def _fresh_defaults() -> dict[str, dict[str, Any]]:
    return {k: dict(v) for k, v in _DEFAULTS.items()}


@dataclass
class LLMConfig:
    active_provider: str = "ollama"
    providers: dict[str, dict[str, Any]] = field(default_factory=_fresh_defaults)

    @classmethod
    def load(cls) -> "LLMConfig":
        cfg = cls()
        if CONFIG_PATH.exists():
            try:
                data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return cfg
            cfg.active_provider = data.get("active_provider", cfg.active_provider)
            stored = data.get("providers", {})
            for name, defaults in _DEFAULTS.items():
                merged = dict(defaults)
                merged.update(stored.get(name, {}))
                cfg.providers[name] = merged
        return cfg

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(
            json.dumps(
                {"active_provider": self.active_provider, "providers": self.providers},
                indent=2, ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def api_key_for(self, name: str) -> str:
        env = _ENV_KEYS.get(name, "")
        if env and os.environ.get(env):
            return os.environ[env]
        return self.providers.get(name, {}).get("api_key", "")

    def model_for(self, name: str) -> str:
        return self.providers.get(name, {}).get(
            "model", _DEFAULTS.get(name, {}).get("model", "")
        )

    def base_url_for(self, name: str) -> str:
        return self.providers.get(name, {}).get(
            "base_url", _DEFAULTS.get(name, {}).get("base_url", "")
        )
