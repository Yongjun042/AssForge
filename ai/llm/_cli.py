"""설치된 CLI(claude / codex)를 호출하기 위한 공통 헬퍼.

API 키 대신 각 CLI 가 자체적으로 보관한 로그인 인증을 사용한다. Windows 에서
`claude.EXE` 와 `codex.CMD` 모두 인자 리스트 형태로 직접 실행된다(shell 불필요).
"""
from __future__ import annotations

import shutil
import subprocess
import sys

# Windows 에서 콘솔 창이 깜빡이지 않도록.
_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def find_cli(names: list[str]) -> str | None:
    """PATH 에서 첫 번째로 발견되는 실행 파일의 전체 경로 (없으면 None)."""
    for name in names:
        path = shutil.which(name)
        if path:
            return path
    return None


def run_cli(
    args: list[str],
    *,
    stdin_text: str = "",
    timeout: float = 180.0,
    cwd: str | None = None,
) -> subprocess.CompletedProcess:
    """CLI 를 1회 실행하고 CompletedProcess 반환. 호출자가 returncode 를 확인한다.

    subprocess.TimeoutExpired / FileNotFoundError 는 호출자가 잡아 LLM 예외로
    변환한다.
    """
    return subprocess.run(
        args,
        input=stdin_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=_CREATE_NO_WINDOW,
        cwd=cwd,
    )
