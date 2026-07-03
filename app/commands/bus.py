"""CommandBus — all edits go through commands for undo/redo.

Every mutation to the project data is a Command. The bus executes
commands, records them for undo, and handles redo truncation.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable


class Command(ABC):
    """Base class for all undoable commands."""

    @abstractmethod
    def execute(self) -> None:
        """Perform the action."""

    @abstractmethod
    def undo(self) -> None:
        """Reverse the action."""

    @abstractmethod
    def description(self) -> str:
        """Human-readable description for the undo menu."""


# is_clean 의 도달 불가 기준점 — 히스토리 트림으로 저장/로드 시점의 상태로
# 되돌아갈 수 없게 되면 이 센티널이 marker 가 되어 어떤 top 과도 일치하지 않는다.
class _UnreachableMarker(Command):
    def execute(self) -> None:  # pragma: no cover - 실행되지 않음
        pass

    def undo(self) -> None:  # pragma: no cover
        pass

    def description(self) -> str:  # pragma: no cover
        return ""


_CLEAN_UNREACHABLE = _UnreachableMarker()


class CommandBus:
    """Dispatches commands and manages undo/redo history."""

    def __init__(self, max_history: int = 200) -> None:
        self._undo_stack: list[Command] = []
        self._redo_stack: list[Command] = []
        self._max_history = max_history
        self._listeners: list[Callable[[], None]] = []
        # Command that sat on top of the undo stack at the last save/load.
        # None means "clean when the stack is empty". Used by is_clean so the
        # modified flag clears when the user undoes back to the saved state.
        self._clean_marker: Command | None = None

    def execute(self, cmd: Command) -> None:
        """Execute a command and push it to the undo stack."""
        cmd.execute()
        self._undo_stack.append(cmd)
        self._redo_stack.clear()
        if len(self._undo_stack) > self._max_history:
            trimmed = self._undo_stack.pop(0)
            # 트림된 편집은 undo 로 되돌릴 수 없다 — 저장 기준점이 빈 스택
            # (None) 이거나 방금 트림된 커맨드였다면, 전부 undo 해서 스택이
            # 비어도 문서에는 트림된 편집이 남아 있으므로 결코 clean 이 아니다.
            if self._clean_marker is None or self._clean_marker is trimmed:
                self._clean_marker = _CLEAN_UNREACHABLE
        self._notify()

    def undo(self) -> bool:
        """Undo the last command. Returns True if successful."""
        if not self._undo_stack:
            return False
        cmd = self._undo_stack.pop()
        cmd.undo()
        self._redo_stack.append(cmd)
        self._notify()
        return True

    def redo(self) -> bool:
        """Redo the last undone command. Returns True if successful."""
        if not self._redo_stack:
            return False
        cmd = self._redo_stack.pop()
        cmd.execute()
        self._undo_stack.append(cmd)
        self._notify()
        return True

    def clear(self) -> None:
        self._undo_stack.clear()
        self._redo_stack.clear()
        self._clean_marker = None
        self._notify()

    def mark_clean(self) -> None:
        """Record the current state as the saved/clean baseline."""
        self._clean_marker = self._undo_stack[-1] if self._undo_stack else None
        self._notify()

    @property
    def is_clean(self) -> bool:
        """True when the current state matches the last marked-clean baseline.

        Compares the top-of-stack command by identity, so undoing/redoing back
        to the saved point reports clean, while diverging (a new edit after
        undo) reports dirty.
        """
        top = self._undo_stack[-1] if self._undo_stack else None
        return top is self._clean_marker

    @property
    def can_undo(self) -> bool:
        return len(self._undo_stack) > 0

    @property
    def can_redo(self) -> bool:
        return len(self._redo_stack) > 0

    @property
    def undo_description(self) -> str:
        return self._undo_stack[-1].description() if self._undo_stack else ""

    @property
    def redo_description(self) -> str:
        return self._redo_stack[-1].description() if self._redo_stack else ""

    def add_listener(self, callback: Callable[[], None]) -> None:
        """Add a listener called after any undo/redo/execute."""
        self._listeners.append(callback)

    def _notify(self) -> None:
        for cb in self._listeners:
            cb()
