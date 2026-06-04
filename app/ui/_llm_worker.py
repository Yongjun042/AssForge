"""LLM 호출을 GUI 스레드 밖에서 실행하는 워커 — 모달 다이얼로그가 멈추지 않게.

interpret_command / author_effects 같은 블로킹 LLM 호출을 백그라운드 QThread 에서
1회 실행하고 done(result, error) 시그널로 결과를 돌려준다. 스레드/워커 참조를
self 에 잡아 두어 GC 가 실행 중 QThread 를 파괴하지 않게 한다.
"""
from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import QObject, QThread, Signal


class _Worker(QObject):
    finished = Signal(object, str)  # (result | None, error)

    def __init__(self, fn: Callable[[], Any]) -> None:
        super().__init__()
        self._fn = fn

    def run(self) -> None:
        try:
            result = self._fn()
        except Exception as exc:  # 어떤 LLM/네트워크 오류든 UI 로 전달
            self.finished.emit(None, str(exc))
            return
        self.finished.emit(result, "")


class LLMTaskRunner(QObject):
    """fn 을 백그라운드에서 1회 실행하고 done(result, error) 으로 통지."""

    done = Signal(object, str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._thread: QThread | None = None
        self._worker: _Worker | None = None

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    def start(self, fn: Callable[[], Any]) -> None:
        self.wait()  # 이전 작업이 남아 있으면 정리
        self._thread = QThread()
        self._worker = _Worker(fn)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._on_finished)
        self._worker.finished.connect(self._thread.quit)
        self._thread.start()

    def _on_finished(self, result: object, error: str) -> None:
        self.done.emit(result, error)

    def wait(self) -> None:
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait()
            self._thread = None
            self._worker = None
