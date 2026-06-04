"""Codex / OpenAI 프로바이더. `openai` SDK 를 lazy import.

base_url 을 지정하면 OpenAI 호환 엔드포인트(Azure, 로컬 게이트웨이 등)도 사용 가능.
"""
from __future__ import annotations

from .base import LLMProvider, LLMResponse, LLMResponseError, LLMUnavailable


class OpenAIProvider(LLMProvider):
    name = "openai"
    label = "Codex (OpenAI)"
    default_model = "gpt-4o-mini"
    requires_api_key = True

    def is_available(self) -> tuple[bool, str]:
        try:
            import openai  # noqa: F401
        except ImportError:
            return False, "openai SDK 미설치 (pip install openai)"
        if not self.api_key:
            return False, "OPENAI_API_KEY 없음 (설정 또는 환경변수)"
        return True, f"준비됨 ({self.model})"

    def complete(
        self, system, user, *, temperature=0.2, max_tokens=2048, force_json=False,
    ) -> LLMResponse:
        try:
            import openai
        except ImportError as e:
            raise LLMUnavailable("openai SDK 미설치 (pip install openai)") from e
        if not self.api_key:
            raise LLMUnavailable("OPENAI_API_KEY 없음")

        kwargs: dict = {"api_key": self.api_key}
        if self.base_url:
            kwargs["base_url"] = self.base_url
        client = openai.OpenAI(**kwargs)

        params: dict = {
            "model": self.model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if force_json:
            # response_format=json_object 는 프롬프트에 'json' 단어가 있어야 한다.
            params["response_format"] = {"type": "json_object"}

        try:
            resp = client.chat.completions.create(**params)
        except openai.OpenAIError as e:
            raise LLMResponseError(f"OpenAI API 오류: {e}") from e

        text = resp.choices[0].message.content or ""
        return LLMResponse(text=text, provider=self.name, model=self.model, raw=resp)
