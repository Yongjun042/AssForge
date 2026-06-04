"""LLM 프로바이더 추상화 — Codex(OpenAI) / Claude(Anthropic) / Ollama 공통 인터페이스.

모든 프로바이더는 동일한 complete / complete_json 계약을 따른다.
설치되지 않은 SDK나 없는 키는 예외가 아니라 is_available() 의 (False, 사유) 로
보고하고, 실제 호출 시에만 LLMUnavailable 을 던진다.
"""
from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


class LLMError(Exception):
    """LLM 작업의 베이스 예외."""


class LLMUnavailable(LLMError):
    """프로바이더를 실행할 수 없음 — SDK/키/서버 누락."""


class LLMResponseError(LLMError):
    """응답은 받았으나 사용할 수 없음 — JSON 파싱 실패, 거부 등."""


@dataclass(slots=True)
class LLMResponse:
    text: str
    provider: str = ""
    model: str = ""
    raw: Any = None


@dataclass(slots=True)
class LLMProviderInfo:
    name: str       # 내부 id: "claude" | "openai" | "ollama"
    label: str      # UI 라벨
    available: bool
    detail: str
    model: str


def extract_json(text: str) -> Any:
    """LLM 응답에서 JSON 값을 추출.

    ```json 펜스, 객체 앞뒤의 산문, 단순 잡음을 허용한다.
    파싱 가능한 게 없으면 LLMResponseError.
    """
    if not text or not text.strip():
        raise LLMResponseError("빈 응답")
    s = text.strip()

    fence = re.search(r"```(?:json)?\s*(.*?)```", s, re.DOTALL)
    if fence:
        s = fence.group(1).strip()

    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass

    # 첫 번째 균형 잡힌 { } 또는 [ ] 블록을 찾는다 (문자열 내부 괄호 무시).
    start = next((i for i, ch in enumerate(s) if ch in "{["), None)
    if start is None:
        raise LLMResponseError(f"JSON 객체를 찾지 못함: {text[:120]!r}")

    pairs = {"}": "{", "]": "["}
    stack: list[str] = []
    in_str = False
    esc = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if not stack or stack[-1] != pairs[ch]:
                break
            stack.pop()
            if not stack:
                candidate = s[start:i + 1]
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError as e:
                    raise LLMResponseError(f"JSON 파싱 실패: {e}") from e
    raise LLMResponseError(f"JSON 균형 실패: {text[:120]!r}")


class LLMProvider(ABC):
    """LLM 프로바이더 베이스. 하위 클래스는 name/label/default_model 을 채운다."""

    name: str = ""
    label: str = ""
    default_model: str = ""
    requires_api_key: bool = False

    def __init__(self, model: str = "", api_key: str = "", base_url: str = "") -> None:
        self.model = model or self.default_model
        self.api_key = api_key
        self.base_url = base_url

    @abstractmethod
    def is_available(self) -> tuple[bool, str]:
        """(사용가능?, 사람이 읽을 사유). 절대 예외를 던지지 않는다."""

    @abstractmethod
    def complete(
        self,
        system: str,
        user: str,
        *,
        temperature: float = 0.2,
        max_tokens: int = 2048,
        force_json: bool = False,
    ) -> LLMResponse:
        """단일 턴 완성. LLMUnavailable / LLMResponseError 를 던질 수 있다."""

    def complete_json(
        self,
        system: str,
        user: str,
        *,
        temperature: float = 0.1,
        max_tokens: int = 2048,
    ) -> Any:
        """force_json 으로 완성한 뒤 JSON 값으로 파싱해 반환."""
        resp = self.complete(
            system, user, temperature=temperature,
            max_tokens=max_tokens, force_json=True,
        )
        return extract_json(resp.text)

    def info(self) -> LLMProviderInfo:
        ok, detail = self.is_available()
        return LLMProviderInfo(self.name, self.label, ok, detail, self.model)
