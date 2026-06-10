"""Concrete edit commands for subtitle manipulation."""
from __future__ import annotations

from typing import Any

from app.commands.bus import Command
from core.project.project_db import ProjectDB, EventRow


class CompositeCommand(Command):
    """여러 Command 를 하나의 undo 단위로 묶는다. 실행은 정방향, undo 는 역방향.

    AI 자연어 편집/효과 적용처럼 여러 줄에 걸친 변경을 한 번의 Ctrl+Z 로
    되돌리기 위한 래퍼. 빈 리스트면 no-op.
    """

    def __init__(self, commands: list[Command], description: str = "일괄 편집") -> None:
        self._commands = commands
        self._description = description

    def execute(self) -> None:
        for cmd in self._commands:
            cmd.execute()

    def undo(self) -> None:
        for cmd in reversed(self._commands):
            cmd.undo()

    def description(self) -> str:
        return self._description


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


class BulkInsertEventsCommand(Command):
    """Insert multiple events as one atomic, undoable operation.

    삽입 시 같은 트랙의 기존 이벤트 중 ``order_index >= target`` 인 행을
    ``len(events)`` 만큼 밀어 올린 뒤 새 이벤트를 그 자리에 꽂는다.
    이렇게 해야 사용자가 paste 한 라인들이 기존 라인 사이에 인터리브되지 않는다.
    Undo 는 새 행 삭제 후 기존 행을 되밀어 원복한다.
    """

    def __init__(self, db: ProjectDB, events: list[EventRow]) -> None:
        self._db = db
        self._events = events
        if events:
            track_ids = {e.track_id for e in events}
            if len(track_ids) != 1:
                raise ValueError(
                    "BulkInsertEventsCommand: 모든 이벤트는 같은 track_id 여야 합니다."
                )
            self._track_id: str | None = events[0].track_id
            self._target_order = min(e.order_index for e in events)
            self._shift = len(events)
        else:
            self._track_id = None
            self._target_order = 0
            self._shift = 0

    def execute(self) -> None:
        if not self._events:
            return
        # 1) 기존 라인을 위로 밀기
        self._db.conn.execute(
            "UPDATE events SET order_index = order_index + ? "
            "WHERE track_id = ? AND order_index >= ?",
            (self._shift, self._track_id, self._target_order),
        )
        # 2) 새 라인 삽입 (commit 은 insert_event 가 매번 수행)
        for ev in self._events:
            self._db.insert_event(ev)

    def undo(self) -> None:
        if not self._events:
            return
        # 1) 새 라인 삭제
        for ev in self._events:
            self._db.delete_event(ev.id)
        # 2) 위로 밀려 있던 기존 라인을 원복 — 새 라인을 모두 지웠으니
        #    [target_order + shift, ∞) 구간에 남은 건 모두 기존 라인이다.
        self._db.conn.execute(
            "UPDATE events SET order_index = order_index - ? "
            "WHERE track_id = ? AND order_index >= ?",
            (self._shift, self._track_id, self._target_order + self._shift),
        )
        self._db.conn.commit()

    def description(self) -> str:
        return f"이벤트 {len(self._events)}개 일괄 삽입"


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
    """Shift start/end times of selected events.

    Undo 는 스냅샷 복원이다 — 시프트는 MAX(0, …) 클램프 때문에 역연산이
    불가역이라(0 으로 잘린 줄에 +Δ 를 더하면 원값이 아님) 변경 전 값을
    저장해 두었다가 그대로 되돌린다.
    """

    def __init__(self, db: ProjectDB, event_ids: list[str], delta_ms: int) -> None:
        self._db = db
        self._event_ids = list(event_ids)
        self._delta_ms = delta_ms
        self._old: dict[str, tuple[int, int]] = {}

    def execute(self) -> None:
        if not self._event_ids:
            return
        placeholders = ",".join("?" * len(self._event_ids))
        rows = self._db.conn.execute(
            f"SELECT id, start_ms, end_ms FROM events WHERE id IN ({placeholders})",
            self._event_ids,
        ).fetchall()
        self._old = {r["id"]: (r["start_ms"], r["end_ms"]) for r in rows}
        for eid in self._event_ids:
            self._db.conn.execute(
                "UPDATE events SET start_ms=MAX(0, start_ms+?), end_ms=MAX(0, end_ms+?) WHERE id=?",
                (self._delta_ms, self._delta_ms, eid)
            )
        self._db.conn.commit()

    def undo(self) -> None:
        for eid, (s_ms, e_ms) in self._old.items():
            self._db.conn.execute(
                "UPDATE events SET start_ms=?, end_ms=? WHERE id=?",
                (s_ms, e_ms, eid)
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
