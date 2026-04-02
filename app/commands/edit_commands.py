"""Concrete edit commands for subtitle manipulation."""
from __future__ import annotations

import copy
import uuid
from typing import Any

from app.commands.bus import Command
from core.project.project_db import ProjectDB, EventRow, LockState


class UpdateEventCommand(Command):
    """Update one or more fields of an event."""

    def __init__(self, db: ProjectDB, event_id: str, changes: dict[str, Any]) -> None:
        self._db = db
        self._event_id = event_id
        self._changes = changes
        self._old_values: dict[str, Any] = {}

    def execute(self) -> None:
        rows = self._db.conn.execute(
            "SELECT * FROM events WHERE id=?", (self._event_id,)
        ).fetchall()
        if not rows:
            return
        row = dict(rows[0])
        self._old_values = {k: row.get(k) for k in self._changes}
        sets = ", ".join(f"{k}=?" for k in self._changes)
        vals = list(self._changes.values()) + [self._event_id]
        self._db.conn.execute(f"UPDATE events SET {sets} WHERE id=?", vals)
        self._db.conn.commit()

    def undo(self) -> None:
        if not self._old_values:
            return
        sets = ", ".join(f"{k}=?" for k in self._old_values)
        vals = list(self._old_values.values()) + [self._event_id]
        self._db.conn.execute(f"UPDATE events SET {sets} WHERE id=?", vals)
        self._db.conn.commit()

    def description(self) -> str:
        return "이벤트 수정"


class InsertEventCommand(Command):
    """Insert a new event into a track."""

    def __init__(self, db: ProjectDB, event: EventRow) -> None:
        self._db = db
        self._event = event

    def execute(self) -> None:
        self._db.insert_event(self._event)

    def undo(self) -> None:
        self._db.delete_event(self._event.id)

    def description(self) -> str:
        return "이벤트 삽입"


class DeleteEventCommand(Command):
    """Delete an event."""

    def __init__(self, db: ProjectDB, event_id: str) -> None:
        self._db = db
        self._event_id = event_id
        self._deleted_event: EventRow | None = None

    def execute(self) -> None:
        rows = self._db.conn.execute(
            "SELECT * FROM events WHERE id=?", (self._event_id,)
        ).fetchall()
        if rows:
            self._deleted_event = self._db._row_to_event(rows[0])
            self._db.delete_event(self._event_id)

    def undo(self) -> None:
        if self._deleted_event:
            self._db.insert_event(self._deleted_event)

    def description(self) -> str:
        return "이벤트 삭제"


class ShiftTimesCommand(Command):
    """Shift start/end times of selected events."""

    def __init__(self, db: ProjectDB, event_ids: list[str], delta_ms: int) -> None:
        self._db = db
        self._event_ids = event_ids
        self._delta_ms = delta_ms

    def execute(self) -> None:
        for eid in self._event_ids:
            self._db.conn.execute(
                "UPDATE events SET start_ms=MAX(0, start_ms+?), end_ms=MAX(0, end_ms+?) WHERE id=?",
                (self._delta_ms, self._delta_ms, eid)
            )
        self._db.conn.commit()

    def undo(self) -> None:
        for eid in self._event_ids:
            self._db.conn.execute(
                "UPDATE events SET start_ms=MAX(0, start_ms+?), end_ms=MAX(0, end_ms+?) WHERE id=?",
                (-self._delta_ms, -self._delta_ms, eid)
            )
        self._db.conn.commit()

    def description(self) -> str:
        return f"시간 이동 ({self._delta_ms:+d}ms)"


class ToggleCommentCommand(Command):
    """Toggle comment state of events."""

    def __init__(self, db: ProjectDB, event_ids: list[str]) -> None:
        self._db = db
        self._event_ids = event_ids

    def execute(self) -> None:
        for eid in self._event_ids:
            self._db.conn.execute(
                "UPDATE events SET is_comment = CASE WHEN is_comment=1 THEN 0 ELSE 1 END WHERE id=?",
                (eid,)
            )
        self._db.conn.commit()

    def undo(self) -> None:
        self.execute()  # toggle is its own inverse

    def description(self) -> str:
        return "주석 전환"
