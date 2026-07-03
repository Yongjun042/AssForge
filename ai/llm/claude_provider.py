"""Claude 프로바이더 — 설치된 `claude` CLI(Claude Code)를 호출.

API 키가 아니라 claude CLI 가 보관한 로그인 인증을 사용한다. `claude -p`(print
모드)로 1회 완성하고, 시스템 프롬프트는 `--system-prompt`, 사용자 프롬프트는 stdin
으로 전달한다(긴 자막 컨텍스트도 인자 길이 제한에 걸리지 않는다).
"""
from __future__ import annotations

import subprocess
import tempfile

from ._cli import find_cli, run_cli
from .base import LLMProvider, LLMResponse, LLMResponseError, LLMUnavailable

_JSON_DIRECTIVE = (
    "Respond with a single valid JSON value and nothing else. "
    "No markdown code fences, no prose. Do not use any tools."
)


class ClaudeProvider(LLMProvider):
    name = "claude"
    label = "Claude (claude CLI)"
    default_model = "sonnet"
    requires_api_key = False

    def _exe(self) -> str | None:
        return find_cli(["claude"])

    def is_available(self) -> tuple[bool, str]:
        if not self._exe():
            return False, "claude CLI 미설치 (Claude Code 설치 후 `claude` 가 PATH 에 있어야 함)"
        # 바이너리 존재만 확인 — 로그인 여부는 첫 호출에서 드러난다.
        # (프리플라이트로 CLI 를 실제 실행하면 수 초씩 걸려 설정 UI 가 느려짐)
        return True, f"claude CLI 사용 ({self.model or '기본 모델'}) — 로그인은 첫 실행 시 확인"

    def complete(
        self, system, user, *, temperature=0.2, max_tokens=2048, force_json=False,
    ) -> LLMResponse:
        # NOTE: claude CLI 는 temperature/max_tokens 노브를 노출하지 않는다 —
        # 두 인자는 시그니처 호환용으로 받되 적용되지 않는다 (ollama 만 적용).
        exe = self._exe()
        if not exe:
            raise LLMUnavailable("claude CLI 미설치")

        sys_prompt = system or ""
        if force_json:
            sys_prompt = (sys_prompt + "\n\n" if sys_prompt else "") + _JSON_DIRECTIVE

        # --tools "": 순수 완성 용도 — 도구(파일 읽기 등) 비활성.
        args = [exe, "-p", "--output-format", "text", "--tools", ""]
        if self.model:
            args += ["--model", self.model]
        if sys_prompt:
            args += ["--system-prompt", sys_prompt]

        try:
            # 임시 폴더에서 실행 — 프로젝트 CWD 의 CLAUDE.md 컨텍스트를
            # 끌어들이지 않게(토큰/지연 오버헤드, 출력 왜곡 방지).
            proc = run_cli(args, stdin_text=user, timeout=300,
                           cwd=tempfile.gettempdir())
        except FileNotFoundError as e:
            raise LLMUnavailable(f"claude CLI 실행 실패: {e}") from e
        except subprocess.TimeoutExpired as e:
            raise LLMResponseError("claude CLI 응답 시간 초과 (300초)") from e

        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            low = err.lower()
            if "login" in low or "api key" in low or "unauthorized" in low \
                    or "authentication" in low:
                raise LLMResponseError(
                    "claude CLI 인증 필요 — 터미널에서 `claude` 실행 후 "
                    f"/login 으로 로그인하세요. (원문: {err[:200]})"
                )
            raise LLMResponseError(
                f"claude CLI 오류(코드 {proc.returncode}): {err[:300] or '출력 없음'}"
            )

        text = (proc.stdout or "").strip()
        if not text:
            raise LLMResponseError("claude CLI 빈 응답")
        return LLMResponse(text=text, provider=self.name, model=self.model, raw=proc)
