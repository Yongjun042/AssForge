"""AI 관련 Command — LockState 변경, 제안 적용/거부.

모든 AI 사이드의 변경도 사용자 편집과 동일하게 CommandBus 를 거친다.
한 번의 Accept All 은 한 번의 undo 로 되돌릴 수 있어야 한다.
"""
from __future__ import annotations

from typing import Any

from app.commands.bus import Command
from core.project.project_db import LockState, ProjectDB


class SetLockStateCommand(Command):
    """단일 또는 다수 라인의 lock_state 변경."""

    def __init__(self, db: ProjectDB, event_ids: list[str], new_state: LockState) -> None:
        self._db = db
        self._event_ids = list(event_ids)
        self._new = new_state
        self._old: dict[str, str] = {}

    def execute(self) -> None:
        if not self._event_ids:
            return
        rows = self._db.conn.execute(
            f"SELECT id, lock_state FROM events WHERE id IN ({','.join('?' * len(self._event_ids))})",
            self._event_ids,
        ).fetchall()
        self._old = {r["id"]: r["lock_state"] for r in rows}
        self._db.conn.executemany(
            "UPDATE events SET lock_state=? WHERE id=?",
            [(self._new.value, eid) for eid in self._event_ids],
        )
        self._db.conn.commit()

    def undo(self) -> None:
        if not self._old:
            return
        self._db.conn.executemany(
            "UPDATE events SET lock_state=? WHERE id=?",
            [(state, eid) for eid, state in self._old.items()],
        )
        self._db.conn.commit()

    def description(self) -> str:
        return f"잠금 상태 변경: {self._new.value}"


class ApplyAISuggestionCommand(Command):
    """suggested_start/end_ms 를 start/end_ms 에 반영하고 lock_state=CONFIRMED.

    원래 start/end/lock_state 와 suggested_* 를 모두 보관해 undo 가능.
    """

    def __init__(self, db: ProjectDB, event_ids: list[str]) -> None:
        self._db = db
        self._event_ids = list(event_ids)
        self._snapshot: list[dict[str, Any]] = []

    def execute(self) -> None:
        if not self._event_ids:
            return
        placeholders = ",".join("?" * len(self._event_ids))
        rows = self._db.conn.execute(
            f"""SELECT id, start_ms, end_ms, lock_state,
                       suggested_start_ms, suggested_end_ms
                  FROM events WHERE id IN ({placeholders})""",
            self._event_ids,
        ).fetchall()

        self._snapshot = [dict(r) for r in rows]

        for r in rows:
            if r["suggested_start_ms"] is None or r["suggested_end_ms"] is None:
                continue
            # LOCKED 라인은 변경하지 않음 (안전망)
            if r["lock_state"] == LockState.LOCKED.value:
                continue
            self._db.conn.execute(
                """UPDATE events
                      SET start_ms=?, end_ms=?, lock_state=?,
                          suggested_start_ms=NULL, suggested_end_ms=NULL
                    WHERE id=?""",
                (int(r["suggested_start_ms"]), int(r["suggested_end_ms"]),
                 LockState.CONFIRMED.value, r["id"]),
            )
        self._db.conn.commit()

    def undo(self) -> None:
        for snap in self._snapshot:
            self._db.conn.execute(
                """UPDATE events
                      SET start_ms=?, end_ms=?, lock_state=?,
                          suggested_start_ms=?, suggested_end_ms=?
                    WHERE id=?""",
                (snap["start_ms"], snap["end_ms"], snap["lock_state"],
                 snap["suggested_start_ms"], snap["suggested_end_ms"], snap["id"]),
            )
        self._db.conn.commit()

    def description(self) -> str:
        return f"AI 제안 적용 ({len(self._event_ids)} 줄)"


class RejectAISuggestionCommand(Command):
    """suggested_* 를 NULL 로 비우고 lock_state 를 UNLOCKED 로 되돌림."""

    def __init__(self, db: ProjectDB, event_ids: list[str]) -> None:
        self._db = db
        self._event_ids = list(event_ids)
        self._snapshot: list[dict[str, Any]] = []

    def execute(self) -> None:
        if not self._event_ids:
            return
        placeholders = ",".join("?" * len(self._event_ids))
        rows = self._db.conn.execute(
            f"""SELECT id, lock_state, suggested_start_ms, suggested_end_ms,
                       ai_confidence
                  FROM events WHERE id IN ({placeholders})""",
            self._event_ids,
        ).fetchall()
        self._snapshot = [dict(r) for r in rows]

        for r in rows:
            # LOCKED 는 건드리지 않음
            if r["lock_state"] == LockState.LOCKED.value:
                continue
            new_state = LockState.UNLOCKED.value
            if r["lock_state"] == LockState.CONFIRMED.value:
                new_state = LockState.CONFIRMED.value  # 이미 적용된 라인은 상태 유지
            self._db.conn.execute(
                """UPDATE events
                      SET suggested_start_ms=NULL, suggested_end_ms=NULL,
                          ai_confidence=0.0, lock_state=?
                    WHERE id=?""",
                (new_state, r["id"]),
            )
        self._db.conn.commit()

    def undo(self) -> None:
        for snap in self._snapshot:
            self._db.conn.execute(
                """UPDATE events
                      SET suggested_start_ms=?, suggested_end_ms=?,
                          ai_confidence=?, lock_state=?
                    WHERE id=?""",
                (snap["suggested_start_ms"], snap["suggested_end_ms"],
                 snap["ai_confidence"], snap["lock_state"], snap["id"]),
            )
        self._db.conn.commit()

    def description(self) -> str:
        return f"AI 제안 거부 ({len(self._event_ids)} 줄)"


class WriteAISuggestionsCommand(Command):
    """sync_service 결과를 DB 에 한꺼번에 기록 — 한 번의 undo 로 전체 롤백.

    sync_service.run_sync 는 DB 를 읽기만 하고 제안을 반환만 한다. 실제 쓰기는
    이 Command 가 유일한 지점이라 execute 시점의 스냅샷이 정확한 undo 기준이
    된다. 호출자는 (event_id, start, end, conf) 리스트를 만들어 전달.
    """

    def __init__(self, db: ProjectDB, suggestions: list[tuple[str, int, int, float]]) -> None:
        self._db = db
        self._suggestions = list(suggestions)
        self._before: list[dict[str, Any]] = []

    def execute(self) -> None:
        if not self._suggestions:
            return
        ids = [s[0] for s in self._suggestions]
        placeholders = ",".join("?" * len(ids))
        rows = self._db.conn.execute(
            f"""SELECT id, lock_state, ai_confidence,
                       suggested_start_ms, suggested_end_ms
                  FROM events WHERE id IN ({placeholders})""",
            ids,
        ).fetchall()
        self._before = [dict(r) for r in rows]

        for eid, s_ms, e_ms, conf in self._suggestions:
            self._db.conn.execute(
                """UPDATE events
                      SET suggested_start_ms=?, suggested_end_ms=?,
                          ai_confidence=?, lock_state=?
                    WHERE id=? AND lock_state != ?""",
                (int(s_ms), int(e_ms), float(conf),
                 LockState.AI_SUGGESTED.value, eid, LockState.LOCKED.value),
            )
        self._db.conn.commit()

    def undo(self) -> None:
        for snap in self._before:
            self._db.conn.execute(
                """UPDATE events
                      SET suggested_start_ms=?, suggested_end_ms=?,
                          ai_confidence=?, lock_state=?
                    WHERE id=?""",
                (snap["suggested_start_ms"], snap["suggested_end_ms"],
                 snap["ai_confidence"], snap["lock_state"], snap["id"]),
            )
        self._db.conn.commit()

    def description(self) -> str:
        return f"AI 제안 기록 ({len(self._suggestions)} 줄)"
