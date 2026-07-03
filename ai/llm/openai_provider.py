"""Codex 프로바이더 — 설치된 `codex` CLI(OpenAI Codex)를 호출.

API 키가 아니라 codex CLI 가 보관한 로그인 인증을 사용한다. `codex exec`(비대화형)
을 read-only 샌드박스로 1회 실행하고, 최종 메시지는 `--output-last-message` 파일
에서 읽는다(stdout 은 세션 로그가 섞일 수 있어 파일이 더 깔끔하다). 프로젝트 파일을
건드리지 않도록 임시 디렉터리를 작업 폴더로 지정한다.

클래스명/레지스트리 키는 호환을 위해 `openai`/OpenAIProvider 를 유지한다.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

from ._cli import find_cli, run_cli
from .base import LLMProvider, LLMResponse, LLMResponseError, LLMUnavailable

_JSON_DIRECTIVE = (
    "Respond with a single valid JSON value and nothing else. "
    "No markdown code fences, no prose."
)


class OpenAIProvider(LLMProvider):
    name = "openai"
    label = "Codex (codex CLI)"
    default_model = ""  # 비우면 codex 설정(~/.codex)의 기본 모델 사용
    requires_api_key = False

    def _exe(self) -> str | None:
        return find_cli(["codex"])

    def is_available(self) -> tuple[bool, str]:
        if not self._exe():
            return False, "codex CLI 미설치 (`npm i -g @openai/codex` 후 PATH 확인)"
        # 바이너리 존재만 확인 — 로그인 여부는 첫 호출에서 드러난다.
        return True, f"codex CLI 사용 ({self.model or 'codex 기본 모델'}) — 로그인은 첫 실행 시 확인"

    def complete(
        self, system, user, *, temperature=0.2, max_tokens=2048, force_json=False,
    ) -> LLMResponse:
        # NOTE: codex CLI 는 temperature/max_tokens 노브를 노출하지 않는다 —
        # 두 인자는 시그니처 호환용으로 받되 적용되지 않는다 (ollama 만 적용).
        exe = self._exe()
        if not exe:
            raise LLMUnavailable("codex CLI 미설치")

        # codex 는 별도 system 채널이 없으므로 하나의 프롬프트로 합친다.
        prompt = (system + "\n\n" if system else "") + user
        if force_json:
            prompt += "\n\n" + _JSON_DIRECTIVE

        workdir = tempfile.mkdtemp(prefix="assforge_codex_")
        outfile = os.path.join(workdir, "last_message.txt")
        args = [
            exe, "exec",
            "--skip-git-repo-check",
            "-s", "read-only",
            "-C", workdir,
            "-o", outfile,
        ]
        if self.model:
            args += ["-m", self.model]
        args.append("-")  # 프롬프트는 stdin 으로 전달

        try:
            try:
                proc = run_cli(args, stdin_text=prompt, timeout=420, cwd=workdir)
            except FileNotFoundError as e:
                raise LLMUnavailable(f"codex CLI 실행 실패: {e}") from e
            except subprocess.TimeoutExpired as e:
                raise LLMResponseError("codex CLI 응답 시간 초과 (420초)") from e

            text = ""
            if os.path.exists(outfile):
                try:
                    with open(outfile, encoding="utf-8") as f:
                        text = f.read().strip()
                except OSError:
                    text = ""
            if not text:
                text = (proc.stdout or "").strip()

            if proc.returncode != 0 and not text:
                err = (proc.stderr or "").strip()
                low = err.lower()
                if "login" in low or "unauthorized" in low or "authentication" in low \
                        or "not logged in" in low:
                    raise LLMResponseError(
                        "codex CLI 인증 필요 — 터미널에서 `codex login` 을 "
                        f"실행하세요. (원문: {err[:200]})"
                    )
                if "not supported" in low and "model" in low:
                    raise LLMResponseError(
                        f"codex 가 모델 '{self.model}' 을 지원하지 않습니다 — "
                        "LLM 설정에서 모델을 비우면 codex 기본 모델을 씁니다. "
                        f"(원문: {err[:200]})"
                    )
                raise LLMResponseError(
                    f"codex CLI 오류(코드 {proc.returncode}): {err[:300] or '출력 없음'}"
                )
            if not text:
                raise LLMResponseError("codex CLI 빈 응답")
            return LLMResponse(text=text, provider=self.name, model=self.model, raw=proc)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
