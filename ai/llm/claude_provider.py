"""Claude (Anthropic) 프로바이더. `anthropic` SDK 를 lazy import."""
from __future__ import annotations

from .base import LLMProvider, LLMResponse, LLMResponseError, LLMUnavailable


class ClaudeProvider(LLMProvider):
    name = "claude"
    label = "Claude (Anthropic)"
    default_model = "claude-sonnet-4-6"
    requires_api_key = True

    def is_available(self) -> tuple[bool, str]:
        try:
            import anthropic  # noqa: F401
        except ImportError:
            return False, "anthropic SDK 미설치 (pip install anthropic)"
        if not self.api_key:
            return False, "ANTHROPIC_API_KEY 없음 (설정 또는 환경변수)"
        return True, f"준비됨 ({self.model})"

    def complete(
        self, system, user, *, temperature=0.2, max_tokens=2048, force_json=False,
    ) -> LLMResponse:
        try:
            import anthropic
        except ImportError as e:
            raise LLMUnavailable("anthropic SDK 미설치 (pip install anthropic)") from e
        if not self.api_key:
            raise LLMUnavailable("ANTHROPIC_API_KEY 없음")

        sys_prompt = system or ""
        if force_json:
            sys_prompt = (sys_prompt + "\n\n" if sys_prompt else "") + (
                "Respond with a single valid JSON value and nothing else. "
                "No markdown code fences, no prose."
            )

        client = anthropic.Anthropic(api_key=self.api_key)
        try:
            msg = client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                temperature=temperature,
                system=sys_prompt,
                messages=[{"role": "user", "content": user}],
            )
        except anthropic.APIError as e:
            raise LLMResponseError(f"Anthropic API 오류: {e}") from e

        text = "".join(
            getattr(block, "text", "")
            for block in msg.content
            if getattr(block, "type", "") == "text"
        )
        return LLMResponse(text=text, provider=self.name, model=self.model, raw=msg)
