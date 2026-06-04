"""스타일 CRUD 커맨드 — 모두 undo 가능. Style Manager UI 가 cmd_bus 로 실행한다.

스냅샷 기반 undo: 변경 전 행을 읽어 두었다가 되돌린다. Rename 은 스타일을 참조하는
이벤트의 style_id 도 함께 옮기고, undo 시 정확히 그 이벤트들만 되돌린다.
스타일 삭제는 이벤트를 건드리지 않는다(누락 참조는 QA 가 잡아준다).
"""
from __future__ import annotations

from typing import Any

from app.commands.bus import Command
from core.project.project_db import ProjectDB
from core.style.schema import default_style_props


def _style_props(db: ProjectDB, name: str) -> dict[str, Any] | None:
    row = db.conn.execute("SELECT * FROM styles WHERE name=?", (name,)).fetchone()
    if row is None:
        return None
    props = dict(row)
    props.pop("name", None)
    return props


class ReplaceStylesCommand(Command):
    """MainWindow._styles(list[ParsedStyle]) 를 통째로 교체 — Style Manager OK 시.

    스타일은 self._styles 가 .ass 출력의 단일 출처(serializer 가 shadow_line_idx 로
    이 리스트를 직렬화)라서, DB 가 아니라 이 리스트를 바꿔야 저장에 반영된다.
    이름 변경에 따른 이벤트 재지정은 호출 측이 UpdateEventCommand 들과 함께
    CompositeCommand 로 묶는다. undo 는 이전 리스트로 되돌린다.
    """

    def __init__(self, window: Any, new_styles: list[Any]) -> None:
        self._window = window
        self._new = new_styles
        self._old: list[Any] | None = None

    def execute(self) -> None:
        self._old = self._window._styles
        self._window._styles = self._new

    def undo(self) -> None:
        if self._old is not None:
            self._window._styles = self._old

    def description(self) -> str:
        return "스타일 변경"


class CreateStyleCommand(Command):
    def __init__(self, db: ProjectDB, name: str, props: dict[str, Any] | None = None) -> None:
        self._db = db
        self._name = name
        self._props = props or default_style_props()

    def execute(self) -> None:
        self._db.upsert_style(self._name, **self._props)

    def undo(self) -> None:
        self._db.delete_style(self._name)

    def description(self) -> str:
        return f"스타일 생성 '{self._name}'"


class UpdateStyleCommand(Command):
    def __init__(self, db: ProjectDB, name: str, changes: dict[str, Any]) -> None:
        self._db = db
        self._name = name
        self._changes = changes
        self._old: dict[str, Any] = {}

    def execute(self) -> None:
        row = self._db.conn.execute(
            "SELECT * FROM styles WHERE name=?", (self._name,)
        ).fetchone()
        if row is None:
            return
        current = dict(row)
        self._old = {k: current.get(k) for k in self._changes}
        self._db.upsert_style(self._name, **self._changes)

    def undo(self) -> None:
        if self._old:
            self._db.upsert_style(self._name, **self._old)

    def description(self) -> str:
        return f"스타일 수정 '{self._name}'"


class DeleteStyleCommand(Command):
    def __init__(self, db: ProjectDB, name: str) -> None:
        self._db = db
        self._name = name
        self._props: dict[str, Any] | None = None

    def execute(self) -> None:
        self._props = _style_props(self._db, self._name)
        self._db.delete_style(self._name)

    def undo(self) -> None:
        if self._props is not None:
            self._db.upsert_style(self._name, **self._props)

    def description(self) -> str:
        return f"스타일 삭제 '{self._name}'"


class DuplicateStyleCommand(Command):
    def __init__(self, db: ProjectDB, src_name: str, new_name: str) -> None:
        self._db = db
        self._src = src_name
        self._new = new_name

    def execute(self) -> None:
        props = _style_props(self._db, self._src) or default_style_props()
        self._db.upsert_style(self._new, **props)

    def undo(self) -> None:
        self._db.delete_style(self._new)

    def description(self) -> str:
        return f"스타일 복제 '{self._src}' → '{self._new}'"


class RenameStyleCommand(Command):
    """스타일 이름 변경 + 참조 이벤트 재지정. new_name 은 비어 있어야 함(미존재)."""

    def __init__(self, db: ProjectDB, old_name: str, new_name: str) -> None:
        self._db = db
        self._old = old_name
        self._new = new_name
        self._props: dict[str, Any] | None = None
        self._moved_ids: list[str] = []

    def execute(self) -> None:
        self._props = _style_props(self._db, self._old)
        if self._props is None:
            return
        rows = self._db.conn.execute(
            "SELECT id FROM events WHERE style_id=?", (self._old,)
        ).fetchall()
        self._moved_ids = [r["id"] for r in rows]
        self._db.upsert_style(self._new, **self._props)
        self._db.conn.execute(
            "UPDATE events SET style_id=? WHERE style_id=?", (self._new, self._old)
        )
        self._db.conn.commit()
        self._db.delete_style(self._old)

    def undo(self) -> None:
        if self._props is None:
            return
        self._db.upsert_style(self._old, **self._props)
        if self._moved_ids:
            qmarks = ",".join("?" * len(self._moved_ids))
            self._db.conn.execute(
                f"UPDATE events SET style_id=? WHERE id IN ({qmarks})",
                [self._old, *self._moved_ids],
            )
            self._db.conn.commit()
        self._db.delete_style(self._new)

    def description(self) -> str:
        return f"스타일 이름 변경 '{self._old}' → '{self._new}'"
