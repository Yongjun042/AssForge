"""CommandBus — all edits go through commands for undo/redo.

Every mutation to the project data is a Command. The bus executes
commands, records them for undo, and handles redo truncation.
"""
from __future__ import annotations

import copy
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable


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


class CommandBus:
    """Dispatches commands and manages undo/redo history."""

    def __init__(self, max_history: int = 200) -> None:
        self._undo_stack: list[Command] = []
        self._redo_stack: list[Command] = []
        self._max_history = max_history
        self._listeners: list[Callable[[], None]] = []

    def execute(self, cmd: Command) -> None:
        """Execute a command and push it to the undo stack."""
        cmd.execute()
        self._undo_stack.append(cmd)
        self._redo_stack.clear()
        if len(self._undo_stack) > self._max_history:
            self._undo_stack.pop(0)
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
        self._notify()

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
