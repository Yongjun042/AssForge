"""Ollama (로컬) 프로바이더. stdlib urllib 만으로 REST API 호출.

API 키도 SDK 도 필요 없다. base_url 에서 `ollama serve` 가 떠 있어야 한다.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from .base import LLMProvider, LLMResponse, LLMResponseError, LLMUnavailable


class OllamaProvider(LLMProvider):
    name = "ollama"
    label = "Ollama (로컬)"
    default_model = "llama3.1"
    requires_api_key = False

    @property
    def _base(self) -> str:
        return (self.base_url or "http://localhost:11434").rstrip("/")

    def is_available(self) -> tuple[bool, str]:
        try:
            req = urllib.request.Request(self._base + "/api/tags")
            with urllib.request.urlopen(req, timeout=2) as r:
                data = json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            return False, f"Ollama 서버 응답 없음 ({self._base}) — `ollama serve` 필요"
        models = [m.get("name", "") for m in data.get("models", [])]
        if models and not any(self.model in m for m in models):
            return True, f"서버 OK, '{self.model}' 미설치 (ollama pull {self.model})"
        return True, f"준비됨 ({self.model})"

    def complete(
        self, system, user, *, temperature=0.2, max_tokens=2048, force_json=False,
    ) -> LLMResponse:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user})

        payload = {
            "model": self.model,
            "stream": False,
            "messages": messages,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        if force_json:
            payload["format"] = "json"

        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self._base + "/api/chat",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                data = json.loads(r.read().decode("utf-8"))
        except urllib.error.URLError as e:
            raise LLMUnavailable(f"Ollama 연결 실패: {e}") from e
        except (OSError, json.JSONDecodeError) as e:
            raise LLMResponseError(f"Ollama 응답 오류: {e}") from e

        text = data.get("message", {}).get("content", "")
        if not text:
            raise LLMResponseError("Ollama 빈 응답")
        return LLMResponse(text=text, provider=self.name, model=self.model, raw=data)
