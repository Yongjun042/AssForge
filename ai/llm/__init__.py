"""LLM 프로바이더 추상화 패키지.

사용 예:
    from ai.llm import active_provider, LLMConfig
    prov = active_provider()
    ok, why = prov.is_available()
    if ok:
        spec = prov.complete_json(system_prompt, user_prompt)
"""
from __future__ import annotations

from .base import (
    LLMError,
    LLMProvider,
    LLMProviderInfo,
    LLMResponse,
    LLMResponseError,
    LLMUnavailable,
    extract_json,
)
from .config import LLMConfig
from .registry import (
    PROVIDER_ORDER,
    active_provider,
    build_provider,
    list_provider_info,
)

__all__ = [
    "LLMProvider",
    "LLMResponse",
    "LLMProviderInfo",
    "LLMError",
    "LLMUnavailable",
    "LLMResponseError",
    "extract_json",
    "LLMConfig",
    "build_provider",
    "active_provider",
    "list_provider_info",
    "PROVIDER_ORDER",
]
