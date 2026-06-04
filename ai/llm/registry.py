"""프로바이더 레지스트리 — LLMConfig 로부터 프로바이더 생성, 가용성 조회."""
from __future__ import annotations

from .base import LLMProvider, LLMProviderInfo
from .claude_provider import ClaudeProvider
from .config import LLMConfig
from .ollama_provider import OllamaProvider
from .openai_provider import OpenAIProvider

_CLASSES: dict[str, type[LLMProvider]] = {
    "claude": ClaudeProvider,
    "openai": OpenAIProvider,
    "ollama": OllamaProvider,
}

# UI/조회 표시 순서 — 키 없이 바로 쓰는 설치형 CLI 를 먼저, 로컬 서버를 끝에.
PROVIDER_ORDER = ["claude", "openai", "ollama"]


def build_provider(name: str, config: LLMConfig | None = None) -> LLMProvider:
    config = config or LLMConfig.load()
    cls = _CLASSES.get(name)
    if cls is None:
        raise ValueError(f"알 수 없는 프로바이더: {name}")
    return cls(
        model=config.model_for(name),
        api_key=config.api_key_for(name),
        base_url=config.base_url_for(name),
    )


def active_provider(config: LLMConfig | None = None) -> LLMProvider:
    config = config or LLMConfig.load()
    return build_provider(config.active_provider, config)


def list_provider_info(config: LLMConfig | None = None) -> list[LLMProviderInfo]:
    config = config or LLMConfig.load()
    return [build_provider(n, config).info() for n in PROVIDER_ORDER]
