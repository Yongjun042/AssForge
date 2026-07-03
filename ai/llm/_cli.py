"""설치된 CLI(claude / codex)를 호출하기 위한 공통 헬퍼.

API 키 대신 각 CLI 가 자체적으로 보관한 로그인 인증을 사용한다. Windows 에서
`claude.EXE` 와 `codex.CMD` 모두 인자 리스트 형태로 직접 실행된다(shell 불필요).

취소: 호출 스레드가 CliCancelToken 을 등록해 두면(run_cli 가 thread-local 로
찾는다) 다른 스레드에서 token.cancel() 로 실행 중인 CLI 프로세스 트리를 죽일
수 있다 — 다이얼로그를 닫을 때 GUI 가 CLI 타임아웃까지 멈추지 않게 한다.
"""
from __future__ import annotations

import shutil
import subprocess
import threading

from core.subproc import CREATE_NO_WINDOW as _CREATE_NO_WINDOW
from core.subproc import kill_tree as _kill_tree


def find_cli(names: list[str]) -> str | None:
    """PATH 에서 첫 번째로 발견되는 실행 파일의 전체 경로 (없으면 None)."""
    for name in names:
        path = shutil.which(name)
        if path:
            return path
    return None


class CliCancelToken:
    """run_cli 가 띄운 프로세스를 다른 스레드에서 죽이기 위한 토큰.

    cancel() 이 register 보다 먼저 와도 안전하다(등록 즉시 kill).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._cancelled = False

    def _register(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._proc = proc
            cancelled = self._cancelled
        if cancelled:
            _kill_tree(proc)

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            proc = self._proc
        if proc is not None:
            _kill_tree(proc)


_thread_local = threading.local()


def set_cancel_token(token: CliCancelToken | None) -> None:
    """현재 스레드에서 이후 run_cli 호출이 등록할 취소 토큰 지정."""
    _thread_local.token = token


def run_cli(
    args: list[str],
    *,
    stdin_text: str = "",
    timeout: float = 180.0,
    cwd: str | None = None,
) -> subprocess.CompletedProcess:
    """CLI 를 1회 실행하고 CompletedProcess 반환. 호출자가 returncode 를 확인한다.

    subprocess.TimeoutExpired / FileNotFoundError 는 호출자가 잡아 LLM 예외로
    변환한다. 취소되면 프로세스가 죽고 returncode != 0 으로 돌아온다.
    """
    proc = subprocess.Popen(
        args,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=_CREATE_NO_WINDOW,
        cwd=cwd,
    )
    token: CliCancelToken | None = getattr(_thread_local, "token", None)
    if token is not None:
        token._register(proc)
    try:
        out, err = proc.communicate(input=stdin_text, timeout=timeout)
    except Exception:
        _kill_tree(proc)
        try:
            proc.communicate(timeout=10)
        except Exception:
            pass
        raise
    return subprocess.CompletedProcess(args, proc.returncode, out, err)
