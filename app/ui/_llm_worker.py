"""LLM 호출을 GUI 스레드 밖에서 실행하는 워커 — 모달 다이얼로그가 멈추지 않게.

interpret_command / author_effects 같은 블로킹 LLM 호출을 백그라운드 QThread 에서
1회 실행하고 done(result, error) 시그널로 결과를 돌려준다. 스레드/워커 참조를
self 에 잡아 두어 GC 가 실행 중 QThread 를 파괴하지 않게 한다.

취소: 워커가 CliCancelToken 을 자기 스레드에 등록해 두므로, GUI 쪽에서
cancel() 을 부르면 실행 중인 CLI 프로세스 트리가 죽어 fn 이 즉시 예외로
끝난다 — 다이얼로그를 닫을 때 wait() 가 CLI 타임아웃(수 분)까지 GUI 를
붙잡지 않는다.
"""
from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import QObject, QThread, Signal

from ai.llm._cli import CliCancelToken, set_cancel_token


class _Worker(QObject):
    finished = Signal(object, str)  # (result | None, error)

    def __init__(self, fn: Callable[[], Any], token: CliCancelToken) -> None:
        super().__init__()
        self._fn = fn
        self._token = token

    def run(self) -> None:
        set_cancel_token(self._token)
        try:
            result = self._fn()
        except Exception as exc:  # 어떤 LLM/네트워크 오류든 UI 로 전달
            msg = "취소됨" if self._token.cancelled else str(exc)
            self.finished.emit(None, msg)
            return
        finally:
            set_cancel_token(None)
        self.finished.emit(result, "")


# release() 로 고아가 된 (thread, worker) 강참조 — fn 이 끝날 때까지 살려 둬
# 실행 중 QThread 가 GC 로 파괴되는 크래시를 막는다. 완료 시 자체 정리.
_orphans: set = set()


class LLMTaskRunner(QObject):
    """fn 을 백그라운드에서 1회 실행하고 done(result, error) 으로 통지."""

    done = Signal(object, str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._thread: QThread | None = None
        self._worker: _Worker | None = None
        self._token: CliCancelToken | None = None

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    def start(self, fn: Callable[[], Any]) -> None:
        self.cancel()   # 이전 작업이 남아 있으면 끊고
        self.release()  # 논블로킹 정리 — 안 죽는 백엔드(HTTP)여도 GUI 를 안 막는다
        self._token = CliCancelToken()
        self._thread = QThread()
        self._worker = _Worker(fn, self._token)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._on_finished)
        self._worker.finished.connect(self._thread.quit)
        self._thread.start()

    def cancel(self) -> None:
        """실행 중인 CLI 프로세스를 죽여 fn 을 조기 종료시킨다 (논블로킹)."""
        if self._token is not None and self.busy:
            self._token.cancel()

    def _on_finished(self, result: object, error: str) -> None:
        self.done.emit(result, error)

    def release(self) -> None:
        """스레드 소유권을 논블로킹으로 놓는다.

        CLI 백엔드는 cancel() 로 프로세스가 죽어 곧 끝나지만, Ollama 처럼
        HTTP 블로킹 중인 백엔드는 취소가 안 통한다 — 그런 스레드를 여기서
        wait() 하면 GUI 가 타임아웃(최대 2분)까지 얼어붙는다. 대신 done 연결을
        끊고 고아 목록으로 옮겨 완료 시 자체 정리되게 한 뒤 즉시 반환한다.
        """
        t, w = self._thread, self._worker
        self._thread = None
        self._worker = None
        self._token = None
        if t is None:
            return
        if not t.isRunning():
            t.deleteLater()
            if w is not None:
                w.deleteLater()
            return
        try:
            w.finished.disconnect(self._on_finished)
        except Exception:
            pass
        _orphans.add((t, w))

        def _reap(tt=t, ww=w) -> None:
            _orphans.discard((tt, ww))
            ww.deleteLater()
            tt.deleteLater()

        t.finished.connect(_reap)

    def wait(self) -> None:
        """블로킹 정리 — CLI 백엔드처럼 cancel 이 확실히 통하는 경우에만 사용."""
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait()
            self._thread = None
            self._worker = None
            self._token = None
