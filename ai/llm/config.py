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

# claude/openai 는 설치된 CLI(claude / codex)의 자체 로그인 인증을 쓰므로
# 환경변수 API 키가 필요 없다. (ollama 도 키 불필요.)
_ENV_KEYS = {
    "claude": "",
    "openai": "",
    "ollama": "",
}

_DEFAULTS: dict[str, dict[str, Any]] = {
    # model 은 CLI 의 모델 별칭/이름 (claude: sonnet/opus/haiku 등, 비우면 CLI 기본).
    "claude": {"model": "sonnet", "api_key": "", "base_url": ""},
    # codex: 비우면 ~/.codex 설정의 기본 모델 사용.
    "openai": {"model": "", "api_key": "", "base_url": ""},
    # 로컬에 설치된 모델로 기본값을 맞춰 즉시 동작하게 한다. 설정에서 변경 가능.
    "ollama": {
        "model": "hf.co/unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF:UD-Q4_K_XL",
        "api_key": "",
        "base_url": "http://localhost:11434",
    },
}


# SDK 시절(API 직접 호출) 저장값 → CLI 시절 값 마이그레이션.
# 옛 llm.json 의 모델명이 그대로 CLI 에 넘어가면 codex 가 거부한다
# (예: `codex exec -m gpt-4o-mini` → 400 unsupported model).
_LEGACY_MODEL_MIGRATION: dict[str, dict[str, str]] = {
    "openai": {"gpt-4o-mini": "", "gpt-4o": "", "gpt-4.1-mini": ""},
    "claude": {"claude-sonnet-4-6": "sonnet", "claude-3-5-sonnet-latest": "sonnet"},
}


def _fresh_defaults() -> dict[str, dict[str, Any]]:
    return {k: dict(v) for k, v in _DEFAULTS.items()}


@dataclass
class LLMConfig:
    # 설치된 claude CLI 가 키 없이 바로 동작하므로 기본값으로 둔다.
    # (설정에서 codex/ollama 로 변경 가능.)
    active_provider: str = "claude"
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
            migrated = False
            for name, defaults in _DEFAULTS.items():
                merged = dict(defaults)
                merged.update(stored.get(name, {}))
                # SDK 시절 모델명이 남아 있으면 CLI 별칭으로 치환 (거부 방지)
                legacy = _LEGACY_MODEL_MIGRATION.get(name, {})
                if merged.get("model") in legacy:
                    merged["model"] = legacy[merged["model"]]
                    migrated = True
                cfg.providers[name] = merged
            if migrated:
                try:
                    cfg.save()  # 다음 로드부터는 마이그레이션 불필요
                except OSError:
                    pass
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
